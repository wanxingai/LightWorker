"""Working memory, durable workspace memory, and hierarchical AGENTS.md loading."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from .models import ApprovalPolicy, ToolCategory
from .policy import redact_text
from .storage import RunStore
from .tool_protocol import tool_info


def workspace_scope(repo: str | Path) -> str:
    normalized = str(Path(repo).expanduser().resolve())
    return hashlib.sha256(normalized.encode()).hexdigest()[:24]


class WorkingMemory:
    def __init__(self, store: RunStore, run_id: str):
        self.store = store
        self.run_id = run_id
        self._lock = threading.RLock()

    def load(self) -> dict[str, Any]:
        try:
            value = self.store.read_json(self.run_id, "working-memory.json")
        except (FileNotFoundError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def set(self, key: str, value: Any) -> dict[str, Any]:
        if not key or len(key) > 128:
            raise ValueError("working-memory key must contain 1 to 128 characters")
        with self._lock:
            memory = self.load()
            memory[key] = value
            self.store.write_json(self.run_id, "working-memory.json", memory)
            return memory


class WorkspaceMemory:
    def __init__(self, state_dir: Path, *, candidate_ttl_days: int = 30):
        self.path = state_dir.expanduser().resolve() / "memory.sqlite3"
        self.candidate_ttl_days = candidate_ttl_days
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS memory (
                    id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    provenance TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT
                )
                """
            )
            connection.execute("CREATE INDEX IF NOT EXISTS memory_scope_status ON memory(scope, status)")

    def propose(
        self,
        *,
        scope: str,
        kind: Literal["user", "feedback", "project", "reference"],
        content: str,
        provenance: str,
        confidence: float = 0.5,
    ) -> dict[str, Any]:
        safe = redact_text(content.strip())
        if not safe or len(safe) > 20_000:
            raise ValueError("memory content must contain 1 to 20000 characters")
        if "[redacted]" in safe and safe != content.strip():
            raise ValueError("memory containing a detected secret cannot be stored")
        now = datetime.now(UTC)
        value = {
            "id": uuid4().hex,
            "scope": scope,
            "kind": kind,
            "content": safe,
            "provenance": redact_text(provenance)[:2000],
            "confidence": max(0.0, min(float(confidence), 1.0)),
            "status": "candidate",
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(days=self.candidate_ttl_days)).isoformat(),
        }
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO memory VALUES (:id, :scope, :kind, :content, :provenance, "
                ":confidence, :status, :created_at, :expires_at)",
                value,
            )
        return value

    def promote(self, memory_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            result = connection.execute(
                "UPDATE memory SET status='promoted', expires_at=NULL WHERE id=? RETURNING *",
                (memory_id,),
            ).fetchone()
        if result is None:
            raise ValueError("memory entry not found")
        return dict(result)

    def delete(self, memory_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM memory WHERE id=?", (memory_id,))
        return cursor.rowcount > 0

    def list(self, *, scope: str, include_candidates: bool = True, limit: int = 100) -> list[dict[str, Any]]:
        self.prune_expired()
        statuses = ("promoted", "candidate") if include_candidates else ("promoted",)
        placeholders = ",".join("?" for _ in statuses)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM memory WHERE scope=? AND status IN ({placeholders}) "
                "ORDER BY created_at DESC LIMIT ?",
                (scope, *statuses, min(limit, 500)),
            ).fetchall()
        return [dict(row) for row in rows]

    def search(self, *, scope: str, query: str, limit: int = 10) -> list[dict[str, Any]]:
        terms = [term.lower() for term in query.split() if term]
        values = self.list(scope=scope, include_candidates=False, limit=500)
        if not terms:
            return values[:limit]
        scored: list[tuple[int, dict[str, Any]]] = []
        for value in values:
            haystack = f"{value['kind']} {value['content']} {value['provenance']}".lower()
            score = sum(haystack.count(term) for term in terms)
            if score:
                scored.append((score, value))
        scored.sort(key=lambda item: (item[0], item[1]["created_at"]), reverse=True)
        return [value for _, value in scored[:limit]]

    def prune_expired(self) -> int:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM memory WHERE status='candidate' AND expires_at IS NOT NULL AND expires_at < ?",
                (datetime.now(UTC).isoformat(),),
            )
        return cursor.rowcount


