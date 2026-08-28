from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lightworker import sandbox_helper


@pytest.fixture
def helper_workspace(git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(sandbox_helper, "WORKSPACE", git_repo)
    return git_repo


def policy(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "protected_patterns": ["pyproject.toml", ".github/workflows/**"],
        "sensitive_read_patterns": [".env", "*.pem", "secrets/**"],
        "max_patch_bytes": 100_000,
        "max_changed_files": 5,
        "max_read_bytes": 10_000,
        "max_output_bytes": 10_000,
        "max_pip_requirements": 3,
    }
    values.update(overrides)
    return values


def test_list_read_search_are_bounded(helper_workspace: Path):
    listed = sandbox_helper.list_files({"path": ".", "limit": 50}, policy())
    read = sandbox_helper.read_file({"path": "app.py"}, policy())
    search = sandbox_helper.search_text({"pattern": "answer", "path": "."}, policy())

    assert "app.py" in listed["files"]
    assert "1: def answer" in read["content"]
    assert search["found"] is True
    assert "app.py:1" in search["matches"]


def test_path_escape_and_symlink_escape_are_blocked(helper_workspace: Path):
    (helper_workspace / "escape").symlink_to("/etc/passwd")
    with pytest.raises(sandbox_helper.HelperError, match="unsafe workspace path"):
        sandbox_helper.read_file({"path": "../outside"}, policy())
    with pytest.raises(sandbox_helper.HelperError, match="escapes workspace"):
        sandbox_helper.read_file({"path": "escape"}, policy())


def test_sensitive_files_are_hidden_from_model_tools(helper_workspace: Path):
    (helper_workspace / ".env").write_text("API_KEY=unsafe", encoding="utf-8")
    (helper_workspace / "public.txt").write_text("safe", encoding="utf-8")

    listed = sandbox_helper.list_files({"path": ".", "limit": 50}, policy())
    search = sandbox_helper.search_text({"pattern": "unsafe", "path": "."}, policy())

    assert ".env" not in listed["files"]
    assert search["found"] is False
    with pytest.raises(sandbox_helper.HelperError, match="sensitive file"):
        sandbox_helper.read_file({"path": ".env"}, policy())


def test_apply_patch_changes_safe_file(helper_workspace: Path):
    patch = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
 def answer():
-    return 1
+    return 2
"""
    response = sandbox_helper.apply_patch({"patch": patch}, policy())

    assert response["changed_files"] == ["app.py"]
    assert "return 2" in (helper_workspace / "app.py").read_text(encoding="utf-8")


def test_apply_patch_accepts_missing_final_newline(helper_workspace: Path):
    patch = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
 def answer():
-    return 1
+    return 2"""

    response = sandbox_helper.apply_patch({"patch": patch}, policy())

    assert response["changed_files"] == ["app.py"]
    assert "return 2" in (helper_workspace / "app.py").read_text(encoding="utf-8")


def test_git_diff_includes_new_untracked_files(helper_workspace: Path):
    patch = """diff --git a/new_module.py b/new_module.py
new file mode 100644
--- /dev/null
+++ b/new_module.py
@@ -0,0 +1 @@
+ANSWER = 42
"""
    sandbox_helper.apply_patch({"patch": patch}, policy())

    response = sandbox_helper.git_diff({"full": True}, policy())

    assert "diff --git a/new_module.py b/new_module.py" in response["diff"]
    assert "new file mode 100644" in response["diff"]
    assert "+ANSWER = 42" in response["diff"]


def test_protected_file_and_deletion_are_blocked(helper_workspace: Path):
    protected = """diff --git a/pyproject.toml b/pyproject.toml
--- a/pyproject.toml
+++ b/pyproject.toml
@@ -1,2 +1,2 @@
 [tool.pytest.ini_options]
-pythonpath = ["."]
+pythonpath = ["src"]
"""
    deletion = """diff --git a/app.py b/app.py
deleted file mode 100644
--- a/app.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def answer():
-    return 1
"""
    with pytest.raises(sandbox_helper.HelperError, match="protected path"):
        sandbox_helper.apply_patch({"patch": protected}, policy())
    with pytest.raises(sandbox_helper.HelperError, match="deletion"):
        sandbox_helper.apply_patch({"patch": deletion}, policy())

    approved = sandbox_helper.apply_patch(
        {"patch": deletion, "allow_delete": True},
        policy(),
    )
    assert approved["changed_files"] == ["app.py"]
    assert not (helper_workspace / "app.py").exists()


@pytest.mark.parametrize(
    "patch, message",
    [
        (
            """diff --git a/app.py b/moved.py
similarity index 100%
rename from app.py
rename to moved.py
""",
            "deletion",
        ),
        (
            """diff --git a/link b/link
new file mode 120000
--- /dev/null
+++ b/link
@@ -0,0 +1 @@
+/etc/passwd
""",
            "special file mode",
        ),
    ],
)
def test_rename_and_special_file_patches_are_blocked(helper_workspace: Path, patch: str, message: str):
    with pytest.raises(sandbox_helper.HelperError, match=message):
        sandbox_helper.apply_patch({"patch": patch}, policy())


@pytest.mark.parametrize(
    "argv",
    [
        ["sh", "-c", "pytest"],
        ["rm", "-rf", "."],
        ["ruff", "format", "."],
        ["python", "-c", "print('unsafe')"],
    ],
)
def test_non_allowlisted_commands_are_blocked(argv: list[str]):
    with pytest.raises(sandbox_helper.HelperError, match="not allowlisted|blocked option"):
        sandbox_helper.validate_command(argv)


@pytest.mark.parametrize("requirement", ["git+https://example.com/a.git", "../local", "pkg --index-url=x"])
def test_unsafe_pip_requirements_fail_before_execution(requirement: str, monkeypatch: pytest.MonkeyPatch):
    called = False

    def no_command(*args: object, **kwargs: object):
        nonlocal called
        called = True
        raise AssertionError("pip must not run")

    monkeypatch.setattr(sandbox_helper, "run_command_raw", no_command)
    with pytest.raises(sandbox_helper.HelperError, match="unsupported requirement"):
        sandbox_helper.pip_install({"requirements": [requirement]}, policy())
    assert called is False


def test_pip_preflight_is_offline_and_reports_satisfied(monkeypatch: pytest.MonkeyPatch):
    captured: list[str] = []

    def fake_command(argv: list[str], *, timeout: int):
        captured.extend(argv)
        return subprocess.CompletedProcess(argv, 0, "Requirement already satisfied: pytest\n", "")

    monkeypatch.setattr(sandbox_helper, "run_command_raw", fake_command)

    result = sandbox_helper.pip_check_requirements({"requirements": ["pytest"]}, policy())

    assert result["satisfied"] is True
    assert "--no-index" in captured
    assert "--dry-run" in captured


def test_run_command_timeout_kills_process_group(helper_workspace: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sandbox_helper, "validate_command", lambda argv: None)
    with pytest.raises(subprocess.TimeoutExpired):
        sandbox_helper.run_command_raw(
            ["python", "-c", "import time; time.sleep(5)"],
            timeout=1,
        )
