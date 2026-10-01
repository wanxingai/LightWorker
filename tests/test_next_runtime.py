from __future__ import annotations

import json
import multiprocessing
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from LightAgent import AgentRuntime, ContextBudget, Session

from lightworker.agents import AgentFactory
from lightworker.config import WorkerConfig
from lightworker.context import TaskContextCompactor
from lightworker.continuation import ContinuableAgents
from lightworker.control import ControlStore
from lightworker.evidence import EvidenceStore
from lightworker.jobs import JobInterrupted, JobRegistry
from lightworker.loop_modes import CodeModeTools, WorkflowTools
from lightworker.message_queue import ConversationMessageQueue
from lightworker.models import ApprovalPolicy, RunRecord, RunStatus, ToolCategory
from lightworker.plugins import PluginManager
from lightworker.providers import ProviderComposition
from lightworker.sandbox_helper import HelperError, ptc_eval
from lightworker.schedules import ScheduleStore
from lightworker.sessions import AtomicSessionStore, SequenceConflict
from lightworker.storage import RunStore
from lightworker.tool_protocol import ApprovalBroker, EventLog, ToolCatalog, tool_info
from lightworker.web import TaskScheduler, create_app


def _append_in_process(state_dir, index):
    store = RunStore(Path(state_dir))
    for number in range(5):
        store.session("parallel").append("test", {"worker": index, "number": number})
        ConversationMessageQueue(store).enqueue("parallel", f"{index}-{number}")


def make_store(tmp_path):
    store = RunStore(tmp_path / "state")
    store.create(
        RunRecord(
            run_id="task",
            task="研究并编写脚本",
            repo=str(tmp_path),
            workspace=str(tmp_path),
            status=RunStatus.SUCCEEDED,
        )
    )
    return store


def catalog(store):
    events = EventLog(store, "task")
    broker = ApprovalBroker(store, "task", events)
    return ToolCatalog(broker=broker, events=events), broker


