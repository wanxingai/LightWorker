"""Session-backed per-conversation follow-up queue."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from .storage import RunStore

try:
    from LightAgent import (
        AgentRuntime,
        InboxMessageType,
        SqliteSessionStore,
    )
except ImportError:  # pragma: no cover - guarded by the package dependency
    AgentRuntime = None  # type: ignore[assignment]
    InboxMessageType = None  # type: ignore[assignment]
    SqliteSessionStore = None  # type: ignore[assignment]

QueueStatus = Literal["pending", "running", "completed", "guided", "failed"]


class ConversationMessageQueue:
    """Project a durable queue from a conversation-level LightAgent Session.

    ``message-queue.json`` remains a human-readable compatibility cache. The
    append-only Session is authoritative after an existing JSON queue has been
    migrated on first access.
    """

    filename = "message-queue.json"
    snapshot_filename = "lightagent-conversation.json"

    def __init__(self, store: RunStore, *, session_store: Any | None = None):
        if AgentRuntime is None or SqliteSessionStore is None:
            raise RuntimeError("LightAgent Session API is unavailable; install LightAgent>=0.10,<0.16")
        self.store = store
        self.session_store = session_store or SqliteSessionStore(
            store.state_dir / "lightagent-sessions.sqlite3"
        )
        self._lock = threading.RLock()

    def active(self, root_run_id: str) -> list[dict[str, Any]]:
        return [
            dict(item) for item in self._read(root_run_id) if item.get("status") in {"pending", "running"}
        ]

    def all(self, root_run_id: str) -> list[dict[str, Any]]:
        return [dict(item) for item in self._read(root_run_id)]

    def enqueue(self, root_run_id: str, message: str) -> dict[str, Any]:
        with self._lock:
            self._ensure_migrated(root_run_id)
            runtime = self._open(root_run_id)
            item = {
                "id": uuid4().hex,
                "message": message,
                "status": "pending",
                "created_at": _now(),
                "run_id": None,
            }
            runtime.inbox.enqueue(
                InboxMessageType.FOLLOWUP,
                message,
                message_id=str(item["id"]),
                correlation_id=root_run_id,
                metadata={
                    "lightworker_queue_item_id": item["id"],
                    "root_run_id": root_run_id,
                    "attempt": 1,
                },
            )
            self._append(runtime, "lightworker.queue.enqueued", {"item": item})
            self._sync_cache(root_run_id, runtime)
            return dict(item)

    def claim(self, root_run_id: str, item_id: str, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            items = self._read(root_run_id)
            if any(item.get("status") == "running" for item in items):
                return None
            item = next((value for value in items if value.get("id") == item_id), None)
            if item is None or item.get("status") != "pending":
                return None
            runtime = self._open(root_run_id)
            message = self._active_inbox_message(runtime, item_id)
            if message is None:
                message = runtime.inbox.enqueue(
                    InboxMessageType.FOLLOWUP,
                    item.get("message"),
                    message_id=f"{item_id}-recovered-{uuid4().hex}",
                    correlation_id=root_run_id,
                    metadata={
                        "lightworker_queue_item_id": item_id,
                        "root_run_id": root_run_id,
                        "recovered": True,
                    },
                )
            started_at = _now()
            self._record_inbox_status(runtime, message, "claimed", timestamp=started_at)
            self._append(
                runtime,
                "lightworker.queue.claimed",
                {"item_id": item_id, "run_id": run_id, "started_at": started_at},
            )
            projected = self._project(runtime.session)
            self._sync_cache(root_run_id, runtime, projected)
            return dict(self._find(projected, item_id))

    def release(self, root_run_id: str, item_id: str, error: str = "") -> None:
        with self._lock:
            items = self._read(root_run_id)
            item = self._find(items, item_id)
            runtime = self._open(root_run_id)
            active = self._active_inbox_message(runtime, item_id)
            if active is not None:
                self._record_inbox_status(
                    runtime,
                    active,
                    "rejected",
                    reason=error or "queued run released for retry",
                )
            attempt = self._attempt_count(runtime, item_id) + 1
            runtime.inbox.enqueue(
                InboxMessageType.FOLLOWUP,
                item.get("message"),
                message_id=f"{item_id}-attempt-{attempt}-{uuid4().hex}",
                correlation_id=root_run_id,
                metadata={
                    "lightworker_queue_item_id": item_id,
                    "root_run_id": root_run_id,
                    "attempt": attempt,
                },
            )
            payload: dict[str, Any] = {"item_id": item_id, "released_at": _now()}
            if error:
                payload["last_error"] = error
            self._append(runtime, "lightworker.queue.released", payload)
            self._sync_cache(root_run_id, runtime)

    def complete(self, root_run_id: str, item_id: str, *, error: str = "") -> None:
        with self._lock:
            items = self._read(root_run_id)
            self._find(items, item_id)
            runtime = self._open(root_run_id)
            active = self._active_inbox_message(runtime, item_id)
            if active is not None:
                if error:
                    self._record_inbox_status(runtime, active, "rejected", reason=error)
                else:
                    if _enum_value(active.status) == "pending":
                        self._record_inbox_status(runtime, active, "claimed")
                    self._record_inbox_status(
                        runtime,
                        active,
                        "completed",
                        result={"lightworker_run_status": "succeeded"},
                    )
            payload: dict[str, Any] = {
                "item_id": item_id,
                "status": "failed" if error else "completed",
                "completed_at": _now(),
            }
            if error:
                payload["error"] = error
            self._append(runtime, "lightworker.queue.completed", payload)
            self._sync_cache(root_run_id, runtime)

    def guide(self, root_run_id: str, item_id: str) -> dict[str, Any]:
        with self._lock:
            items = self._read(root_run_id)
            item = self._find(items, item_id)
            if item.get("status") != "pending":
                raise ValueError("only a pending message can guide the current task")
            runtime = self._open(root_run_id)
            active = self._active_inbox_message(runtime, item_id)
            if active is not None:
                self._record_inbox_status(
                    runtime,
                    active,
                    "rejected",
                    reason="converted to steering",
                )
            steering = runtime.inbox.enqueue(
                InboxMessageType.STEERING,
                item.get("message"),
                message_id=f"steering-{item_id}",
                correlation_id=root_run_id,
                metadata={
                    "lightworker_queue_item_id": item_id,
                    "root_run_id": root_run_id,
                    "converted_from": "followup",
                },
            )
            self._record_inbox_status(runtime, steering, "claimed")
            self._record_inbox_status(
                runtime,
                steering,
                "completed",
                result={"delivery": "control_store", "safe_boundary": True},
            )
            guided_at = _now()
            self._append(
                runtime,
                "lightworker.queue.guided",
                {"item_id": item_id, "guided_at": guided_at},
            )
            projected = self._project(runtime.session)
            self._sync_cache(root_run_id, runtime, projected)
            return dict(self._find(projected, item_id))

    def roots(self) -> list[str]:
        roots: set[str] = set()
        if self.store.runs_dir.exists():
            roots.update(
                path.parent.name for path in self.store.runs_dir.glob(f"*/{self.filename}") if path.is_file()
            )
        for session in self.session_store.list(limit=10_000):
            root_run_id = session.metadata.get("lightworker_root_run_id")
            if session.metadata.get("kind") == "lightworker.conversation" and root_run_id:
                roots.add(str(root_run_id))
        return sorted(roots)

    def snapshot(self, root_run_id: str) -> dict[str, Any]:
        """Return a redaction-ready native conversation runtime snapshot."""
        with self._lock:
            self._ensure_migrated(root_run_id)
            runtime = self._open(root_run_id)
            snapshot = runtime.snapshot()
            snapshot["queue"] = self._project(runtime.session)
            snapshot["source_of_truth"] = "lightagent_session"
            return snapshot

    def _read(self, root_run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_migrated(root_run_id)
            runtime = self._open(root_run_id)
            items = self._project(runtime.session)
            self._sync_cache(root_run_id, runtime, items)
            return items

    def _ensure_migrated(self, root_run_id: str) -> None:
        runtime = self._open(root_run_id)
        event_types = {event.type for event in runtime.session.events}
        migration_started = "lightworker.queue.migration_started" in event_types
        migration_completed = "lightworker.queue.migration_completed" in event_types
        native_queue_created = "lightworker.queue.enqueued" in event_types and not migration_started
        if migration_completed or native_queue_created:
            return
        legacy = self._read_cache(root_run_id)
        if not legacy:
            return
        if not migration_started:
            self._append(
                runtime,
                "lightworker.queue.migration_started",
                {"source": self.filename, "item_count": len(legacy)},
            )
        migrated_ids = {str(item.get("id")) for item in self._project(runtime.session)}
        for raw_item in legacy:
            item = dict(raw_item)
            item.setdefault("id", uuid4().hex)
            if str(item["id"]) in migrated_ids:
                continue
            item.setdefault("message", "")
            item.setdefault("status", "pending")
            item.setdefault("created_at", _now())
            item.setdefault("run_id", None)
            message = runtime.inbox.enqueue(
                InboxMessageType.FOLLOWUP,
                item["message"],
                message_id=str(item["id"]),
                correlation_id=root_run_id,
                metadata={
                    "lightworker_queue_item_id": item["id"],
                    "root_run_id": root_run_id,
                    "migrated": True,
                },
            )
            base = {**item, "status": "pending", "run_id": None}
            self._append(runtime, "lightworker.queue.enqueued", {"item": base})
            item_status = str(item.get("status") or "pending")
            if item_status == "running":
                self._record_inbox_status(
                    runtime,
                    message,
                    "claimed",
                    timestamp=str(item.get("started_at") or _now()),
                )
                self._append(
                    runtime,
                    "lightworker.queue.claimed",
                    {
                        "item_id": item["id"],
                        "run_id": item.get("run_id"),
                        "started_at": item.get("started_at") or _now(),
                    },
                )
            elif item_status in {"completed", "failed"}:
                if item_status == "completed":
                    self._record_inbox_status(runtime, message, "claimed")
                    self._record_inbox_status(runtime, message, "completed")
                else:
                    self._record_inbox_status(
                        runtime,
                        message,
                        "rejected",
                        reason=str(item.get("error") or "migrated failed queue item"),
                    )
                self._append(
                    runtime,
                    "lightworker.queue.completed",
                    {
                        "item_id": item["id"],
                        "status": item_status,
                        "completed_at": item.get("completed_at") or _now(),
                        "error": item.get("error", ""),
                    },
                )
            elif item_status == "guided":
                self._record_inbox_status(
                    runtime,
                    message,
                    "rejected",
                    reason="migrated guided queue item",
                )
                self._append(
                    runtime,
                    "lightworker.queue.guided",
                    {"item_id": item["id"], "guided_at": item.get("guided_at") or _now()},
                )
        self._append(
            runtime,
            "lightworker.queue.migration_completed",
            {"source": self.filename, "item_count": len(legacy)},
        )
        self._sync_cache(root_run_id, runtime)

    def _open(self, root_run_id: str) -> Any:
        runtime = AgentRuntime(session_store=self.session_store)
        runtime.open_session(
            self._session_id(root_run_id),
            metadata={
                "kind": "lightworker.conversation",
                "lightworker_root_run_id": root_run_id,
                "source_of_truth": True,
            },
        )
        runtime.context.run_id = root_run_id
        return runtime

    def _sync_cache(
        self,
        root_run_id: str,
        runtime: Any,
        items: list[dict[str, Any]] | None = None,
    ) -> None:
        projected = items if items is not None else self._project(runtime.session)
        # Targeted Inbox transitions are appended to the public Session event
        # schema. Reopen once so this diagnostic cache reflects the restored
        # native Inbox rather than the adapter instance's stale in-memory list.
        refreshed = self._open(root_run_id)
        self.store.write_json(root_run_id, self.filename, projected)
        self.store.write_json(
            root_run_id,
            self.snapshot_filename,
            {
                "session_id": refreshed.session.session_id,
                "source_of_truth": "lightagent_session",
                "event_count": len(refreshed.session.events),
                "updated_at": refreshed.session.updated_at,
                "queue": projected,
                "inbox": [message.to_dict() for message in refreshed.inbox.list()],
            },
        )

    def _read_cache(self, root_run_id: str) -> list[dict[str, Any]]:
        try:
            value = self.store.read_json(root_run_id, self.filename)
        except (FileNotFoundError, ValueError):
            return []
        if not isinstance(value, list):
            return []
        return [dict(item) for item in value if isinstance(item, dict)]

    def _append(self, runtime: Any, event_type: str, data: dict[str, Any]) -> None:
        runtime.session.append(event_type, data, run_id=runtime.context.run_id)
        self.session_store.save(runtime.session)

    def _record_inbox_status(
        self,
        runtime: Any,
        message: Any,
        status: Literal["claimed", "completed", "rejected"],
        *,
        timestamp: str | None = None,
        result: Any = None,
        reason: str = "",
    ) -> None:
        """Append a targeted Inbox transition missing from LightAgent 0.10's API.

        AgentInbox currently exposes only ``claim_next``. A conversation queue
        must transition a specific logical item after retries, so the adapter
        emits the same public Session event schema that ``AgentInbox.restore``
        consumes. This can be replaced with ``claim(message_id)`` when the core
        runtime adds it.
        """
        value = message.to_dict() if hasattr(message, "to_dict") else dict(message)
        now = timestamp or _now()
        value["status"] = status
        if status == "claimed":
            value["claimed_at"] = now
            payload = {"message": value}
            event_type = "inbox.claimed"
        elif status == "completed":
            value["claimed_at"] = value.get("claimed_at") or now
            value["completed_at"] = now
            payload = {"message": value, "result": result}
            event_type = "inbox.completed"
        else:
            value["completed_at"] = now
            payload = {"message": value, "reason": reason}
            event_type = "inbox.rejected"
        self._append(runtime, event_type, payload)

    @staticmethod
    def _project(session: Any) -> list[dict[str, Any]]:
        ordered: list[str] = []
        by_id: dict[str, dict[str, Any]] = {}
        for event in session.events:
            data = event.data
            if event.type == "lightworker.queue.enqueued":
                item = data.get("item")
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                item_id = str(item["id"])
                if item_id not in by_id:
                    ordered.append(item_id)
                by_id[item_id] = dict(item)
            elif event.type == "lightworker.queue.claimed":
                item = by_id.get(str(data.get("item_id") or ""))
                if item is not None:
                    item.update(
                        {
                            "status": "running",
                            "run_id": data.get("run_id"),
                            "started_at": data.get("started_at"),
                        }
                    )
            elif event.type == "lightworker.queue.released":
                item = by_id.get(str(data.get("item_id") or ""))
                if item is not None:
                    item.update({"status": "pending", "run_id": None})
                    item.pop("started_at", None)
                    if data.get("last_error"):
                        item["last_error"] = data["last_error"]
            elif event.type == "lightworker.queue.completed":
                item = by_id.get(str(data.get("item_id") or ""))
                if item is not None:
                    item.update(
                        {
                            "status": data.get("status") or "completed",
                            "completed_at": data.get("completed_at"),
                        }
                    )
                    if data.get("error"):
                        item["error"] = data["error"]
            elif event.type == "lightworker.queue.guided":
                item = by_id.get(str(data.get("item_id") or ""))
                if item is not None:
                    item.update({"status": "guided", "guided_at": data.get("guided_at")})
        return [dict(by_id[item_id]) for item_id in ordered]

    @staticmethod
    def _active_inbox_message(runtime: Any, item_id: str) -> Any | None:
        for message in reversed(runtime.inbox.list()):
            if message.metadata.get("lightworker_queue_item_id") != item_id:
                continue
            if _enum_value(message.status) in {"pending", "claimed"}:
                return message
        return None

    @staticmethod
    def _attempt_count(runtime: Any, item_id: str) -> int:
        return sum(
            1
            for message in runtime.inbox.list()
            if message.type == InboxMessageType.FOLLOWUP
            and message.metadata.get("lightworker_queue_item_id") == item_id
        )

    @staticmethod
    def _session_id(root_run_id: str) -> str:
        return f"lightworker-conversation-{root_run_id}"

    @staticmethod
    def _find(items: list[dict[str, Any]], item_id: str) -> dict[str, Any]:
        item = next((value for value in items if value.get("id") == item_id), None)
        if item is None:
            raise KeyError(item_id)
        return item


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _now() -> str:
    return datetime.now(UTC).isoformat()
