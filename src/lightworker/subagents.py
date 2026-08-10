"""Supervisor-managed read-only subagents with bounded parallel fan-out."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from .models import ToolCategory
from .policy import redact_text, redact_value
from .resources import limit_agent_model_calls
from .storage import RunStore
from .tool_protocol import EventLog, metadata_for, tool_info

ROLES = {"explore", "research", "code", "test", "review", "rag"}


class SubagentManager:
    def __init__(
        self,
        *,
        agent_factory: Any,
        tools: list[Any],
        store: RunStore,
        run_id: str,
        events: EventLog,
        max_children: int = 4,
        max_depth: int = 2,
        max_total_agents: int = 8,
        depth: int = 0,
        parent_agent_id: str = "supervisor",
        model_semaphore: threading.Semaphore | None = None,
        tree_lock: threading.RLock | None = None,
    ):
        self.agent_factory = agent_factory
        self.read_tools = [tool for tool in tools if metadata_for(tool).is_read_only]
        self.store = store
        self.run_id = run_id
        self.events = events
        self.max_children = max_children
        self.max_depth = max_depth
        self.max_total_agents = max_total_agents
        self.depth = depth
        self.parent_agent_id = parent_agent_id
        self.model_semaphore = model_semaphore or threading.Semaphore(max_children)
        self._lock = tree_lock or threading.RLock()
        self.tools = [self.delegate_task, self.delegate_tasks] if depth < max_depth else []

    @tool_info(
        "delegate_task",
        "Delegate one bounded evidence-gathering or patch-proposal task to a specialist subagent.",
        [
            {
                "name": "role",
                "description": "explore, research, code, test, review, or rag",
                "type": "string",
                "required": True,
            },
            {"name": "task", "description": "Concrete bounded subtask", "type": "string", "required": True},
        ],
        category=ToolCategory.AGENT,
        concurrency_safe=True,
        timeout_seconds=900,
    )
    def delegate_task(self, role: str, task: str) -> str:
        return self.delegate_tasks([{"role": role, "task": task}])

    @tool_info(
        "delegate_tasks",
        "Run up to four independent read-only specialist subtasks in parallel. Code agents propose patches; "
        "only the supervisor may apply workspace changes.",
        [
            {
                "name": "tasks",
                "description": "Array of {role, task} objects",
                "type": "array",
                "required": True,
            }
        ],
        category=ToolCategory.AGENT,
        concurrency_safe=True,
        timeout_seconds=1200,
    )
    def delegate_tasks(self, tasks: list[dict[str, Any]]) -> str:
        if self.depth >= self.max_depth:
            return json.dumps({"ok": False, "error": "subagent depth limit reached"})
        if not tasks or len(tasks) > self.max_children:
            return json.dumps(
                {"ok": False, "error": f"tasks must contain 1 to {self.max_children} items"},
                ensure_ascii=False,
            )
        normalized: list[dict[str, str]] = []
        for item in tasks:
            if not isinstance(item, dict):
                return json.dumps({"ok": False, "error": "each subtask must be an object"})
            role = str(item.get("role") or "").lower()
            task = str(item.get("task") or "").strip()
            if role not in ROLES or not task:
                return json.dumps({"ok": False, "error": f"invalid subagent role or task: {role}"})
            normalized.append({"role": role, "task": task})

        results: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=len(normalized), thread_name_prefix="lightworker-agent") as pool:
            futures = [pool.submit(self._execute, item["role"], item["task"]) for item in normalized]
            for future in as_completed(futures):
                results.append(future.result())
        results.sort(key=lambda item: item["index"])
        return json.dumps({"ok": all(item["ok"] for item in results), "results": results}, ensure_ascii=False)

    def _execute(self, role: str, task: str) -> dict[str, Any]:
        agent_id = uuid4().hex[:12]
        try:
            index = self._add_node(agent_id, role, task)
        except RuntimeError as exc:
            return {
                "index": self.max_total_agents,
                "agent_id": agent_id,
                "role": role,
                "ok": False,
                "content": "",
                "error": str(exc),
            }
        self.events.emit(
            "subagent_started",
            {"agent_id": agent_id, "parent_id": self.parent_agent_id, "role": role, "task": task},
        )
        child = SubagentManager(
            agent_factory=self.agent_factory,
            tools=self.read_tools,
            store=self.store,
            run_id=self.run_id,
            events=self.events,
            max_children=self.max_children,
            max_depth=self.max_depth,
            max_total_agents=self.max_total_agents,
            depth=self.depth + 1,
            parent_agent_id=agent_id,
            model_semaphore=self.model_semaphore,
            tree_lock=self._lock,
        )
        tools = [*self.read_tools, *child.tools]
        allowed = {str(tool.tool_info["tool_name"]) for tool in tools}
        try:
            if hasattr(self.agent_factory, "specialist"):
                agent = self.agent_factory.specialist(role, allowed_tools=allowed)
            else:
                agent = self.agent_factory.worker(allowed_tools=allowed)
            limit_agent_model_calls(agent, self.model_semaphore)
            prompt = (
                f"You are a {role} subagent at depth {self.depth + 1}. Complete only this bounded subtask. "
                "All workspace tools are read-only. For code work, return a concrete unified-diff proposal "
                "without claiming it was applied. Include evidence and uncertainties.\n\nSUBTASK:\n" + task
            )
            result = agent.run(
                prompt,
                tools=tools,
                trace=True,
                result_format="object",
                max_retry=4,
                max_tool_iterations=4,
                run_group_id=self.run_id,
            )
            trace = list(getattr(result, "trace", []) or [])
            self._save_trace(agent_id, result, attempt=1)
            recovered = False
            recovery_error: str | None = None
            if self._result_failed(result):
                replacement, recovery_error = self._recover(agent_id, role, task, result, trace)
                if replacement is not None and not self._result_failed(replacement):
                    result = replacement
                    recovered = True
            content = str(getattr(result, "content", result))
            error = getattr(result, "error", None)
            invalid = self._result_failed(result)
            if error:
                final_error = str(error)
            elif invalid:
                final_error = recovery_error or "subagent did not produce a valid response"
            else:
                final_error = None
            value = {
                "index": index,
                "agent_id": agent_id,
                "role": role,
                "ok": not bool(error) and not invalid,
                "content": content,
                "recovered": recovered,
                "error": final_error,
            }
        except Exception as exc:
            value = {
                "index": index,
                "agent_id": agent_id,
                "role": role,
                "ok": False,
                "content": "",
                "error": redact_text(str(exc)),
            }
        self._finish_node(agent_id, value)
        self.events.emit("subagent_completed", value)
        return value

    def _recover(
        self,
        agent_id: str,
        role: str,
        task: str,
        failed_result: Any,
        trace: list[Any],
    ) -> tuple[Any | None, str | None]:
        reason = self._failure_reason(failed_result)
        self.events.emit(
            "subagent_recovery_started",
            {"agent_id": agent_id, "role": role, "reason": reason},
        )
        try:
            if hasattr(self.agent_factory, "specialist"):
                finalizer = self.agent_factory.specialist(role, allowed_tools=set())
            else:
                finalizer = self.agent_factory.worker(allowed_tools=set())
            limit_agent_model_calls(finalizer, self.model_semaphore)
            evidence = self._trace_evidence(trace)
            prompt = (
                "FINAL SUBAGENT SYNTHESIS PASS. The earlier tool-using pass ended without a valid final "
                "response. No tools are available now. Complete the bounded subtask using only the untrusted "
                "captured evidence below. Preserve exact source URLs and citations, distinguish facts from "
                "inference, state missing data, and never invent values. Return a useful report even when "
                "evidence is incomplete.\n\n"
                f"SUBTASK:\n{task}\n\n"
                f"CAPTURED EVIDENCE:\n{evidence or 'No usable tool evidence was captured.'}"
            )
            recovered = finalizer.run(
                prompt,
                tools=[],
                trace=True,
                result_format="object",
                max_retry=2,
                max_tool_iterations=2,
                run_group_id=self.run_id,
                parent_trace_id=getattr(failed_result, "trace_id", None),
                use_skills=False,
            )
            self._save_trace(agent_id, recovered, attempt=2)
            valid = not self._result_failed(recovered)
            self.events.emit(
                "subagent_recovery_completed",
                {"agent_id": agent_id, "role": role, "valid": valid},
            )
            if valid:
                return recovered, None
            return recovered, "subagent synthesis recovery did not produce a valid response"
        except Exception as exc:  # noqa: BLE001 - isolate a specialist failure from the supervisor
            error = redact_text(str(exc))
            self.events.emit(
                "subagent_recovery_completed",
                {"agent_id": agent_id, "role": role, "valid": False, "error": error},
            )
            return None, f"subagent synthesis recovery failed: {error}"

    def _save_trace(self, agent_id: str, result: Any, *, attempt: int) -> None:
        events = list(getattr(result, "trace", []) or [])
        lines = [json.dumps(redact_value(event), ensure_ascii=False, default=str) for event in events]
        if lines:
            self.store.write_text(
                self.run_id,
                f"subagents/{agent_id}/attempt-{attempt}-trace.jsonl",
                "\n".join(lines) + "\n",
            )

    @staticmethod
    def _trace_evidence(trace: list[Any]) -> str:
        values: list[dict[str, Any]] = []
        for event in trace:
            if not isinstance(event, dict) or event.get("type") != "tool_result":
                continue
            data = event.get("data") or {}
            tool = str(data.get("name") or "")
            if not tool or tool in {"delegate_task", "delegate_tasks"}:
                continue
            output: Any = data.get("output")
            if isinstance(output, str):
                try:
                    output = json.loads(output)
                except json.JSONDecodeError:
                    output = output[:5000]
            encoded = json.dumps(redact_value(output), ensure_ascii=False, default=str)
            if len(encoded) > 6000:
                output = encoded[:6000] + "…[truncated]"
            values.append({"tool": tool, "output": output})
            if len(values) >= 16:
                break
        return json.dumps(values, ensure_ascii=False, default=str, indent=2)[:40_000]

    @staticmethod
    def _result_failed(result: Any) -> bool:
        if getattr(result, "error", None):
            return True
        content = str(getattr(result, "content", result) or "").strip().lower()
        if not content or content in {
            "failed to generate a valid response.",
            "failed to stream a valid response.",
        }:
            return True
        return any(
            isinstance(event, dict)
            and event.get("type") == "run_end"
            and (event.get("data") or {}).get("success") is False
            for event in list(getattr(result, "trace", []) or [])
        )

    @staticmethod
    def _failure_reason(result: Any) -> str:
        if getattr(result, "error", None):
            return redact_text(str(result.error))
        for event in reversed(list(getattr(result, "trace", []) or [])):
            if not isinstance(event, dict) or event.get("type") != "run_end":
                continue
            data = event.get("data") or {}
            if data.get("success") is False:
                return redact_text(str(data.get("error") or data.get("stage") or "agent run failed"))
        return "tool loop ended without a final answer"

    def _tree(self) -> dict[str, Any]:
        try:
            value = self.store.read_json(self.run_id, "agent-tree.json")
        except (FileNotFoundError, ValueError):
            value = {"agents": []}
        return value if isinstance(value, dict) else {"agents": []}

    def _add_node(self, agent_id: str, role: str, task: str) -> int:
        with self._lock:
            tree = self._tree()
            if len(tree.get("agents") or []) >= self.max_total_agents:
                raise RuntimeError(f"global subagent limit reached: {self.max_total_agents}")
            index = len(tree.get("agents") or [])
            tree.setdefault("agents", []).append(
                {
                    "index": index,
                    "agent_id": agent_id,
                    "parent_id": self.parent_agent_id,
                    "role": role,
                    "task": task,
                    "depth": self.depth + 1,
                    "status": "running",
                    "started_at": datetime.now(UTC).isoformat(),
                }
            )
            self.store.write_json(self.run_id, "agent-tree.json", tree)
            return index

    def _finish_node(self, agent_id: str, result: dict[str, Any]) -> None:
        with self._lock:
            tree = self._tree()
            node = next((item for item in tree.get("agents") or [] if item.get("agent_id") == agent_id), None)
            if node is not None:
                node.update(
                    {
                        "status": "completed" if result["ok"] else "failed",
                        "result": result["content"],
                        "error": result["error"],
                        "recovered": bool(result.get("recovered")),
                        "ended_at": datetime.now(UTC).isoformat(),
                    }
                )
                self.store.write_json(self.run_id, "agent-tree.json", tree)
