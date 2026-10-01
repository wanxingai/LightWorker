"""Durable source evidence and content-addressed artifact records."""

from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from .policy import redact_value


class EvidenceStore:
    def __init__(self, store, run_id):
        self.store, self.run_id = store, run_id

    def list(self):
        try:
            return self.store.read_json(self.run_id, "evidence.json")["sources"]
        except FileNotFoundError:
            return []

    def capture(self, tool: str, output: Any):
        if not any(marker in tool for marker in ("web", "http", "browser", "rag")):
            return []
        if isinstance(output, str):
            try:
                output = json.loads(output)
            except ValueError:
                return []
        if isinstance(output, dict) and output.get("ok") is False:
            return []
        candidates = []

        def visit(value):
            if isinstance(value, list):
                for child in value:
                    visit(child)
            elif isinstance(value, dict):
                url = value.get("url") or value.get("source_url") or value.get("link")
                citation = value.get("citation") or value.get("citation_id")
                content = next(
                    (
                        value.get(k)
                        for k in ("content", "body", "text", "excerpt", "snippet", "description")
                        if value.get(k)
                    ),
                    "",
                )
                if url or citation:
                    if url:
                        parsed = urlsplit(str(url))
                        if parsed.scheme not in {"https", "http"} or parsed.username or parsed.password:
                            return
                    candidates.append(
                        {
                            "url": str(url or ""),
                            "citation": str(citation or ""),
                            "title": str(value.get("title") or value.get("path") or url or citation),
                            "content": str(content),
                            "source_type": "rag" if "rag" in tool else "web",
                            "evidence_kind": "search_lead" if "search" in tool else "retrieved",
                        }
                    )
                for key in ("results", "sources", "items", "documents", "citations"):
                    if key in value:
                        visit(value[key])

        visit(output)
        added = []
        with self.store.transaction(self.run_id):
            sources = self.list()
            for candidate in candidates:
                candidate = redact_value(candidate)
                digest = hashlib.sha256(candidate["content"].encode()).hexdigest()
                identity = f"{candidate['url']}:{candidate['citation']}:{digest}"
                if any(item["identity"] == identity for item in sources):
                    continue
                candidate.update(
                    id=len(sources) + 1,
                    evidence_id=f"E{len(sources) + 1}",
                    content_hash=digest,
                    identity=identity,
                    tool=tool,
                    observed_at=datetime.now(UTC).isoformat(),
                    excerpt=html.unescape(re.sub(r"<[^>]+>", " ", candidate["content"]))[:2000],
                    site=urlsplit(candidate["url"]).netloc,
                )
                sources.append(candidate)
                added.append(candidate)
                self.store.session(self.run_id).append("evidence.added", {"evidence": candidate})
            if added:
                self.store.write_json(self.run_id, "evidence.json", {"sources": sources})
        return added


class ArtifactRegistry:
    def __init__(self, store, run_id):
        self.store, self.run_id = store, run_id

    def list(self):
        try:
            return self.store.read_json(self.run_id, "artifacts.json")["artifacts"]
        except FileNotFoundError:
            return []

    def register(self, name: str, *, kind="file", evidence_ids=None):
        path = self.store.artifact_path(self.run_id, name)
        resolved = path.resolve()
        if not resolved.is_relative_to(self.store.run_dir(self.run_id).resolve()) or not path.is_file():
            raise ValueError("artifact must be a file inside the task directory")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.store.transaction(self.run_id):
            items = self.list()
            item = next((i for i in items if i["path"] == name), None)
            if item and item["content_hash"] == digest:
                return item
            previous_hash = item["content_hash"] if item else None
            value = {
                "artifact_id": hashlib.sha256(name.encode()).hexdigest()[:16],
                "path": name,
                "kind": kind,
                "content_hash": digest,
                "previous_hash": previous_hash,
                "size": path.stat().st_size,
                "evidence_ids": evidence_ids or [],
                "created_at": datetime.now(UTC).isoformat(),
            }
            items = [i for i in items if i["path"] != name] + [value]
            self.store.write_json(self.run_id, "artifacts.json", {"artifacts": items})
            self.store.session(self.run_id).append("artifact.created", {"artifact": value})
            return value
