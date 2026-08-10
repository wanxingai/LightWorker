"""Durable per-conversation follow-up queue."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from .storage import RunStore

QueueStatus = Literal["pending", "running", "completed", "guided", "failed"]


class ConversationMessageQueue:
    """Persist queued follow-ups on the root run and update them atomically."""

    filename = "message-queue.json"

    def __init__(self, store: RunStore):
        self.store = store
        self._lock = threading.RLock()

    def active(self, root_run_id: str) -> list[dict[str, Any]]:
        return [
            dict(item)
            for item in self._read(root_run_id)
            if item.get("status") in {"pending", "running"}
        ]

    def all(self, root_run_id: str) -> list[dict[str, Any]]:
        return [dict(item) for item in self._read(root_run_id)]

    def enqueue(self, root_run_id: str, message: str) -> dict[str, Any]:
        with self._lock:
            items = self._read(root_run_id)
            item = {
                "id": uuid4().hex,
                "message": message,
                "status": "pending",
                "created_at": datetime.now(UTC).isoformat(),
                "run_id": None,
            }
            items.append(item)
            self._write(root_run_id, items)
            return dict(item)

    def claim(self, root_run_id: str, item_id: str, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            items = self._read(root_run_id)
            if any(item.get("status") == "running" for item in items):
                return None
            item = next((value for value in items if value.get("id") == item_id), None)
            if item is None or item.get("status") != "pending":
                return None
            item.update(
                {
                    "status": "running",
                    "run_id": run_id,
                    "started_at": datetime.now(UTC).isoformat(),
                }
            )
            self._write(root_run_id, items)
            return dict(item)

    def release(self, root_run_id: str, item_id: str, error: str = "") -> None:
        with self._lock:
            items = self._read(root_run_id)
            item = self._find(items, item_id)
            item.update({"status": "pending", "run_id": None})
            item.pop("started_at", None)
            if error:
                item["last_error"] = error
            self._write(root_run_id, items)

    def complete(self, root_run_id: str, item_id: str, *, error: str = "") -> None:
        with self._lock:
            items = self._read(root_run_id)
            item = self._find(items, item_id)
            item.update(
                {
                    "status": "failed" if error else "completed",
                    "completed_at": datetime.now(UTC).isoformat(),
                }
            )
            if error:
                item["error"] = error
            self._write(root_run_id, items)

    def guide(self, root_run_id: str, item_id: str) -> dict[str, Any]:
        with self._lock:
            items = self._read(root_run_id)
            item = self._find(items, item_id)
            if item.get("status") != "pending":
                raise ValueError("only a pending message can guide the current task")
            item.update(
                {
                    "status": "guided",
                    "guided_at": datetime.now(UTC).isoformat(),
                }
            )
            self._write(root_run_id, items)
            return dict(item)

    def roots(self) -> list[str]:
        if not self.store.runs_dir.exists():
            return []
        return [
            path.parent.name
            for path in self.store.runs_dir.glob(f"*/{self.filename}")
            if path.is_file()
        ]

    def _read(self, root_run_id: str) -> list[dict[str, Any]]:
        try:
            value = self.store.read_json(root_run_id, self.filename)
        except (FileNotFoundError, ValueError):
            return []
        if not isinstance(value, list):
            return []
        return [dict(item) for item in value if isinstance(item, dict)]

    def _write(self, root_run_id: str, items: list[dict[str, Any]]) -> None:
        self.store.write_json(root_run_id, self.filename, items)

    @staticmethod
    def _find(items: list[dict[str, Any]], item_id: str) -> dict[str, Any]:
        item = next((value for value in items if value.get("id") == item_id), None)
        if item is None:
            raise KeyError(item_id)
        return item
