"""Local-only FastAPI control plane for LightWorker."""

from __future__ import annotations

import json
import queue
import re
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import uuid4

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import __version__
from .analysis_tools import CredentialVault, sanitize_and_capture_credentials
from .config import WorkerConfig, parse_verification_command
from .models import RunRecord, RunStatus, TaskSpec, VerificationCommand, VerificationKind
from .policy import redact_value
from .sandbox_helper import HelperError, validate_command
from .storage import RunStore
from .workflow import CodingTaskRunner
from .workspace import WorkspaceError, WorkspaceManager

ARTIFACTS = {
    "plan": ("plan.md", "text/markdown; charset=utf-8"),
    "summary": ("summary.md", "text/markdown; charset=utf-8"),
    "diff": ("changes.patch", "text/x-diff; charset=utf-8"),
    "trace": ("trace.jsonl", "application/x-ndjson; charset=utf-8"),
    "status": ("git-status.txt", "text/plain; charset=utf-8"),
}
ACTIVE_STATUSES = {RunStatus.CREATED, RunStatus.PREPARING, RunStatus.RUNNING}
SAFE_LOG_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.log$")


class RunCreateRequest(BaseModel):
    source_mode: Literal["empty", "existing"] = "empty"
    repo: str | None = Field(default=None, max_length=4096)
    task: str = Field(min_length=1, max_length=20_000)
    test_commands: list[str] = Field(default_factory=list, max_length=10)
    lint_commands: list[str] = Field(default_factory=list, max_length=10)
    include_dirty: bool = False
    max_repairs: int | None = Field(default=None, ge=0, le=3)

    @field_validator("task")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value cannot be blank")
        return value.strip()


class ReviewDecisionRequest(BaseModel):
    decision: Literal["approved", "rejected"]
    note: str = Field(default="", max_length=4000)


class FollowUpRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)

    @field_validator("message")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value cannot be blank")
        return value.strip()


class TaskManagerProtocol(Protocol):
    def submit(self, run_id: str, action: str, function: Callable[[], Any]) -> None: ...

    def status(self, run_id: str) -> dict[str, Any] | None: ...

    def close(self) -> None: ...


