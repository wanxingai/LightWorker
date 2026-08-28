"""Pure-Python read-only workspace fallback; it never executes a host command."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any

from ..config import ResourceLimits
from .base import SandboxBackend, SandboxError


class ReadOnlyWorkspaceSandbox(SandboxBackend):
    """Keep non-coding tasks usable when Docker is unavailable without weakening shell isolation."""

    supports_write = False
    supports_shell = False

    def __init__(
        self,
        *,
        run_id: str,
        workspace: Path,
        image: str,
        limits: ResourceLimits,
        protected_patterns: list[str],
        pip_index_url: str,
        max_pip_requirements: int,
        sensitive_read_patterns: list[str] | None = None,
        **kwargs: Any,
    ):
        del run_id, image, protected_patterns, pip_index_url, max_pip_requirements, kwargs
        self.workspace = workspace.resolve()
        self.limits = limits
        self.sensitive_read_patterns = list(sensitive_read_patterns or [])
        self.started = False

    def start(self) -> None:
        if not self.workspace.is_dir():
            raise SandboxError("workspace is missing")
        self.started = True

    def stop(self) -> None:
        self.started = False

    def call(
        self,
        action: str,
        params: dict[str, Any],
        *,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        del timeout
        if not self.started:
            raise SandboxError("read-only workspace has not been started")
        try:
            if action == "health":
                return {"ok": True, "workspace": str(self.workspace), "mode": "read-only-no-host-shell"}
            if action == "list_files":
                return {"ok": True, **self._list_files(params)}
            if action == "read_file":
                return {"ok": True, **self._read_file(params)}
            if action == "search_text":
                return {"ok": True, **self._search_text(params)}
            if action == "git_status":
                return {"ok": True, "status": "", "degraded": True}
            if action == "git_diff":
                return {"ok": True, "diff": "", "degraded": True}
            raise SandboxError(f"{action} requires Docker; host command and workspace writes remain disabled")
        except (OSError, ValueError, SandboxError) as exc:
            return {"ok": False, "error": str(exc), "error_type": type(exc).__name__}

    def install_requirements(self, requirements: list[str], *, timeout: int = 900) -> dict[str, Any]:
        del requirements, timeout
        return {"ok": False, "error": "dependency installation requires Docker"}

    @classmethod
    def image_exists(cls, image: str) -> bool:
        del image
        return False

    @classmethod
    def build_image(cls, image: str, dockerfile: Path, context: Path) -> None:
        del image, dockerfile, context
        raise SandboxError("read-only fallback does not build images")

    def _list_files(self, params: dict[str, Any]) -> dict[str, Any]:
        root = self._safe_path(str(params.get("path") or "."), must_exist=True)
        if not root.is_dir():
            raise ValueError("list_files path must be a directory")
        maximum = min(int(params.get("limit") or 500), 2000)
        values: list[str] = []
        for path in sorted(root.rglob("*")):
            if not path.is_file() or ".git" in path.parts:
                continue
            relative = path.relative_to(self.workspace).as_posix()
            if self._sensitive(relative):
                continue
            values.append(relative)
            if len(values) >= maximum:
                return {"files": values, "truncated": True, "degraded": True}
        return {"files": values, "truncated": False, "degraded": True}

    def _read_file(self, params: dict[str, Any]) -> dict[str, Any]:
        relative = str(params.get("path") or "")
        if self._sensitive(relative):
            raise ValueError(f"sensitive file cannot be read: {relative}")
        path = self._safe_path(relative, must_exist=True)
        if not path.is_file():
            raise ValueError("read_file path must be a regular file")
        raw = path.read_bytes()
        if len(raw) > self.limits.max_read_bytes:
            raise ValueError("file exceeds configured read limit")
        if b"\0" in raw:
            raise ValueError("binary files cannot be read")
        lines = raw.decode("utf-8", errors="replace").splitlines()
        start = max(int(params.get("start_line") or 1), 1)
        end = int(params.get("end_line") or 0)
        selected = lines[start - 1 : end if end > 0 else None]
        content = "\n".join(f"{index}: {line}" for index, line in enumerate(selected, start=start))
        return {
            "path": relative,
            "content": self._cap(content),
            "size": len(raw),
            "line_count": len(lines),
            "degraded": True,
        }

    def _search_text(self, params: dict[str, Any]) -> dict[str, Any]:
        pattern = str(params.get("pattern") or "")
        if not pattern:
            raise ValueError("search pattern is required")
        root = self._safe_path(str(params.get("path") or "."), must_exist=True)
        fixed = bool(params.get("fixed_strings", True))
        case_sensitive = bool(params.get("case_sensitive", False))
        flags = 0 if case_sensitive else re.IGNORECASE
        expression = re.compile(re.escape(pattern) if fixed else pattern, flags)
        matches: list[str] = []
        for path in sorted(root.rglob("*")):
            if not path.is_file() or ".git" in path.parts:
                continue
            relative = path.relative_to(self.workspace).as_posix()
            if self._sensitive(relative) or path.stat().st_size > self.limits.max_read_bytes:
                continue
            raw = path.read_bytes()
            if b"\0" in raw:
                continue
            for line_number, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), start=1):
                if expression.search(line):
                    matches.append(f"{relative}:{line_number}:{line}")
                    if len("\n".join(matches).encode()) >= self.limits.max_tool_output_bytes:
                        return {
                            "matches": self._cap("\n".join(matches)),
                            "found": True,
                            "truncated": True,
                            "degraded": True,
                        }
        return {
            "matches": self._cap("\n".join(matches)),
            "found": bool(matches),
            "degraded": True,
        }

    def _safe_path(self, relative: str, *, must_exist: bool) -> Path:
        path = Path(relative)
        if not relative or path.is_absolute() or ".." in path.parts or ".git" in path.parts:
            raise ValueError("unsafe workspace path")
        candidate = (self.workspace / path).resolve(strict=must_exist)
        try:
            candidate.relative_to(self.workspace)
        except ValueError as exc:
            raise ValueError("path escapes workspace") from exc
        return candidate

    def _sensitive(self, relative: str) -> bool:
        return any(
            fnmatch.fnmatchcase(relative, pattern)
            or fnmatch.fnmatchcase(relative, pattern.removesuffix("/**"))
            for pattern in self.sensitive_read_patterns
        )

    def _cap(self, value: str) -> str:
        raw = value.encode("utf-8")
        if len(raw) <= self.limits.max_tool_output_bytes:
            return value
        return raw[: self.limits.max_tool_output_bytes].decode("utf-8", errors="ignore") + "\n…[truncated]"
