"""Atomic LightAgent Session storage and canonical LightWorker projections.

The SQLite adapter preserves the public LightAgent event schema. Compatibility
files are disposable projections; they never supersede an existing Session.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any

from LightAgent import Session, SqliteSessionStore

from .policy import redact_value

_transactions = threading.local()


class SequenceConflict(RuntimeError):
    pass


class SessionFlowStore:
    def __init__(self, store):
        self.store = store

    def save_run(self, run_id, record):
        self.store.write_json(run_id, f"flow/{run_id}.json", record)

    def load_run(self, run_id):
        try:
            return self.store.read_json(run_id, f"flow/{run_id}.json")
        except FileNotFoundError:
            return None

    def list_runs(self):
        return [value for run in self.store.list() if (value := self.load_run(run.run_id)) is not None]


class AtomicSessionStore(SqliteSessionStore):
    """Append without replacing history, including when an agent holds stale state."""

    @contextmanager
    def transaction(self):
        connections = getattr(_transactions, "connections", None)
        if connections is None:
            connections = _transactions.connections = {}
        key = str(Path(self.path).resolve())
        if key in connections:
            yield connections[key]
            return
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connections[key] = connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connections.pop(key, None)
            connection.close()

    @staticmethod
    def _load(connection, session_id: str) -> Session | None:
        row = connection.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            return None
        events = connection.execute(
            "SELECT payload FROM session_events WHERE session_id=? ORDER BY sequence", (session_id,)
        ).fetchall()
        return Session.from_dict(
            {
                "session_id": session_id,
                "metadata": json.loads(row["metadata"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "schema_version": row["schema_version"],
                "events": [json.loads(event["payload"]) for event in events],
            }
        )

    def get(self, session_id: str) -> Session | None:
        with self.transaction() as connection:
            return self._load(connection, str(session_id))

    def create(self, session=None, *, metadata=None):
        value = session or Session(metadata=metadata or {})
        with self.transaction() as connection:
            if self._load(connection, value.session_id) is not None:
                raise ValueError(f"session already exists: {value.session_id}")
            self.save(value)
        return deepcopy(value)

    def save(self, session: Session) -> None:
        session.validate()
        with self.transaction() as connection:
            persisted = self._load(connection, session.session_id)
            known = {event.event_id: event for event in persisted.events} if persisted else {}
            merged = persisted or Session(
                session_id=session.session_id,
                metadata=redact_value(session.metadata),
                created_at=session.created_at,
                schema_version=session.schema_version,
            )
            additions = []
            for event in session.events:
                if event.event_id in known:
                    continue
                value = deepcopy(event)
                value.sequence = len(merged.events) + 1
                value.data = redact_value(value.data)
                merged.events.append(value)
                additions.append(value)
            merged.metadata.update(redact_value(session.metadata))
            merged.updated_at = session.updated_at
            connection.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET "
                "metadata=excluded.metadata, updated_at=excluded.updated_at",
                (
                    merged.session_id,
                    json.dumps(merged.metadata, ensure_ascii=False),
                    merged.created_at,
                    merged.updated_at,
                    merged.schema_version,
                ),
            )
            connection.executemany(
                "INSERT INTO session_events VALUES (?,?,?,?)",
                [
                    (merged.session_id, e.sequence, e.event_id, json.dumps(e.to_dict(), ensure_ascii=False))
                    for e in additions
                ],
            )
            session.events = deepcopy(merged.events)
            session.metadata = deepcopy(merged.metadata)

    def append_if_sequence(
        self,
        session_id: str,
        event_type: str,
        data: dict[str, Any],
        *,
        expected_sequence: int | None = None,
        idempotency_key: str | None = None,
        **fields: Any,
    ):
        with self.transaction() as connection:
            session = self._load(connection, session_id) or Session(session_id=session_id)
            if idempotency_key:
                for event in session.events:
                    if event.data.get("idempotency_key") == idempotency_key:
                        return event
            if expected_sequence is not None and len(session.events) != expected_sequence:
                raise SequenceConflict(f"expected {expected_sequence}; found {len(session.events)}")
            payload = redact_value(dict(data))
            if idempotency_key:
                payload["idempotency_key"] = idempotency_key
            event = session.append(event_type, payload, **fields)
            self.save(session)
            return event


class TaskSession:
    def __init__(self, state_dir: Path, run_id: str):
        self.run_id = run_id
        self.session_id = run_id
        self.store = session_store(str(state_dir / "lightagent-sessions.sqlite3"))

    def append(self, event_type: str, data: dict[str, Any], *, idempotency_key=None, **fields):
        return self.store.append_if_sequence(
            self.session_id,
            event_type,
            data,
            idempotency_key=idempotency_key,
            run_id=self.run_id,
            **fields,
        )

    def events(self):
        session = self.store.get(self.session_id)
        return session.events if session else []

    def state(self, name: str):
        for event in reversed(self.events()):
            if event.type == "lightworker.state.updated" and event.data.get("name") == name:
                return deepcopy(event.data["value"])
        return None

    def set_state(self, name: str, value: Any):
        with self.store.transaction():
            if self.state(name) == redact_value(value):
                return
            self.append("lightworker.state.updated", {"name": name, "value": value})

    def export(self):
        session = self.store.get(self.session_id)
        return session.to_dict() if session else {"session_id": self.session_id, "events": []}

    def replay(self):
        states = {}
        for event in self.events():
            if event.type == "lightworker.state.updated":
                states[event.data["name"]] = event.data["value"]
        return {"session_id": self.session_id, "states": states, "event_count": len(self.events())}


@lru_cache(maxsize=128)
def session_store(path: str) -> AtomicSessionStore:
    return AtomicSessionStore(path)