class WebTaskManager:
    """One daemon worker keeps local model and Docker tasks serialized."""

    def __init__(self) -> None:
        self._jobs: queue.Queue[tuple[str, str, Callable[[], Any]] | None] = queue.Queue()
        self._states: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._thread = threading.Thread(target=self._work, name="lightworker-web-jobs", daemon=True)
        self._thread.start()

    def submit(self, run_id: str, action: str, function: Callable[[], Any]) -> None:
        with self._lock:
            current = self._states.get(run_id)
            if current and current["state"] in {"queued", "running"}:
                raise ValueError(f"run {run_id} already has an active job")
            self._states[run_id] = {"state": "queued", "action": action, "error": None}
            self._jobs.put((run_id, action, function))

    def status(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._states.get(run_id)
            return dict(value) if value else None

    def close(self) -> None:
        self._jobs.put(None)

    def _work(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            run_id, action, function = job
            with self._lock:
                self._states[run_id] = {"state": "running", "action": action, "error": None}
            try:
                function()
            except Exception as exc:  # pragma: no cover - defensive boundary around runner
                with self._lock:
                    self._states[run_id] = {
                        "state": "failed",
                        "action": action,
                        "error": str(exc),
                    }
            else:
                with self._lock:
                    self._states[run_id] = {"state": "completed", "action": action, "error": None}
            finally:
                self._jobs.task_done()


def create_app(
    settings: WorkerConfig,
    *,
    task_manager: TaskManagerProtocol | None = None,
    runner_factory: Callable[[WorkerConfig], CodingTaskRunner] = CodingTaskRunner,
) -> FastAPI:
    own_manager = task_manager is None
    manager = task_manager or WebTaskManager()
    store = RunStore(settings.state_dir)
    _migrate_legacy_credentials(store, settings.state_dir)
    static_dir = Path(__file__).resolve().parent / "web_static"

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        if own_manager:
            manager.close()

    app = FastAPI(
        title="LightWorker",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.store = store
    app.state.task_manager = manager
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(static_dir / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "version": __version__,
            "model": settings.model.model,
            "model_configured": bool(settings.model.model and settings.model.resolved_api_key),
            "image": settings.image,
            "state_dir": str(settings.state_dir),
            "local_only": True,
        }

    @app.get("/api/runs")
    def list_runs() -> list[dict[str, Any]]:
        return _conversation_summaries(store, manager)

    @app.post("/api/runs", status_code=status.HTTP_202_ACCEPTED)
    def create_run(payload: RunCreateRequest) -> dict[str, Any]:
        run_id = uuid4().hex
        sanitized, credentials = sanitize_and_capture_credentials([payload.task])
        CredentialVault(settings.state_dir).merge(run_id, credentials)
        workspace_manager = WorkspaceManager()
        if payload.source_mode == "empty":
            try:
                repo = workspace_manager.create_empty_repository(settings.state_dir / "scratch" / run_id)
            except WorkspaceError as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc
        else:
            if not payload.repo or not payload.repo.strip():
                raise HTTPException(status_code=422, detail="repo is required for existing mode")
            repo = Path(payload.repo).expanduser().resolve()
            try:
                workspace_manager.validate_repository(repo)
            except WorkspaceError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

        verification = list(settings.verification)
        verification.extend(_parse_commands(payload.test_commands, VerificationKind.TEST, "test"))
        verification.extend(_parse_commands(payload.lint_commands, VerificationKind.LINT, "lint"))
        if not verification:
            verification = autodetect_verification(repo)
        spec = TaskSpec(
            run_id=run_id,
            repo=repo,
            task=sanitized[0],
            include_dirty=payload.include_dirty,
            language=settings.language,
            verification=verification,
            max_repairs=(payload.max_repairs if payload.max_repairs is not None else settings.max_repairs),
            image=settings.image,
            source_mode=payload.source_mode,
        )
        try:
            manager.submit(spec.run_id, "run", lambda: runner_factory(settings).run(spec))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"run_id": spec.run_id, "status": "queued"}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        record = _load_record(store, run_id)
        return _run_detail(store, record, manager.status(run_id))

    @app.post("/api/runs/{run_id}/followups", status_code=status.HTTP_202_ACCEPTED)
    def create_followup(run_id: str, payload: FollowUpRequest) -> dict[str, Any]:
        selected = _load_record(store, run_id)
        conversation = _conversation_records(store, selected)
        parent = conversation[-1]
        parent_job = manager.status(parent.run_id)
        if parent.status in ACTIVE_STATUSES or (
            parent_job and parent_job.get("state") in {"queued", "running"}
        ):
            raise HTTPException(status_code=409, detail="wait for the current turn to finish")
        workspace = Path(parent.workspace or "").expanduser().resolve()
        if not workspace.is_dir():
            raise HTTPException(status_code=409, detail="saved workspace is missing")
        try:
            parent_spec = TaskSpec.model_validate(parent.metadata["task_spec"])
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=409, detail="saved task configuration is invalid") from exc

        root = conversation[0]
        sanitized, credentials = sanitize_and_capture_credentials(
            [*[item.task for item in conversation], payload.message]
        )
        CredentialVault(settings.state_dir).merge(root.run_id, credentials)
        for record, safe_task in zip(conversation, sanitized[:-1], strict=True):
            if record.task == safe_task:
                continue
            record.task = safe_task
            record.metadata = redact_value(record.metadata)
            store.save(record)
        followup_id = uuid4().hex
        spec = TaskSpec(
            run_id=followup_id,
            repo=workspace,
            task=sanitized[-1],
            include_dirty=True,
            language=parent_spec.language,
            verification=parent_spec.verification,
            max_repairs=parent_spec.max_repairs,
            image=parent_spec.image,
            source_mode=parent_spec.source_mode,
            parent_run_id=parent.run_id,
            root_run_id=root.run_id,
            conversation_context=_build_conversation_context(store, conversation),
        )
        try:
            manager.submit(spec.run_id, "followup", lambda: runner_factory(settings).run(spec))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "run_id": spec.run_id,
            "root_run_id": root.run_id,
            "parent_run_id": parent.run_id,
            "status": "queued",
        }

    @app.get("/api/runs/{run_id}/artifacts/{artifact}")
    def get_artifact(run_id: str, artifact: str) -> PlainTextResponse:
        _load_record(store, run_id)
        if artifact not in ARTIFACTS:
            raise HTTPException(status_code=404, detail="unknown artifact")
        filename, media_type = ARTIFACTS[artifact]
        return PlainTextResponse(
            _read_artifact(store, run_id, filename),
            media_type=media_type,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/runs/{run_id}/logs")
    def list_logs(run_id: str) -> list[dict[str, Any]]:
        _load_record(store, run_id)
        directory = store.artifact_path(run_id, "logs")
        if not directory.is_dir():
            return []
        return [
            {"name": path.name, "size": path.stat().st_size}
            for path in sorted(directory.glob("*.log"))
            if path.is_file()
        ]

    @app.get("/api/runs/{run_id}/logs/{name}")
    def get_log(run_id: str, name: str) -> PlainTextResponse:
        _load_record(store, run_id)
        if not SAFE_LOG_NAME.fullmatch(name):
            raise HTTPException(status_code=404, detail="unknown log")
        return PlainTextResponse(
            _read_artifact(store, run_id, f"logs/{name}"),
            media_type="text/plain; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/runs/{run_id}/resume", status_code=status.HTTP_202_ACCEPTED)
    def resume_run(run_id: str) -> dict[str, str]:
        record = _load_record(store, run_id)
        if record.status not in {RunStatus.FAILED, RunStatus.INTERRUPTED}:
            raise HTTPException(status_code=409, detail="only failed or interrupted runs can resume")
        if record.metadata.get("execution_mode") in {"general", "analysis"}:
            raise HTTPException(
                status_code=409,
                detail="general tasks continue through a follow-up message rather than a coding checkpoint",
            )
        _submit_existing(manager, run_id, "resume", lambda: runner_factory(settings).resume(run_id))
        return {"run_id": run_id, "status": "queued"}

    @app.post("/api/runs/{run_id}/rerun", status_code=status.HTTP_202_ACCEPTED)
    def rerun_verify(run_id: str) -> dict[str, str]:
        record = _load_record(store, run_id)
        if record.status in ACTIVE_STATUSES:
            raise HTTPException(status_code=409, detail="run is still active")
        _submit_existing(
            manager,
            run_id,
            "rerun_verify",
            lambda: runner_factory(settings).rerun_from_verify(run_id),
        )
        return {"run_id": run_id, "status": "queued"}

    @app.post("/api/runs/{run_id}/approval", status_code=status.HTTP_202_ACCEPTED)
    def decide_approval(run_id: str, payload: ReviewDecisionRequest) -> dict[str, str]:
        _load_record(store, run_id)
        flow_record = _load_flow_record(store, run_id)
        approval = _pending_approval(flow_record)
        if approval is None:
            raise HTTPException(status_code=409, detail="run has no pending approval")
        _submit_existing(
            manager,
            run_id,
            "approval",
            lambda: runner_factory(settings).decide_approval(
                run_id,
                approval["step"],
                payload.decision,
                payload.note.strip(),
            ),
        )
        return {"run_id": run_id, "status": "queued"}

    @app.post("/api/runs/{run_id}/review")
    def review_run(run_id: str, payload: ReviewDecisionRequest) -> dict[str, Any]:
        record = _load_record(store, run_id)
        if record.status in ACTIVE_STATUSES:
            raise HTTPException(status_code=409, detail="run is still active")
        if not _has_text_artifact(store, run_id, "changes.patch"):
            raise HTTPException(status_code=409, detail="run has no patch to review")
        decision = {
            "decision": payload.decision,
            "note": payload.note.strip(),
            "recorded_at": datetime.now(UTC).isoformat(),
            "effect": "audit_only; original repository was not modified",
        }
        store.write_json(run_id, "review-decision.json", decision)
        return decision

    return app


def autodetect_verification(repo: Path) -> list[VerificationCommand]:
    if (repo / "tests").is_dir() and any(
        (repo / name).exists() for name in ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini")
    ):
        return [
            VerificationCommand(
                name="auto-pytest",
                argv=["pytest", "-q"],
                kind=VerificationKind.TEST,
            )
        ]
    return []


def _parse_commands(values: list[str], kind: VerificationKind, prefix: str) -> list[VerificationCommand]:
    commands: list[VerificationCommand] = []
    for index, raw in enumerate(values, start=1):
        if not raw.strip():
            continue
        command = parse_verification_command(raw, kind=kind, index=index)
        try:
            validate_command(command.argv)
        except HelperError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        command.name = f"{prefix}-{index}"
        commands.append(command)
    return commands


def _load_record(store: RunStore, run_id: str) -> RunRecord:
    try:
        return store.load(run_id)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc


def _run_summary(record: RunRecord, job: dict[str, Any] | None) -> dict[str, Any]:
    return redact_value({
        "run_id": record.run_id,
        "task": record.task,
        "repo": record.repo,
        "status": record.status.value,
        "current_step": record.current_step,
        "updated_at": record.updated_at.isoformat(),
        "verification_passed": sum(item.passed for item in record.verification),
        "verification_total": len(record.verification),
        "job": job,
        "source_mode": _source_mode(record),
        "parent_run_id": _parent_run_id(record),
        "root_run_id": _root_run_id(record),
    })


def _conversation_summaries(
    store: RunStore,
    manager: TaskManagerProtocol,
) -> list[dict[str, Any]]:
    groups: dict[str, list[RunRecord]] = {}
    for record in reversed(store.list()):
        groups.setdefault(_root_run_id(record), []).append(record)
    summaries: list[dict[str, Any]] = []
    for root_id, records in groups.items():
        records.sort(key=lambda item: item.created_at)
        latest = records[-1]
        summary = _run_summary(latest, manager.status(latest.run_id))
        summary["task"] = redact_value(records[0].task)
        summary["root_run_id"] = root_id
        summary["turn_count"] = len(records)
        summaries.append(summary)
    return sorted(summaries, key=lambda item: item["updated_at"], reverse=True)


def _run_detail(store: RunStore, record: RunRecord, job: dict[str, Any] | None) -> dict[str, Any]:
    flow_record = _load_flow_record(store, record.run_id)
    has_changes = _has_text_artifact(store, record.run_id, "changes.patch")
    payload = redact_value(record.model_dump(mode="json"))
    payload["job"] = job
    payload["steps"] = _load_steps(flow_record)
    payload["current_step"] = _current_step(payload["steps"], record.current_step, job)
    payload["activity"] = _load_activity(flow_record)
    payload["artifacts"] = {
        name: (
            has_changes
            if name == "diff"
            else store.artifact_path(record.run_id, filename).is_file()
        )
        for name, (filename, _) in ARTIFACTS.items()
    }
    payload["has_changes"] = has_changes
    approval_in_progress = bool(
        job
        and job.get("action") == "approval"
        and job.get("state") in {"queued", "running"}
    )
    payload["approval_request"] = None if approval_in_progress else _pending_approval(flow_record)
    payload["review"] = _read_optional_json(store, record.run_id, "review-decision.json")
    payload["source_mode"] = _source_mode(record)
    conversation = _conversation_records(store, record)
    payload["root_run_id"] = conversation[0].run_id
    payload["parent_run_id"] = _parent_run_id(record)
    payload["conversation_title"] = redact_value(conversation[0].task)
    payload["conversation"] = [_conversation_turn(store, item) for item in conversation]
    plan = _read_optional_json(store, record.run_id, "plan.json")
    task_type = str(plan.get("task_type") or "") if isinstance(plan, dict) else ""
    payload["task_type"] = task_type
    payload["answer_only"] = task_type.strip().lower().replace("_", "-") in {
        "answer-only",
        "conversation-answer",
        "follow-up-answer",
    }
    execution_mode = record.metadata.get("execution_mode")
    payload["unified_mode"] = execution_mode == "unified"
    payload["general_only"] = execution_mode in {"general", "analysis"}
    payload["analysis_only"] = execution_mode == "analysis"
    return payload


def _conversation_turn(store: RunStore, record: RunRecord) -> dict[str, Any]:
    return redact_value({
        "run_id": record.run_id,
        "message": record.task,
        "status": record.status.value,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
        "error": record.error,
        "source_mode": _source_mode(record),
        "summary": _read_optional_text(store, record.run_id, "summary.md"),
        "diff": _read_optional_text(store, record.run_id, "changes.patch"),
    })


def _conversation_records(store: RunStore, selected: RunRecord) -> list[RunRecord]:
    root_id = _root_run_id(selected)
    records = [record for record in store.list() if _root_run_id(record) == root_id]
    if not records:
        return [selected]
    return sorted(records, key=lambda item: item.created_at)


def _migrate_legacy_credentials(store: RunStore, state_dir: Path) -> None:
    """Move credentials from legacy run records into the host-bound vault."""

    groups: dict[str, list[RunRecord]] = {}
    for record in store.list():
        groups.setdefault(_root_run_id(record), []).append(record)
    vault = CredentialVault(state_dir)
    for root_id, records in groups.items():
        records.sort(key=lambda item: item.created_at)
        sanitized, credentials = sanitize_and_capture_credentials([item.task for item in records])
        if credentials:
            vault.merge(root_id, credentials)
        for record, safe_task in zip(records, sanitized, strict=True):
            safe_metadata = redact_value(record.metadata)
            if record.task == safe_task and record.metadata == safe_metadata:
                continue
            record.task = safe_task
            record.metadata = safe_metadata
            store.save(record)


def _root_run_id(record: RunRecord) -> str:
    task_spec = record.metadata.get("task_spec")
    if isinstance(task_spec, dict) and task_spec.get("root_run_id"):
        return str(task_spec["root_run_id"])
    return record.run_id


def _parent_run_id(record: RunRecord) -> str | None:
    task_spec = record.metadata.get("task_spec")
    if isinstance(task_spec, dict) and task_spec.get("parent_run_id"):
        return str(task_spec["parent_run_id"])
    return None


def _build_conversation_context(store: RunStore, records: list[RunRecord]) -> str:
    lines = [f"原始目标 / Original goal:\n{records[0].task}", "此前轮次 / Previous turns:"]
    for index, record in enumerate(records[-4:], start=max(1, len(records) - 3)):
        result = _read_optional_text(store, record.run_id, "summary.md") or record.error or "无可用总结"
        lines.extend(
            [
                f"\n[Turn {index}] 用户 / User:\n{record.task}",
                f"[Turn {index}] LightWorker:\n{result[:4000]}",
            ]
        )
    text = "\n".join(lines)
    return text if len(text) <= 14_000 else text[:2000] + "\n…\n" + text[-12_000:]


def _source_mode(record: RunRecord) -> str:
    task_spec = record.metadata.get("task_spec")
    if isinstance(task_spec, dict):
        return str(task_spec.get("source_mode") or "existing")
    return "existing"


def _load_flow_record(store: RunStore, run_id: str) -> dict[str, Any]:
    path = store.artifact_path(run_id, f"flow/{run_id}.json")
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_steps(flow_record: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "name": str(step.get("name") or ""),
            "status": str(step.get("status") or "pending"),
            "duration_ms": step.get("duration_ms"),
            "error": step.get("error"),
            "started_at": step.get("started_at"),
            "ended_at": step.get("ended_at"),
        }
        for step in flow_record.get("steps", [])
        if isinstance(step, dict)
    ]


