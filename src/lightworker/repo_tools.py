"""LightAgent-compatible tool functions backed by a SandboxBackend."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from .models import InstalledRequirement, RunRecord, VerificationCommand, VerificationResult
from .policy import redact_text
from .sandbox import SandboxBackend, SandboxError
from .storage import RunStore


def tool_info(
    name: str, description: str, params: list[dict[str, Any]]
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
        function.tool_info = {  # type: ignore[attr-defined]
            "tool_name": name,
            "tool_title": name.replace("_", " ").title(),
            "tool_description": description,
            "tool_params": params,
        }
        return function

    return decorate


class RepositoryTools:
    def __init__(
        self,
        *,
        sandbox: SandboxBackend,
        store: RunStore,
        run_id: str,
        verification: list[VerificationCommand],
        on_install: Callable[[InstalledRequirement], None] | None = None,
    ):
        self.sandbox = sandbox
        self.store = store
        self.run_id = run_id
        self.verification = verification
        self.on_install = on_install
        self.verification_round = 0
        self.read_tools = [self.list_files, self.read_file, self.search_text, self.git_status, self.git_diff]
        self.write_tools = [*self.read_tools, self.apply_patch, self.pip_install]
        self.review_tools = [self.read_file, self.git_status, self.git_diff]

    @tool_info(
        "list_files",
        "List repository files below a safe relative directory. Never accepts absolute paths or .git paths.",
        [
            {
                "name": "path",
                "description": "Relative directory, default .",
                "type": "string",
                "required": False,
            },
            {"name": "limit", "description": "Maximum files", "type": "integer", "required": False},
        ],
    )
    def list_files(self, path: str = ".", limit: int = 500) -> str:
        return self._model_result(self.sandbox.call("list_files", {"path": path, "limit": limit}))

    @tool_info(
        "read_file",
        "Read a UTF-8 repository file with line numbers and strict size/path bounds.",
        [
            {
                "name": "path",
                "description": "Safe repository-relative path",
                "type": "string",
                "required": True,
            },
            {
                "name": "start_line",
                "description": "First line (1-based)",
                "type": "integer",
                "required": False,
            },
            {
                "name": "end_line",
                "description": "Last line (inclusive)",
                "type": "integer",
                "required": False,
            },
        ],
    )
    def read_file(self, path: str, start_line: int = 1, end_line: int = 0) -> str:
        return self._model_result(
            self.sandbox.call(
                "read_file",
                {"path": path, "start_line": start_line, "end_line": end_line},
            )
        )

    @tool_info(
        "search_text",
        "Search repository text using ripgrep. Fixed-string, case-insensitive search is the safe default.",
        [
            {"name": "pattern", "description": "Text or regex to search", "type": "string", "required": True},
            {"name": "path", "description": "Relative search root", "type": "string", "required": False},
            {
                "name": "fixed_strings",
                "description": "Treat pattern literally",
                "type": "boolean",
                "required": False,
            },
            {
                "name": "case_sensitive",
                "description": "Use case-sensitive matching",
                "type": "boolean",
                "required": False,
            },
        ],
    )
    def search_text(
        self,
        pattern: str,
        path: str = ".",
        fixed_strings: bool = True,
        case_sensitive: bool = False,
    ) -> str:
        return self._model_result(
            self.sandbox.call(
                "search_text",
                {
                    "pattern": pattern,
                    "path": path,
                    "fixed_strings": fixed_strings,
                    "case_sensitive": case_sensitive,
                },
            )
        )

    @tool_info("git_status", "Return the isolated workspace Git status.", [])
    def git_status(self) -> str:
        return self._model_result(self.sandbox.call("git_status", {}))

    @tool_info("git_diff", "Return the current isolated workspace diff, capped for model context.", [])
    def git_diff(self) -> str:
        return self._model_result(self.sandbox.call("git_diff", {"full": False}))

    @tool_info(
        "apply_patch",
        "Apply a unified Git patch after fail-closed path, protected-file, deletion, "
        "size, and syntax checks.",
        [
            {
                "name": "patch",
                "description": "Unified diff including diff --git headers",
                "type": "string",
                "required": True,
            }
        ],
    )
    def apply_patch(self, patch: str) -> str:
        return self._model_result(self.sandbox.call("apply_patch", {"patch": patch}, timeout=120))

    @tool_info(
        "pip_install",
        "Install PyPI packages inside the task container. "
        "The network is attached only for this audited call.",
        [
            {
                "name": "requirements",
                "description": (
                    "Package names with optional version constraints; URLs, paths and pip flags are forbidden"
                ),
                "type": "array",
                "required": True,
            }
        ],
    )
    def pip_install(self, requirements: list[str]) -> str:
        response = self.sandbox.install_requirements(requirements)
        log_name = f"logs/pip-{len(self.installed_requirements()) + 1}.log"
        self.store.write_text(self.run_id, log_name, redact_text(str(response.pop("full_output", ""))))
        installed = InstalledRequirement(
            requested=[str(item) for item in response.get("requirements") or requirements],
            frozen=[str(item) for item in response.get("frozen") or []],
        )
        if self.on_install:
            self.on_install(installed)
        response["log_path"] = log_name
        return self._model_result(response)

    def run_verification(self) -> list[VerificationResult]:
        self.verification_round += 1
        results: list[VerificationResult] = []
        for command in self.verification:
            response = self.sandbox.call(
                "run_command",
                {"argv": command.argv, "timeout": command.timeout_seconds},
                timeout=command.timeout_seconds,
            )
            full_output = redact_text(str(response.pop("full_output", "")))
            log_name = f"logs/verify-{self.verification_round}-{command.name}.log"
            self.store.write_text(self.run_id, log_name, full_output)
            result = VerificationResult(
                name=command.name,
                kind=command.kind,
                argv=command.argv,
                exit_code=int(response.get("exit_code", 1)),
                passed=int(response.get("exit_code", 1)) == 0 and not bool(response.get("timed_out")),
                timed_out=bool(response.get("timed_out")),
                duration_ms=float(response.get("duration_ms") or 0),
                output_excerpt=redact_text(str(response.get("output") or "")),
                log_path=log_name,
                required=command.required,
            )
            results.append(result)
        self.store.write_json(
            self.run_id,
            f"verification-{self.verification_round}.json",
            [item.model_dump(mode="json") for item in results],
        )
        return results

    def full_diff(self) -> str:
        response = self.sandbox.call("git_diff", {"full": True}, timeout=120)
        if not response.get("ok"):
            raise SandboxError(str(response.get("error") or "failed to read diff"))
        return str(response.get("diff") or "")

    def installed_requirements(self) -> list[InstalledRequirement]:
        try:
            record = self.store.load(self.run_id)
        except (FileNotFoundError, ValueError):
            return []
        return record.installed_requirements

    @staticmethod
    def _model_result(response: dict[str, Any]) -> str:
        if not response.get("ok"):
            return json.dumps(
                {"ok": False, "error": redact_text(str(response.get("error") or "sandbox operation failed"))},
                ensure_ascii=False,
            )
        safe = {key: value for key, value in response.items() if key != "full_output"}
        return json.dumps(safe, ensure_ascii=False)


def update_install_record(store: RunStore, run_id: str, installed: InstalledRequirement) -> None:
    record: RunRecord = store.load(run_id)
    record.installed_requirements.append(installed)
    store.save(record)
