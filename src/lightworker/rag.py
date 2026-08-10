"""Small workspace-scoped RAG index using SQLite FTS5 with optional document readers."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .config import RAGConfig
from .models import ApprovalPolicy, ToolCategory
from .tool_protocol import tool_info

TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".rst", ".html", ".htm", ".json", ".csv", ".tsv"}
OPTIONAL_SUFFIXES = {".pdf", ".docx"}


class RAGIndex:
    def __init__(self, state_dir: Path, *, scope: str, config: RAGConfig):
        self.path = state_dir.expanduser().resolve() / "rag.sqlite3"
        self.scope = scope
        self.config = config
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
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS rag_documents (
                    id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    path TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    suffix TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(scope, path)
                );
                CREATE TABLE IF NOT EXISTS rag_chunks (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    path TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    FOREIGN KEY(document_id) REFERENCES rag_documents(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS rag_chunks_scope ON rag_chunks(scope, document_id, ordinal);
                CREATE TABLE IF NOT EXISTS rag_embeddings (
                    chunk_id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    model TEXT NOT NULL,
                    vector TEXT NOT NULL,
                    FOREIGN KEY(chunk_id) REFERENCES rag_chunks(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS rag_embeddings_scope ON rag_embeddings(scope, model);
                CREATE VIRTUAL TABLE IF NOT EXISTS rag_fts USING fts5(
                    content,
                    chunk_id UNINDEXED,
                    scope UNINDEXED,
                    path UNINDEXED,
                    tokenize='unicode61'
                );
                """
            )

    def ingest(self, workspace: Path, relative_paths: list[str]) -> dict[str, Any]:
        values: list[dict[str, Any]] = []
        for relative in relative_paths:
            try:
                path = self._safe_workspace_file(workspace, relative)
                values.append(self._ingest_file(path, path.relative_to(workspace.resolve()).as_posix()))
            except (OSError, ValueError) as exc:
                values.append({"path": relative, "ok": False, "error": str(exc)})
        return {
            "ok": all(item.get("ok") for item in values),
            "documents": values,
            "indexed": sum(bool(item.get("ok")) for item in values),
        }

    def _ingest_file(self, path: Path, relative: str) -> dict[str, Any]:
        suffix = path.suffix.lower()
        if suffix not in TEXT_SUFFIXES | OPTIONAL_SUFFIXES:
            raise ValueError(f"unsupported document type: {suffix or '<none>'}")
        raw = path.read_bytes()
        if len(raw) > 10_485_760:
            raise ValueError("document exceeds 10 MiB limit")
        digest = hashlib.sha256(raw).hexdigest()
        with self._lock, self._connect() as connection:
            current = connection.execute(
                "SELECT id, content_hash FROM rag_documents WHERE scope=? AND path=?",
                (self.scope, relative),
            ).fetchone()
            if current and current["content_hash"] == digest:
                count = connection.execute(
                    "SELECT count(*) FROM rag_chunks WHERE document_id=?",
                    (current["id"],),
                ).fetchone()[0]
                embedded = connection.execute(
                    """
                    SELECT count(*) FROM rag_embeddings e JOIN rag_chunks c ON c.id=e.chunk_id
                    WHERE c.document_id=? AND e.model=?
                    """,
                    (current["id"], self.config.embedding_model),
                ).fetchone()[0]
                if not self.config.embeddings_enabled or embedded == count:
                    return {
                        "ok": True,
                        "path": relative,
                        "unchanged": True,
                        "chunks": count,
                        "embeddings": embedded,
                    }
        text = self._extract(path, raw)
        chunks = self._chunk(text)
        embedding_vectors: list[list[float]] = []
        embedding_error: str | None = None
        if self.config.embeddings_enabled and chunks:
            try:
                embedding_vectors = self._embed(chunks)
            except Exception as exc:  # noqa: BLE001 - provider failures must fall back to FTS5
                embedding_error = str(exc)
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT id, content_hash FROM rag_documents WHERE scope=? AND path=?",
                (self.scope, relative),
            ).fetchone()
            document_id = str(existing["id"]) if existing else uuid4().hex
            if existing:
                old_ids = [
                    row[0]
                    for row in connection.execute(
                        "SELECT id FROM rag_chunks WHERE document_id=?",
                        (document_id,),
                    ).fetchall()
                ]
                for chunk_id in old_ids:
                    connection.execute("DELETE FROM rag_fts WHERE chunk_id=?", (chunk_id,))
                    connection.execute("DELETE FROM rag_embeddings WHERE chunk_id=?", (chunk_id,))
                connection.execute("DELETE FROM rag_chunks WHERE document_id=?", (document_id,))
                connection.execute(
                    "UPDATE rag_documents SET content_hash=?, suffix=?, size=?, updated_at=? WHERE id=?",
                    (digest, suffix, len(raw), now, document_id),
                )
            else:
                connection.execute(
                    "INSERT INTO rag_documents VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (document_id, self.scope, relative, digest, suffix, len(raw), now),
                )
            for ordinal, chunk in enumerate(chunks, start=1):
                chunk_id = uuid4().hex
                connection.execute(
                    "INSERT INTO rag_chunks VALUES (?, ?, ?, ?, ?, ?)",
                    (chunk_id, document_id, self.scope, relative, ordinal, chunk),
                )
                connection.execute(
                    "INSERT INTO rag_fts(content, chunk_id, scope, path) VALUES (?, ?, ?, ?)",
                    (chunk, chunk_id, self.scope, relative),
                )
                if ordinal <= len(embedding_vectors):
                    connection.execute(
                        "INSERT INTO rag_embeddings VALUES (?, ?, ?, ?)",
                        (
                            chunk_id,
                            self.scope,
                            self.config.embedding_model,
                            json.dumps(embedding_vectors[ordinal - 1], separators=(",", ":")),
                        ),
                    )
        return {
            "ok": True,
            "path": relative,
            "unchanged": False,
            "chunks": len(chunks),
            "embeddings": len(embedding_vectors),
            "embedding_error": embedding_error,
        }

    def search(self, query: str, limit: int | None = None) -> list[dict[str, Any]]:
        maximum = min(limit or self.config.max_results, 50)
        candidate_limit = min(max(maximum * 4, 20), 200)
        fts_query = self._fts_query(query)
        rows: list[sqlite3.Row] = []
        if fts_query:
            with self._connect() as connection:
                try:
                    rows = connection.execute(
                        """
                        SELECT c.id, c.path, c.ordinal, c.content, bm25(rag_fts) AS rank
                        FROM rag_fts JOIN rag_chunks c ON c.id = rag_fts.chunk_id
                        WHERE rag_fts MATCH ? AND rag_fts.scope = ?
                        ORDER BY rank LIMIT ?
                        """,
                        (fts_query, self.scope, candidate_limit),
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = []
        if not rows:
            terms = [item for item in re.split(r"\s+", query.strip()) if item]
            with self._connect() as connection:
                candidates = connection.execute(
                    "SELECT id, path, ordinal, content, 0.0 AS rank FROM rag_chunks WHERE scope=? LIMIT 1000",
                    (self.scope,),
                ).fetchall()
            scored = [
                (sum(str(row["content"]).lower().count(term.lower()) for term in terms), row)
                for row in candidates
            ]
            rows = [row for score, row in sorted(scored, key=lambda item: item[0], reverse=True) if score][
                :candidate_limit
            ]
        lexical = [self._result(row, score=-float(row["rank"]), retrieval="fts5") for row in rows]
        if not self.config.embeddings_enabled:
            return lexical[:maximum]
        try:
            semantic = self._semantic_search(query, candidate_limit)
        except Exception:  # noqa: BLE001 - retrieval must remain available when embeddings fail
            return lexical[:maximum]
        if not semantic:
            return lexical[:maximum]
        return self._reciprocal_rank_fusion(lexical, semantic, maximum)

    @staticmethod
    def _result(row: sqlite3.Row, *, score: float, retrieval: str) -> dict[str, Any]:
        return {
            "chunk_id": row["id"],
            "path": row["path"],
            "chunk": row["ordinal"],
            "content": row["content"],
            "score": score,
            "retrieval": retrieval,
            "citation": f"[{row['path']}#chunk-{row['ordinal']}]",
        }

    def _semantic_search(self, query: str, limit: int) -> list[dict[str, Any]]:
        query_vector = self._embed([query])[0]
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT c.id, c.path, c.ordinal, c.content, e.vector
                FROM rag_embeddings e JOIN rag_chunks c ON c.id=e.chunk_id
                WHERE e.scope=? AND e.model=?
                ORDER BY c.rowid DESC LIMIT 10000
                """,
                (self.scope, self.config.embedding_model),
            ).fetchall()
        scored: list[tuple[float, sqlite3.Row]] = []
        for row in rows:
            try:
                vector = json.loads(row["vector"])
                similarity = self._cosine(query_vector, vector)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            scored.append((similarity, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [self._result(row, score=score, retrieval="embedding") for score, row in scored[:limit]]

    @staticmethod
    def _reciprocal_rank_fusion(
        lexical: list[dict[str, Any]],
        semantic: list[dict[str, Any]],
        limit: int,
    ) -> list[dict[str, Any]]:
        combined: dict[str, dict[str, Any]] = {}
        scores: dict[str, float] = {}
        sources: dict[str, set[str]] = {}
        for result_set in (lexical, semantic):
            for rank, item in enumerate(result_set, start=1):
                chunk_id = str(item["chunk_id"])
                combined.setdefault(chunk_id, item)
                scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (60 + rank)
                sources.setdefault(chunk_id, set()).add(str(item["retrieval"]))
        ordered = sorted(combined, key=lambda chunk_id: scores[chunk_id], reverse=True)[:limit]
        results: list[dict[str, Any]] = []
        for chunk_id in ordered:
            item = dict(combined[chunk_id])
            item["score"] = scores[chunk_id]
            item["retrieval"] = "+".join(sorted(sources[chunk_id]))
            results.append(item)
        return results

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        if not left or len(left) != len(right):
            raise ValueError("embedding dimensions do not match")
        left_norm = math.sqrt(math.fsum(value * value for value in left))
        right_norm = math.sqrt(math.fsum(value * value for value in right))
        if not left_norm or not right_norm:
            return 0.0
        return math.fsum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)

    def _embed(self, texts: list[str]) -> list[list[float]]:
        api_key = os.getenv(self.config.embedding_api_key_env)
        if not api_key:
            raise ValueError(f"missing embedding API key env: {self.config.embedding_api_key_env}")
        from openai import OpenAI

        client_kwargs: dict[str, Any] = {"api_key": api_key}
        if self.config.embedding_base_url:
            client_kwargs["base_url"] = self.config.embedding_base_url
        client = OpenAI(**client_kwargs)
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.config.embedding_batch_size):
            batch = texts[start : start + self.config.embedding_batch_size]
            response = client.embeddings.create(model=self.config.embedding_model, input=batch)
            data = sorted(response.data, key=lambda item: item.index)
            if len(data) != len(batch):
                raise RuntimeError("embedding provider returned an unexpected vector count")
            for item in data:
                vector = [float(value) for value in item.embedding]
                if not vector or not all(math.isfinite(value) for value in vector):
                    raise RuntimeError("embedding provider returned an invalid vector")
                vectors.append(vector)
        return vectors

    def read_chunk(self, chunk_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, path, ordinal, content FROM rag_chunks WHERE id=? AND scope=?",
                (chunk_id, self.scope),
            ).fetchone()
        if row is None:
            raise ValueError("RAG chunk not found")
        return {
            "chunk_id": row["id"],
            "path": row["path"],
            "chunk": row["ordinal"],
            "content": row["content"],
            "citation": f"[{row['path']}#chunk-{row['ordinal']}]",
        }

    def list_documents(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT d.*, count(c.id) AS chunks FROM rag_documents d
                LEFT JOIN rag_chunks c ON c.document_id=d.id
                WHERE d.scope=? GROUP BY d.id ORDER BY d.path
                """,
                (self.scope,),
            ).fetchall()
        return [dict(row) for row in rows]

    def remove(self, relative: str) -> bool:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT id FROM rag_documents WHERE scope=? AND path=?",
                (self.scope, relative),
            ).fetchone()
            if row is None:
                return False
            chunk_ids = [
                item[0]
                for item in connection.execute(
                    "SELECT id FROM rag_chunks WHERE document_id=?",
                    (row["id"],),
                ).fetchall()
            ]
            for chunk_id in chunk_ids:
                connection.execute("DELETE FROM rag_fts WHERE chunk_id=?", (chunk_id,))
                connection.execute("DELETE FROM rag_embeddings WHERE chunk_id=?", (chunk_id,))
            connection.execute("DELETE FROM rag_chunks WHERE document_id=?", (row["id"],))
            connection.execute("DELETE FROM rag_documents WHERE id=?", (row["id"],))
        return True

    def _chunk(self, text: str) -> list[str]:
        text = text.strip()
        if not text:
            return []
        size = self.config.chunk_tokens * 4
        overlap = min(self.config.chunk_overlap_tokens * 4, max(size - 1, 0))
        chunks: list[str] = []
        cursor = 0
        while cursor < len(text):
            end = min(cursor + size, len(text))
            if end < len(text):
                boundary = max(text.rfind("\n\n", cursor, end), text.rfind("。", cursor, end))
                if boundary > cursor + size // 2:
                    end = boundary + 1
            chunk = text[cursor:end].strip()
            if chunk:
                chunks.append(chunk)
            if end >= len(text):
                break
            cursor = max(end - overlap, cursor + 1)
        return chunks

    @staticmethod
    def _extract(path: Path, raw: bytes) -> str:
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            try:
                from pypdf import PdfReader
            except ImportError as exc:
                raise ValueError("PDF ingestion requires optional package pypdf") from exc
            return "\n\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(raw)).pages)
        if suffix == ".docx":
            try:
                from docx import Document
            except ImportError as exc:
                raise ValueError("DOCX ingestion requires optional package python-docx") from exc
            document = Document(io.BytesIO(raw))
            return "\n".join(paragraph.text for paragraph in document.paragraphs)
        text = raw.decode("utf-8", errors="replace")
        if suffix in {".html", ".htm"}:
            text = re.sub(r"<(script|style)\b.*?</\1>", " ", text, flags=re.IGNORECASE | re.DOTALL)
            text = re.sub(r"<[^>]+>", " ", text)
        elif suffix == ".json":
            try:
                text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
            except json.JSONDecodeError:
                pass
        elif suffix in {".csv", ".tsv"}:
            delimiter = "\t" if suffix == ".tsv" else ","
            rows = csv.reader(io.StringIO(text), delimiter=delimiter)
            text = "\n".join(" | ".join(row) for row in rows)
        return " ".join(text.split()) if suffix in {".html", ".htm"} else text

    @staticmethod
    def _fts_query(query: str) -> str:
        terms = re.findall(r"[\w\u3400-\u9fff]+", query, flags=re.UNICODE)
        return " OR ".join(f'"{term.replace(chr(34), "")}"' for term in terms[:20])

    @staticmethod
    def _safe_workspace_file(workspace: Path, relative: str) -> Path:
        if (
            not relative
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or ".git" in Path(relative).parts
        ):
            raise ValueError("unsafe workspace document path")
        root = workspace.resolve()
        path = (root / relative).resolve(strict=True)
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError("document path escapes workspace") from exc
        if not path.is_file():
            raise ValueError("document is not a regular file")
        return path


