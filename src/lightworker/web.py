"""Local-only FastAPI control plane for LightWorker."""

from __future__ import annotations

import asyncio
import json
import re
import threading
import urllib.parse
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, status
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import __version__
from .analysis_tools import CredentialVault, sanitize_and_capture_credentials
from .config import WorkerConfig, parse_verification_command
from .context import ContextCompressor
from .control import ControlStore
from .goals import GoalManager
from .memory import WorkspaceMemory, workspace_scope
from .models import (
    GoalBudget,
    RunRecord,
    RunStatus,
    RuntimeMode,
    TaskSpec,
    VerificationCommand,
    VerificationKind,
)
from .policy import redact_value
from .rag import RAGIndex
from .sandbox_helper import HelperError, validate_command
from .skills import SkillRegistry
from .storage import RunStore
from .tool_protocol import ApprovalBroker, EventLog
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
    runtime_mode: RuntimeMode | None = None
    goal_mode: bool = True

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


class ControlRequest(BaseModel):
    reason: str = Field(default="", max_length=4000)


class SteeringRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)

    @field_validator("message")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value cannot be blank")
        return value.strip()


class MemoryCreateRequest(BaseModel):
    kind: Literal["user", "feedback", "project", "reference"]
    content: str = Field(min_length=1, max_length=20_000)
    provenance: str = Field(min_length=1, max_length=2000)
    confidence: float = Field(default=0.8, ge=0, le=1)
    promote: bool = False


class RAGIngestRequest(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=100)


class GoalUpdateRequest(BaseModel):
    objective: str | None = Field(default=None, min_length=1, max_length=20_000)
    acceptance_criteria: list[str] | None = Field(default=None, max_length=100)
    budget: GoalBudget | None = None


class TaskManagerProtocol(Protocol):
    def submit(self, run_id: str, action: str, function: Callable[[], Any]) -> None: ...

    def status(self, run_id: str) -> dict[str, Any] | None: ...

    def close(self) -> None: ...


