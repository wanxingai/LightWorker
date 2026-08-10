"""Durable pause, cancellation, and live steering state."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any, Literal

from .storage import RunStore


class ControlStore:
    def __init__(self, store: RunStore, run_id: str):
        self.store = store
        self.run_id = run_id
        self._lock = threading.RLock()

    def state(self) -> dict[str, Any]:
        try:
            value = self.store.read_json(self.run_id, "control.json")
        except (FileNotFoundError, ValueError):
            value = {}
        return {
            "state": str(value.get("state") or "running") if isinstance(value, dict) else "running",
            "reason": str(value.get("reason") or "") if isinstance(value, dict) else "",
            "steering": list(value.get("steering") or []) if isinstance(value, dict) else [],
            "updated_at": value.get("updated_at") if isinstance(value, dict) else None,
        }

    def set_state(self, state: Literal["running", "paused", "cancelled"], reason: str = "") -> dict[str, Any]:
        with self._lock:
            value = self.state()
            value.update(
                {
                    "state": state,
                    "reason": reason,
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            )
            self.store.write_json(self.run_id, "control.json", value)
            return value

    def add_steering(self, message: str) -> dict[str, Any]:
        with self._lock:
            value = self.state()
            value["steering"].append(
                {
                    "message": message,
                    "created_at": datetime.now(UTC).isoformat(),
                    "consumed": False,
                }
            )
            value["updated_at"] = datetime.now(UTC).isoformat()
            self.store.write_json(self.run_id, "control.json", value)
            return value["steering"][-1]

    def consume_steering(self) -> list[str]:
        with self._lock:
            value = self.state()
            messages = [
                str(item.get("message") or "") for item in value["steering"] if not item.get("consumed")
            ]
            if messages:
                for item in value["steering"]:
                    if not item.get("consumed"):
                        item["consumed"] = True
                        item["consumed_at"] = datetime.now(UTC).isoformat()
                self.store.write_json(self.run_id, "control.json", value)
            return [message for message in messages if message]

    def blocking_reason(self) -> str | None:
        value = self.state()
        if value["state"] == "cancelled":
            return value["reason"] or "task cancelled by user"
        if value["state"] == "paused":
            return value["reason"] or "task paused by user"
        return None