def _current_step(
    steps: list[dict[str, Any]],
    recorded_step: str | None,
    job: dict[str, Any] | None,
) -> str | None:
    if not job or job.get("state") not in {"queued", "running"}:
        return recorded_step
    for step in steps:
        if step["status"] == "waiting_approval":
            return f"approval:{step['name']}"
        if step["status"] == "running":
            return step["name"]
    for step in steps:
        if step["status"] == "pending":
            return step["name"]
    return recorded_step


def _load_activity(flow_record: dict[str, Any]) -> list[dict[str, Any]]:
    activity: list[dict[str, Any]] = []
    for raw_step in flow_record.get("steps", []):
        if not isinstance(raw_step, dict):
            continue
        tools: list[dict[str, Any]] = []
        notices: list[dict[str, str]] = []
        model_calls = 0
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        for raw_event in raw_step.get("trace", []) or []:
            if not isinstance(raw_event, dict):
                continue
            event_type = str(raw_event.get("type") or "")
            data = raw_event.get("data") if isinstance(raw_event.get("data"), dict) else {}
            if event_type == "tool_call":
                tools.append(
                    {
                        "name": str(data.get("name") or "tool"),
                        "arguments": _display_value(data.get("arguments"), limit=1600),
                        "output": None,
                        "latency_ms": None,
                        "timestamp": raw_event.get("timestamp"),
                    }
                )
            elif event_type == "tool_result":
                name = str(data.get("name") or "tool")
                target = next(
                    (item for item in reversed(tools) if item["name"] == name and item["output"] is None),
                    None,
                )
                if target is None:
                    target = {
                        "name": name,
                        "arguments": "",
                        "output": None,
                        "latency_ms": None,
                        "timestamp": raw_event.get("timestamp"),
                    }
                    tools.append(target)
                target["output"] = _display_value(data.get("output"), limit=4000)
                target["latency_ms"] = data.get("latency_ms")
            elif event_type == "model_request":
                model_calls += 1
                raw_usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
                for key in usage:
                    value = raw_usage.get(key)
                    if isinstance(value, (int, float)):
                        usage[key] += int(value)
            elif event_type in {"error", "hook_block"}:
                message = data.get("error") or data.get("reason") or data.get("message")
                if message:
                    notices.append({"type": event_type, "message": _display_value(message, limit=1000)})

        content = raw_step.get("content")
        verification_passed: bool | None = None
        step_name = str(raw_step.get("name") or "")
        if step_name.startswith("verify_") and content:
            try:
                verification_payload = json.loads(content) if isinstance(content, str) else content
            except (TypeError, json.JSONDecodeError):
                verification_payload = None
            if (
                isinstance(verification_payload, dict)
                and verification_payload.get("configured") is True
                and isinstance(verification_payload.get("passed"), bool)
            ):
                verification_passed = verification_payload["passed"]
        activity.append(
            {
                "name": step_name,
                "status": str(raw_step.get("status") or "pending"),
                "duration_ms": raw_step.get("duration_ms"),
                "started_at": raw_step.get("started_at"),
                "ended_at": raw_step.get("ended_at"),
                "error": _display_value(raw_step.get("error"), limit=1200),
                "output": _display_value(content, limit=8000) if content else "",
                "tools": tools,
                "notices": notices,
                "model_calls": model_calls,
                "usage": usage,
                "verification_passed": verification_passed,
            }
        )
    return activity


