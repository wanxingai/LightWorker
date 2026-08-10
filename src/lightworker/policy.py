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
