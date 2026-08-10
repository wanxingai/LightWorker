"""Atomic run metadata and artifact persistence."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .models import RunRecord, RunStatus, utc_now


class RunStore:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir.expanduser().resolve()
        self.runs_dir = self.state_dir / "runs"

    def create(self, record: RunRecord) -> Path:
        directory = self.run_dir(record.run_id)
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "logs").mkdir()
        (directory / "flow").mkdir()
        self.save(record)
        return directory

    def save(self, record: RunRecord) -> None:
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
        if not self.runs_dir.exists():
            return []
        records: list[RunRecord] = []
        for path in sorted(self.runs_dir.glob("*/run.json")):
            try:
                records.append(RunRecord.model_validate_json(path.read_text(encoding="utf-8")))
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
        return self.write_text(run_id, name, json.dumps(value, ensure_ascii=False, indent=2, default=str))

    def read_json(self, run_id: str, name: str) -> Any:
        return json.loads(self.artifact_path(run_id, name).read_text(encoding="utf-8"))


def _safe_identifier(value: str) -> str:
    if not value or any(
        char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in value
    ):
        raise ValueError("identifier contains unsafe characters")
    return value


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
