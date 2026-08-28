from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from conftest import run_git
from fastapi.testclient import TestClient

from lightworker.analysis_tools import CredentialVault
from lightworker.config import WorkerConfig
from lightworker.control import ControlStore
from lightworker.models import RunRecord, RunStatus, TaskSpec
from lightworker.storage import RunStore
from lightworker.web import create_app


class ImmediateManager:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, str]] = []
        self.states: dict[str, dict[str, Any]] = {}

    def submit(self, run_id: str, action: str, function: Any) -> None:
        self.jobs.append((run_id, action))
        self.states[run_id] = {"state": "running", "action": action, "error": None}
        function()
        self.states[run_id] = {"state": "completed", "action": action, "error": None}

    def status(self, run_id: str) -> dict[str, Any] | None:
        return self.states.get(run_id)

    def close(self) -> None:
        return None


class CapturingRunner:
    specs: list[Any] = []
    approvals: list[tuple[str, str, str, str]] = []

    def __init__(self, config: WorkerConfig) -> None:
        self.config = config

    def run(self, spec: Any) -> None:
        self.__class__.specs.append(spec)

    def resume(self, run_id: str) -> None:
        return None

    def rerun_from_verify(self, run_id: str) -> None:
        return None

    def decide_approval(self, run_id: str, step: str, decision: str, note: str) -> None:
        self.__class__.approvals.append((run_id, step, decision, note))


class DeferredManager:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, str, Any]] = []
        self.states: dict[str, dict[str, Any]] = {}

    def submit(self, run_id: str, action: str, function: Any) -> None:
        self.jobs.append((run_id, action, function))
        self.states[run_id] = {"state": "queued", "action": action, "error": None}

    def status(self, run_id: str) -> dict[str, Any] | None:
        return self.states.get(run_id)

    def run_next(self) -> None:
        run_id, action, function = next(job for job in self.jobs if self.states[job[0]]["state"] == "queued")
        self.states[run_id] = {"state": "running", "action": action, "error": None}
        function()
        self.states[run_id] = {"state": "completed", "action": action, "error": None}

    def close(self) -> None:
        return None


class QueueCompletingRunner:
    specs: list[TaskSpec] = []

    def __init__(self, config: WorkerConfig) -> None:
        self.store = RunStore(config.state_dir)

    def run(self, spec: TaskSpec) -> RunRecord:
        self.__class__.specs.append(spec)
        record = RunRecord(
            run_id=spec.run_id,
            task=spec.task,
            repo=str(spec.repo),
            workspace=str(spec.repo),
            status=RunStatus.SUCCEEDED,
            metadata={"task_spec": spec.model_dump(mode="json")},
        )
        self.store.create(record)
        return record


def make_client(tmp_path: Path, manager: ImmediateManager | None = None) -> tuple[TestClient, WorkerConfig]:
    config = WorkerConfig(
        state_dir=tmp_path / "state",
        model={
            "model": "test-model",
            "base_url": "http://model.invalid/v1",
            "api_key": "test-api-secret",
        },
    )
    app = create_app(
        config,
        task_manager=manager or ImmediateManager(),
        runner_factory=CapturingRunner,
    )
    return TestClient(app), config


def test_dashboard_and_health_never_expose_api_key(tmp_path: Path):
    client, _ = make_client(tmp_path)

    page = client.get("/")
    health = client.get("/api/health")

    assert page.status_code == 200
    assert "LightWorker" in page.text
    assert "conversation" in page.text
    assert "approvalDialog" in page.text
    assert "人工审阅" not in page.text
    assert health.status_code == 200
    assert health.json()["model"] == "test-model"
    assert health.json()["model_configured"] is True
    assert "test-api-secret" not in health.text


def test_web_ui_bundles_safe_markdown_renderer(tmp_path: Path):
    client, _ = make_client(tmp_path)

    page = client.get("/")
    renderer = client.get("/static/markdown.js")
    app = client.get("/static/app.js")

    assert page.status_code == 200
    assert renderer.status_code == 200
    assert app.status_code == 200
    assert "/static/markdown.js" in page.text
    assert "LightWorkerMarkdown" in renderer.text
    assert "escapeHtml" in renderer.text
    assert "safeHref" in renderer.text
    assert "setMarkdownContent" in app.text
    assert "dataset.rawMarkdown" in app.text
    assert 'summaryContent").textContent = content.trim()' not in app.text


