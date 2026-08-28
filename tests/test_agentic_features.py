from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from LightAgent import RunResult

import lightworker.browser_tools as browser_module
from lightworker.agents import AgentFactory
from lightworker.browser_tools import BrowserTools
from lightworker.config import BrowserConfig, ModelConfig, RAGConfig, SkillsConfig, WorkerConfig
from lightworker.context import ContextCompressor
from lightworker.memory import WorkspaceMemory
from lightworker.models import RunRecord, RunStatus, TaskSpec, VerificationCommand
from lightworker.rag import RAGIndex
from lightworker.sandbox import DockerSandbox
from lightworker.sandbox_helper import HelperError, validate_shell_command
from lightworker.skills import SkillRegistry
from lightworker.storage import RunStore
from lightworker.subagents import SubagentManager
from lightworker.tool_protocol import EventLog
from lightworker.workflow import CodingTaskRunner

PATCH = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
 def answer():
-    return 1
+    return 2
"""


class AgenticSandbox:
    instances: list[AgenticSandbox] = []

    def __init__(self, **kwargs: Any):
        self.diff = ""
        self.started = False
        self.shell_calls: list[list[str]] = []
        self.__class__.instances.append(self)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def install_requirements(self, requirements: list[str], *, timeout: int = 900) -> dict[str, Any]:
        return {"ok": True, "requirements": requirements, "exit_code": 0, "frozen": requirements}

    def call(self, action: str, params: dict[str, Any], *, timeout: int | None = None) -> dict[str, Any]:
        if action == "apply_patch":
            self.diff = params["patch"]
            return {"ok": True, "changed_files": ["app.py"], "status": " M app.py\n"}
        if action == "git_diff":
            return {"ok": True, "diff": self.diff}
        if action == "git_status":
            return {"ok": True, "status": " M app.py\n" if self.diff else ""}
        if action == "run_command":
            return {
                "ok": True,
                "argv": params["argv"],
                "exit_code": 0,
                "timed_out": False,
                "duration_ms": 2,
                "output": "1 passed",
                "full_output": "1 passed",
            }
        if action == "shell_exec":
            self.shell_calls.append(list(params["argv"]))
            return {
                "ok": True,
                "argv": params["argv"],
                "exit_code": 0,
                "timed_out": False,
                "duration_ms": 1,
                "output": "ok",
                "full_output": "ok",
            }
        if action in {"list_files", "read_file", "search_text"}:
            return {"ok": True, "files": ["app.py"], "content": "fixture", "matches": ""}
        if action == "health":
            return {"ok": True}
        raise AssertionError(action)


class DynamicAgent:
    name = "LightWorker"

    def __init__(self, behavior: str):
        self.behavior = behavior

    def run(self, query: str, **kwargs: Any) -> RunResult:
        # LightAgent currently copies run(metadata=...) into provider parameters,
        # so LightWorker runtime identifiers must remain in Session/run fields.
        assert "metadata" not in kwargs
        assert kwargs.get("session_id")
        tools = {tool.tool_info["tool_name"]: tool for tool in kwargs.get("tools", [])}
        if self.behavior == "patch":
            tools["apply_patch"](patch=PATCH)
        elif self.behavior == "shell":
            tools["shell_exec"](argv=["python", "script.py"], timeout_seconds=30)
        return RunResult(
            content="动态任务已完成",
            usage={"total_tokens": 100},
            trace=[{"type": "model_request", "data": {"request_index": 1}}],
        )


class DynamicFactory:
    def __init__(self, behavior: str):
        self.behavior = behavior

    def worker(self, **kwargs: Any) -> DynamicAgent:
        return DynamicAgent(self.behavior)

    def specialist(self, role: str, **kwargs: Any) -> DynamicAgent:
        return DynamicAgent("answer")


class SessionExportingAgent(DynamicAgent):
    def export_session(self, session_id: str) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "metadata": {},
            "events": [{"sequence": 1, "type": "session.started"}],
        }


class SessionExportingFactory(DynamicFactory):
    def worker(self, **kwargs: Any) -> SessionExportingAgent:
        return SessionExportingAgent(self.behavior)


class NativeSessionFactory(AgentFactory):
    def worker(self, **kwargs: Any) -> Any:
        agent = super().worker(**kwargs)

        class StaticCompletions:
            def create(self, **request: Any) -> Any:
                del request
                message = SimpleNamespace(content="原生运行时任务完成", tool_calls=None)
                usage = SimpleNamespace(prompt_tokens=2, completion_tokens=1, total_tokens=3)
                return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)

        agent.client = SimpleNamespace(chat=SimpleNamespace(completions=StaticCompletions()))
        return agent


class RecoveringFactory(DynamicFactory):
    def __init__(self):
        super().__init__("answer")
        self.worker_calls = 0

    def worker(self, **kwargs: Any) -> DynamicAgent:
        self.worker_calls += 1
        if self.worker_calls == 1:
            return ExhaustedAgent("answer")
        return DynamicAgent("answer")


class ExhaustedAgent(DynamicAgent):
    def run(self, query: str, **kwargs: Any) -> RunResult:
        assert "metadata" not in kwargs
        return RunResult(
            content="Failed to generate a valid response.",
            trace=[{"type": "run_end", "data": {"success": False, "error": "max_retry_reached"}}],
        )


class EvidenceExhaustedAgent:
    name = "evidence-exhausted"

    def run(self, query: str, **kwargs: Any) -> RunResult:
        return RunResult(
            content="Failed to generate a valid response.",
            trace_id="first-attempt",
            trace=[
                {
                    "type": "tool_result",
                    "data": {
                        "name": "http_get",
                        "output": '{"ok":true,"url":"https://example.com/source","body":"evidence"}',
                    },
                },
                {
                    "type": "run_end",
                    "data": {"success": False, "error": "max_retry_reached"},
                },
            ],
        )


class CapturingFinalizerAgent:
    name = "capturing-finalizer"

    def __init__(self, prompts: list[str]):
        self.prompts = prompts

    def run(self, query: str, **kwargs: Any) -> RunResult:
        self.prompts.append(query)
        assert kwargs["tools"] == []
        return RunResult(
            content="Recovered evidence report with https://example.com/source",
            trace_id="second-attempt",
            trace=[{"type": "run_end", "data": {"success": True}}],
        )


class SubagentRecoveryFactory:
    def __init__(self):
        self.finalizer_prompts: list[str] = []

    def specialist(self, role: str, *, allowed_tools: set[str]) -> Any:
        if allowed_tools:
            return EvidenceExhaustedAgent()
        return CapturingFinalizerAgent(self.finalizer_prompts)


def agentic_config(tmp_path: Path, *, shell: bool = False) -> WorkerConfig:
    return WorkerConfig(
        state_dir=tmp_path / "state",
        model={"model": "fake"},
        analysis={"enabled": False},
        shell={"enabled": shell},
        browser={"enabled": False},
        memory={"enabled": False},
        skills={"enabled": False},
        mcp={"enabled": False},
        rag={"enabled": False},
    )


def test_agentic_runtime_combines_patch_verification_goal_and_events(git_repo: Path, tmp_path: Path):
    AgenticSandbox.instances.clear()
    runner = CodingTaskRunner(
        agentic_config(tmp_path),
        agent_factory=DynamicFactory("patch"),
        sandbox_factory=AgenticSandbox,
    )
    spec = TaskSpec(
        repo=git_repo,
        task="修复 answer",
        verification=[VerificationCommand(name="pytest", argv=["pytest", "-q"])],
    )

    record = runner.run(spec)

    assert record.status == RunStatus.SUCCEEDED
    assert record.metadata["execution_mode"] == "agentic"
    assert runner.store.artifact_path(spec.run_id, "changes.patch").read_text() == PATCH
    assert runner.store.artifact_path(spec.run_id, "goal.json").is_file()
    events = runner.store.artifact_path(spec.run_id, "events.jsonl").read_text()
    assert "tool_started" in events
    assert "verification_completed" in events
    assert "agentic_run_completed" in events


def test_agentic_runtime_exports_native_lightagent_session(git_repo: Path, tmp_path: Path):
    runner = CodingTaskRunner(
        agentic_config(tmp_path),
        agent_factory=SessionExportingFactory("answer"),
        sandbox_factory=AgenticSandbox,
    )
    spec = TaskSpec(repo=git_repo, task="分析资料")

    record = runner.run(spec)

    assert record.status == RunStatus.SUCCEEDED
    assert record.metadata["lightagent_session_id"] == spec.run_id
    session = runner.store.read_json(spec.run_id, "lightagent-session.json")
    assert session["session_id"] == spec.run_id
    assert session["events"][0]["type"] == "session.started"
    events = runner.store.artifact_path(spec.run_id, "events.jsonl").read_text()
    assert "lightagent_session_exported" in events


def test_agentic_runtime_synchronizes_native_goal_and_followup_inbox(
    git_repo: Path,
    tmp_path: Path,
):
    config = agentic_config(tmp_path)
    factory = NativeSessionFactory(
        ModelConfig(model="fake-model", api_key="test-key"),
        state_dir=config.state_dir,
        runtime=config.runtime,
    )
    runner = CodingTaskRunner(
        config,
        agent_factory=factory,
        sandbox_factory=AgenticSandbox,
    )
    spec = TaskSpec(
        repo=git_repo,
        task="继续分析资料",
        parent_run_id="root-run",
        root_run_id="root-run",
        queue_item_id="queued-message",
    )

    record = runner.run(spec)

    assert record.status == RunStatus.SUCCEEDED
    runtime = runner.store.read_json(spec.run_id, "lightagent-runtime.json")
    assert runtime["goals"][0]["status"] == "completed"
    assert runtime["inbox"][0]["message_id"] == "queued-message"
    assert runtime["inbox"][0]["status"] == "completed"
    assert runtime["budget"]["usage"]["model_calls"] == 1
    events = runner.store.artifact_path(spec.run_id, "events.jsonl").read_text()
    assert "lightagent_runtime_synchronized" in events


def test_exact_shell_approval_is_durable_and_resumable(git_repo: Path, tmp_path: Path):
    AgenticSandbox.instances.clear()
    runner = CodingTaskRunner(
        agentic_config(tmp_path, shell=True),
        agent_factory=DynamicFactory("shell"),
        sandbox_factory=AgenticSandbox,
    )
    spec = TaskSpec(repo=git_repo, task="在容器里运行脚本")

    waiting = runner.run(spec)
    approvals = runner.store.read_json(spec.run_id, "approvals.json")
    request_id = approvals["requests"][0]["request_id"]

    assert waiting.status == RunStatus.WAITING_APPROVAL
    assert not AgenticSandbox.instances[0].shell_calls

    completed = runner.decide_approval(spec.run_id, request_id, "approved", "允许这组参数")

    assert completed.status == RunStatus.SUCCEEDED
    assert AgenticSandbox.instances[-1].shell_calls == [["python", "script.py"]]


def test_analysis_degrades_to_read_only_without_docker(
    git_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(DockerSandbox, "daemon_available", staticmethod(lambda: False))
    runner = CodingTaskRunner(
        agentic_config(tmp_path),
        agent_factory=DynamicFactory("answer"),
    )
    spec = TaskSpec(repo=git_repo, task="分析已有资料并回答，不修改文件")

    record = runner.run(spec)

    assert record.status == RunStatus.SUCCEEDED
    assert "Docker is unavailable" in record.metadata["degraded_mode"]
    manifest = runner.store.read_json(spec.run_id, "tool-manifest.json")
    names = {item["tool_name"] for item in manifest}
    assert "read_file" in names
    assert "apply_patch" not in names
    assert "shell_exec" not in names


def test_agentic_runtime_recovers_tool_loop_exhaustion_with_tool_free_finalizer(
    git_repo: Path,
    tmp_path: Path,
):
    factory = RecoveringFactory()
    runner = CodingTaskRunner(
        agentic_config(tmp_path),
        agent_factory=factory,
        sandbox_factory=AgenticSandbox,
    )

    record = runner.run(TaskSpec(repo=git_repo, task="分析资料并给出结论"))

    assert record.status == RunStatus.SUCCEEDED
    assert factory.worker_calls == 2
    assert runner.store.artifact_path(record.run_id, "summary.md").read_text().strip() == "动态任务已完成"
    events = runner.store.artifact_path(record.run_id, "events.jsonl").read_text()
    assert "final_answer_recovery_started" in events
    assert '"valid": true' in events


def test_subagent_recovers_exhausted_tool_loop_from_captured_evidence(tmp_path: Path):
    store = RunStore(tmp_path / "state")
    run_id = "subagent-recovery"
    store.create(RunRecord(run_id=run_id, task="research", repo="/workspace"))
    events = EventLog(store, run_id)
    factory = SubagentRecoveryFactory()
    manager = SubagentManager(
        agent_factory=factory,
        tools=[],
        store=store,
        run_id=run_id,
        events=events,
    )

    result = json.loads(manager.delegate_task("research", "Collect current market evidence"))

    assert result["ok"] is True
    assert result["results"][0]["ok"] is True
    assert result["results"][0]["recovered"] is True
    assert result["results"][0]["error"] is None
    assert "https://example.com/source" in factory.finalizer_prompts[0]
    agent_id = result["results"][0]["agent_id"]
    assert store.artifact_path(run_id, f"subagents/{agent_id}/attempt-1-trace.jsonl").is_file()
    assert store.artifact_path(run_id, f"subagents/{agent_id}/attempt-2-trace.jsonl").is_file()
    tree = store.read_json(run_id, "agent-tree.json")
    assert tree["agents"][0]["status"] == "completed"
    assert tree["agents"][0]["recovered"] is True
    event_text = store.artifact_path(run_id, "events.jsonl").read_text()
    assert "subagent_recovery_started" in event_text
    assert "subagent_recovery_completed" in event_text


def test_browser_backend_runs_on_one_dedicated_thread_outside_asyncio_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    backend_threads: list[int] = []

    class ThreadBoundBackend:
        def __init__(self, config: BrowserConfig):
            backend_threads.append(threading.get_ident())

        def open(self, url: str) -> dict[str, Any]:
            backend_threads.append(threading.get_ident())
            return {"ok": True, "url": url}

        def close(self) -> None:
            backend_threads.append(threading.get_ident())

    monkeypatch.setattr(browser_module, "PlaywrightBackend", ThreadBoundBackend)
    tools = BrowserTools(
        config=BrowserConfig(),
        store=RunStore(tmp_path / "state"),
        run_id="browser-thread",
    )

    async def call_from_event_loop() -> dict[str, Any]:
        return json.loads(tools.browser_open("https://example.com/"))

    try:
        result = asyncio.run(call_from_event_loop())
    finally:
        tools.close()

    assert result["ok"] is True
    assert len(set(backend_threads)) == 1
    assert backend_threads[0] != threading.get_ident()


def test_rag_fts5_incremental_ingest_search_and_remove(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "market.md").write_text(
        "LPG demand is rising while propane inventories are falling.",
        encoding="utf-8",
    )
    index = RAGIndex(tmp_path / "state", scope="project", config=RAGConfig())

    first = index.ingest(workspace, ["market.md"])
    second = index.ingest(workspace, ["market.md"])
    found = index.search("propane inventories")

    assert first["indexed"] == 1
    assert second["documents"][0]["unchanged"] is True
    assert found[0]["path"] == "market.md"
    assert found[0]["citation"].startswith("[market.md#chunk-")
    assert index.remove("market.md") is True
    assert index.list_documents() == []


def test_rag_optional_embeddings_use_hybrid_search_and_incremental_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "fuel.md").write_text("Bottled fuel supply is tightening.", encoding="utf-8")
    (workspace / "garden.md").write_text("Garden planting notes.", encoding="utf-8")
    index = RAGIndex(
        tmp_path / "state",
        scope="project",
        config=RAGConfig(embeddings_enabled=True, embedding_model="fake-embedding"),
    )
    calls: list[list[str]] = []

    def fake_embed(texts: list[str]) -> list[list[float]]:
        calls.append(texts)
        return [
            [1.0, 0.0] if "bottled fuel" in text.lower() or text == "concept-vector" else [0.0, 1.0]
            for text in texts
        ]

    monkeypatch.setattr(index, "_embed", fake_embed)

    first = index.ingest(workspace, ["fuel.md", "garden.md"])
    second = index.ingest(workspace, ["fuel.md"])
    found = index.search("concept-vector")

    assert first["documents"][0]["embeddings"] == 1
    assert second["documents"][0]["unchanged"] is True
    assert len(calls) == 3  # two document batches and one query; unchanged ingest is cached
    assert found[0]["path"] == "fuel.md"
    assert "embedding" in found[0]["retrieval"]


def test_rag_embedding_failure_falls_back_to_fts5(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "market.md").write_text("Propane inventories are falling.", encoding="utf-8")
    index = RAGIndex(
        tmp_path / "state",
        scope="project",
        config=RAGConfig(embeddings_enabled=True),
    )

    def unavailable(_texts: list[str]) -> list[list[float]]:
        raise RuntimeError("embedding endpoint unavailable")

    monkeypatch.setattr(index, "_embed", unavailable)

    ingested = index.ingest(workspace, ["market.md"])
    found = index.search("propane")

    assert ingested["documents"][0]["ok"] is True
    assert ingested["documents"][0]["embedding_error"] == "embedding endpoint unavailable"
    assert found[0]["path"] == "market.md"
    assert found[0]["retrieval"] == "fts5"


def test_workspace_memory_requires_promotion_and_rejects_secrets(tmp_path: Path):
    memory = WorkspaceMemory(tmp_path / "state", candidate_ttl_days=7)
    candidate = memory.propose(
        scope="project",
        kind="project",
        content="Use pytest for verification",
        provenance="AGENTS.md",
        confidence=0.9,
    )

    assert memory.search(scope="project", query="pytest") == []
    memory.promote(candidate["id"])
    assert memory.search(scope="project", query="pytest")[0]["status"] == "promoted"
    with pytest.raises(ValueError, match="secret"):
        memory.propose(
            scope="project",
            kind="reference",
            content="api_key=supersecret",
            provenance="user",
        )


def test_context_compression_preserves_goal_decisions_and_recent_turns():
    turns = [
        {"user": f"request {index} " * 100, "assistant": f"result {index} " * 100} for index in range(10)
    ]
    result = ContextCompressor(context_window_tokens=300, compression_ratio=0.6).compress_turns(
        turns,
        objective="Complete the market report",
        acceptance_criteria=["Cite sources"],
        decisions=["Approved public web search"],
        keep_recent=2,
    )

    assert result.compressed is True
    assert "Complete the market report" in result.text
    assert "Approved public web search" in result.text
    assert "request 9" in result.text


def test_markdown_skill_precedence_and_conflicts(tmp_path: Path):
    workspace = tmp_path / "workspace"
    project = workspace / ".lightworker" / "skills" / "report"
    user = tmp_path / "user-skills" / "report"
    project.mkdir(parents=True)
    user.mkdir(parents=True)
    project.joinpath("SKILL.md").write_text(
        "---\nname: report\ndescription: Project report skill\n---\nProject instructions",
        encoding="utf-8",
    )
    user.joinpath("SKILL.md").write_text(
        "---\nname: report\ndescription: User report skill\n---\nUser instructions",
        encoding="utf-8",
    )
    registry = SkillRegistry(
        workspace=workspace,
        config=SkillsConfig(user_directories=[tmp_path / "user-skills"]),
    )

    registry.discover()

    assert registry.get("report").source == "project"
    assert registry.activate("report") == "Project instructions"
    assert len(registry.conflicts["report"]) == 2


def test_expanded_shell_still_blocks_inline_and_destructive_commands():
    policy = {
        "shell_allowed_programs": ["python", "git", "uv"],
        "max_shell_argv_items": 20,
    }
    validate_shell_command(["python", "script.py", "--check"], policy)
    validate_shell_command(["git", "log", "-1"], policy)
    with pytest.raises(HelperError, match="inline Python"):
        validate_shell_command(["python", "-c", "print(1)"], policy)
    with pytest.raises(HelperError, match="destructive"):
        validate_shell_command(["git", "reset", "--hard"], policy)
