"""Docker terminal processes exposed as durable Jobs."""

from __future__ import annotations

import hashlib
import json
import time

from .jobs import JobInterrupted
from .models import ApprovalPolicy, ToolCategory
from .tool_protocol import tool_info


class TerminalTools:
    def __init__(self, sandbox, jobs):
        self.sandbox, self.jobs = sandbox, jobs
        self.jobs.register_handler("terminal", self._drive)
        self.tools = [self.terminal_start, self.terminal_send]

    @tool_info(
        "terminal_start",
        "Start an approved argv process in Docker as a background Job. "
        "Use job_output for incremental output and job_control for pause/cancel.",
        [{"name": "argv", "type": "array", "required": True}],
        category=ToolCategory.SHELL,
        is_read_only=False,
        is_write=True,
        sandbox_required=True,
        approval_policy=ApprovalPolicy.ALWAYS,
    )
    def terminal_start(self, argv):
        job = self.jobs.create("terminal", {"argv": argv})
        self.jobs.dispatch(job["job_id"])
        return json.dumps({"ok": True, "job_id": job["job_id"]})

    def _drive(self, job, context):
        terminal_id = self._identity(job["job_id"], context.epoch)
        response = self.sandbox.call(
            "terminal_start", {"argv": job["payload"]["argv"], "terminal_id": terminal_id}
        )
        if not response.get("ok", True):
            raise RuntimeError(response.get("error", "terminal startup failed"))
        cursor = 0
        try:
            while True:
                context.checkpoint()
                result = self.sandbox.call("terminal_poll", {"terminal_id": terminal_id, "after": cursor})
                cursor = result.get("cursor", cursor)
                if result.get("output"):
                    context.output(result["output"])
                if not result.get("running") and result.get("drained", True):
                    if result.get("exit_code", 0):
                        raise RuntimeError(f"terminal exited with code {result['exit_code']}")
                    return result
                time.sleep(0.2)
        except JobInterrupted:
            self.sandbox.call("terminal_stop", {"terminal_id": terminal_id})
            raise

    @staticmethod
    def _identity(job_id, epoch):
        return hashlib.sha256(f"{job_id}:{epoch}".encode()).hexdigest()[:32]

    @tool_info(
        "terminal_send",
        "Write input to an owned Docker terminal process.",
        [
            {"name": "job_id", "type": "string", "required": True},
            {"name": "text", "type": "string", "required": True},
        ],
        category=ToolCategory.SHELL,
        is_read_only=False,
        is_write=True,
        sandbox_required=True,
        approval_policy=ApprovalPolicy.ALWAYS,
    )
    def terminal_send(self, job_id, text):
        job = self.jobs.get(job_id)
        if job["kind"] != "terminal" or job["status"] != "running":
            raise ValueError("job is not an active owned terminal")
        return json.dumps(
            self.sandbox.call(
                "terminal_send", {"terminal_id": self._identity(job_id, job["lease_epoch"]), "text": text}
            )
        )