def test_web_ui_collapses_completed_process_before_final_output(tmp_path: Path):
    client, _ = make_client(tmp_path)

    page = client.get("/")
    app = client.get("/static/app.js")

    assert page.status_code == 200
    assert app.status_code == 200
    assert 'id="processPanel"' in page.text
    assert 'id="processDuration"' in page.text
    assert page.text.index('id="processPanel"') < page.text.index('id="summaryBlock"')
    assert "function elapsedDuration" in app.text
    assert 'label = "已处理"' in app.text
    assert "panel.open = !successful" in app.text
    assert 'activeProcess && step.status === "pending"' in app.text


def test_web_ui_exposes_stream_activity_and_composer_runtime_controls(tmp_path: Path):
    client, _ = make_client(tmp_path)

    page = client.get("/")
    app = client.get("/static/app.js")
    styles = client.get("/static/styles.css")

    assert page.status_code == 200
    assert app.status_code == 200
    assert styles.status_code == 200
    for element_id in ("processProgress", "composerContext", "composerModel", "composerStopButton"):
        assert f'id="{element_id}"' in page.text
    assert "function toolActivity" in app.text
    assert 'kind: "文件", label: "读取了工作区文件"' in app.text
    assert 'composerStopButton").addEventListener' in app.text
    assert ".activity-step.agentic-stream > summary" in styles.text
    assert ".composer-model" in styles.text


def test_web_ui_exposes_message_actions_sidebar_collapse_and_citation_popover(tmp_path: Path):
    client, _ = make_client(tmp_path)

    page = client.get("/")
    app = client.get("/static/app.js")
    renderer = client.get("/static/markdown.js")
    styles = client.get("/static/styles.css")

    assert page.status_code == 200
    for element_id in (
        "sidebarCollapseButton",
        "sidebarExpandButton",
        "currentUserActions",
        "currentAssistantActions",
        "citationPopover",
        "citationLink",
    ):
        assert f'id="{element_id}"' in page.text
    assert "function mountMessageActions" in app.text
    assert "function enhanceCitations" in app.text
    assert "function setSidebarCollapsed" in app.text
    assert 'textContent = "投诉"' in app.text
    assert 'textContent = "点赞"' in app.text
    assert "https?:\\/\\/" in renderer.text
    assert ".message:hover .message-actions" in styles.text
    assert "body.sidebar-collapsed .app-shell" in styles.text
    assert ".citation-popover" in styles.text


def test_web_ui_exposes_running_followup_queue_and_guidance_controls(tmp_path: Path):
    client, _ = make_client(tmp_path)

    page = client.get("/")
    app = client.get("/static/app.js")
    styles = client.get("/static/styles.css")

    for element_id in ("messageQueuePanel", "messageQueueCount", "messageQueueList"):
        assert f'id="{element_id}"' in page.text
    assert "function renderMessageQueue" in app.text
    assert "function guideQueuedMessage" in app.text
    assert 'result.status === "followup_queued"' in app.text
    assert "busy && !hasInput" in app.text
    assert 'textContent = "引导"' in app.text
    assert ".message-queue-item" in styles.text
    assert ".guide-button" in styles.text


