from __future__ import annotations

from pathlib import Path

import pytest

from lightworker.config import ResourceLimits
from lightworker.sandbox import DockerSandbox

PATCH = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
 def answer():
-    return 1
+    return 2
"""

NEW_FILE_PATCH = """diff --git a/new_module.py b/new_module.py
new file mode 100644
--- /dev/null
+++ b/new_module.py
@@ -0,0 +1 @@
+ANSWER = 42
"""


def docker_available() -> bool:
    return DockerSandbox.daemon_available() and DockerSandbox.image_exists("lightworker-python:3.11")


@pytest.mark.docker
@pytest.mark.skipif(not docker_available(), reason="Docker daemon or LightWorker image unavailable")
def test_real_docker_sandbox_isolated_patch_and_verify(git_repo: Path):
    source = (git_repo / "app.py").read_text(encoding="utf-8")
    (git_repo / ".env").write_text("API_KEY=must-not-reach-model", encoding="utf-8")
    sandbox = DockerSandbox(
        run_id="docker-integration",
        workspace=git_repo,
        image="lightworker-python:3.11",
        limits=ResourceLimits(cpus=1, memory="1g", pids=128),
        protected_patterns=["pyproject.toml", ".github/workflows/**"],
        pip_index_url="https://pypi.org/simple",
        max_pip_requirements=3,
        sensitive_read_patterns=[".env", "*.pem"],
    )
    try:
        sandbox.start()
        health = sandbox.call("health", {})
        security = sandbox.call("security_probe", {})
        listed = sandbox.call("list_files", {"path": "."})
        sensitive = sandbox.call("read_file", {"path": ".env"})
        with pytest.MonkeyPatch.context() as patcher:
            patcher.setattr(
                sandbox,
                "_create_egress_network",
                lambda: pytest.fail("egress must not be created for an existing dependency"),
            )
            existing = sandbox.install_requirements(["pytest"])
        failed = sandbox.call("run_command", {"argv": ["pytest", "-q"], "timeout": 120})
        patched = sandbox.call("apply_patch", {"patch": PATCH.rstrip("\n")})
        new_file = sandbox.call("apply_patch", {"patch": NEW_FILE_PATCH})
        diff = sandbox.call("git_diff", {"full": True})
        passed = sandbox.call("run_command", {"argv": ["pytest", "-q"], "timeout": 120})

        assert health["ok"] is True
        assert health["uid"] == 10001
        assert security == {
            "ok": True,
            "uid": 10001,
            "root_write_allowed": False,
            "docker_socket_exists": False,
            "network_available": False,
        }
        assert ".env" not in listed["files"]
        assert sensitive["ok"] is False
        assert "sensitive file" in sensitive["error"]
        assert existing["already_satisfied"] is True
        assert failed["exit_code"] == 1
        assert patched["changed_files"] == ["app.py"]
        assert new_file["changed_files"] == ["new_module.py"]
        assert "new file mode 100644" in diff["diff"]
        assert "+ANSWER = 42" in diff["diff"]
        assert passed["exit_code"] == 0
        assert "1 passed" in passed["output"]
    finally:
        sandbox.stop()

    assert (git_repo / "app.py").read_text(encoding="utf-8") != source


@pytest.mark.docker
@pytest.mark.network
@pytest.mark.skipif(not docker_available(), reason="Docker daemon or LightWorker image unavailable")
def test_pip_network_is_temporarily_attached_and_then_removed(git_repo: Path):
    sandbox = DockerSandbox(
        run_id="docker-pip-network",
        workspace=git_repo,
        image="lightworker-python:3.11",
        limits=ResourceLimits(cpus=1, memory="1g", pids=128),
        protected_patterns=["pyproject.toml"],
        pip_index_url="https://pypi.org/simple",
        max_pip_requirements=3,
    )
    try:
        sandbox.start()
        before = sandbox.call("security_probe", {})
        installed = sandbox.install_requirements(["tomli==2.2.1"], timeout=180)
        after = sandbox.call("security_probe", {})

        assert before["network_available"] is False
        assert installed["ok"] is True
        assert installed["exit_code"] == 0, installed
        assert any(line.lower().startswith("tomli==2.2.1") for line in installed["frozen"])
        assert after["network_available"] is False
    finally:
        sandbox.stop()