class AgentsInstructions:
    def __init__(self, *, global_file: Path, max_bytes: int = 131_072):
        self.global_file = global_file.expanduser()
        self.max_bytes = max_bytes

    def load(self, workspace: Path) -> list[dict[str, str]]:
        values: list[dict[str, str]] = []
        consumed = 0
        candidates: list[tuple[str, Path]] = []
        if self.global_file.is_file():
            candidates.append(("user", self.global_file))
        root_file = workspace / "AGENTS.md"
        if root_file.is_file():
            candidates.append(("project", root_file))
        for path in sorted(workspace.rglob("AGENTS.md")):
            if path == root_file or ".git" in path.parts:
                continue
            candidates.append(("nested", path))
        for level, path in candidates:
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            if consumed + len(raw) > self.max_bytes:
                break
            consumed += len(raw)
            content = redact_text(raw.decode("utf-8", errors="replace"))
            location = str(path if level == "user" else path.relative_to(workspace))
            values.append({"level": level, "path": location, "content": content})
        return values

    @staticmethod
    def render(values: list[dict[str, str]]) -> str:
        if not values:
            return ""
        sections = ["AGENTS.md INSTRUCTIONS / 项目指令（scope follows directory hierarchy）"]
        for value in values:
            sections.append(f"\n[{value['level']}: {value['path']}]\n{value['content']}")
        return "\n".join(sections)


class MemoryTools:
    def __init__(self, working: WorkingMemory, workspace: WorkspaceMemory, scope: str):
        self.working = working
        self.workspace = workspace
        self.scope = scope
        self.tools = [
            self.working_memory_get,
            self.working_memory_set,
            self.memory_search,
            self.memory_propose,
            self.memory_promote,
            self.memory_delete,
        ]

    @tool_info("working_memory_get", "Read task-scoped working memory.", [], category=ToolCategory.MEMORY)
    def working_memory_get(self) -> str:
        return json.dumps(self.working.load(), ensure_ascii=False)

    @tool_info(
        "working_memory_set",
        "Store task-scoped scratch state that is not promoted to persistent memory.",
        [
            {"name": "key", "description": "Memory key", "type": "string", "required": True},
            {"name": "value", "description": "JSON-compatible value", "type": "object", "required": True},
        ],
        category=ToolCategory.MEMORY,
        is_read_only=False,
        is_write=True,
    )
    def working_memory_set(self, key: str, value: dict[str, Any]) -> str:
        return json.dumps(self.working.set(key, value), ensure_ascii=False)

    @tool_info(
        "memory_search",
        "Search promoted workspace memory. Candidate memories are never injected automatically.",
        [
            {"name": "query", "description": "Search text", "type": "string", "required": True},
            {"name": "limit", "description": "Maximum results", "type": "integer", "required": False},
        ],
        category=ToolCategory.MEMORY,
    )
    def memory_search(self, query: str, limit: int = 10) -> str:
        return json.dumps(
            self.workspace.search(scope=self.scope, query=query, limit=min(limit, 50)), ensure_ascii=False
        )

    @tool_info(
        "memory_propose",
        "Create an expiring memory candidate with provenance. It is not active until the user promotes it.",
        [
            {
                "name": "kind",
                "description": "user, feedback, project, or reference",
                "type": "string",
                "required": True,
            },
            {
                "name": "content",
                "description": "Memory content without secrets",
                "type": "string",
                "required": True,
            },
            {
                "name": "provenance",
                "description": "Where this fact came from",
                "type": "string",
                "required": True,
            },
            {
                "name": "confidence",
                "description": "Confidence from 0 to 1",
                "type": "number",
                "required": False,
            },
        ],
        category=ToolCategory.MEMORY,
        is_read_only=False,
        is_write=True,
    )
    def memory_propose(self, kind: str, content: str, provenance: str, confidence: float = 0.5) -> str:
        if kind not in {"user", "feedback", "project", "reference"}:
            return json.dumps({"ok": False, "error": "unsupported memory kind"})
        try:
            value = self.workspace.propose(
                scope=self.scope,
                kind=kind,  # type: ignore[arg-type]
                content=content,
                provenance=provenance,
                confidence=confidence,
            )
            return json.dumps({"ok": True, "memory": value}, ensure_ascii=False)
        except ValueError as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @tool_info(
        "memory_promote",
        "Promote one reviewed candidate into persistent workspace memory.",
        [{"name": "memory_id", "description": "Candidate identifier", "type": "string", "required": True}],
        category=ToolCategory.MEMORY,
        is_read_only=False,
        is_write=True,
        approval_policy=ApprovalPolicy.ALWAYS,
    )
    def memory_promote(self, memory_id: str) -> str:
        return json.dumps(self.workspace.promote(memory_id), ensure_ascii=False)

    @tool_info(
        "memory_delete",
        "Permanently delete one persistent or candidate memory entry.",
        [{"name": "memory_id", "description": "Memory identifier", "type": "string", "required": True}],
        category=ToolCategory.MEMORY,
        is_read_only=False,
        is_write=True,
        is_destructive=True,
        approval_policy=ApprovalPolicy.ALWAYS,
    )
    def memory_delete(self, memory_id: str) -> str:
        return json.dumps({"ok": self.workspace.delete(memory_id)})