def test_run_detail_extracts_citations_from_tool_evidence(tmp_path: Path):
    client, config = make_client(tmp_path)
    store = RunStore(config.state_dir)
    store.create(
        RunRecord(
            run_id="citation-run",
            task="分析外部资料",
            repo="/repo",
            workspace="/workspace",
            status=RunStatus.SUCCEEDED,
        )
    )
    store.write_text(
        "citation-run",
        "summary.md",
        "该工具支持现代 Python 项目管理。[来源](https://example.com/uv-guide)",
    )
    store.write_json(
        "citation-run",
        "flow/citation-run.json",
        {
            "steps": [
                {
                    "name": "analysis",
                    "status": "success",
                    "trace": [
                        {
                            "type": "tool_result",
                            "timestamp": "2026-08-11T02:30:00+00:00",
                            "data": {
                                "name": "web_search",
                                "output": json.dumps(
                                    {
                                        "ok": True,
                                        "results": [
                                            {
                                                "title": "uv：现代化的 Python 包和项目管理工具",
                                                "url": "https://example.com/uv-guide",
                                                "snippet": "2026-08-10 发布。介绍 uv run 和虚拟环境管理。",
                                            }
                                        ],
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                }
            ]
        },
    )

    detail = client.get("/api/runs/citation-run")

    assert detail.status_code == 200
    citations = detail.json()["citations"]
    assert citations == detail.json()["conversation"][0]["citations"]
    assert citations[0]["id"] == 1
    assert citations[0]["url"] == "https://example.com/uv-guide"
    assert citations[0]["site"] == "example.com"
    assert citations[0]["published_at"] == "2026-08-10"
    assert "uv run" in citations[0]["excerpt"]


def test_create_run_validates_repo_and_commands(git_repo: Path, tmp_path: Path):
    CapturingRunner.specs.clear()
    manager = ImmediateManager()
    client, _ = make_client(tmp_path, manager)

    created = client.post(
        "/api/runs",
        json={
            "source_mode": "existing",
            "repo": str(git_repo),
            "task": "Fix the answer",
            "test_commands": ["pytest -q"],
            "lint_commands": ["ruff check ."],
            "include_dirty": False,
            "max_repairs": 1,
        },
    )
    blocked = client.post(
        "/api/runs",
        json={
            "source_mode": "existing",
            "repo": str(git_repo),
            "task": "Unsafe command",
            "test_commands": ["sh -c pytest"],
        },
    )

    assert created.status_code == 202
    assert manager.jobs[0][1] == "run"
    assert CapturingRunner.specs[0].verification[0].argv == ["pytest", "-q"]
    assert CapturingRunner.specs[0].verification[1].argv == ["ruff", "check", "."]
    assert CapturingRunner.specs[0].max_repairs == 1
    assert blocked.status_code == 422
    assert "allowlisted" in blocked.json()["detail"]


def test_create_run_defaults_to_managed_empty_repository(tmp_path: Path):
    CapturingRunner.specs.clear()
    client, config = make_client(tmp_path)

    created = client.post(
        "/api/runs",
        json={
            "task": "Create a small Python package",
            "test_commands": ["pytest -q"],
        },
    )

    assert created.status_code == 202
    spec = CapturingRunner.specs[0]
    assert spec.source_mode == "empty"
    assert spec.repo.parent == config.state_dir / "scratch"
    assert run_git(spec.repo, "status", "--porcelain") == ""
    assert run_git(spec.repo, "ls-files") == ""


def test_create_run_redacts_and_vaults_host_bound_api_key(tmp_path: Path):
    CapturingRunner.specs.clear()
    client, config = make_client(tmp_path)
    secret = "sk-exampleSecretValue1234567890"

    created = client.post(
        "/api/runs",
        json={"task": f"使用 api-key={secret} 调用 https://api.example.com/v1/search"},
    )

    assert created.status_code == 202
    spec = CapturingRunner.specs[-1]
    assert secret not in spec.task
    assert "[redacted]" in spec.task
    assert CredentialVault(config.state_dir).get(created.json()["run_id"], "api.example.com") == secret


def test_run_list_and_detail_redact_legacy_credentials(tmp_path: Path):
    client, config = make_client(tmp_path)
    store = RunStore(config.state_dir)
    secret = "sk-legacySecretValue1234567890"
    spec = TaskSpec(
        run_id="legacy-secret-run",
        repo=tmp_path,
        task=f"api-key={secret} 调用 https://api.example.com/v1/search",
    )
    store.create(
        RunRecord(
            run_id=spec.run_id,
            task=spec.task,
            repo=str(tmp_path),
            workspace=str(tmp_path),
            status=RunStatus.FAILED,
            metadata={"task_spec": spec.model_dump(mode="json")},
        )
    )
    store.write_text(spec.run_id, "summary.md", f"请求失败，credential={secret}")

    listed = client.get("/api/runs")
    detail = client.get(f"/api/runs/{spec.run_id}")

    assert listed.status_code == 200
    assert detail.status_code == 200
    assert secret not in listed.text
    assert secret not in detail.text
    assert "[redacted]" in listed.text
    assert "[redacted]" in detail.text


def test_app_startup_migrates_legacy_credentials_to_vault(tmp_path: Path):
    config = WorkerConfig(
        state_dir=tmp_path / "state",
        model={
            "model": "test-model",
            "base_url": "http://model.invalid/v1",
            "api_key": "test-api-secret",
        },
    )
    store = RunStore(config.state_dir)
    secret = "sk-legacySecretValue1234567890"
    spec = TaskSpec(
        run_id="legacy-migration-run",
        repo=tmp_path,
        task=f"api-key={secret} 调用 https://api.example.com/v1/search",
    )
    store.create(
        RunRecord(
            run_id=spec.run_id,
            task=spec.task,
            repo=str(tmp_path),
            workspace=str(tmp_path),
            status=RunStatus.FAILED,
            metadata={"task_spec": spec.model_dump(mode="json")},
        )
    )

    create_app(config, task_manager=ImmediateManager(), runner_factory=CapturingRunner)

    migrated = store.load(spec.run_id)
    assert secret not in migrated.task
    assert secret not in json.dumps(migrated.metadata)
    assert CredentialVault(config.state_dir).get(spec.run_id, "api.example.com") == secret


def test_run_detail_artifacts_logs_and_review(tmp_path: Path):
    client, config = make_client(tmp_path)
    store = RunStore(config.state_dir)
    record = RunRecord(
        run_id="web-run-1",
        task="Review this patch",
        repo="/repo",
        workspace="/workspace",
        status=RunStatus.SUCCEEDED,
    )
    store.create(record)
    store.write_text("web-run-1", "changes.patch", "diff --git a/a.py b/a.py\n")
    store.write_text("web-run-1", "summary.md", "# Summary\n\nDone.\n")
    store.write_text("web-run-1", "logs/verify.log", "1 passed\n")
    store.write_json(
        "web-run-1",
        "flow/web-run-1.json",
        {
            "steps": [
                {
                    "name": "edit",
                    "status": "success",
                    "duration_ms": 10,
                    "content": "Edited a.py",
                    "trace": [
                        {
                            "type": "tool_call",
                            "timestamp": "2026-08-09T10:00:00+00:00",
                            "data": {"name": "apply_patch", "arguments": "diff --git a/a.py"},
                        },
                        {
                            "type": "tool_result",
                            "timestamp": "2026-08-09T10:00:01+00:00",
                            "data": {"name": "apply_patch", "output": '{"ok": true}', "latency_ms": 8},
                        },
                    ],
                },
                {
                    "name": "verify_0",
                    "status": "success",
                    "duration_ms": 10,
                    "content": json.dumps(
                        {
                            "configured": True,
                            "passed": False,
                            "results": [{"name": "test-1", "passed": False}],
                        }
                    ),
                },
            ]
        },
    )

    detail = client.get("/api/runs/web-run-1")
    summary = client.get("/api/runs/web-run-1/artifacts/summary")
    logs = client.get("/api/runs/web-run-1/logs")
    log = client.get("/api/runs/web-run-1/logs/verify.log")
    review = client.post(
        "/api/runs/web-run-1/review",
        json={"decision": "approved", "note": "Looks good"},
    )

    assert detail.status_code == 200
    assert detail.json()["artifacts"]["diff"] is True
    assert detail.json()["has_changes"] is True
    assert detail.json()["steps"][0]["name"] == "edit"
    assert detail.json()["activity"][0]["tools"][0]["name"] == "apply_patch"
    assert detail.json()["activity"][0]["tools"][0]["output"] == '{"ok": true}'
    assert detail.json()["activity"][1]["verification_passed"] is False
    assert summary.text.startswith("# Summary")
    assert logs.json() == [{"name": "verify.log", "size": 9}]
    assert log.text == "1 passed\n"
    assert review.json()["decision"] == "approved"
    assert review.json()["effect"].startswith("audit_only")
    saved = json.loads(store.artifact_path("web-run-1", "review-decision.json").read_text())
    assert saved["note"] == "Looks good"


def test_empty_patch_is_not_reported_as_file_change(tmp_path: Path):
    client, config = make_client(tmp_path)
    store = RunStore(config.state_dir)
    store.create(
        RunRecord(
            run_id="no-change-run",
            task="Inspect only",
            repo="/repo",
            status=RunStatus.NEEDS_ATTENTION,
        )
    )
    store.write_text("no-change-run", "changes.patch", "\n")

    detail = client.get("/api/runs/no-change-run")

    assert detail.status_code == 200
    assert detail.json()["has_changes"] is False
    assert detail.json()["artifacts"]["diff"] is False
    assert (
        client.post(
            "/api/runs/no-change-run/review",
            json={"decision": "approved", "note": "nothing changed"},
        ).status_code
        == 409
    )


def test_general_task_detail_and_resume_use_conversation_flow(tmp_path: Path):
    client, config = make_client(tmp_path)
    store = RunStore(config.state_dir)
    store.create(
        RunRecord(
            run_id="general-failed-run",
            task="写一份计划",
            repo="/repo",
            status=RunStatus.FAILED,
            metadata={"execution_mode": "general"},
        )
    )

    detail = client.get("/api/runs/general-failed-run")
    resumed = client.post("/api/runs/general-failed-run/resume")

    assert detail.status_code == 200
    assert detail.json()["general_only"] is True
    assert detail.json()["analysis_only"] is False
    assert resumed.status_code == 409
    assert "follow-up" in resumed.json()["detail"]


def test_pending_flow_approval_is_exposed_and_submitted(tmp_path: Path):
    CapturingRunner.approvals.clear()
    manager = ImmediateManager()
    client, config = make_client(tmp_path, manager)
    store = RunStore(config.state_dir)
    store.create(
        RunRecord(
            run_id="approval-run",
            task="Perform a high-risk migration",
            repo="/repo",
            status=RunStatus.NEEDS_ATTENTION,
            current_step="approval:edit",
        )
    )
    store.write_json(
        "approval-run",
        "flow/approval-run.json",
        {
            "status": "waiting_approval",
            "approvals": {},
            "steps": [
                {
                    "name": "edit",
                    "status": "waiting_approval",
                    "approval_request_id": "request-1",
                    "error": "High-risk edit requires approval",
                }
            ],
        },
    )

    detail = client.get("/api/runs/approval-run")
    decision = client.post(
        "/api/runs/approval-run/approval",
        json={"decision": "approved", "note": "Proceed carefully"},
    )

    assert detail.json()["approval_request"]["request_id"] == "request-1"
    assert detail.json()["approval_request"]["step"] == "edit"
    assert decision.status_code == 202
    assert CapturingRunner.approvals == [("approval-run", "edit", "approved", "Proceed carefully")]


def test_running_detail_derives_current_flow_step(tmp_path: Path):
    manager = ImmediateManager()
    manager.states["progress-run"] = {"state": "running", "action": "run", "error": None}
    client, config = make_client(tmp_path, manager)
    store = RunStore(config.state_dir)
    store.create(
        RunRecord(
            run_id="progress-run",
            task="Continue through the flow",
            repo="/repo",
            status=RunStatus.RUNNING,
            current_step="sandbox",
        )
    )
    store.write_json(
        "progress-run",
        "flow/progress-run.json",
        {
            "steps": [
                {"name": "intake", "status": "success"},
                {"name": "context", "status": "pending"},
                {"name": "plan", "status": "pending"},
            ]
        },
    )

    detail = client.get("/api/runs/progress-run")

    assert detail.status_code == 200
    assert detail.json()["current_step"] == "context"


def test_missing_and_active_runs_reject_invalid_actions(tmp_path: Path):
    client, config = make_client(tmp_path)
    store = RunStore(config.state_dir)
    store.create(
        RunRecord(
            run_id="active-run",
            task="Still running",
            repo="/repo",
            status=RunStatus.RUNNING,
        )
    )

    assert client.get("/api/runs/missing").status_code == 404
    assert client.post("/api/runs/active-run/rerun", json={}).status_code == 409
    assert (
        client.post(
            "/api/runs/active-run/review",
            json={"decision": "rejected", "note": "too early"},
        ).status_code
        == 409
    )


def test_followup_inherits_workspace_settings_and_conversation_context(
    git_repo: Path,
    tmp_path: Path,
):
    CapturingRunner.specs.clear()
    manager = ImmediateManager()
    client, config = make_client(tmp_path, manager)
    store = RunStore(config.state_dir)
    root_spec = TaskSpec(
        run_id="root-run",
        repo=git_repo,
        task="分析这些市场资料",
        source_mode="empty",
        max_repairs=1,
    )
    store.create(
        RunRecord(
            run_id="root-run",
            task=root_spec.task,
            repo=str(git_repo),
            workspace=str(git_repo),
            status=RunStatus.FAILED,
            error="缺少实时资料",
            metadata={"task_spec": root_spec.model_dump(mode="json")},
        )
    )
    store.write_text("root-run", "summary.md", "请补充库存和价格数据。")

    response = client.post(
        "/api/runs/root-run/followups",
        json={"message": "补充资料：库存稳定，价格近五日横盘。请继续分析。"},
    )

    assert response.status_code == 202
    assert manager.jobs[-1][1] == "followup"
    followup = CapturingRunner.specs[-1]
    assert followup.parent_run_id == "root-run"
    assert followup.root_run_id == "root-run"
    assert followup.repo == git_repo
    assert followup.include_dirty is True
    assert followup.source_mode == "empty"
    assert followup.max_repairs == 1
    assert "分析这些市场资料" in followup.conversation_context
    assert "请补充库存和价格数据" in followup.conversation_context


def test_active_followups_queue_and_pending_message_can_guide_current_run(
    git_repo: Path,
    tmp_path: Path,
):
    manager = DeferredManager()
    client, config = make_client(tmp_path, manager)
    store = RunStore(config.state_dir)
    root_spec = TaskSpec(run_id="queue-root", repo=git_repo, task="分析市场", source_mode="empty")
    store.create(
        RunRecord(
            run_id="queue-root",
            task=root_spec.task,
            repo=str(git_repo),
            workspace=str(git_repo),
            status=RunStatus.RUNNING,
            metadata={"task_spec": root_spec.model_dump(mode="json")},
        )
    )

    first = client.post("/api/runs/queue-root/followups", json={"message": "先核实库存"})
    second = client.post("/api/runs/queue-root/followups", json={"message": "再分析价格"})

    assert first.status_code == 202
    assert first.json()["status"] == "followup_queued"
    assert second.json()["status"] == "followup_queued"
    detail = client.get("/api/runs/queue-root").json()
    assert [item["message"] for item in detail["message_queue"]] == ["先核实库存", "再分析价格"]
    assert not manager.jobs

    guided = client.post(
        f"/api/runs/queue-root/queue/{first.json()['queue_item']['id']}/guide",
        json={},
    )

    assert guided.status_code == 200
    assert guided.json()["status"] == "guidance_accepted"
    assert ControlStore(store, "queue-root").consume_steering() == ["先核实库存"]
    remaining = client.get("/api/runs/queue-root").json()["message_queue"]
    assert [item["message"] for item in remaining] == ["再分析价格"]

    native = client.app.state.message_queue.snapshot("queue-root")
    assert native["source_of_truth"] == "lightagent_session"
    assert native["session"]["session_id"] == "lightworker-conversation-queue-root"
    inbox = {item["message_id"]: item for item in native["inbox"]}
    assert inbox[first.json()["queue_item"]["id"]]["status"] == "rejected"
    assert inbox[f"steering-{first.json()['queue_item']['id']}"]["status"] == "completed"


def test_native_conversation_session_is_authoritative_over_queue_cache(
    git_repo: Path,
    tmp_path: Path,
):
    manager = DeferredManager()
    client, config = make_client(tmp_path, manager)
    store = RunStore(config.state_dir)
    spec = TaskSpec(run_id="native-root", repo=git_repo, task="分析市场", source_mode="empty")
    store.create(
        RunRecord(
            run_id=spec.run_id,
            task=spec.task,
            repo=str(git_repo),
            workspace=str(git_repo),
            status=RunStatus.RUNNING,
            metadata={"task_spec": spec.model_dump(mode="json")},
        )
    )
    queued = client.post(
        "/api/runs/native-root/followups",
        json={"message": "原生 Session 中的消息"},
    ).json()["queue_item"]

    store.write_json(
        "native-root",
        "message-queue.json",
        [{"id": "corrupted", "message": "被改坏的缓存", "status": "pending"}],
    )
    detail = client.get("/api/runs/native-root").json()

    assert [item["id"] for item in detail["message_queue"]] == [queued["id"]]
    assert detail["conversation_runtime"]["source_of_truth"] == "lightagent_session"
    cached = store.read_json("native-root", "message-queue.json")
    assert [item["id"] for item in cached] == [queued["id"]]
    snapshot = store.read_json("native-root", "lightagent-conversation.json")
    assert snapshot["session_id"] == "lightworker-conversation-native-root"


def test_legacy_queue_is_migrated_to_native_conversation_session(
    git_repo: Path,
    tmp_path: Path,
):
    manager = DeferredManager()
    client, config = make_client(tmp_path, manager)
    store = RunStore(config.state_dir)
    spec = TaskSpec(run_id="legacy-root", repo=git_repo, task="分析市场", source_mode="empty")
    store.create(
        RunRecord(
            run_id=spec.run_id,
            task=spec.task,
            repo=str(git_repo),
            workspace=str(git_repo),
            status=RunStatus.RUNNING,
            metadata={"task_spec": spec.model_dump(mode="json")},
        )
    )
    store.write_json(
        "legacy-root",
        "message-queue.json",
        [
            {
                "id": "legacy-message",
                "message": "迁移这条旧消息",
                "status": "pending",
                "created_at": "2026-08-16T00:00:00+00:00",
                "run_id": None,
            }
        ],
    )

    detail = client.get("/api/runs/legacy-root").json()
    native = client.app.state.message_queue.snapshot("legacy-root")

    assert [item["id"] for item in detail["message_queue"]] == ["legacy-message"]
    event_types = [event["type"] for event in native["session"]["events"]]
    assert "lightworker.queue.migration_started" in event_types
    assert "lightworker.queue.migration_completed" in event_types
    assert native["inbox"][0]["message_id"] == "legacy-message"


def test_native_queue_retry_keeps_logical_order_and_inbox_history(
    git_repo: Path,
    tmp_path: Path,
):
    client, config = make_client(tmp_path, DeferredManager())
    store = RunStore(config.state_dir)
    spec = TaskSpec(run_id="retry-root", repo=git_repo, task="执行任务", source_mode="empty")
    store.create(
        RunRecord(
            run_id=spec.run_id,
            task=spec.task,
            repo=str(git_repo),
            workspace=str(git_repo),
            status=RunStatus.RUNNING,
            metadata={"task_spec": spec.model_dump(mode="json")},
        )
    )
    queue = client.app.state.message_queue
    first = queue.enqueue("retry-root", "重试任务 A")
    second = queue.enqueue("retry-root", "后续任务 B")

    assert queue.claim("retry-root", first["id"], "attempt-one") is not None
    queue.release("retry-root", first["id"], "worker temporarily unavailable")
    assert queue.claim("retry-root", first["id"], "attempt-two") is not None
    queue.complete("retry-root", first["id"])

    projected = queue.all("retry-root")
    assert [item["id"] for item in projected] == [first["id"], second["id"]]
    assert [item["status"] for item in projected] == ["completed", "pending"]
    native = queue.snapshot("retry-root")
    attempts = [
        message
        for message in native["inbox"]
        if message["metadata"].get("lightworker_queue_item_id") == first["id"]
    ]
    assert [message["status"] for message in attempts] == ["rejected", "completed"]


def test_queued_followups_dispatch_sequentially_after_each_run_finishes(
    git_repo: Path,
    tmp_path: Path,
):
    QueueCompletingRunner.specs.clear()
    manager = DeferredManager()
    config = WorkerConfig(
        state_dir=tmp_path / "state",
        model={
            "model": "test-model",
            "base_url": "http://model.invalid/v1",
            "api_key": "test-api-secret",
        },
    )
    app = create_app(config, task_manager=manager, runner_factory=QueueCompletingRunner)
    client = TestClient(app)
    store = RunStore(config.state_dir)
    root_spec = TaskSpec(run_id="serial-root", repo=git_repo, task="执行主任务", source_mode="empty")
    store.create(
        RunRecord(
            run_id="serial-root",
            task=root_spec.task,
            repo=str(git_repo),
            workspace=str(git_repo),
            status=RunStatus.RUNNING,
            metadata={"task_spec": root_spec.model_dump(mode="json")},
        )
    )
    client.post("/api/runs/serial-root/followups", json={"message": "排队任务 A"})
    client.post("/api/runs/serial-root/followups", json={"message": "排队任务 B"})

    store.update_status("serial-root", RunStatus.SUCCEEDED)
    first_detail = client.get("/api/runs/serial-root").json()

    assert [item["status"] for item in first_detail["message_queue"]] == ["running", "pending"]
    assert [job[1] for job in manager.jobs] == ["queued_followup"]

    manager.run_next()

    root_detail = client.get("/api/runs/serial-root").json()
    assert [item["status"] for item in root_detail["message_queue"]] == ["running"]
    assert [spec.task for spec in QueueCompletingRunner.specs] == ["排队任务 A"]
    assert [job[1] for job in manager.jobs] == ["queued_followup", "queued_followup"]

    manager.run_next()

    assert client.get("/api/runs/serial-root").json()["message_queue"] == []
    assert [spec.task for spec in QueueCompletingRunner.specs] == ["排队任务 A", "排队任务 B"]
    assert all(spec.queue_item_id for spec in QueueCompletingRunner.specs)


def test_run_list_groups_followups_and_detail_returns_conversation(
    git_repo: Path,
    tmp_path: Path,
):
    client, config = make_client(tmp_path)
    store = RunStore(config.state_dir)
    root_spec = TaskSpec(run_id="thread-root", repo=git_repo, task="创建风险指标", source_mode="empty")
    store.create(
        RunRecord(
            run_id="thread-root",
            task=root_spec.task,
            repo=str(git_repo),
            workspace=str(git_repo),
            status=RunStatus.SUCCEEDED,
            metadata={"task_spec": root_spec.model_dump(mode="json")},
        )
    )
    store.write_text("thread-root", "summary.md", "首轮已经创建指标。")
    child_spec = TaskSpec(
        run_id="thread-child",
        repo=git_repo,
        task="再补充一个夏普比率",
        source_mode="empty",
        parent_run_id="thread-root",
        root_run_id="thread-root",
        conversation_context="首轮上下文",
    )
    store.create(
        RunRecord(
            run_id="thread-child",
            task=child_spec.task,
            repo=str(git_repo),
            workspace=str(git_repo),
            status=RunStatus.SUCCEEDED,
            metadata={"task_spec": child_spec.model_dump(mode="json")},
        )
    )
    store.write_text("thread-child", "summary.md", "已补充夏普比率。")
    store.write_json(
        "thread-child",
        "plan.json",
        {
            "task_type": "answer-only",
            "risk": "low",
            "summary": {"zh": "回答", "en": "Answer"},
            "items": [],
        },
    )

    runs = client.get("/api/runs")
    detail = client.get("/api/runs/thread-child")

    assert runs.status_code == 200
    assert len(runs.json()) == 1
    assert runs.json()[0]["run_id"] == "thread-child"
    assert runs.json()[0]["root_run_id"] == "thread-root"
    assert runs.json()[0]["task"] == "创建风险指标"
    assert runs.json()[0]["turn_count"] == 2
    assert detail.json()["conversation_title"] == "创建风险指标"
    assert [turn["message"] for turn in detail.json()["conversation"]] == [
        "创建风险指标",
        "再补充一个夏普比率",
    ]
    assert detail.json()["conversation"][0]["summary"] == "首轮已经创建指标。"
    assert detail.json()["answer_only"] is True