def wait_job(registry, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = registry.get(job_id)
        if job["status"] not in {"pending", "running"}:
            return job
        time.sleep(0.01)
    raise AssertionError(registry.get(job_id))


def test_multiprocess_session_and_queue_are_atomic(tmp_path):
    store = RunStore(tmp_path / "state")
    store.session("parallel").append("start", {})
    processes = [
        multiprocessing.get_context("spawn").Process(
            target=_append_in_process, args=(str(store.state_dir), i)
        )
        for i in range(3)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(15)
        assert process.exitcode == 0
    events = store.session("parallel").events()
    assert len([e for e in events if e.type == "test"]) == 15
    assert [e.sequence for e in events] == list(range(1, len(events) + 1))
    queue = ConversationMessageQueue(store)
    assert len(queue.active("parallel")) == 15
    item = queue.active("parallel")[0]
    with ThreadPoolExecutor(max_workers=3) as pool:
        claims = list(pool.map(lambda _: queue.claim("parallel", item["id"], "next"), range(3)))
    assert len([claim for claim in claims if claim]) == 1


def test_stale_session_save_merges_and_cas_idempotency(tmp_path):
    native = AtomicSessionStore(tmp_path / "sessions.db")
    native.create(Session(session_id="s"))
    first, second = native.get("s"), native.get("s")
    first.append("a", {})
    second.append("b", {})
    native.save(first)
    native.save(second)
    assert [e.type for e in native.get("s").events] == ["a", "b"]
    event = native.append_if_sequence("s", "c", {}, expected_sequence=2, idempotency_key="once")
    assert native.append_if_sequence("s", "c", {}, idempotency_key="once").event_id == event.event_id
    with pytest.raises(SequenceConflict):
        native.append_if_sequence("s", "d", {}, expected_sequence=2)


def test_session_survives_deleted_or_corrupt_cache(tmp_path):
    store = make_store(tmp_path)
    store.write_json("task", "goal.json", {"objective": "keep"})
    store.artifact_path("task", "run.json").unlink()
    store.artifact_path("task", "goal.json").write_text("broken")
    assert store.load("task").task == "研究并编写脚本"
    assert store.read_json("task", "goal.json")["objective"] == "keep"
    assert [r.run_id for r in store.list()] == ["task"]


def test_job_fencing_outputs_and_restart(tmp_path):
    store = make_store(tmp_path)
    registry = JobRegistry(store, "task", lease_seconds=0.02)
    job = registry.create("fixture", {}, idempotency_key="same")
    assert registry.create("fixture", {}, idempotency_key="same")["job_id"] == job["job_id"]
    old = registry.claim(job["job_id"])
    time.sleep(0.03)
    cold = JobRegistry(store, "task")
    assert cold.recover()[0]["status"] == "interrupted"
    fresh = cold.claim(job["job_id"])
    with pytest.raises(JobInterrupted):
        registry.finish(job["job_id"], old["lease_epoch"], result="stale")
    cold.output(job["job_id"], "new", epoch=fresh["lease_epoch"])
    cold.control(job["job_id"], "cancel")
    with pytest.raises(JobInterrupted):
        cold.output(job["job_id"], "stale", epoch=fresh["lease_epoch"])
    assert cold.get(job["job_id"])["output"] == [{"cursor": 1, "value": "new"}]


def test_custom_jobs_do_not_break_native_runtime_replay(tmp_path):
    store = make_store(tmp_path)
    registry = JobRegistry(store, "task")
    job = registry.create("terminal", {"argv": ["python", "app.py"]})
    claim = registry.claim(job["job_id"])
    registry.output(job["job_id"], "fixture", epoch=claim["lease_epoch"])
    registry.finish(job["job_id"], claim["lease_epoch"], result="done")
    runtime = AgentRuntime(session_store=store.session("task").store)
    assert runtime.open_session("task").session_id == "task"
    assert runtime.jobs.list() == []  # Extension records are not native JobRecord schemas.
    assert any(e.type == "lightworker.job.completed" for e in store.session("task").events())


def test_real_lightagent_model_loop_can_replay_extended_session(tmp_path, monkeypatch):
    from openai.types.chat import ChatCompletion

    store = make_store(tmp_path)
    JobRegistry(store, "task").create("task", {"action": "run"})
    config = WorkerConfig(state_dir=store.state_dir, model={"model": "test", "api_key": "test-only"})
    factory = AgentFactory(config.model, runtime=config.runtime, state_dir=store.state_dir)
    worker = factory.worker(allowed_tools=set(), extra_hooks=[])
    completion = ChatCompletion(
        id="test",
        object="chat.completion",
        created=1,
        model="test",
        choices=[
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "verified"}}
        ],
    )
    monkeypatch.setattr(worker.client.chat.completions, "create", lambda **kwargs: completion)
    result = worker.run("No tools; say verified.", tools=[], session_id="task", result_format="object")
    assert result.content == "verified" and not result.error
    assert any(e.type == "turn.completed" for e in store.session("task").events())


def test_real_lightagent_multiturn_tools_accept_sdk_messages(tmp_path, monkeypatch):
    from openai.types.chat import ChatCompletion

    store = make_store(tmp_path)
    config = WorkerConfig(state_dir=store.state_dir, model={"model": "test", "api_key": "test-only"})
    worker = AgentFactory(config.model, runtime=config.runtime, state_dir=store.state_dir).worker(
        allowed_tools={"fixture"}, extra_hooks=[]
    )

    @tool_info("fixture", "read fixture", [])
    def fixture():
        return "fixture result"

    messages = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call1", "type": "function", "function": {"name": "fixture", "arguments": "{}"}}
            ],
        },
        {"role": "assistant", "content": "verified tool"},
    ]
    completions = iter(
        ChatCompletion(
            id=str(index),
            object="chat.completion",
            created=1,
            model="test",
            choices=[
                {"index": 0, "finish_reason": "tool_calls" if index == 0 else "stop", "message": message}
            ],
        )
        for index, message in enumerate(messages)
    )
    monkeypatch.setattr(worker.client.chat.completions, "create", lambda **kwargs: next(completions))
    result = worker.run(
        "Use fixture then summarize.", tools=[fixture], session_id="task", result_format="object"
    )
    assert result.content == "verified tool" and not result.error
    assert any(e.type == "tool.completed" for e in store.session("task").events())


