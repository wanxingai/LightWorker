from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from LightAgent import RunResult

from lightworker.config import WorkerConfig
from lightworker.models import InstalledRequirement, RunStatus, TaskSpec, VerificationCommand
from lightworker.workflow import (
    CodingTaskRunner,
    _approve_edit_by_risk,
    _diff_requires_verification,
    _protected_patterns,
)

PLAN = json.dumps(
    {
        "task_type": "bugfix",
        "risk": "low",
        "summary": {"zh": "修复 answer", "en": "Fix answer"},
        "items": [
            {
                "id": "1",
                "description": {"zh": "修改返回值", "en": "Change return value"},
                "files": ["app.py"],
            }
        ],
        "verification": ["pytest -q"],
    },
    ensure_ascii=False,
)

REVIEW = json.dumps(
    {
        "summary": {"zh": "返回值已修复", "en": "The return value is fixed"},
        "changes": [{"zh": "修改 app.py", "en": "Updated app.py"}],
        "verification": [{"zh": "pytest 通过", "en": "pytest passed"}],
        "residual_risks": [],
    },
    ensure_ascii=False,
)

PATCH = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
 def answer():
-    return 1
+    return 2
"""


class FakeSandbox:
    instances: list[FakeSandbox] = []

    def __init__(self, **kwargs: Any):
        self.diff = ""
        self.verify_calls = 0
        self.started = False
        self.stopped = False
        self.installs: list[list[str]] = []
        self.__class__.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def install_requirements(self, requirements: list[str], *, timeout: int = 900):
        self.installs.append(requirements)
        return {"ok": True, "requirements": requirements, "exit_code": 0, "frozen": requirements}

    def call(self, action: str, params: dict[str, Any], *, timeout: int | None = None):
        if action == "apply_patch":
            self.diff = params["patch"]
            return {"ok": True, "changed_files": ["app.py"], "status": " M app.py\n"}
        if action == "run_command":
            self.verify_calls += 1
            exit_code = 1 if self.verify_calls == 1 else 0
            output = "failed assertion" if exit_code else "1 passed"
            return {
                "ok": True,
                "argv": params["argv"],
                "exit_code": exit_code,
                "timed_out": False,
                "duration_ms": 10,
                "output": output,
                "full_output": output,
            }
        if action == "git_diff":
            return {"ok": True, "diff": self.diff}
        if action == "git_status":
            return {"ok": True, "status": " M app.py\n" if self.diff else ""}
        if action in {"list_files", "read_file", "search_text"}:
            return {"ok": True, "content": "fixture", "files": ["app.py"], "matches": "app.py:1"}
        if action == "health":
            return {"ok": True}
        raise AssertionError(action)


class PlannerAgent:
    name = "PlannerAgent"

    def run(self, query: str, **kwargs: Any):
        if "required JSON schema" in query:
            return RunResult(content=PLAN)
        return RunResult(content="中文上下文 / English context")


class CoderAgent:
    name = "CodingAgent"

    def run(self, query: str, **kwargs: Any):
        apply_patch = next(tool for tool in kwargs["tools"] if tool.tool_info["tool_name"] == "apply_patch")
        apply_patch(PATCH)
        return RunResult(content="已修改 / changed")


class ReviewerAgent:
    name = "ReviewerAgent"

    def run(self, query: str, **kwargs: Any):
        return RunResult(content=REVIEW)


class FakeAgentFactory:
    def planner(self, **kwargs: Any):
        return PlannerAgent()

    def coder(self, **kwargs: Any):
        return CoderAgent()

    def reviewer(self, **kwargs: Any):
        return ReviewerAgent()


def test_full_flow_repairs_failed_verification_and_preserves_source(git_repo: Path, tmp_path: Path):
    FakeSandbox.instances.clear()
    config = WorkerConfig(state_dir=tmp_path / "state", model={"model": "fake"})
    runner = CodingTaskRunner(
        config,
        agent_factory=FakeAgentFactory(),
        sandbox_factory=FakeSandbox,
    )
    source_before = (git_repo / "app.py").read_text(encoding="utf-8")
    spec = TaskSpec(
        repo=git_repo,
        task="Fix answer",
        verification=[VerificationCommand(name="test", argv=["pytest", "-q"])],
        max_repairs=2,
    )

    record = runner.run(spec)

    assert record.status == RunStatus.SUCCEEDED
    assert record.verification[0].passed is True
    assert FakeSandbox.instances[0].verify_calls == 2
    assert FakeSandbox.instances[0].stopped is True
    assert (git_repo / "app.py").read_text(encoding="utf-8") == source_before
    run_dir = runner.store.run_dir(spec.run_id)
    assert (run_dir / "changes.patch").read_text(encoding="utf-8") == PATCH
    assert "实施计划" in (run_dir / "plan.md").read_text(encoding="utf-8")
    assert "Task Summary" in (run_dir / "summary.md").read_text(encoding="utf-8")
    assert (run_dir / "trace.jsonl").exists()


def test_flow_without_verification_is_needs_attention(git_repo: Path, tmp_path: Path):
    FakeSandbox.instances.clear()
    runner = CodingTaskRunner(
        WorkerConfig(state_dir=tmp_path / "state", model={"model": "fake"}),
        agent_factory=FakeAgentFactory(),
        sandbox_factory=FakeSandbox,
    )
    spec = TaskSpec(repo=git_repo, task="Fix answer", verification=[], max_repairs=2)

    record = runner.run(spec)

    assert record.status == RunStatus.NEEDS_ATTENTION
    assert FakeSandbox.instances[0].verify_calls == 0


def test_rerun_replays_recorded_dependencies(git_repo: Path, tmp_path: Path):
    FakeSandbox.instances.clear()
    runner = CodingTaskRunner(
        WorkerConfig(state_dir=tmp_path / "state", model={"model": "fake"}),
        agent_factory=FakeAgentFactory(),
        sandbox_factory=FakeSandbox,
    )
    spec = TaskSpec(
        repo=git_repo,
        task="Fix answer",
        verification=[VerificationCommand(name="test", argv=["pytest", "-q"])],
        max_repairs=2,
    )
    record = runner.run(spec)
    record.installed_requirements.append(InstalledRequirement(requested=["tomli==2.2.1"]))
    runner.store.save(record)

    runner.rerun_from_verify(spec.run_id)

    assert FakeSandbox.instances[-1].installs == [["tomli==2.2.1"]]


def test_empty_source_only_relaxes_new_project_manifest(git_repo: Path):
    config = WorkerConfig(protected_patterns=["pyproject.toml", ".env", ".github/workflows/**"])
    existing = TaskSpec(repo=git_repo, task="existing")
    empty = TaskSpec(repo=git_repo, task="empty", source_mode="empty")

    assert "pyproject.toml" in _protected_patterns(config, existing)
    assert "pyproject.toml" not in _protected_patterns(config, empty)
    assert ".env" in _protected_patterns(config, empty)
    assert ".github/workflows/**" in _protected_patterns(config, empty)


def test_high_risk_edit_waits_for_durable_approval(git_repo: Path, tmp_path: Path):
    FakeSandbox.instances.clear()
    high_risk_plan = json.loads(PLAN)
    high_risk_plan["risk"] = "high"

    class HighRiskPlannerAgent(PlannerAgent):
        def run(self, query: str, **kwargs: Any):
            if "required JSON schema" in query:
                return RunResult(content=json.dumps(high_risk_plan, ensure_ascii=False))
            return super().run(query, **kwargs)

    class HighRiskAgentFactory(FakeAgentFactory):
        def planner(self, **kwargs: Any):
            return HighRiskPlannerAgent()

    runner = CodingTaskRunner(
        WorkerConfig(state_dir=tmp_path / "state", model={"model": "fake"}),
        agent_factory=HighRiskAgentFactory(),
        sandbox_factory=FakeSandbox,
    )
    spec = TaskSpec(
        repo=git_repo,
        task="Perform a risky migration",
        verification=[VerificationCommand(name="test", argv=["pytest", "-q"])],
        max_repairs=2,
    )

    waiting = runner.run(spec)
    flow_record = json.loads(
        runner.store.artifact_path(spec.run_id, f"flow/{spec.run_id}.json").read_text()
    )

    assert waiting.status == RunStatus.NEEDS_ATTENTION
    assert waiting.current_step == "approval:execute"
    assert next(step for step in flow_record["steps"] if step["name"] == "execute")["status"] == (
        "waiting_approval"
    )
    assert not runner.store.artifact_path(spec.run_id, "changes.patch").exists()

    completed = runner.decide_approval(spec.run_id, "execute", "approved", "Proceed")

    assert completed.status == RunStatus.SUCCEEDED
    assert runner.store.artifact_path(spec.run_id, "changes.patch").read_text() == PATCH


def test_missing_or_high_plan_requires_approval():
    low = _approve_edit_by_risk(None, {"outputs": {"plan": PLAN}})
    high_plan = json.loads(PLAN)
    high_plan["risk"] = "high"
    high = _approve_edit_by_risk(
        None,
        {"outputs": {"plan": json.dumps(high_plan, ensure_ascii=False)}},
    )
    missing = _approve_edit_by_risk(None, {"outputs": {}})
    non_coding_plan = json.loads(PLAN)
    non_coding_plan["task_type"] = "non-coding"
    non_coding = _approve_edit_by_risk(
        None,
        {"outputs": {"plan": json.dumps(non_coding_plan, ensure_ascii=False)}},
    )
    market_research_plan = json.loads(PLAN)
    market_research_plan["task_type"] = "market_research"
    market_research_plan["risk"] = "high"
    market_research_plan["items"][0]["files"] = []
    market_research = _approve_edit_by_risk(
        None,
        {"outputs": {"plan": json.dumps(market_research_plan, ensure_ascii=False)}},
    )
    market_tool_plan = json.loads(json.dumps(market_research_plan))
    market_tool_plan["items"][0]["files"] = ["market_client.py"]
    market_tool = _approve_edit_by_risk(
        None,
        {"outputs": {"plan": json.dumps(market_tool_plan, ensure_ascii=False)}},
    )

    assert low is True
    assert high["action"] == "pending"
    assert missing["action"] == "pending"
    assert non_coding is True
    assert market_research["action"] == "reject"
    assert market_tool["action"] == "pending"


def test_only_code_like_file_changes_require_deterministic_verification():
    assert _diff_requires_verification("+++ b/src/app.py\n") is True
    assert _diff_requires_verification("+++ b/README.md\n") is False
    assert _diff_requires_verification("+++ b/data/example.csv\n") is False


def test_domain_label_does_not_block_workspace_execution(git_repo: Path, tmp_path: Path):
    non_coding_plan = json.loads(PLAN)
    non_coding_plan["task_type"] = "non-coding"
    non_coding_plan["risk"] = "low"

    class NonCodingPlannerAgent(PlannerAgent):
        def run(self, query: str, **kwargs: Any):
            if "required JSON schema" in query:
                return RunResult(content=json.dumps(non_coding_plan, ensure_ascii=False))
            return super().run(query, **kwargs)

    class NonCodingAgentFactory(FakeAgentFactory):
        def planner(self, **kwargs: Any):
            return NonCodingPlannerAgent()

    runner = CodingTaskRunner(
        WorkerConfig(state_dir=tmp_path / "state", model={"model": "fake"}),
        agent_factory=NonCodingAgentFactory(),
        sandbox_factory=FakeSandbox,
    )
    spec = TaskSpec(
        repo=git_repo,
        task="研究市场资料并实现处理工具",
        verification=[VerificationCommand(name="test", argv=["pytest", "-q"])],
        max_repairs=1,
    )

    record = runner.run(spec)
    flow_record = json.loads(
        runner.store.artifact_path(spec.run_id, f"flow/{spec.run_id}.json").read_text()
    )

    assert record.status == RunStatus.SUCCEEDED
    assert record.error is None
    assert flow_record["status"] == "success"
    assert all(step["status"] != "waiting_approval" for step in flow_record["steps"])
    assert runner.store.artifact_path(spec.run_id, "changes.patch").read_text() == PATCH


def test_answer_only_followup_succeeds_without_editing_files(git_repo: Path, tmp_path: Path):
    answer_plan = json.loads(PLAN)
    answer_plan["task_type"] = "answer-only"
    answer_plan["risk"] = "low"
    answer_plan["summary"] = {
        "zh": "根据用户补充的数据，基准情景为区间震荡。",
        "en": "Based on the supplied data, the base case is range-bound trading.",
    }
    answer_plan["items"] = [
        {
            "id": "1",
            "description": {"zh": "资料显示库存稳定。", "en": "The material shows stable inventory."},
            "files": [],
        }
    ]

    class AnswerPlannerAgent(PlannerAgent):
        def run(self, query: str, **kwargs: Any):
            if "required JSON schema" in query:
                return RunResult(content=json.dumps(answer_plan, ensure_ascii=False))
            return super().run(query, **kwargs)

    class AnswerAgentFactory(FakeAgentFactory):
        def planner(self, **kwargs: Any):
            return AnswerPlannerAgent()

    runner = CodingTaskRunner(
        WorkerConfig(state_dir=tmp_path / "state", model={"model": "fake"}),
        agent_factory=AnswerAgentFactory(),
        sandbox_factory=FakeSandbox,
    )
    spec = TaskSpec(
        repo=git_repo,
        task="根据我补充的库存数据继续分析",
        parent_run_id="parent-run",
        root_run_id="root-run",
        conversation_context="原问题和用户提供的数据",
        max_repairs=0,
    )

    record = runner.run(spec)

    assert record.status == RunStatus.SUCCEEDED
    assert record.error is None
    assert not runner.store.artifact_path(spec.run_id, "changes.patch").read_text().strip()
    summary = runner.store.artifact_path(spec.run_id, "summary.md").read_text()
    assert "任务回答" in summary
    assert "基准情景为区间震荡" in summary
    assert "文件变更 / File changes: 无 / none" in summary


def test_unified_flow_handles_text_task_without_file_changes(git_repo: Path, tmp_path: Path):
    answer_plan = json.loads(PLAN)
    answer_plan["task_type"] = "answer-only"
    answer_plan["summary"] = {"zh": "已完成三种语气的改写。", "en": "Completed three tone variants."}
    answer_plan["items"] = [
        {
            "id": "1",
            "description": {"zh": "正式、简洁、友好。", "en": "Formal, concise, and friendly."},
            "files": [],
        }
    ]
    seen_tools: set[str] = set()

    class UnifiedPlanner(PlannerAgent):
        def run(self, query: str, **kwargs: Any):
            seen_tools.update(tool.tool_info["tool_name"] for tool in kwargs.get("tools", []))
            if "required JSON schema" in query:
                return RunResult(content=json.dumps(answer_plan, ensure_ascii=False))
            return RunResult(content="已理解任务并收集上下文。")

    class UnifiedFactory(FakeAgentFactory):
        def planner(self, **kwargs: Any):
            return UnifiedPlanner()

    FakeSandbox.instances.clear()
    runner = CodingTaskRunner(
        WorkerConfig(state_dir=tmp_path / "state", model={"model": "fake"}),
        agent_factory=UnifiedFactory(),
        sandbox_factory=FakeSandbox,
    )
    spec = TaskSpec(repo=git_repo, task="把产品说明改成三种语气", source_mode="empty")

    record = runner.run(spec)

    assert record.status == RunStatus.SUCCEEDED
    assert record.metadata["execution_mode"] == "unified"
    assert "http_request" in seen_tools
    assert FakeSandbox.instances[0].started is True
    assert FakeSandbox.instances[0].stopped is True
    assert runner.store.artifact_path(spec.run_id, "changes.patch").read_text() == ""
    assert not runner.store.artifact_path(spec.run_id, "route.json").exists()


def test_unified_executor_combines_http_and_workspace_tools(git_repo: Path, tmp_path: Path):
    planner_tools: set[str] = set()
    executor_tools: set[str] = set()

    class UnifiedPlanner(PlannerAgent):
        def run(self, query: str, **kwargs: Any):
            planner_tools.update(tool.tool_info["tool_name"] for tool in kwargs.get("tools", []))
            return super().run(query, **kwargs)

    class UnifiedExecutor(CoderAgent):
        def run(self, query: str, **kwargs: Any):
            executor_tools.update(tool.tool_info["tool_name"] for tool in kwargs.get("tools", []))
            return super().run(query, **kwargs)

    class UnifiedFactory(FakeAgentFactory):
        def planner(self, **kwargs: Any):
            return UnifiedPlanner()

        def executor(self, **kwargs: Any):
            return UnifiedExecutor()

    FakeSandbox.instances.clear()
    runner = CodingTaskRunner(
        WorkerConfig(state_dir=tmp_path / "state", model={"model": "fake"}),
        agent_factory=UnifiedFactory(),
        sandbox_factory=FakeSandbox,
    )
    spec = TaskSpec(
        repo=git_repo,
        task="研究外部资料并据此修复代码",
        verification=[VerificationCommand(name="test", argv=["pytest", "-q"])],
        max_repairs=1,
    )

    record = runner.run(spec)

    assert record.status == RunStatus.SUCCEEDED
    assert record.metadata["execution_mode"] == "unified"
    assert {"http_request", "read_file"}.issubset(planner_tools)
    assert {"http_request", "apply_patch"}.issubset(executor_tools)
    assert runner.store.artifact_path(spec.run_id, "changes.patch").read_text() == PATCH
