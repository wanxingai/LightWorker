"""Dynamic LightAgent runtime that unifies research, analysis, and workspace changes."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .browser_tools import BrowserTools
from .config import WorkerConfig
from .control import ControlStore
from .goals import GoalManager, GoalTools
from .mcp_tools import MCPToolProvider
from .memory import AgentsInstructions, MemoryTools, WorkingMemory, WorkspaceMemory, workspace_scope
from .models import GoalStatus, RunRecord, RunStatus, TaskSpec, VerificationResult
from .native_runtime import NativeRuntimeLifecycle
from .policy import make_runtime_hook, redact_value
from .rag import RAGIndex, RAGTools
from .repo_tools import RepositoryTools
from .resources import limit_agent_model_calls, resource_pool
from .shell_tools import ShellTools
from .skills import SkillRegistry, SkillTools
from .storage import RunStore
from .subagents import SubagentManager
from .tool_protocol import ApprovalBroker, EventLog, ToolCatalog


class AgenticRuntime:
    def __init__(
        self,
        *,
        config: WorkerConfig,
        store: RunStore,
        spec: TaskSpec,
        record: RunRecord,
        sandbox: Any,
        repo_tools: RepositoryTools,
        external_tools: list[Any],
        agent_factory: Any,
    ):
        self.config = config
        self.store = store
        self.spec = spec
        self.record = record
        self.sandbox = sandbox
        self.repo_tools = repo_tools
        self.external_tools = external_tools
        self.agent_factory = agent_factory
        self.events = EventLog(store, spec.run_id)
        self.approvals = ApprovalBroker(store, spec.run_id, self.events)
        self.control = ControlStore(store, spec.run_id)
        self.goal = GoalManager(store, spec.run_id)
        self.resources = resource_pool(config.state_dir, config.scheduler)
        self.browser: BrowserTools | None = None
        self.native_lifecycle: NativeRuntimeLifecycle | None = None

    def run(self) -> RunRecord:
        started = time.perf_counter()
        self.record.metadata["lightagent_session_id"] = self._session_id()
        self.store.save(self.record)
        goal = self.goal.create(
            self.spec.task,
            acceptance_criteria=["Complete the user's requested outcome with tool-grounded evidence."],
            budget=self.config.runtime.goal_budget,
        )
        self.control.set_state("running")
        self.events.emit(
            "agentic_run_started",
            {
                "run_id": self.spec.run_id,
                "runtime": "agentic",
                "goal_id": goal.goal_id,
                "resume": self.store.artifact_path(self.spec.run_id, "trace.jsonl").is_file(),
            },
        )
        wrapped_tools, catalog, skill_registry, mcp_errors = self._build_tools()
        names = {str(tool.tool_info["tool_name"]) for tool in wrapped_tools}
        self.native_lifecycle = NativeRuntimeLifecycle(
            session_id=self._session_id(),
            run_id=self.spec.run_id,
            objective=self.spec.task,
            acceptance_criteria=goal.acceptance_criteria,
            parent_run_id=self.spec.parent_run_id,
            root_run_id=self.spec.root_run_id,
            queue_item_id=self.spec.queue_item_id,
            local_goal_id=goal.goal_id,
        )
        runtime_hook = make_runtime_hook(
            control=self.control,
            goal=self.goal,
            events=self.events,
        )
        worker = self.agent_factory.worker(
            allowed_tools=names,
            extra_hooks=[runtime_hook, self.native_lifecycle.hook()],
        )
        self.native_lifecycle.bind(worker)
        limit_agent_model_calls(worker, self.resources.model)
        query = self._prompt(skill_registry, mcp_errors)
        self.store.write_json(
            self.spec.run_id,
            "tool-manifest.json",
            [
                {key: value for key, value in tool.tool_info.items() if key != "approval_check"}
                for tool in wrapped_tools
            ],
        )
        try:
            result = worker.run(
                query,
                tools=wrapped_tools,
                trace=True,
                result_format="object",
                max_retry=self.config.runtime.max_tool_iterations,
                max_tool_iterations=self.config.runtime.max_tool_iterations,
                user_id=self._memory_scope(),
                run_group_id=self.spec.root_run_id or self.spec.run_id,
                session_id=self._session_id(),
                use_skills=False,
            )
            self._save_trace(result)
            self._record_usage(result, catalog, time.perf_counter() - started)
            if self.approvals.pending():
                return self._sync_native_runtime(self._waiting_for_approval(result))
            budget = self.goal.exceeded_budget()
            if (
                self._result_failed(result)
                and self.control.state()["state"] == "running"
                and (not budget or "tool_calls" in budget)
            ):
                result = self._recover_final_answer(result)
            diff = self.repo_tools.full_diff()
            verification: list[VerificationResult] = []
            if diff.strip():
                verification = self._verify_and_repair(worker, wrapped_tools, catalog, result)
                if self.approvals.pending():
                    return self._sync_native_runtime(self._waiting_for_approval(result))
                diff = self.repo_tools.full_diff()
            return self._sync_native_runtime(self._finalize(result, diff, verification))
        finally:
            self._persist_lightagent_session(worker)
            if self.browser is not None:
                self.browser.close()

    def _build_tools(self) -> tuple[list[Any], ToolCatalog, SkillRegistry, list[dict[str, str]]]:
        repository_tools = (
            self.repo_tools.write_tools
            if getattr(self.sandbox, "supports_write", True)
            else self.repo_tools.read_tools
        )
        tools = [*repository_tools, *self.external_tools]
        if self.config.shell.enabled and getattr(self.sandbox, "supports_shell", True):
            tools.extend(ShellTools(sandbox=self.sandbox, store=self.store, run_id=self.spec.run_id).tools)

        scope = self._memory_scope()
        if self.config.memory.enabled:
            memory_tools = MemoryTools(
                WorkingMemory(self.store, self.spec.run_id),
                WorkspaceMemory(
                    self.config.state_dir,
                    candidate_ttl_days=self.config.memory.candidate_ttl_days,
                ),
                scope,
            )
            tools.extend(memory_tools.tools)
        tools.extend(GoalTools(self.goal).tools)

        skill_registry = SkillRegistry(
            workspace=Path(self.record.workspace or self.store.workspace_dir(self.spec.run_id)),
            config=self.config.skills,
        )
        if self.config.skills.enabled:
            skill_config = self.config.skills.model_copy(
                update={
                    "allow_scripts": self.config.skills.allow_scripts
                    and getattr(self.sandbox, "supports_shell", True)
                }
            )
            tools.extend(SkillTools(skill_registry, self.sandbox, skill_config).tools)

        workspace = Path(self.record.workspace or self.store.workspace_dir(self.spec.run_id)).resolve()
        if self.config.rag.enabled:
            tools.extend(
                RAGTools(
                    RAGIndex(self.config.state_dir, scope=scope, config=self.config.rag),
                    workspace,
                ).tools
            )

        if self.config.browser.enabled:
            self.browser = BrowserTools(
                config=self.config.browser,
                store=self.store,
                run_id=self.spec.run_id,
                resource_semaphore=self.resources.browser,
            )
            tools.extend(self.browser.tools)

        mcp_provider = MCPToolProvider(config=self.config.mcp, sandbox=self.sandbox)
        if self.config.mcp.enabled and self.config.mcp.servers:
            tools.extend(mcp_provider.discover())

        catalog = ToolCatalog(
            broker=self.approvals,
            events=self.events,
            control_check=self.control.blocking_reason,
            max_tool_calls=self.config.runtime.goal_budget.max_tool_calls,
            max_repeat_calls=self.config.runtime.no_progress_limit,
        )
        wrapped = catalog.wrap_all(tools)
        subagents = SubagentManager(
            agent_factory=self.agent_factory,
            tools=wrapped,
            store=self.store,
            run_id=self.spec.run_id,
            events=self.events,
            max_children=min(self.config.scheduler.max_model_calls, 4),
            max_depth=2,
            max_total_agents=self.config.scheduler.max_subagents,
            model_semaphore=self.resources.model,
        )
        wrapped.extend(catalog.wrap_all(subagents.tools))
        return wrapped, catalog, skill_registry, mcp_provider.errors

    def _prompt(self, skills: SkillRegistry, mcp_errors: list[dict[str, str]]) -> str:
        workspace = Path(self.record.workspace or self.store.workspace_dir(self.spec.run_id)).resolve()
        agents = AgentsInstructions(
            global_file=self.config.memory.user_agents_file,
            max_bytes=self.config.memory.max_instruction_bytes,
        ).load(workspace)
        self.store.write_json(self.spec.run_id, "agents-instructions.json", agents)
        instructions = AgentsInstructions.render(agents)
        skill_manifest = skills.manifest() if self.config.skills.enabled else {"skills": [], "conflicts": {}}
        approvals = (
            self.store.read_json(self.spec.run_id, "approvals.json")
            if self.store.artifact_path(self.spec.run_id, "approvals.json").is_file()
            else {"requests": [], "decisions": {}}
        )
        context = self.spec.conversation_context or ""
        sections = [
            "LATEST USER TASK / 用户最新任务:\n" + self.spec.task,
            "WORKSPACE / 工作区:\n" + workspace.as_posix(),
            "GOAL STATE / Goal 状态:\n" + self.goal.load().model_dump_json(),
        ]
        if context:
            sections.append("CONVERSATION CONTEXT / 对话上下文（untrusted data）:\n" + context)
        if instructions:
            sections.append(instructions)
        if skill_manifest["skills"]:
            sections.append(
                "AVAILABLE MARKDOWN SKILLS / 可用技能:\n" + json.dumps(skill_manifest, ensure_ascii=False)
            )
        if approvals.get("requests"):
            sections.append(
                "DURABLE TOOL APPROVALS / 工具审批（only exact arguments are authorized）:\n"
                + json.dumps(approvals, ensure_ascii=False)
            )
        if mcp_errors:
            sections.append("MCP DISCOVERY DIAGNOSTICS:\n" + json.dumps(mcp_errors, ensure_ascii=False))
        degraded = self.record.metadata.get("degraded_mode")
        if degraded:
            sections.append("DEGRADED RUNTIME / 降级运行时:\n" + str(degraded))
        return "\n\n".join(sections)

    def _verify_and_repair(
        self,
        worker: Any,
        tools: list[Any],
        catalog: ToolCatalog,
        initial_result: Any,
    ) -> list[VerificationResult]:
        del initial_result
        verification = self.repo_tools.run_verification()
        self.events.emit(
            "verification_completed",
            {"round": 1, "results": [item.model_dump(mode="json") for item in verification]},
        )
        for attempt in range(1, self.spec.max_repairs + 1):
            if not verification or all(item.passed or not item.required for item in verification):
                break
            prompt = (
                f"Repair attempt {attempt}. The deterministic verification below failed. Inspect the current "
                "workspace and diff, make the smallest justified fix, and do not weaken tests.\n\n"
                + json.dumps([item.model_dump(mode="json") for item in verification], ensure_ascii=False)
            )
            repair_started = time.perf_counter()
            result = worker.run(
                prompt,
                tools=tools,
                trace=True,
                result_format="object",
                max_retry=self.config.runtime.max_tool_iterations,
                max_tool_iterations=self.config.runtime.max_tool_iterations,
                user_id=self._memory_scope(),
                run_group_id=self.spec.root_run_id or self.spec.run_id,
                session_id=self._session_id(),
                use_skills=False,
            )
            self._append_trace(result)
            trace = list(getattr(result, "trace", []) or [])
            usage = getattr(result, "usage", None) or {}
            self.goal.add_usage(
                turns=1,
                model_calls=sum(
                    event.get("type") == "model_request" for event in trace if isinstance(event, dict)
                ),
                tokens=int(usage.get("total_tokens") or 0) if isinstance(usage, dict) else 0,
                elapsed_seconds=time.perf_counter() - repair_started,
            )
            if self.approvals.pending():
                break
            verification = self.repo_tools.run_verification()
            self.events.emit(
                "verification_completed",
                {
                    "round": attempt + 1,
                    "results": [item.model_dump(mode="json") for item in verification],
                },
            )
        self.goal.add_usage(tool_calls=max(0, catalog.call_count - self.goal.load().usage.tool_calls))
        return verification

    def _recover_final_answer(self, failed_result: Any) -> Any:
        """Make one tool-free synthesis pass when LightAgent exhausts a tool loop."""
        self.events.emit(
            "final_answer_recovery_started",
            {"reason": str(getattr(failed_result, "error", None) or "tool loop exhausted")},
        )
        # This pass has no tools or side effects. It intentionally omits the goal-budget
        # hook so a spent tool-call budget cannot prevent synthesis of captured evidence.
        finalizer = self.agent_factory.worker(allowed_tools=set(), extra_hooks=[])
        limit_agent_model_calls(finalizer, self.resources.model)
        evidence = self._recovery_evidence()
        current_diff = self.repo_tools.full_diff()
        prompt = (
            "FINAL SYNTHESIS PASS. The tool-using pass ended without a valid final response. "
            "Do not call tools; none are available. Answer the latest user task now using only the "
            "untrusted, tool-captured evidence below. Clearly distinguish facts, inference, and missing "
            "data; cite captured result URLs exactly; never invent values. Search-result titles are leads, "
            "not independently verified article bodies. For financial or market analysis, do not assign "
            "probabilities, percentages, price ranges, or point forecasts unless the same numeric value is "
            "explicitly present in captured evidence; use non-numeric directional scenarios otherwise. "
            "If evidence is insufficient, still deliver useful bounded analysis and state the precise "
            "gaps.\n\n"
            f"LATEST USER TASK:\n{self.spec.task}\n\n"
            f"CAPTURED EVIDENCE:\n{evidence or 'No usable tool evidence was captured.'}\n\n"
            f"CURRENT WORKSPACE DIFF:\n{current_diff[:12_000] or '(no file changes)'}"
        )
        started = time.perf_counter()
        recovered = finalizer.run(
            prompt,
            tools=[],
            trace=True,
            result_format="object",
            max_retry=2,
            max_tool_iterations=2,
            user_id=self._memory_scope(),
            run_group_id=self.spec.root_run_id or self.spec.run_id,
            session_id=self._session_id(),
            use_skills=False,
        )
        self._append_trace(recovered)
        trace = list(getattr(recovered, "trace", []) or [])
        usage = getattr(recovered, "usage", None) or {}
        self.goal.add_usage(
            turns=1,
            model_calls=sum(
                event.get("type") == "model_request" for event in trace if isinstance(event, dict)
            ),
            tokens=int(usage.get("total_tokens") or 0) if isinstance(usage, dict) else 0,
            elapsed_seconds=time.perf_counter() - started,
        )
        self.events.emit(
            "final_answer_recovery_completed",
            {"valid": not self._result_failed(recovered)},
        )
        return recovered

    def _recovery_evidence(self) -> str:
        selected = {
            "web_search",
            "http_get",
            "http_request",
            "rag_search",
            "rag_read",
            "read_file",
            "search_text",
        }
        values: list[dict[str, Any]] = []
        seen_failures: set[str] = set()
        for event in self.events.read(limit=500):
            if event.get("type") != "tool_completed":
                continue
            data = event.get("data") or {}
            tool = str(data.get("tool") or "")
            if tool not in selected:
                continue
            output = str(data.get("output") or "")
            try:
                payload = json.loads(output)
            except json.JSONDecodeError:
                payload = output
            if isinstance(payload, dict) and payload.get("ok") is False:
                error = str(payload.get("error") or f"HTTP status {payload.get('status')}")
                fingerprint = f"{tool}:{error}"
                if fingerprint in seen_failures:
                    continue
                seen_failures.add(fingerprint)
                values.append({"tool": tool, "ok": False, "error": error, "url": payload.get("url")})
            else:
                values.append({"tool": tool, "ok": True, "evidence": self._compact_evidence(payload)})
            if len(values) >= 16:
                break
        encoded = json.dumps(values, ensure_ascii=False, default=str, indent=2)
        return encoded[:48_000]

    @staticmethod
    def _compact_evidence(payload: Any) -> Any:
        if not isinstance(payload, dict):
            return str(payload)[:3500]
        compact = {
            key: value
            for key, value in payload.items()
            if key
            in {
                "query",
                "provider",
                "results",
                "status",
                "url",
                "content_type",
                "body",
                "path",
                "matches",
                "content",
                "citation",
            }
        }
        if isinstance(compact.get("results"), list):
            compact["results"] = compact["results"][:8]
        for key in ("body", "content", "matches"):
            if key in compact:
                text = str(compact[key])
                text = " ".join(text.replace("<", " <").split())
                compact[key] = text[:5000]
        return compact

    @staticmethod
    def _result_failed(result: Any) -> bool:
        if getattr(result, "error", None):
            return True
        content = str(getattr(result, "content", "") or "").strip().lower()
        if not content or content in {
            "failed to generate a valid response.",
            "failed to stream a valid response.",
        }:
            return True
        for event in list(getattr(result, "trace", []) or []):
            if not isinstance(event, dict) or event.get("type") != "run_end":
                continue
            data = event.get("data") or {}
            if data.get("success") is False:
                return True
        return False

    def _waiting_for_approval(self, result: Any) -> RunRecord:
        content = str(getattr(result, "content", "") or "")
        pending = self.approvals.pending()
        summary = content.rstrip() + "\n\n"
        summary += "任务正在等待工具审批 / Waiting for tool approval:\n"
        summary += "\n".join(f"- {item['tool']}: {item['reason']}" for item in pending)
        self.store.write_text(self.spec.run_id, "summary.md", summary.strip() + "\n")
        diff = self.repo_tools.full_diff()
        if diff.strip():
            self.store.write_text(self.spec.run_id, "changes.patch", diff)
            self.store.write_text(
                self.spec.run_id,
                "git-status.txt",
                _ok_value(self.repo_tools.git_status(), "status"),
            )
        self.goal.update_status(GoalStatus.WAITING_APPROVAL, "exact tool approval required")
        record = self.store.load(self.spec.run_id)
        record.status = RunStatus.WAITING_APPROVAL
        record.current_step = f"approval:{pending[0]['request_id']}"
        record.error = "等待用户审批 / waiting for user approval"
        record.trace_id = getattr(result, "trace_id", None)
        record.metadata["tool_calls"] = self.goal.load().usage.tool_calls
        record.metadata["has_changes"] = bool(diff.strip())
        self.store.save(record)
        self.events.emit("agentic_run_waiting_approval", {"requests": pending})
        return record

    def _finalize(
        self,
        result: Any,
        diff: str,
        verification: list[VerificationResult],
    ) -> RunRecord:
        self.store.write_text(self.spec.run_id, "changes.patch", diff)
        self.store.write_text(
            self.spec.run_id,
            "git-status.txt",
            _ok_value(self.repo_tools.git_status(), "status"),
        )
        content = str(getattr(result, "content", "") or "")
        if diff.strip():
            content += "\n\n## Verification / 验证\n"
            if verification:
                content += "\n".join(
                    f"- {item.name}: {'passed' if item.passed else 'failed'} "
                    f"(exit {item.exit_code}, log `{item.log_path}`)"
                    for item in verification
                )
            else:
                content += "- No deterministic verification was configured. / 未配置确定性验证。"
        self.store.write_text(self.spec.run_id, "summary.md", content.strip() + "\n")

        error = getattr(result, "error", None)
        if not error and self._result_failed(result):
            error = "agent did not produce a valid final response"
        control = self.control.state()["state"]
        budget = self.goal.exceeded_budget()
        record = self.store.load(self.spec.run_id)
        record.trace_id = getattr(result, "trace_id", None)
        record.verification = verification
        record.current_step = None
        if control == "cancelled":
            record.status = RunStatus.CANCELLED
            record.error = "task cancelled by user"
            self.goal.update_status(GoalStatus.CANCELLED, record.error)
        elif control == "paused":
            record.status = RunStatus.PAUSED
            record.error = "task paused by user"
            self.goal.update_status(GoalStatus.PAUSED, record.error)
        elif budget and self._result_failed(result):
            record.status = RunStatus.BUDGET_LIMITED
            record.error = budget
            self.goal.update_status(GoalStatus.BUDGET_LIMITED, budget)
        elif error:
            record.status = RunStatus.FAILED
            record.error = str(error)
            self.goal.update_status(GoalStatus.FAILED, record.error)
        elif not diff.strip():
            record.status = RunStatus.SUCCEEDED
            record.error = None
            self.goal.update_status(GoalStatus.COMPLETED)
        elif verification and all(item.passed or not item.required for item in verification):
            record.status = RunStatus.SUCCEEDED
            record.error = None
            self.goal.update_status(GoalStatus.COMPLETED)
        elif not verification and not _diff_requires_code_verification(diff):
            record.status = RunStatus.SUCCEEDED
            record.error = None
            self.goal.update_status(GoalStatus.COMPLETED)
        else:
            record.status = RunStatus.NEEDS_ATTENTION
            record.error = "verification failed or was not configured for code changes"
            self.goal.update_status(GoalStatus.WAITING_INPUT, record.error)
        record.metadata["execution_mode"] = "agentic"
        record.metadata["has_changes"] = bool(diff.strip())
        self.store.save(record)
        self.events.emit(
            "agentic_run_completed",
            {"status": record.status.value, "has_changes": bool(diff.strip()), "error": record.error},
        )
        return record

    def _record_usage(self, result: Any, catalog: ToolCatalog, elapsed: float) -> None:
        trace = list(getattr(result, "trace", []) or [])
        model_calls = sum(event.get("type") == "model_request" for event in trace if isinstance(event, dict))
        usage = getattr(result, "usage", None) or {}
        tokens = int(usage.get("total_tokens") or 0) if isinstance(usage, dict) else 0
        self.goal.add_usage(
            turns=1,
            tool_calls=catalog.call_count,
            model_calls=model_calls,
            tokens=tokens,
            elapsed_seconds=elapsed,
        )

    def _save_trace(self, result: Any) -> None:
        events = list(getattr(result, "trace", []) or [])
        lines = [json.dumps(redact_value(event), ensure_ascii=False, default=str) for event in events]
        self.store.write_text(self.spec.run_id, "trace.jsonl", ("\n".join(lines) + "\n") if lines else "")

    def _append_trace(self, result: Any) -> None:
        events = list(getattr(result, "trace", []) or [])
        lines = [json.dumps(redact_value(event), ensure_ascii=False, default=str) for event in events]
        if lines:
            self.store.append_text(self.spec.run_id, "trace.jsonl", "\n".join(lines) + "\n")

    def _persist_lightagent_session(self, worker: Any) -> None:
        """Project the native durable Session into the per-run artifact directory for inspection."""
        export = getattr(worker, "export_session", None)
        if not callable(export):
            return
        session_id = self._session_id()
        try:
            payload = export(session_id)
            if not isinstance(payload, dict):
                return
            self.store.write_json(
                self.spec.run_id,
                "lightagent-session.json",
                redact_value(payload),
            )
        except Exception as exc:  # noqa: BLE001 - session projection must not mask the task result
            self.events.emit(
                "lightagent_session_export_failed",
                {"session_id": session_id, "error": redact_value(str(exc))},
            )
            return
        events = payload.get("events")
        self.events.emit(
            "lightagent_session_exported",
            {
                "session_id": session_id,
                "event_count": len(events) if isinstance(events, list) else 0,
            },
        )

    def _sync_native_runtime(self, record: RunRecord) -> RunRecord:
        lifecycle = self.native_lifecycle
        session_store = getattr(self.agent_factory, "session_store", None)
        if lifecycle is None or session_store is None:
            return record
        try:
            snapshot = lifecycle.finalize(
                session_store,
                record.status,
                record.error or "",
                budget_limits=getattr(self.agent_factory, "budget_limits", None),
            )
            if snapshot is None:
                return record
            self.store.write_json(
                self.spec.run_id,
                "lightagent-runtime.json",
                redact_value(snapshot),
            )
            self.events.emit(
                "lightagent_runtime_synchronized",
                {
                    "session_id": self._session_id(),
                    "status": record.status.value,
                    "inbox_count": len(snapshot.get("inbox") or []),
                    "goal_count": len(snapshot.get("goals") or []),
                },
            )
        except Exception as exc:  # noqa: BLE001 - native projection must not mask the task result
            self.events.emit(
                "lightagent_runtime_sync_failed",
                {"session_id": self._session_id(), "error": redact_value(str(exc))},
            )
        return record

    def _session_id(self) -> str:
        # A task run owns one native Session. Follow-up runs retain their explicit
        # conversation context until the Web queue is migrated to AgentInbox, so
        # using the root ID here would replay the same history twice.
        return self.spec.run_id

    def _memory_scope(self) -> str:
        root_id = self.spec.root_run_id or self.spec.run_id
        try:
            repo = self.store.load(root_id).repo
        except (FileNotFoundError, ValueError):
            repo = self.record.repo
        return workspace_scope(repo)


def _ok_value(raw: str, key: str) -> str:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    return str(value.get(key) or "") if isinstance(value, dict) else ""


CODE_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".go",
    ".h",
    ".hpp",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".sql",
    ".swift",
    ".ts",
    ".tsx",
    ".vue",
}


def _diff_requires_code_verification(diff: str) -> bool:
    paths = [line[6:] for line in diff.splitlines() if line.startswith("+++ b/")]
    return any(Path(path).suffix.lower() in CODE_SUFFIXES for path in paths)