def test_compaction_keeps_latest_input_and_complete_tool_groups(tmp_path):
    store = make_store(tmp_path)
    store.write_json("task", "goal.json", {"objective": "preserve goal"})
    store.write_json(
        "task",
        "approvals.json",
        {"requests": [{"tool": "write", "status": "pending", "fingerprint": "exact"}]},
    )
    messages = [{"role": "system", "content": "instructions"}, {"role": "user", "content": "initial"}]
    for index in range(8):
        messages.extend(
            [
                {
                    "role": "assistant",
                    "content": "x" * 2000,
                    "tool_calls": [
                        {
                            "id": f"call{index}",
                            "type": "function",
                            "function": {"name": "read", "arguments": "{}"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": f"call{index}", "content": "y" * 500},
            ]
        )
    messages.append({"role": "user", "content": "latest instruction"})
    compacted = TaskContextCompactor(store, "task").compact_to_budget(
        messages, ContextBudget(max_tokens=1400, reserved_output_tokens=100)
    )
    assert compacted.removed_count > 0
    assert any(m.get("content") == "latest instruction" for m in compacted.messages)
    assert "preserve goal" in compacted.summary and "exact" in compacted.summary
    calls = {c["id"] for m in compacted.messages for c in m.get("tool_calls", [])}
    assert all(m["tool_call_id"] in calls for m in compacted.messages if m["role"] == "tool")
    assert store.read_json("task", "context-state.json")["removed_count"] > 0


def test_parent_cancel_blocks_child_job_completion(tmp_path):
    store = make_store(tmp_path)
    registry = JobRegistry(store, "task")
    job = registry.create("fixture", {})

    def operation(context):
        ControlStore(store, "task").set_state("cancelled")
        return "must not publish"

    registry.start(job["job_id"], operation).result(5)
    assert registry.get(job["job_id"])["status"] == "cancelled"
    assert registry.get(job["job_id"])["result"] is None


def test_native_provider_preserves_exact_approval_and_rejects_unguarded(tmp_path):
    store = make_store(tmp_path)
    calls = []

    @tool_info(
        "write",
        "write fixture",
        [{"name": "value", "type": "string", "required": True}],
        is_read_only=False,
        is_write=True,
        approval_policy=ApprovalPolicy.ALWAYS,
    )
    def write(value):
        calls.append(value)
        return json.dumps({"ok": True})

    wrapped, broker = catalog(store)
    providers = ProviderComposition(store=store, run_id="task")
    with pytest.raises(ValueError, match="guarded"):
        providers.compose([write])
    tool = providers.compose([wrapped.wrap(write)])[0]
    request = json.loads(tool(value="a"))
    assert request["approval_required"] and not calls
    broker.decide(request["request_id"], "approved")
    assert json.loads(tool(value="a"))["ok"] and calls == ["a"]
    assert json.loads(tool(value="b"))["approval_required"] and calls == ["a"]
    providers.close()


def test_readonly_workspace_fallback_does_not_enable_shell(tmp_path):
    store = make_store(tmp_path)
    wrapped, _ = catalog(store)

    @tool_info("read_file", "bound read", [], sandbox_required=True)
    def read():
        return "safe isolated read"

    @tool_info(
        "shell_exec",
        "docker only",
        [],
        category=ToolCategory.SHELL,
        sandbox_required=True,
        is_read_only=False,
        is_write=True,
    )
    def shell():
        pytest.fail("must not run without Docker")

    providers = ProviderComposition(store=store, run_id="task", sandbox_available=False)
    tools = {t.tool_info["tool_name"]: t for t in providers.compose(wrapped.wrap_all([read, shell]))}
    assert tools["read_file"]() == "safe isolated read"
    with pytest.raises(PermissionError, match="Docker"):
        tools["shell_exec"]()
    providers.close()


def test_large_json_remains_parseable_and_full_evidence_is_saved(tmp_path):
    store = make_store(tmp_path)

    @tool_info("http_get", "fixture", [], output_limit_bytes=1024)
    def fetch():
        return json.dumps({"url": "https://example.com/data", "content": "证据" * 5000})

    wrapped, _ = catalog(store)
    result = json.loads(wrapped.wrap(fetch)())
    assert result["truncated"]
    source = EvidenceStore(store, "task").list()[0]
    assert len(source["content"]) == 10000
    assert source["evidence_kind"] == "retrieved"
    assert store.read_json("task", "artifacts.json")["artifacts"]


def test_workflow_checkpoints_do_not_reexecute_completed_step(tmp_path):
    store = make_store(tmp_path)
    calls = []

    @tool_info("step", "fixture", [{"name": "value", "type": "integer", "required": True}])
    def step(value):
        calls.append(value)
        return json.dumps({"ok": value == 1})

    workflow = WorkflowTools(store, "task", [step])
    steps = [
        {"id": "a", "tool": "step", "arguments": {"value": 1}},
        {"id": "b", "tool": "step", "arguments": {"value": 2}},
    ]
    assert not json.loads(workflow.workflow_run("flow", steps))["ok"]
    assert not json.loads(WorkflowTools(store, "task", [step]).workflow_run("flow", steps))["ok"]
    assert calls == [1, 2, 2]
    with pytest.raises(ValueError, match="changed"):
        workflow.workflow_run("flow", steps[:1])


def test_docker_code_bridge_replays_prior_calls_without_side_effects(tmp_path):
    store = make_store(tmp_path)
    calls = []

    @tool_info("write", "fixture", [{"name": "value", "type": "integer", "required": True}])
    def write(value):
        calls.append(value)
        return {"ok": True, "value": value}

    class Sandbox:
        def call(self, action, params, **kwargs):
            assert action == "ptc_eval"
            return ptc_eval(params, {})

    source = "a = call('write', {'value': 1})\nresult = call('write', {'value': 2})"
    code = CodeModeTools(Sandbox(), [write], store, "task")
    assert json.loads(code.run_code(source))["ok"]
    assert json.loads(CodeModeTools(Sandbox(), [write], store, "task").run_code(source))["ok"]
    assert calls == [1, 2]


@pytest.mark.parametrize(
    "source",
    [
        "import os",
        "result = ().__class__",
        "while True: pass",
        "result = open('/etc/passwd')",
        "result = list(range(10000))",
    ],
)
def test_code_language_rejects_escape_and_unbounded_calls(source):
    with pytest.raises(HelperError):
        ptc_eval({"source": source, "results": []}, {})


def test_subagents_keep_fifo_turns_and_session_after_cold_rebuild(tmp_path):
    store = make_store(tmp_path)
    seen = []

    class Child:
        def run(self, message, **kwargs):
            seen.append((message, kwargs["session_id"]))
            store.session(kwargs["session_id"]).append("assistant.message", {"content": message})
            return SimpleNamespace(content=f"answer:{message}", error=None)

    class Factory:
        def specialist(self, role, **kwargs):
            return Child()

    children = ContinuableAgents(store=store, run_id="task", agent_factory=Factory(), tools=[])
    value = json.loads(children.spawn_agent("research", "first"))
    assert wait_job(children.jobs, value["job_id"])["status"] == "completed"
    children.close()
    cold = ContinuableAgents(store=store, run_id="task", agent_factory=Factory(), tools=[])
    next_turn = json.loads(cold.send_message(value["agent_id"], "second"))
    assert wait_job(cold.jobs, next_turn["job_id"])["status"] == "completed"
    assert [message for message, _ in seen] == ["first", "second"]
    assert seen[0][1] == seen[1][1]
    assert len(store.session(seen[0][1]).events()) == 2
    cold.close()


def test_cold_pending_child_turn_activates_after_completed_job(tmp_path):
    store = make_store(tmp_path)

    class Factory:
        def specialist(self, role, **kwargs):
            return SimpleNamespace(run=lambda message, **kwargs: SimpleNamespace(content=message, error=None))

    children = ContinuableAgents(store=store, run_id="task", agent_factory=Factory(), tools=[])
    value = json.loads(children.spawn_agent("research", "first"))
    wait_job(children.jobs, value["job_id"])
    children.close()
    with store.transaction("task"):
        state = store.read_json("task", "agents.json")
        state["agents"][value["agent_id"]]["messages"].append(
            {"id": "cold-turn", "message": "next turn", "status": "pending"}
        )
        store.write_json("task", "agents.json", state)
    cold = ContinuableAgents(store=store, run_id="task", agent_factory=Factory(), tools=[])
    cold.recover()
    agent = cold._load()["agents"][value["agent_id"]]
    assert agent["job_id"] != value["job_id"]
    assert wait_job(cold.jobs, agent["job_id"])["status"] == "completed"
    assert cold._load()["agents"][value["agent_id"]]["result"] == "next turn"
    cold.close()


def test_child_message_api_reactivates_completed_parent(tmp_path):
    store = make_store(tmp_path)
    state = {"agents": {"child": {"agent_id": "child", "status": "idle", "messages": []}}}
    store.write_json("task", "agents.json", state)
    resumed = []

    class Runner:
        def __init__(self, settings):
            pass

        def resume(self, run_id):
            resumed.append(run_id)

    app = create_app(WorkerConfig(state_dir=store.state_dir), runner_factory=Runner)
    with TestClient(app) as client:
        response = client.post("/api/runs/task/agents/child/message", json={"message": "continue"})
        assert response.status_code == 200 and response.json()["status"] == "resuming_parent"
        app.state.task_manager._futures["task"].result(5)
        assert resumed == ["task"]
        assert (
            store.read_json("task", "agents.json")["agents"]["child"]["messages"][0]["message"] == "continue"
        )


def test_schedule_claims_are_fenced_and_occurrence_id_is_stable(tmp_path):
    store = make_store(tmp_path)
    schedules = ScheduleStore(store)
    item = schedules.create(task="refresh", source_run_id="task", next_at=10, interval_seconds=60)
    first = schedules.due(now=11)[0]
    assert not ScheduleStore(store).due(now=12)
    second = schedules.due(now=72)[0]
    assert first["pending_run_id"] == second["pending_run_id"]
    with pytest.raises(ValueError, match="stale"):
        schedules.dispatched(item["schedule_id"], epoch=first["lease_epoch"])
    schedules.dispatched(item["schedule_id"], epoch=second["lease_epoch"])
    assert schedules.list()[0]["occurrence"] == 1


def test_plugins_pin_every_file_and_only_apply_trusted_presets(tmp_path):
    store = make_store(tmp_path)
    directory = tmp_path / "plugins" / "sample"
    directory.mkdir(parents=True)
    manifest = {
        "name": "sample",
        "version": "1",
        "workflows": {"read": [{"id": "a", "tool": "git_status", "arguments": {}}]},
    }
    (directory / "plugin.json").write_text(json.dumps(manifest))
    manager = PluginManager(store, [directory.parent])
    config = WorkerConfig(state_dir=store.state_dir)
    assert not manager.apply(config).runtime.workflow_presets
    plugin = manager.discover()[0]
    manager.trust(directory, plugin["digest"])
    assert "sample_read" in manager.apply(config).runtime.workflow_presets
    (directory / "new.txt").write_text("changed")
    assert not manager.discover()[0]["trusted"]
    with pytest.raises(ValueError, match="changed"):
        manager.trust(directory, plugin["digest"])


def test_task_scheduler_is_durable_and_does_not_deadlock_with_child_jobs(tmp_path):
    store = make_store(tmp_path)
    scheduler = TaskScheduler(max_workers=1, store=store)
    scheduler.submit("task", "run", lambda: {"done": True})
    scheduler._futures["task"].result(5)
    assert scheduler.status("task")["state"] == "completed"
    cold = TaskScheduler(store=store)
    assert cold.status("task")["state"] == "completed"
    scheduler.close()
    cold.close()


def test_new_control_plane_endpoints_and_origin_guard(tmp_path):
    store = make_store(tmp_path)
    app = create_app(WorkerConfig(state_dir=store.state_dir))
    with TestClient(app) as client:
        assert client.get("/api/runs/task/session").json()["session_id"] == "task"
        assert client.post("/api/runs/task/checkpoint", json={}).status_code == 200
        assert client.get("/api/capabilities").json()["runtime"]["durable_jobs"]
        assert client.get("/api/runs/task/artifact-manifest").status_code == 200
        assert (
            client.post(
                "/api/runs/task/checkpoint", json={}, headers={"Origin": "https://malicious.example"}
            ).status_code
            == 403
        )
        assert client.get("/api/plugins").json() == []
