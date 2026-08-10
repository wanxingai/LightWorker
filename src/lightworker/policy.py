"""Fail-closed LightAgent policy hooks and redaction helpers."""

from __future__ import annotations

import re
from typing import Any

try:
    from LightAgent import HookDecision, PolicyHook
except ImportError:  # pragma: no cover - rendered as a doctor diagnostic at runtime
    HookDecision = None  # type: ignore[assignment]
    PolicyHook = None  # type: ignore[assignment]


BLOCKED_BUILTIN_TOOLS = {
    "execute_python_code",
    "execute_python_file",
    "execute_python_code_stream",
    "upload_file_to_oss",
}

SECRET_PATTERNS = [
    re.compile(r"(?i)\b(api[_-]?key|secret|password|token)\s*[:=]\s*([^\s,;]+)"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[opusr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
]


def redact_text(value: str) -> str:
    text = value
    for pattern in SECRET_PATTERNS:
        if pattern.groups >= 2:
            text = pattern.sub(lambda match: f"{match.group(1)}=[redacted]", text)
        else:
            text = pattern.sub("[redacted]", text)
    return text


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    if isinstance(value, dict):
        return {key: redact_value(item) for key, item in value.items()}
    return value


def make_policy_hooks(*, allowed_tools: set[str]) -> list[Any]:
    if PolicyHook is None or HookDecision is None:
        raise RuntimeError("LightAgent policy API is unavailable; install LightAgent>=0.9.7,<0.10")

    def authorize(ctx: Any) -> Any:
        if ctx.phase == "before_tool_call":
            tool_name = str(ctx.payload.get("tool_name") or "")
            if tool_name in BLOCKED_BUILTIN_TOOLS or tool_name not in allowed_tools:
                return HookDecision.block(f"tool is not authorized for this agent: {tool_name}")
        if ctx.phase == "before_model_request":
            params = redact_value(dict(ctx.payload.get("params") or {}))
            authorized_schemas = []
            for schema in params.get("tools") or []:
                function = schema.get("function") if isinstance(schema, dict) else None
                tool_name = str((function or {}).get("name") or "")
                if tool_name in allowed_tools and tool_name not in BLOCKED_BUILTIN_TOOLS:
                    authorized_schemas.append(schema)
            if authorized_schemas:
                params["tools"] = authorized_schemas
            else:
                params.pop("tools", None)
                params.pop("tool_choice", None)
            return HookDecision.replace({"params": params})
        if ctx.phase == "after_tool_result":
            return HookDecision.replace(
                {
                    "tool_name": ctx.payload.get("tool_name"),
                    "output": redact_value(ctx.payload.get("output")),
                }
            )
        return None

    return [
        PolicyHook(
            authorize,
            phases={"before_tool_call", "before_model_request", "after_tool_result"},
            failure_mode="block",
            timeout=3.0,
            name="lightworker_fail_closed_policy",
        )
    ]


def make_runtime_hook(*, control: Any, goal: Any, events: Any) -> Any:
    """Create a fail-closed hook for pause/cancel, live steering, and goal budgets."""
    if PolicyHook is None or HookDecision is None:
        raise RuntimeError("LightAgent policy API is unavailable; install LightAgent>=0.9.7,<0.10")

    def runtime(ctx: Any) -> Any:
        if ctx.phase in {"before_model_request", "before_tool_call"}:
            blocking = control.blocking_reason()
            if blocking:
                events.emit("runtime_blocked", {"phase": ctx.phase, "reason": blocking})
                return HookDecision.block(blocking)
            exceeded = goal.exceeded_budget()
            if exceeded:
                events.emit("budget_exceeded", {"phase": ctx.phase, "reason": exceeded})
                return HookDecision.block(exceeded)
        if ctx.phase == "before_model_request":
            steering = control.consume_steering()
            if steering:
                params = dict(ctx.payload.get("params") or {})
                messages = list(params.get("messages") or [])
                for message in steering:
                    messages.append(
                        {
                            "role": "user",
                            "content": "LIVE USER STEERING / 用户实时补充（高优先级用户消息）:\n" + message,
                        }
                    )
                params["messages"] = messages
                events.emit("steering_consumed", {"messages": steering})
                return HookDecision.replace({"params": params})
        return None

    return PolicyHook(
        runtime,
        phases={"before_model_request", "before_tool_call"},
        failure_mode="block",
        timeout=3.0,
        name="lightworker_runtime_control",
    )
