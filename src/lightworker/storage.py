"""Atomic run metadata and artifact persistence."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import RunRecord, RunStatus, utc_now


class RunStore:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir.expanduser().resolve()
        self.runs_dir = self.state_dir / "runs"

    def session(self, run_id: str):
        from .sessions import TaskSession

        _safe_identifier(run_id)
        return TaskSession(self.state_dir, run_id)

    def transaction(self, run_id: str):
        return self.session(run_id).store.transaction()

    def create(self, record: RunRecord) -> Path:
        directory = self.run_dir(record.run_id)
        if self.session(record.run_id).state("run.json") is not None or (directory / "run.json").exists():
            raise FileExistsError(f"run already exists: {record.run_id}")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "logs").mkdir(exist_ok=True)
        (directory / "flow").mkdir(exist_ok=True)
        self.save(record)
        return directory

    def save(self, record: RunRecord) -> None:
        from .analysis_tools import CredentialVault, sanitize_and_capture_credentials

        sanitized, credentials = sanitize_and_capture_credentials([record.task])
        if credentials:
            CredentialVault(self.state_dir).merge(
                str(record.metadata.get("task_spec", {}).get("root_run_id") or record.run_id), credentials
            )
            record.task = sanitized[0]
            from .policy import redact_value

            record.metadata = redact_value(record.metadata)
        record.updated_at = utc_now()
        self.write_json(record.run_id, "run.json", record.model_dump(mode="json"))

    def load(self, run_id: str) -> RunRecord:
        payload = self.read_json(run_id, "run.json")
        return RunRecord.model_validate(payload)

    def update_status(
        self,
        run_id: str,
        status: RunStatus,
        *,
        current_step: str | None = None,
        error: str | None = None,
    ) -> RunRecord:
        record = self.load(run_id)
        record.status = status
        record.current_step = current_step
        record.error = error
        self.save(record)
        return record

    def list(self) -> list[RunRecord]:
        records: list[RunRecord] = []
        identities = {p.parent.name for p in self.runs_dir.glob("*/run.json")}
        from .sessions import session_store

        with session_store(str(self.state_dir / "lightagent-sessions.sqlite3")).transaction() as connection:
            rows = connection.execute(
                "SELECT DISTINCT session_id FROM session_events "
                "WHERE json_extract(payload, '$.data.name')='run.json'"
            ).fetchall()
            identities.update(row[0] for row in rows)
        for run_id in sorted(identities):
            try:
                records.append(self.load(run_id))
            except (ValueError, OSError):
                continue
        return sorted(records, key=lambda item: item.created_at, reverse=True)

    def run_dir(self, run_id: str) -> Path:
        safe = _safe_identifier(run_id)
        return self.runs_dir / safe

    def workspace_dir(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "workspace"

    def artifact_path(self, run_id: str, name: str) -> Path:
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("artifact name must be a safe relative path")
        return self.run_dir(run_id) / relative

    def write_text(self, run_id: str, name: str, value: str) -> Path:
        path = self.artifact_path(run_id, name)
        _atomic_write(path, value.encode("utf-8"))
        if name in {"summary.md", "changes.patch", "plan.md", "git-status.txt"}:
            self.session(run_id).set_state(f"artifact:{name}", value)
        return path

    def append_text(self, run_id: str, name: str, value: str) -> Path:
        path = self.artifact_path(run_id, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        return path

    def write_json(self, run_id: str, name: str, value: Any) -> Path:
        if _canonical_json(name):
            self.session(run_id).set_state(name, value)
        return self.write_text(run_id, name, json.dumps(value, ensure_ascii=False, indent=2, default=str))

    def read_json(self, run_id: str, name: str) -> Any:
        if _canonical_json(name):
            with self.transaction(run_id):
                projected = self.session(run_id).state(name)
                if projected is not None:
                    return projected
                value = json.loads(self.artifact_path(run_id, name).read_text(encoding="utf-8"))
                self.session(run_id).set_state(name, value)
                return value
        return json.loads(self.artifact_path(run_id, name).read_text(encoding="utf-8"))


def _canonical_json(name: str) -> bool:
    return name.startswith("flow/") or name in {
        "run.json",
        "goal.json",
        "control.json",
        "approvals.json",
        "agent-tree.json",
        "working-memory.json",
        "tool-manifest.json",
        "plan.json",
        "review-decision.json",
        "jobs.json",
        "evidence.json",
        "artifacts.json",
        "agents.json",
        "providers.json",
        "provider-health.json",
        "schedules.json",
        "context-state.json",
        "plugin-lock.json",
        "workflow-state.json",
        "workflow-tool-resume.json",
        "code-state.json",
        "task-request.json",
    }


def _safe_identifier(value: str) -> str:
    if not value or any(
        char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in value
    ):
        raise ValueError("identifier contains unsafe characters")
    return value


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
