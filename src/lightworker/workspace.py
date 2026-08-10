"""Safe, reproducible snapshots of local Git repositories."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class WorkspaceError(RuntimeError):
    pass


class WorkspaceManager:
    def __init__(self, *, max_untracked_bytes: int = 10 * 1024 * 1024):
        self.max_untracked_bytes = max_untracked_bytes

    def validate_repository(self, repo: Path) -> Path:
        resolved = repo.expanduser().resolve()
        if not resolved.is_dir():
            raise WorkspaceError(f"repository does not exist: {resolved}")
        result = self._git(resolved, "rev-parse", "--show-toplevel")
        root = Path(result.stdout.strip()).resolve()
        if root != resolved:
            raise WorkspaceError(f"--repo must point to the Git root: {root}")
        return resolved

    def create_empty_repository(self, destination: Path) -> Path:
        """Create a managed Git repository whose checked-out tree is empty."""
        resolved = destination.expanduser().resolve()
        if resolved.exists():
            raise WorkspaceError(f"scratch repository already exists: {resolved}")
        resolved.mkdir(parents=True)
        commands = [
            ["git", "init", "--quiet"],
            [
                "git",
                "-c",
                "user.name=LightWorker",
                "-c",
                "user.email=worker@lightworker.invalid",
                "-c",
                "core.hooksPath=/dev/null",
                "commit",
                "--quiet",
                "--allow-empty",
                "--message",
                "Initialize empty LightWorker workspace",
            ],
        ]
        for command in commands:
            result = subprocess.run(
                command,
                cwd=resolved,
                text=True,
                capture_output=True,
                check=False,
                env=_safe_git_env(),
            )
            if result.returncode:
                raise WorkspaceError(result.stderr.strip() or "failed to create empty repository")
        return resolved

    def create_snapshot(self, repo: Path, destination: Path, *, include_dirty: bool = False) -> str:
        root = self.validate_repository(repo)
        if destination.exists():
            raise WorkspaceError(f"workspace already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        commit = self._git(root, "rev-parse", "HEAD").stdout.strip()
        clone = subprocess.run(
            ["git", "clone", "--no-hardlinks", "--no-local", "--no-checkout", str(root), str(destination)],
            text=True,
            capture_output=True,
            check=False,
            env=_safe_git_env(),
        )
        if clone.returncode:
            raise WorkspaceError(clone.stderr.strip() or "git clone failed")
        self._git(destination, "checkout", "--detach", commit)
        if include_dirty:
            self._copy_dirty_state(root, destination)
        return commit

    def _copy_dirty_state(self, source: Path, destination: Path) -> None:
        diff = self._git(source, "diff", "--binary", "--no-ext-diff", "HEAD").stdout
        if diff:
            applied = subprocess.run(
                ["git", "apply", "--binary", "--whitespace=nowarn", "-"],
                cwd=destination,
                input=diff,
                text=True,
                capture_output=True,
                check=False,
                env=_safe_git_env(),
            )
            if applied.returncode:
                raise WorkspaceError(f"failed to apply dirty tracked files: {applied.stderr.strip()}")

        raw = self._git(source, "ls-files", "--others", "--exclude-standard", "-z").stdout
        total = 0
        for item in raw.split("\0"):
            if not item:
                continue
            relative = Path(item)
            if relative.is_absolute() or ".." in relative.parts:
                raise WorkspaceError(f"unsafe untracked path: {item}")
            source_path = source / relative
            if source_path.is_symlink():
                raise WorkspaceError(f"untracked symlinks are not supported: {item}")
            if not source_path.is_file():
                continue
            total += source_path.stat().st_size
            if total > self.max_untracked_bytes:
                raise WorkspaceError("untracked files exceed snapshot size limit")
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target)

    @staticmethod
    def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "diff.external=", *args],
            cwd=repo,
            text=True,
            capture_output=True,
            check=False,
            env=_safe_git_env(),
        )
        if result.returncode:
            raise WorkspaceError(result.stderr.strip() or f"git {' '.join(args)} failed")
        return result


def _safe_git_env() -> dict[str, str]:
    import os

    allowed = {
        key: value
        for key, value in os.environ.items()
        if key not in {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"}
    }
    allowed.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"})
    return allowed