class TaskScheduler:
    """Bounded concurrent task scheduler with observable queue/running state."""

    def __init__(self, max_workers: int = 2) -> None:
        self._states: dict[str, dict[str, Any]] = {}
        self._futures: dict[str, Future[Any]] = {}
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="lightworker-task",
        )

    def submit(self, run_id: str, action: str, function: Callable[[], Any]) -> None:
        with self._lock:
            current = self._states.get(run_id)
            if current and current["state"] in {"queued", "running"}:
                raise ValueError(f"run {run_id} already has an active job")
            self._states[run_id] = {
                "state": "queued",
                "action": action,
                "error": None,
                "queued_at": datetime.now(UTC).isoformat(),
            }
            self._futures[run_id] = self._executor.submit(self._work, run_id, action, function)

    def status(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._states.get(run_id)
            return dict(value) if value else None

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _work(self, run_id: str, action: str, function: Callable[[], Any]) -> None:
        with self._lock:
            self._states[run_id] = {
                "state": "running",
                "action": action,
                "error": None,
                "started_at": datetime.now(UTC).isoformat(),
            }
        try:
            function()
        except Exception as exc:  # pragma: no cover - defensive boundary around runner
            with self._lock:
                self._states[run_id] = {
                    "state": "failed",
                    "action": action,
                    "error": str(exc),
                    "ended_at": datetime.now(UTC).isoformat(),
                }
        else:
            with self._lock:
                self._states[run_id] = {
                    "state": "completed",
                    "action": action,
                    "error": None,
                    "ended_at": datetime.now(UTC).isoformat(),
                }


# Backwards-compatible import for integrations built against the serial scheduler.
WebTaskManager = TaskScheduler


def create_app(
    settings: WorkerConfig,
    *,
    task_manager: TaskManagerProtocol | None = None,
    runner_factory: Callable[[WorkerConfig], CodingTaskRunner] = CodingTaskRunner,
) -> FastAPI:
    own_manager = task_manager is None
    manager = task_manager or TaskScheduler(max_workers=settings.scheduler.max_tasks)
    store = RunStore(settings.state_dir)
    _migrate_legacy_credentials(store, settings.state_dir)
    _recover_stale_runs(store, manager)
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
            runtime_mode=payload.runtime_mode or settings.runtime.mode,
            goal_mode=payload.goal_mode,
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

    @app.get("/api/runs/{run_id}/events")
    async def stream_events(
        run_id: str,
        after: int = Query(default=0, ge=0),
    ) -> StreamingResponse:
        _load_record(store, run_id)

        async def generate():
            cursor = after
            idle_rounds = 0
            while True:
                events = EventLog(store, run_id).read(after=cursor, limit=500)
                if events:
                    idle_rounds = 0
                    for event in events:
                        cursor = max(cursor, int(event.get("sequence") or 0))
                        yield (
                            f"id: {cursor}\nevent: update\ndata: "
                            + json.dumps(
                                event,
                                ensure_ascii=False,
                            )
                            + "\n\n"
                        )
                else:
                    idle_rounds += 1
                    yield ": keep-alive\n\n"
                current = store.load(run_id)
                job = manager.status(run_id)
                active = current.status in ACTIVE_STATUSES or bool(
                    job and job.get("state") in {"queued", "running"}
                )
                if not active and idle_rounds >= 2:
                    break
                await asyncio.sleep(0.75)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/runs/{run_id}/followups", status_code=status.HTTP_202_ACCEPTED)
    def create_followup(run_id: str, payload: FollowUpRequest) -> dict[str, Any]:
        selected = _load_record(store, run_id)
        conversation = _conversation_records(store, selected)
        parent = conversation[-1]
        parent_job = manager.status(parent.run_id)
        if parent.status in ACTIVE_STATUSES or (
            parent_job and parent_job.get("state") in {"queued", "running"}
        ):
            safe_message, credentials = sanitize_and_capture_credentials([payload.message])
            root = conversation[0]
            CredentialVault(settings.state_dir).merge(root.run_id, credentials)
            steering = ControlStore(store, parent.run_id).add_steering(safe_message[0])
            EventLog(store, parent.run_id).emit("steering_received", steering)
            return {
                "run_id": parent.run_id,
                "root_run_id": root.run_id,
                "parent_run_id": _parent_run_id(parent),
                "status": "steering_accepted",
            }
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
            conversation_context=_build_conversation_context(store, conversation, settings),
            runtime_mode=parent_spec.runtime_mode,
            goal_mode=parent_spec.goal_mode,
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

    @app.get("/api/runs/{run_id}/browser/{name}")
    def get_browser_artifact(run_id: str, name: str) -> FileResponse:
        _load_record(store, run_id)
        if not re.fullmatch(r"screenshot-[1-9][0-9]*\.png", name):
            raise HTTPException(status_code=404, detail="unknown browser artifact")
        path = store.artifact_path(run_id, f"browser/{name}")
        if not path.is_file():
            raise HTTPException(status_code=404, detail="browser artifact not available")
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})

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
        if record.status not in {
            RunStatus.FAILED,
            RunStatus.INTERRUPTED,
            RunStatus.PAUSED,
            RunStatus.NEEDS_ATTENTION,
        }:
            raise HTTPException(status_code=409, detail="run is not resumable")
        if record.metadata.get("execution_mode") in {"general", "analysis"}:
            raise HTTPException(
                status_code=409,
                detail="legacy general tasks continue through a follow-up message",
            )
        ControlStore(store, run_id).set_state("running")
        _submit_existing(manager, run_id, "resume", lambda: runner_factory(settings).resume(run_id))
        return {"run_id": run_id, "status": "queued"}

    @app.post("/api/runs/{run_id}/pause")
    def pause_run(run_id: str, payload: ControlRequest) -> dict[str, Any]:
        record = _load_record(store, run_id)
        if record.status not in ACTIVE_STATUSES:
            raise HTTPException(status_code=409, detail="only an active run can be paused")
        value = ControlStore(store, run_id).set_state("paused", payload.reason.strip())
        EventLog(store, run_id).emit("pause_requested", value)
        return value

    @app.post("/api/runs/{run_id}/cancel")
    def cancel_run(run_id: str, payload: ControlRequest) -> dict[str, Any]:
        record = _load_record(store, run_id)
        if record.status not in ACTIVE_STATUSES:
            raise HTTPException(status_code=409, detail="only an active run can be cancelled")
        value = ControlStore(store, run_id).set_state("cancelled", payload.reason.strip())
        EventLog(store, run_id).emit("cancel_requested", value)
        return value

    @app.post("/api/runs/{run_id}/steer")
    def steer_run(run_id: str, payload: SteeringRequest) -> dict[str, Any]:
        record = _load_record(store, run_id)
        if record.status not in ACTIVE_STATUSES:
            raise HTTPException(status_code=409, detail="only an active run accepts live steering")
        sanitized, credentials = sanitize_and_capture_credentials([payload.message])
        CredentialVault(settings.state_dir).merge(_root_run_id(record), credentials)
        value = ControlStore(store, run_id).add_steering(sanitized[0])
        EventLog(store, run_id).emit("steering_received", value)
        return value

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
        tool_approval = _pending_tool_approval(store, run_id)
        if tool_approval is not None:
            _submit_existing(
                manager,
                run_id,
                "approval",
                lambda: runner_factory(settings).decide_approval(
                    run_id,
                    tool_approval["request_id"],
                    payload.decision,
                    payload.note.strip(),
                ),
            )
            return {"run_id": run_id, "status": "queued"}
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

    @app.get("/api/capabilities")
    def capabilities() -> dict[str, Any]:
        return {
            "runtime": {
                "default": settings.runtime.mode.value,
                "modes": ["agentic", "workflow"],
                "goal": True,
                "automatic_context_compression": True,
            },
            "tools": {
                "docker_shell": settings.shell.enabled,
                "web_search": settings.analysis.search_enabled,
                "browser": settings.browser.enabled,
                "browser_backend": settings.browser.backend,
                "browser_profile": "ephemeral",
                "skills": settings.skills.enabled,
                "mcp": settings.mcp.enabled,
                "rag_fts5": settings.rag.enabled,
                "rag_embeddings": settings.rag.embeddings_enabled,
                "memory": settings.memory.enabled,
                "subagents": True,
            },
            "limits": settings.scheduler.model_dump(mode="json"),
        }

    @app.get("/api/runs/{run_id}/memory")
    def list_memory(run_id: str, include_candidates: bool = True) -> list[dict[str, Any]]:
        record = _load_record(store, run_id)
        memory = WorkspaceMemory(
            settings.state_dir,
            candidate_ttl_days=settings.memory.candidate_ttl_days,
        )
        return memory.list(
            scope=_scope_for_record(store, record),
            include_candidates=include_candidates,
        )

    @app.get("/api/runs/{run_id}/goal")
    def get_goal(run_id: str) -> dict[str, Any]:
        _load_record(store, run_id)
        try:
            return GoalManager(store, run_id).load().model_dump(mode="json")
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="goal state not available") from exc

    @app.patch("/api/runs/{run_id}/goal")
    def update_goal(run_id: str, payload: GoalUpdateRequest) -> dict[str, Any]:
        _load_record(store, run_id)
        manager = GoalManager(store, run_id)
        try:
            goal = manager.load()
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="goal state not available") from exc
        if payload.objective is not None:
            goal.objective = payload.objective
        if payload.acceptance_criteria is not None:
            goal.acceptance_criteria = [item for item in payload.acceptance_criteria if item.strip()]
        if payload.budget is not None:
            goal.budget = payload.budget
        return manager.save(goal).model_dump(mode="json")

    @app.post("/api/runs/{run_id}/memory")
    def create_memory(run_id: str, payload: MemoryCreateRequest) -> dict[str, Any]:
        record = _load_record(store, run_id)
        memory = WorkspaceMemory(
            settings.state_dir,
            candidate_ttl_days=settings.memory.candidate_ttl_days,
        )
        try:
            value = memory.propose(
                scope=_scope_for_record(store, record),
                kind=payload.kind,
                content=payload.content,
                provenance=payload.provenance,
                confidence=payload.confidence,
            )
            return memory.promote(value["id"]) if payload.promote else value
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/runs/{run_id}/memory/{memory_id}/promote")
    def promote_memory(run_id: str, memory_id: str) -> dict[str, Any]:
        _load_record(store, run_id)
        try:
            return WorkspaceMemory(settings.state_dir).promote(memory_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.delete("/api/runs/{run_id}/memory/{memory_id}")
    def delete_memory(run_id: str, memory_id: str) -> dict[str, bool]:
        _load_record(store, run_id)
        return {"ok": WorkspaceMemory(settings.state_dir).delete(memory_id)}

    @app.get("/api/runs/{run_id}/skills")
    def list_skills(run_id: str) -> dict[str, Any]:
        record = _load_record(store, run_id)
        workspace = Path(record.workspace or "").resolve()
        if not workspace.is_dir():
            raise HTTPException(status_code=409, detail="saved workspace is missing")
        return SkillRegistry(workspace=workspace, config=settings.skills).manifest()

    @app.get("/api/runs/{run_id}/rag")
    def list_rag(run_id: str) -> list[dict[str, Any]]:
        record = _load_record(store, run_id)
        return RAGIndex(
            settings.state_dir,
            scope=_scope_for_record(store, record),
            config=settings.rag,
        ).list_documents()

    @app.post("/api/runs/{run_id}/rag")
    def ingest_rag(run_id: str, payload: RAGIngestRequest) -> dict[str, Any]:
        record = _load_record(store, run_id)
        workspace = Path(record.workspace or "").resolve()
        if not workspace.is_dir():
            raise HTTPException(status_code=409, detail="saved workspace is missing")
        return RAGIndex(
            settings.state_dir,
            scope=_scope_for_record(store, record),
            config=settings.rag,
        ).ingest(workspace, payload.paths)

    @app.delete("/api/runs/{run_id}/rag")
    def remove_rag(run_id: str, path: str = Query(min_length=1, max_length=4096)) -> dict[str, bool]:
        record = _load_record(store, run_id)
        return {
            "ok": RAGIndex(
                settings.state_dir,
                scope=_scope_for_record(store, record),
                config=settings.rag,
            ).remove(path)
        }

    @app.get("/api/mcp")
    def mcp_configuration() -> dict[str, Any]:
        return {
            "enabled": settings.mcp.enabled,
            "servers": {
                name: {
                    "transport": value.transport,
                    "url_host": urllib.parse.urlsplit(value.url).hostname if value.url else None,
                    "command": value.command,
                    "disabled": value.disabled,
                    "allowed_tools": value.allowed_tools,
                    "read_only_tools": value.read_only_tools,
                }
                for name, value in settings.mcp.servers.items()
            },
        }

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
    return redact_value(
        {
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
        }
    )


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
    events = EventLog(store, record.run_id).read(limit=500)
    has_changes = _has_text_artifact(store, record.run_id, "changes.patch")
    payload = redact_value(record.model_dump(mode="json"))
    payload["job"] = job
    payload["steps"] = _load_steps(flow_record) or _agentic_steps(events, record)
    payload["current_step"] = _current_step(payload["steps"], record.current_step, job)
    payload["activity"] = _load_activity(flow_record) or _agentic_activity(events, record)
    payload["events"] = events
    payload["artifacts"] = {
        name: (has_changes if name == "diff" else store.artifact_path(record.run_id, filename).is_file())
        for name, (filename, _) in ARTIFACTS.items()
    }
    payload["has_changes"] = has_changes
    approval_in_progress = bool(
        job and job.get("action") == "approval" and job.get("state") in {"queued", "running"}
    )
    payload["approval_request"] = (
        None
        if approval_in_progress
        else (_pending_tool_approval(store, record.run_id) or _pending_approval(flow_record))
    )
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
    payload["unified_mode"] = execution_mode in {"unified", "agentic"}
    payload["general_only"] = execution_mode in {"general", "analysis"}
    payload["analysis_only"] = execution_mode == "analysis"
    payload["goal"] = _read_optional_json(store, record.run_id, "goal.json")
    payload["agent_tree"] = _read_optional_json(store, record.run_id, "agent-tree.json") or {"agents": []}
    payload["tool_manifest"] = _read_optional_json(store, record.run_id, "tool-manifest.json") or []
    browser_dir = store.artifact_path(record.run_id, "browser")
    payload["browser_artifacts"] = (
        [path.name for path in sorted(browser_dir.glob("screenshot-*.png")) if path.is_file()]
        if browser_dir.is_dir()
        else []
    )
    return payload


def _conversation_turn(store: RunStore, record: RunRecord) -> dict[str, Any]:
    return redact_value(
        {
            "run_id": record.run_id,
            "message": record.task,
            "status": record.status.value,
            "created_at": record.created_at.isoformat(),
            "updated_at": record.updated_at.isoformat(),
            "error": record.error,
            "source_mode": _source_mode(record),
            "summary": _read_optional_text(store, record.run_id, "summary.md"),
            "diff": _read_optional_text(store, record.run_id, "changes.patch"),
        }
    )


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


def _recover_stale_runs(store: RunStore, manager: TaskManagerProtocol) -> None:
    """Turn process-orphaned active records into explicit resumable checkpoints."""
    for record in store.list():
        if record.status not in ACTIVE_STATUSES or manager.status(record.run_id) is not None:
            continue
        record.status = RunStatus.INTERRUPTED
        record.error = "LightWorker service restarted; this task can be resumed from its durable workspace."
        record.current_step = None
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


def _scope_for_record(store: RunStore, record: RunRecord) -> str:
    root_id = _root_run_id(record)
    try:
        root_repo = store.load(root_id).repo
    except (FileNotFoundError, ValueError):
        root_repo = record.repo
    return workspace_scope(root_repo)


def _build_conversation_context(
    store: RunStore,
    records: list[RunRecord],
    settings: WorkerConfig,
) -> str:
    turns = [
        {
            "user": record.task,
            "assistant": _read_optional_text(store, record.run_id, "summary.md")
            or record.error
            or "无可用总结",
        }
        for record in records
    ]
    decisions: list[str] = []
    for record in records:
        approvals = _read_optional_json(store, record.run_id, "approvals.json")
        if not isinstance(approvals, dict):
            continue
        for value in (approvals.get("decisions") or {}).values():
            if isinstance(value, dict):
                decisions.append(f"approval={value.get('decision')} note={value.get('note') or ''}".strip())
    result = ContextCompressor(
        context_window_tokens=settings.runtime.context_window_tokens,
        compression_ratio=settings.runtime.compression_ratio,
    ).compress_turns(
        turns,
        objective=records[0].task,
        acceptance_criteria=["Continue the original task using all relevant user-provided materials."],
        decisions=decisions,
    )
    return result.text


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


def _agentic_steps(events: list[dict[str, Any]], record: RunRecord) -> list[dict[str, Any]]:
    if not events and record.status == RunStatus.CREATED:
        return []
    started = next((item for item in events if item.get("type") == "agentic_run_started"), None)
    ended = next((item for item in reversed(events) if item.get("type") == "agentic_run_completed"), None)
    status_value = (
        "waiting_approval"
        if record.status == RunStatus.WAITING_APPROVAL
        else "running"
        if record.status in ACTIVE_STATUSES
        else "success"
        if record.status == RunStatus.SUCCEEDED
        else "failed"
    )
    return [
        {
            "name": "agentic_loop",
            "status": status_value,
            "duration_ms": None,
            "error": record.error,
            "started_at": started.get("timestamp") if started else record.created_at.isoformat(),
            "ended_at": ended.get("timestamp") if ended else None,
        }
    ]


def _agentic_activity(events: list[dict[str, Any]], record: RunRecord) -> list[dict[str, Any]]:
    if not events:
        return []
    tools: list[dict[str, Any]] = []
    notices: list[dict[str, str]] = []
    verification: list[dict[str, Any]] = []
    for event in events:
        event_type = str(event.get("type") or "")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if event_type == "tool_started":
            tools.append(
                {
                    "name": str(data.get("tool") or "tool"),
                    "arguments": _display_value(data.get("arguments"), limit=1600),
                    "output": None,
                    "latency_ms": None,
                    "timestamp": event.get("timestamp"),
                }
            )
        elif event_type in {"tool_completed", "tool_failed", "tool_blocked"}:
            name = str(data.get("tool") or "tool")
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
                    "timestamp": event.get("timestamp"),
                }
                tools.append(target)
            target["output"] = _display_value(
                data.get("output") or data.get("error") or data.get("reason"),
                limit=4000,
            )
        elif event_type == "verification_completed":
            verification = list(data.get("results") or [])
        elif event_type in {
            "approval_requested",
            "approval_decided",
            "subagent_started",
            "subagent_completed",
            "budget_exceeded",
            "steering_received",
            "steering_consumed",
            "pause_requested",
            "cancel_requested",
        }:
            notices.append({"type": event_type, "message": _display_value(data, limit=1200)})
    status_value = (
        "waiting_approval"
        if record.status == RunStatus.WAITING_APPROVAL
        else "running"
        if record.status in ACTIVE_STATUSES
        else "success"
        if record.status == RunStatus.SUCCEEDED
        else "failed"
    )
    activity = [
        {
            "name": "agentic_loop",
            "status": status_value,
            "duration_ms": None,
            "started_at": events[0].get("timestamp"),
            "ended_at": events[-1].get("timestamp") if status_value != "running" else None,
            "error": record.error,
            "output": "",
            "tools": tools,
            "notices": notices,
            "model_calls": 0,
            "usage": {},
            "verification_passed": None,
        }
    ]
    if verification:
        activity.append(
            {
                "name": "verify_0",
                "status": "success",
                "duration_ms": sum(float(item.get("duration_ms") or 0) for item in verification),
                "started_at": None,
                "ended_at": None,
                "error": "",
                "output": json.dumps({"configured": True, "results": verification}, ensure_ascii=False),
                "tools": [],
                "notices": [],
                "model_calls": 0,
                "usage": {},
                "verification_passed": all(
                    bool(item.get("passed")) or not bool(item.get("required", True)) for item in verification
                ),
            }
        )
    return activity


def _pending_tool_approval(store: RunStore, run_id: str) -> dict[str, Any] | None:
    pending = ApprovalBroker(store, run_id).pending()
    if not pending:
        return None
    request = pending[0]
    return {
        "request_id": request["request_id"],
        "step": request["request_id"],
        "title": "工具操作需要确认",
        "description": f"{request['tool']}: {request['reason']}",
        "requested_action": _display_value(
            {"tool": request["tool"], "arguments": request.get("arguments") or {}},
            limit=1600,
        ),
    }


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