class RAGTools:
    def __init__(self, index: RAGIndex, workspace: Path):
        self.index = index
        self.workspace = workspace
        self.tools = [self.rag_ingest, self.rag_list, self.rag_search, self.rag_read, self.rag_remove]

    @tool_info(
        "rag_ingest",
        "Incrementally index workspace documents into the scoped SQLite FTS5 knowledge base.",
        [
            {
                "name": "paths",
                "description": "Workspace-relative document paths",
                "type": "array",
                "required": True,
            }
        ],
        category=ToolCategory.RAG,
        is_read_only=False,
        is_write=True,
        concurrency_safe=False,
    )
    def rag_ingest(self, paths: list[str]) -> str:
        return json.dumps(self.index.ingest(self.workspace, paths[:100]), ensure_ascii=False)

    @tool_info("rag_list", "List documents in the current scoped RAG index.", [], category=ToolCategory.RAG)
    def rag_list(self) -> str:
        return json.dumps(self.index.list_documents(), ensure_ascii=False)

    @tool_info(
        "rag_search",
        "Search the scoped SQLite FTS5 index and return source citations.",
        [
            {"name": "query", "description": "Knowledge query", "type": "string", "required": True},
            {"name": "limit", "description": "Maximum results", "type": "integer", "required": False},
        ],
        category=ToolCategory.RAG,
    )
    def rag_search(self, query: str, limit: int = 0) -> str:
        return json.dumps(self.index.search(query, limit or None), ensure_ascii=False)

    @tool_info(
        "rag_read",
        "Read one full RAG chunk by identifier.",
        [{"name": "chunk_id", "description": "Chunk identifier", "type": "string", "required": True}],
        category=ToolCategory.RAG,
    )
    def rag_read(self, chunk_id: str) -> str:
        try:
            return json.dumps(self.index.read_chunk(chunk_id), ensure_ascii=False)
        except ValueError as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @tool_info(
        "rag_remove",
        "Remove one document and all of its chunks from the scoped RAG index.",
        [
            {
                "name": "path",
                "description": "Indexed workspace-relative path",
                "type": "string",
                "required": True,
            }
        ],
        category=ToolCategory.RAG,
        is_read_only=False,
        is_write=True,
        is_destructive=True,
        approval_policy=ApprovalPolicy.ALWAYS,
    )
    def rag_remove(self, path: str) -> str:
        return json.dumps({"ok": self.index.remove(path)})
