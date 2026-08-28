"""Approved Docker-only command execution for coding and analysis workflows."""

from __future__ import annotations

import json

from .models import ApprovalPolicy, ToolCategory
from .policy import redact_text
from .sandbox import SandboxBackend
from .storage import RunStore
from .tool_protocol import tool_info


class ShellTools:
    def __init__(self, *, sandbox: SandboxBackend, store: RunStore, run_id: str):
        self.sandbox = sandbox
        self.store = store
        self.run_id = run_id
        self.call_count = 0
        self.tools = [self.shell_exec]

    @tool_info(
        "shell_exec",
        "Run an exact argv vector inside the isolated Docker task container. No host shell, shell "
        "interpolation, destructive Git, inline Python, or direct package installation is allowed.",
        [
            {"name": "argv", "description": "Command argument vector", "type": "array", "required": True},
            {
                "name": "timeout_seconds",
                "description": "Timeout from 1 to 3600 seconds",
                "type": "integer",
                "required": False,
            },
        ],
        category=ToolCategory.SHELL,
        is_read_only=False,
        is_write=True,
        external_side_effect=True,
        concurrency_safe=False,
        sandbox_required=True,
        approval_policy=ApprovalPolicy.ALWAYS,
        timeout_seconds=3600,
    )
    def shell_exec(self, argv: list[str], timeout_seconds: int = 300) -> str:
        self.call_count += 1
        response = self.sandbox.call(
            "shell_exec",
            {"argv": argv, "timeout": max(1, min(timeout_seconds, 3600))},
            timeout=max(1, min(timeout_seconds, 3600)),
        )
        full_output = redact_text(str(response.pop("full_output", "")))
        log_name = f"logs/shell-{self.call_count}.log"
        self.store.write_text(self.run_id, log_name, full_output)
        response["log_path"] = log_name
        return json.dumps(response, ensure_ascii=False)
