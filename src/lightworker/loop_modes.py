"""Loop profiles and tool orchestration through a Docker-only code bridge."""

from __future__ import annotations

import hashlib
import json

from .models import ApprovalPolicy, ToolCategory
from .tool_protocol import metadata_for, tool_info


class CodeModeTools:
    def __init__(self, sandbox, tools, store, run_id):
        self.sandbox = sandbox
        self.store, self.run_id = store, run_id
        self.functions = {t.tool_info["tool_name"]: t for t in tools}
        self.tools = [self.run_code]

    @tool_info(
        "run_code",
        "Run bounded Python tool orchestration in Docker. Use call('tool', {...}) "
        "and assign result. Each call uses existing policies and approvals. Imports, raw "
        "filesystem/network and unbounded loops are unavailable.",
        [{"name": "source", "type": "string", "required": True}],
        category=ToolCategory.SHELL,
        is_read_only=False,
        is_write=True,
        sandbox_required=True,
        approval_policy=ApprovalPolicy.ALWAYS,
    )
    def run_code(self, source):
        identity = hashlib.sha256(source.encode()).hexdigest()
        with self.store.transaction(self.run_id):
            try:
                state = self.store.read_json(self.run_id, "code-state.json")
            except FileNotFoundError:
                state = {}
            execution = state.get(identity, {"results": [], "status": "pending"})
            if execution["status"] == "completed":
                return json.dumps(execution["response"], ensure_ascii=False)
            if execution["status"] == "running":
                return json.dumps(
                    {
                        "ok": False,
                        "error": "unfinished subcall may have side effects; inspect trace before retrying",
                    }
                )
            state[identity] = execution
            self.store.write_json(self.run_id, "code-state.json", state)
        results = execution["results"]
        for _ in range(24):
            response = self.sandbox.call("ptc_eval", {"source": source, "results": results}, timeout=30)
            if not response.get("ok", True):
                return json.dumps(response, ensure_ascii=False)
            request = response.get("tool_request")
            if not request:
                value = {"ok": True, "result": response.get("result"), "calls": len(results)}
                self._checkpoint(identity, results, "completed", response=value)
                return json.dumps(value, ensure_ascii=False)
            name = request["name"]
            function = self.functions.get(name)
            if function is None or name in {"run_code", "shell_exec"}:
                return json.dumps({"ok": False, "error": f"unavailable bridged tool: {name}"})
            self._checkpoint(identity, results, "running", pending=request)
            value = function(**request.get("arguments", {}))
            try:
                value = json.loads(value) if isinstance(value, str) else value
            except ValueError:
                pass
            if isinstance(value, dict) and value.get("approval_required"):
                self._checkpoint(identity, results, "awaiting_approval", pending=request)
                return json.dumps(value, ensure_ascii=False)
            results.append({"name": name, "arguments": request.get("arguments", {}), "value": value})
            self._checkpoint(identity, results, "pending")
        return json.dumps({"ok": False, "error": "code mode subcall limit reached"})

    def _checkpoint(self, identity, results, status, **fields):
        with self.store.transaction(self.run_id):
            state = self.store.read_json(self.run_id, "code-state.json")
            state[identity] = {"results": results, "status": status, **fields}
            self.store.write_json(self.run_id, "code-state.json", state)


class WorkflowTools:
    """Fixed steps invoke the same guarded capabilities as dynamic agents."""

    def __init__(self, store, run_id, tools, presets=None):
        self.store, self.run_id = store, run_id
        self.functions = {t.tool_info["tool_name"]: t for t in tools}
        self.presets = presets or {}
        self.tools = [self.workflow_run]

    @tool_info(
        "workflow_run",
        "Run ordered capability steps with resumable results. "
        "Each step is {id, tool, arguments}; approvals and policies still apply.",
        [
            {"name": "workflow_id", "type": "string", "required": True},
            {"name": "steps", "type": "array", "required": False},
        ],
        category=ToolCategory.GOAL,
        is_read_only=False,
        is_write=True,
    )
    def workflow_run(self, workflow_id, steps=None):
        steps = steps or self.presets.get(workflow_id)
        if not steps or len(steps) > 24:
            raise ValueError("workflow requires 1 to 24 steps")
        ids = [str(step.get("id", index)) for index, step in enumerate(steps)]
        if len(ids) != len(set(ids)):
            raise ValueError("workflow step IDs must be unique")
        with self.store.transaction(self.run_id):
            try:
                state = self.store.read_json(self.run_id, "workflow-state.json")
            except FileNotFoundError:
                state = {}
            previous = state.get(workflow_id)
            if previous and previous["steps"] != steps:
                raise ValueError("workflow steps changed; use a new workflow_id")
            workflow = previous or {"steps": steps, "completed": [], "results": {}}
            state[workflow_id] = workflow
            self.store.write_json(self.run_id, "workflow-state.json", state)
        for index, step in enumerate(steps):
            step_id = ids[index]
            if step_id in workflow["completed"]:
                continue
            function = self.functions.get(step.get("tool"))
            if function is None:
                raise ValueError("unknown workflow capability")
            with self.store.transaction(self.run_id):
                state = self.store.read_json(self.run_id, "workflow-state.json")
                workflow = state[workflow_id]
                if step_id in workflow["completed"]:
                    continue
                if workflow.get("running"):
                    raise ValueError("unfinished step may have side effects; inspect trace before retrying")
                workflow["running"] = step_id
                self.store.write_json(self.run_id, "workflow-state.json", state)
            self.store.session(self.run_id).append(
                "workflow.step.started",
                {"workflow_id": workflow_id, "step_id": step_id, "tool": step["tool"]},
            )
            value = function(**step.get("arguments", {}))
            try:
                value = json.loads(value) if isinstance(value, str) else value
            except ValueError:
                pass
            if isinstance(value, dict) and (value.get("ok") is False or value.get("approval_required")):
                with self.store.transaction(self.run_id):
                    state = self.store.read_json(self.run_id, "workflow-state.json")
                    if value.get("approval_required") or metadata_for(function).is_read_only:
                        state[workflow_id]["running"] = None
                    self.store.write_json(self.run_id, "workflow-state.json", state)
                return json.dumps({"ok": False, "step_id": step_id, "result": value}, ensure_ascii=False)
            workflow["results"][step_id] = value
            workflow["completed"].append(step_id)
            workflow["running"] = None
            with self.store.transaction(self.run_id):
                state = self.store.read_json(self.run_id, "workflow-state.json")
                state[workflow_id] = workflow
                self.store.write_json(self.run_id, "workflow-state.json", state)
                self.store.session(self.run_id).append(
                    "workflow.step.completed",
                    {"workflow_id": workflow_id, "step_id": step_id, "result": value},
                )
        return json.dumps({"ok": True, "results": workflow["results"]}, ensure_ascii=False)
