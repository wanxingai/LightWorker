from __future__ import annotations

from pathlib import Path

import pytest
from conftest import run_git

from lightworker.workspace import WorkspaceError, WorkspaceManager


def test_clean_snapshot_uses_head_and_does_not_mutate_source(git_repo: Path, tmp_path: Path):
    original_status = run_git(git_repo, "status", "--porcelain")
    (git_repo / "app.py").write_text("def answer():\n    return 99\n", encoding="utf-8")
    destination = tmp_path / "snapshot"

    commit = WorkspaceManager().create_snapshot(git_repo, destination, include_dirty=False)

    assert commit == run_git(git_repo, "rev-parse", "HEAD").strip()
    assert "return 1" in (destination / "app.py").read_text(encoding="utf-8")
    assert "return 99" in (git_repo / "app.py").read_text(encoding="utf-8")
    assert original_status == ""
    assert run_git(git_repo, "status", "--porcelain").startswith(" M app.py")


def test_dirty_snapshot_copies_tracked_and_untracked(git_repo: Path, tmp_path: Path):
    (git_repo / "app.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
    (git_repo / "notes.txt").write_text("untracked", encoding="utf-8")
    destination = tmp_path / "snapshot"

    WorkspaceManager().create_snapshot(git_repo, destination, include_dirty=True)

    assert "return 42" in (destination / "app.py").read_text(encoding="utf-8")
    assert (destination / "notes.txt").read_text(encoding="utf-8") == "untracked"
    assert "app.py" in run_git(destination, "status", "--short")


def test_untracked_symlink_is_rejected(git_repo: Path, tmp_path: Path):
    (git_repo / "unsafe-link").symlink_to("/etc/passwd")
    with pytest.raises(WorkspaceError, match="symlinks"):
        WorkspaceManager().create_snapshot(git_repo, tmp_path / "snapshot", include_dirty=True)


def test_repo_must_point_to_git_root(git_repo: Path):
    with pytest.raises(WorkspaceError, match="Git root"):
        WorkspaceManager().validate_repository(git_repo / "tests")


def test_managed_empty_repository_can_be_snapshotted(tmp_path: Path):
    manager = WorkspaceManager()
    source = manager.create_empty_repository(tmp_path / "scratch")
    destination = tmp_path / "snapshot"

    commit = manager.create_snapshot(source, destination)

    assert commit == run_git(source, "rev-parse", "HEAD").strip()
    assert run_git(source, "ls-files") == ""
    assert run_git(destination, "status", "--porcelain") == ""
