"""LightAgent capability composition, health, and deferred tool presentation."""

from __future__ import annotations

import asyncio
import functools
import json
import threading
from concurrent.futures import ThreadPoolExecutor

from LightAgent import (
    BaseCapabilityProvider,
    CapabilityRegistry,
    CapabilityRisk,
    CapabilitySpec,
    HookDecision,
    PolicyDecision,
    PolicyEngine,
    PolicyHook,
    ProviderHealth,
    RuntimeContext,
)

from .models import ToolCategory
from .tool_protocol import metadata_for, tool_info


def run_async(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


class ToolProvider(BaseCapabilityProvider):
    def __init__(self, name, tools):
        self.name = name
        self.functions = {tool.tool_info["tool_name"]: tool for tool in tools}
        self.calls, self.failures = 0, 0
        self.last_error = None
        specs = []
        for tool in tools:
            meta = metadata_for(tool)
            workspace_read = meta.category == ToolCategory.WORKSPACE and meta.is_read_only
            requires_docker = meta.sandbox_required and not workspace_read
            risk = (
                CapabilityRisk.DESTRUCTIVE
                if meta.is_destructive
                else CapabilityRisk.SENSITIVE
                if meta.external_side_effect or (meta.sandbox_required and not meta.is_read_only)
                else CapabilityRisk.ISOLATED_WRITE
                if meta.is_write
                else CapabilityRisk.READ_ONLY
            )
            specs.append(
                CapabilitySpec(
                    tool.tool_info["tool_name"],
                    description=tool.tool_info["tool_description"],
                    risk=risk,
                    read=meta.is_read_only,
                    write=meta.is_write,
                    network=meta.network_required,
                    execute=requires_docker,
                    requires_sandbox=requires_docker,
                    # The guarded callable owns exact-argument approvals. The native
                    # unconditional gate cannot represent a pending approval result.
                    requires_approval=False,
                    metadata={
                        "approval_policy": meta.approval_policy.value,
                        "approval_enforcer": "lightworker_exact_argument_broker",
                    },
                    timeout=meta.timeout_seconds,
                    output_limit=None,  # ToolCatalog spills without breaking JSON.
                )
            )
        super().__init__(specs)

    async def invoke(self, capability: str, **arguments):
        # Sync browser/MCP/HTTP implementations are isolated from the agent event loop.
        self.calls += 1
        try:
            return await asyncio.to_thread(self.functions[capability], **arguments)
        except Exception as exc:
            self.failures += 1
            self.last_error = type(exc).__name__
            raise

    async def health(self):
        return ProviderHealth(
            healthy=self.started,
            status="ready" if self.started else "stopped",
            metadata={
                "tools": list(self.functions),
                "calls": self.calls,
                "failures": self.failures,
                "last_error_type": self.last_error,
            },
        )


class ProviderComposition:
    def __init__(self, *, store, run_id, sandbox_available=True):
        self.store, self.run_id = store, run_id
        self.context = RuntimeContext(
            run_id=run_id, session_id=run_id, metadata={"sandbox_available": sandbox_available}
        )

        def policy(request):
            if request.capability.requires_sandbox and not sandbox_available:
                return PolicyDecision.block("Docker sandbox is unavailable")
            return PolicyDecision.allow(request.arguments)

        self.registry = CapabilityRegistry(policy_engine=PolicyEngine([policy]))
        self.tools = {}
        self.loaded: set[str] = set()
        self._lock = threading.RLock()

    def compose(self, tools):
        grouped = {}
        for tool in tools:
            if not getattr(tool, "_lightworker_guarded", False):
                raise ValueError("providers accept only policy-guarded tools")
            grouped.setdefault(metadata_for(tool).category.value, []).append(tool)
        for name, values in grouped.items():
            self.registry.register(ToolProvider(name, values))
        run_async(self.registry.mount(self.context))
        for tool in tools:
            name = tool.tool_info["tool_name"]

            def make_proxy(original, capability):
                @functools.wraps(original)
                def proxy(**arguments):
                    return run_async(self.registry.invoke(capability, arguments, context=self.context))

                proxy.tool_info = original.tool_info
                return proxy

            self.tools[name] = make_proxy(tool, name)
        self.store.write_json(self.run_id, "providers.json", self.registry.list(self.context))
        return list(self.tools.values())

    @tool_info(
        "tool_search",
        "Find and activate tools by name, purpose, or category. "
        "Tools keep the same policy and approval requirements after activation.",
        [
            {"name": "query", "type": "string", "required": True},
            {"name": "limit", "type": "integer", "required": False},
        ],
        category=ToolCategory.GOAL,
    )
    def tool_search(self, query, limit=8):
        tokens = query.lower().replace("/", " ").split()
        synonyms = {
            "网页": "browser web",
            "浏览器": "browser",
            "搜索": "search",
            "文件": "file",
            "代码": "shell patch lsp code",
            "记忆": "memory",
            "金融": "web http browser rag",
            "子代理": "agent",
            "资料": "rag",
            "终端": "terminal",
            "计划": "goal workflow",
        }
        for word, expanded in synonyms.items():
            if word in query:
                tokens.extend(expanded.split())
        scored = []
        for name, tool in self.tools.items():
            info = tool.tool_info
            text = f"{name} {info['tool_description']} {metadata_for(tool).category.value}".lower()
            score = sum(token in text for token in tokens)
            if score:
                scored.append((score, name, info))
        selected = sorted(scored, key=lambda x: (-x[0], x[1]))[: max(1, min(limit, 20))]
        with self._lock:
            self.loaded.update(name for _, name, _ in selected)
        self.store.session(self.run_id).append("capability.activated", {"tools": [n for _, n, _ in selected]})
        return json.dumps(
            [
                {"name": name, "description": info["tool_description"], "parameters": info["tool_params"]}
                for _, name, info in selected
            ],
            ensure_ascii=False,
        )

    def presentation_hook(self, enabled=True):
        essentials = {
            "tool_search",
            "goal_get",
            "goal_add_subgoal",
            "goal_update_subgoal",
            "web_search",
            "http_get",
            "list_files",
            "read_file",
            "search_text",
            "git_status",
            "git_diff",
            "apply_patch",
            "delegate_task",
            "delegate_tasks",
        }
        self.loaded.update(essentials)

        def present(context):
            if not enabled or context.phase != "before_model_request":
                return None
            params = dict(context.payload.get("params") or {})
            params["tools"] = [
                s for s in params.get("tools", []) if (s.get("function") or {}).get("name") in self.loaded
            ]
            if not params["tools"]:
                params.pop("tools", None)
                params.pop("tool_choice", None)
            return HookDecision.replace({"params": params})

        return PolicyHook(
            present,
            phases={"before_model_request"},
            failure_mode="block",
            name="lightworker_tool_presentation",
        )

    def health(self):
        return {key: value.to_dict() for key, value in run_async(self.registry.health(self.context)).items()}

    def close(self):
        run_async(self.registry.stop(self.context))
