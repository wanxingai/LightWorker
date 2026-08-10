from __future__ import annotations

import json

from LightAgent import HookContext, RunResult

from lightworker.agents import StructuredOutputAgent, parse_json_model
from lightworker.models import CodingPlan
from lightworker.policy import make_policy_hooks, redact_text

VALID_PLAN = {
    "task_type": "bugfix",
    "risk": "low",
    "summary": {"zh": "修复返回值", "en": "Fix the return value"},
    "items": [
        {
            "id": "1",
            "description": {"zh": "修改 app.py", "en": "Update app.py"},
            "files": ["app.py"],
        }
    ],
    "verification": ["pytest -q"],
}


class SequenceAgent:
    name = "sequence"

    def __init__(self, outputs: list[str]):
        self.outputs = list(outputs)
        self.calls = 0

    def run(self, query: str, **kwargs: object) -> RunResult:
        value = self.outputs[self.calls]
        self.calls += 1
        return RunResult(content=value, trace=[{"type": "call", "data": {"index": self.calls}}])


def test_parse_json_model_accepts_fenced_json():
    content = f"```json\n{json.dumps(VALID_PLAN)}\n```"
    parsed = parse_json_model(content, CodingPlan)
    assert parsed is not None
    assert parsed.summary.zh == "修复返回值"


def test_structured_agent_repairs_invalid_output_once():
    agent = SequenceAgent(["not json", json.dumps(VALID_PLAN)])
    wrapped = StructuredOutputAgent(agent, CodingPlan)

    result = wrapped.run("plan", tools=[lambda: None])

    assert agent.calls == 2
    assert CodingPlan.model_validate_json(result.content).task_type == "bugfix"
    assert len(result.trace) == 2


def test_policy_blocks_builtin_and_unlisted_tools():
    hook = make_policy_hooks(allowed_tools={"read_file"})[0].handler
    builtin = hook(HookContext(phase="before_tool_call", payload={"tool_name": "execute_python_code"}))
    unknown = hook(HookContext(phase="before_tool_call", payload={"tool_name": "delete_file"}))
    allowed = hook(HookContext(phase="before_tool_call", payload={"tool_name": "read_file"}))

    assert builtin.action == "block"
    assert unknown.action == "block"
    assert allowed is None


def test_redaction_hides_common_tokens():
    value = redact_text("api_key=supersecret sk-abcdefghijklmnopqrstuvwxyz")
    assert "supersecret" not in value
    assert "sk-abc" not in value
    assert "[redacted]" in value


def test_redaction_hides_aws_keys_and_private_keys():
    value = redact_text(
        "AKIAIOSFODNN7EXAMPLE\n-----BEGIN PRIVATE KEY-----\nsecret material\n-----END PRIVATE KEY-----"
    )

    assert "AKIAIOSFODNN7EXAMPLE" not in value
    assert "secret material" not in value