def _pending_approval(flow_record: dict[str, Any]) -> dict[str, Any] | None:
    approvals = flow_record.get("approvals") if isinstance(flow_record.get("approvals"), dict) else {}
    for step in flow_record.get("steps", []):
        if not isinstance(step, dict) or step.get("status") != "waiting_approval":
            continue
        name = str(step.get("name") or "")
        request_id = str(step.get("approval_request_id") or "")
        if not name or not request_id or name in approvals:
            continue
        return {
            "request_id": request_id,
            "step": name,
            "title": "高风险操作需要确认" if name == "edit" else "操作需要确认",
            "description": _display_value(step.get("error") or "继续执行前需要你的确认。", limit=1200),
            "requested_action": "修改隔离工作区中的文件" if name == "edit" else f"执行阶段 {name}",
        }
    return None


def _display_value(value: Any, *, limit: int) -> str:
    if value is None:
        return ""
    redacted = redact_value(value)
    if isinstance(redacted, str):
        text = redacted
    else:
        try:
            text = json.dumps(redacted, ensure_ascii=False, indent=2, default=str)
        except (TypeError, ValueError):
            text = str(redacted)
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n…（输出已截断）"


def _has_text_artifact(store: RunStore, run_id: str, name: str) -> bool:
    path = store.artifact_path(run_id, name)
    if not path.is_file():
        return False
    try:
        if path.stat().st_size == 0:
            return False
        return bool(path.read_text(encoding="utf-8", errors="replace").strip())
    except OSError:
        return False


def _read_optional_json(store: RunStore, run_id: str, name: str) -> Any | None:
    path = store.artifact_path(run_id, name)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _read_optional_text(store: RunStore, run_id: str, name: str) -> str:
    path = store.artifact_path(run_id, name)
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _read_artifact(store: RunStore, run_id: str, name: str, *, limit: int = 2_000_000) -> str:
    path = store.artifact_path(run_id, name)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="artifact not available")
    if path.stat().st_size > limit:
        raise HTTPException(status_code=413, detail="artifact is too large for the web viewer")
    return path.read_text(encoding="utf-8", errors="replace")


def _submit_existing(
    manager: TaskManagerProtocol,
    run_id: str,
    action: str,
    function: Callable[[], Any],
) -> None:
    try:
        manager.submit(run_id, action, function)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
