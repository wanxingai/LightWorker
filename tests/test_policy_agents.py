from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from LightAgent import HookContext, HookDecision, PolicyHook, RunResult

import lightworker.agents as agents_module
from lightworker.agents import AgentFactory, StructuredOutputAgent, parse_json_model
from lightworker.config import ModelConfig, RuntimeConfig
from lightworker.models import CodingPlan, GoalBudget, RunStatus
from lightworker.native_runtime import NativeRuntimeLifecycle
from lightworker.policy import make_policy_hooks, make_runtime_hook, redact_text

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


def test_policy_removes_unauthorized_tool_schemas_before_model_request():
    hook = make_policy_hooks(allowed_tools={"read_file"})[0].handler

    def schema(name: str) -> dict[str, object]:
        return {"type": "function", "function": {"name": name, "parameters": {}}}

    decision = hook(
        HookContext(
            phase="before_model_request",
            payload={
                "params": {
                    "tools": [
                        schema("read_file"),
                        schema("execute_python_code"),
                        schema("delete_file"),
                    ],
                    "tool_choice": "auto",
                }
            },
        )
    )
    params = decision.payload["params"]

    assert [item["function"]["name"] for item in params["tools"]] == ["read_file"]

    no_tools = make_policy_hooks(allowed_tools=set())[0].handler(
        HookContext(
            phase="before_model_request",
            payload={"params": {"tools": [schema("execute_python_code")], "tool_choice": "auto"}},
        )
    )
    assert "tools" not in no_tools.payload["params"]
    assert "tool_choice" not in no_tools.payload["params"]


def test_runtime_hook_forwards_consumed_steering_to_the_next_hook():
    class Control:
        def blocking_reason(self) -> None:
            return None

        def consume_steering(self) -> list[str]:
            return ["核对最新库存"]

    class Goal:
        def exceeded_budget(self) -> None:
            return None

    class Events:
        def emit(self, *args: object) -> None:
            del args

    hook = make_runtime_hook(control=Control(), goal=Goal(), events=Events()).handler
    decision = hook(
        HookContext(
            phase="before_model_request",
            payload={"params": {"messages": [{"role": "user", "content": "分析市场"}]}},
        )
    )

    assert decision.payload["lightworker_steering"] == ["核对最新库存"]
    assert decision.payload["params"]["messages"][-1]["content"].endswith("核对最新库存")


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


def test_agent_factory_wires_lightagent_unified_runtime(
    tmp_path: Path,
    monkeypatch,
):
    captured: dict[str, object] = {}

    class FakeLightAgent:
        def __init__(self, **kwargs: object):
            captured.update(kwargs)

    monkeypatch.setattr(agents_module, "LightAgent", FakeLightAgent)
    runtime = RuntimeConfig(
        context_window_tokens=12_000,
        goal_budget=GoalBudget(
            max_model_calls=7,
            max_tool_calls=19,
            max_tokens=24_000,
            max_seconds=600,
        ),
    )
    factory = AgentFactory(
        ModelConfig(model="fake-model"),
        state_dir=tmp_path / "state",
        runtime=runtime,
    )

    factory.worker(allowed_tools={"read_file"})

    session_store = captured["session_store"]
    assert Path(session_store.path) == (tmp_path / "state" / "lightagent-sessions.sqlite3").resolve()
    assert captured["budget_limits"].model_calls == 7
    assert captured["budget_limits"].tool_calls == 19
    assert captured["budget_limits"].tokens == 24_000
    assert captured["budget_limits"].seconds == 600
    assert captured["context_budget"].max_tokens == 12_000
    assert captured["context_budget"].reserved_output_tokens == 3_000


def test_agent_factory_persists_a_real_lightagent_session(tmp_path: Path):
    factory = AgentFactory(
        ModelConfig(model="fake-model", api_key="test-key"),
        state_dir=tmp_path / "state",
        runtime=RuntimeConfig(),
    )
    agent = factory.worker(allowed_tools=set())

    class StaticCompletions:
        def create(self, **kwargs: object) -> object:
            del kwargs
            message = SimpleNamespace(content="done", tool_calls=None)
            usage = SimpleNamespace(prompt_tokens=2, completion_tokens=1, total_tokens=3)
            return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)

    agent.client = SimpleNamespace(chat=SimpleNamespace(completions=StaticCompletions()))

    result = agent.run(
        "inspect the workspace",
        session_id="lightworker-smoke",
        result_format="object",
        trace=True,
        use_skills=False,
    )

    session = agent.export_session("lightworker-smoke")
    assert result.content == "done"
    assert session["session_id"] == "lightworker-smoke"
    assert any(event["type"] == "message.received" for event in session["events"])
    assert any(event["type"] == "turn.completed" for event in session["events"])


def test_native_runtime_lifecycle_syncs_followup_steering_and_goal(tmp_path: Path):
    factory = AgentFactory(
        ModelConfig(model="fake-model", api_key="test-key"),
        state_dir=tmp_path / "state",
        runtime=RuntimeConfig(),
    )
    lifecycle = NativeRuntimeLifecycle(
        session_id="native-lifecycle",
        run_id="followup-run",
        objective="分析新增资料",
        acceptance_criteria=["给出有依据的结论"],
        parent_run_id="root-run",
        root_run_id="root-run",
        queue_item_id="queue-message-1",
        local_goal_id="local-goal",
    )

    def inject_steering(ctx: HookContext) -> HookDecision:
        return HookDecision.replace(
            {
                "params": dict(ctx.payload.get("params") or {}),
                "lightworker_steering": ["优先核对库存数据"],
            }
        )

    agent = factory.worker(
        allowed_tools=set(),
        extra_hooks=[
            PolicyHook(inject_steering, phases={"before_model_request"}),
            lifecycle.hook(),
        ],
    )
    lifecycle.bind(agent)

    class StaticCompletions:
        def create(self, **kwargs: object) -> object:
            del kwargs
            message = SimpleNamespace(content="analysis complete", tool_calls=None)
            usage = SimpleNamespace(prompt_tokens=2, completion_tokens=1, total_tokens=3)
            return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)

    agent.client = SimpleNamespace(chat=SimpleNamespace(completions=StaticCompletions()))

    result = agent.run(
        "follow-up prompt",
        session_id="native-lifecycle",
        result_format="object",
        trace=True,
        use_skills=False,
    )
    snapshot = lifecycle.finalize(
        factory.session_store,
        RunStatus.SUCCEEDED,
        budget_limits=factory.budget_limits,
    )

    assert result.content == "analysis complete"
    assert snapshot is not None
    assert snapshot["goals"][0]["status"] == "completed"
    assert snapshot["goals"][0]["metadata"]["lightworker_goal_id"] == "local-goal"
    assert [item["type"] for item in snapshot["inbox"]] == ["followup", "steering"]
    assert all(item["status"] == "completed" for item in snapshot["inbox"])
    assert snapshot["budget"]["limits"]["model_calls"] == 32


def test_native_runtime_lifecycle_pauses_and_resumes_blocked_goal(tmp_path: Path):
    factory = AgentFactory(
        ModelConfig(model="fake-model", api_key="test-key"),
        state_dir=tmp_path / "state",
        runtime=RuntimeConfig(),
    )

    def run_once(lifecycle: NativeRuntimeLifecycle, reply: str) -> None:
        agent = factory.worker(allowed_tools=set(), extra_hooks=[lifecycle.hook()])
        lifecycle.bind(agent)

        class StaticCompletions:
            def create(self, **kwargs: object) -> object:
                del kwargs
                message = SimpleNamespace(content=reply, tool_calls=None)
                return SimpleNamespace(choices=[SimpleNamespace(message=message)])

        agent.client = SimpleNamespace(chat=SimpleNamespace(completions=StaticCompletions()))
        agent.run("continue", session_id="resume-session", use_skills=False)

    first = NativeRuntimeLifecycle(
        session_id="resume-session",
        run_id="resumable-run",
        objective="完成长任务",
        acceptance_criteria=["完成"],
    )
    run_once(first, "paused")
    paused = first.finalize(
        factory.session_store,
        RunStatus.PAUSED,
        "user paused",
        budget_limits=factory.budget_limits,
    )

    second = NativeRuntimeLifecycle(
        session_id="resume-session",
        run_id="resumable-run",
        objective="完成长任务",
        acceptance_criteria=["完成"],
    )
    run_once(second, "done")
    completed = second.finalize(
        factory.session_store,
        RunStatus.SUCCEEDED,
        budget_limits=factory.budget_limits,
    )

    assert paused is not None and paused["goals"][0]["status"] == "blocked"
    assert completed is not None and completed["goals"][0]["status"] == "completed"
    event_types = [event["type"] for event in completed["session"]["events"]]
    assert "session.paused" in event_types
    assert "session.resumed" in event_types
