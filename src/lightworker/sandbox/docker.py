"""Docker CLI based sandbox with narrowly scoped network attachment."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from ..config import ResourceLimits
from .base import SandboxBackend, SandboxError


class DockerSandbox(SandboxBackend):
    def __init__(
        self,
        *,
        run_id: str,
        workspace: Path,
        image: str,
        limits: ResourceLimits,
        protected_patterns: list[str],
        pip_index_url: str,
        max_pip_requirements: int,
        sensitive_read_patterns: list[str] | None = None,
    ):
        self.run_id = run_id
        self.workspace = workspace.resolve()
        self.image = image
        self.limits = limits
        self.protected_patterns = protected_patterns
        self.pip_index_url = pip_index_url
        self.max_pip_requirements = max_pip_requirements
        self.sensitive_read_patterns = list(sensitive_read_patterns or [])
        suffix = "".join(char for char in run_id.lower() if char.isalnum())[:20]
        if not suffix:
            raise ValueError("run_id must contain alphanumeric characters")
        self.container_name = f"lightworker-{suffix}"
        self.isolation_network_name = f"lightworker-isolated-{suffix}"
        self.egress_network_name = f"lightworker-egress-{suffix}"
        self._started = False
        self._lock = threading.RLock()

    @staticmethod
    def daemon_available() -> bool:
        result = _docker(["info", "--format", "{{.ServerVersion}}"], timeout=10)
        return result.returncode == 0 and bool(result.stdout.strip())

    @classmethod
    def image_exists(cls, image: str) -> bool:
        result = _docker(["image", "inspect", image], timeout=10)
        return result.returncode == 0

    @classmethod
    def build_image(cls, image: str, dockerfile: Path, context: Path) -> None:
        result = _docker(
            ["build", "--file", str(dockerfile), "--tag", image, str(context)],
            timeout=1800,
        )
        if result.returncode:
            raise SandboxError(result.stderr.strip() or result.stdout.strip() or "Docker image build failed")

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            if not self.workspace.is_dir():
                raise SandboxError(f"workspace does not exist: {self.workspace}")
            self._remove_stale_container()
            self._create_isolation_network()
            command = [
                "run",
                "--detach",
                "--name",
                self.container_name,
                "--label",
                f"lightworker.run_id={self.run_id}",
                "--read-only",
                "--network",
                self.isolation_network_name,
                "--cpus",
                str(self.limits.cpus),
                "--memory",
                self.limits.memory,
                "--pids-limit",
                str(self.limits.pids),
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=256m,mode=1777",
                "--tmpfs",
                "/deps:rw,exec,nosuid,size=1g,uid=10001,gid=10001,mode=0755",
                "--tmpfs",
                "/home/worker:rw,noexec,nosuid,size=64m,uid=10001,gid=10001,mode=0755",
                "--env",
                "PYTHONUSERBASE=/deps",
                "--env",
                "PATH=/deps/bin:/usr/local/bin:/usr/bin:/bin",
                "--volume",
                f"{self.workspace}:/workspace:rw",
                "--workdir",
                "/workspace",
                self.image,
                "sleep",
                "infinity",
            ]
            result = _docker(command, timeout=60)
            if result.returncode:
                self._remove_networks()
                raise SandboxError(result.stderr.strip() or "failed to start Docker sandbox")
            self._started = True
            try:
                health = self.call("health", {}, timeout=15)
                if not health.get("ok"):
                    raise SandboxError("sandbox helper health check failed")
            except Exception:
                self.stop()
                raise

    def stop(self) -> None:
        with self._lock:
            if self._started or self._container_exists():
                _docker(["rm", "--force", self.container_name], timeout=30)
            self._remove_networks()
            self._started = False

    def call(self, action: str, params: dict[str, Any], *, timeout: int | None = None) -> dict[str, Any]:
        with self._lock:
            if not self._started:
                raise SandboxError("sandbox has not been started")
            payload = {
                "action": action,
                "params": params,
                "policy": {
                    "protected_patterns": self.protected_patterns,
                    "sensitive_read_patterns": self.sensitive_read_patterns,
                    "max_patch_bytes": self.limits.max_patch_bytes,
                    "max_changed_files": self.limits.max_changed_files,
                    "max_read_bytes": self.limits.max_read_bytes,
                    "max_output_bytes": self.limits.max_tool_output_bytes,
                    "pip_index_url": self.pip_index_url,
                    "max_pip_requirements": self.max_pip_requirements,
                },
            }
            result = _docker(
                ["exec", "--interactive", self.container_name, "lightworker-sandbox-helper"],
                input_text=json.dumps(payload, ensure_ascii=False),
                timeout=(timeout or self.limits.command_timeout_seconds) + 10,
            )
            if result.returncode and not result.stdout.strip():
                raise SandboxError(result.stderr.strip() or f"sandbox action {action!r} failed")
            try:
                response = json.loads(result.stdout)
            except json.JSONDecodeError as exc:
                raise SandboxError(
                    f"sandbox action {action!r} returned invalid JSON: "
                    f"{result.stdout[:500]} {result.stderr[:500]}"
                ) from exc
            if not isinstance(response, dict):
                raise SandboxError(f"sandbox action {action!r} returned a non-object")
            return response

    def install_requirements(self, requirements: list[str], *, timeout: int = 900) -> dict[str, Any]:
        with self._lock:
            if not self._started:
                raise SandboxError("sandbox has not been started")
            preflight = self.call(
                "pip_check_requirements",
                {"requirements": requirements},
                timeout=min(timeout, 120),
            )
            if not preflight.get("ok"):
                return preflight
            if preflight.get("satisfied"):
                preflight.update(
                    {
                        "exit_code": 0,
                        "already_satisfied": True,
                        "frozen": [],
                    }
                )
                return preflight
            self._create_egress_network()
            connected = False
            try:
                result = _docker(
                    ["network", "connect", self.egress_network_name, self.container_name],
                    timeout=30,
                )
                if result.returncode:
                    raise SandboxError(result.stderr.strip() or "failed to attach pip network")
                connected = True
                return self.call(
                    "pip_install", {"requirements": requirements, "timeout": timeout}, timeout=timeout
                )
            finally:
                if connected:
                    _docker(
                        [
                            "network",
                            "disconnect",
                            "--force",
                            self.egress_network_name,
                            self.container_name,
                        ],
                        timeout=30,
                    )
                self._remove_egress_network()
                if self._started:
                    try:
                        self.call("cleanup_processes", {}, timeout=15)
                    except SandboxError:
                        pass

    def __enter__(self) -> DockerSandbox:
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.stop()

    def _container_exists(self) -> bool:
        return _docker(["container", "inspect", self.container_name], timeout=10).returncode == 0

    def _remove_stale_container(self) -> None:
        if self._container_exists():
            result = _docker(["rm", "--force", self.container_name], timeout=30)
            if result.returncode:
                raise SandboxError(result.stderr.strip() or "failed to remove stale sandbox")
        self._remove_networks()

    def _create_isolation_network(self) -> None:
        self._remove_networks()
        result = _docker(
            [
                "network",
                "create",
                "--internal",
                "--label",
                f"lightworker.run_id={self.run_id}",
                self.isolation_network_name,
            ],
            timeout=30,
        )
        if result.returncode:
            raise SandboxError(result.stderr.strip() or "failed to create isolated network")

    def _create_egress_network(self) -> None:
        self._remove_egress_network()
        result = _docker(
            [
                "network",
                "create",
                "--label",
                f"lightworker.run_id={self.run_id}",
                self.egress_network_name,
            ],
            timeout=30,
        )
        if result.returncode:
            raise SandboxError(result.stderr.strip() or "failed to create pip egress network")

    def _remove_egress_network(self) -> None:
        _docker(["network", "rm", self.egress_network_name], timeout=30)

    def _remove_networks(self) -> None:
        self._remove_egress_network()
        _docker(["network", "rm", self.isolation_network_name], timeout=30)


def _docker(
    args: list[str],
    *,
    input_text: str | None = None,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    started = time.perf_counter()
    try:
        return subprocess.run(
            ["docker", *args],
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise SandboxError("Docker CLI is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        duration = round((time.perf_counter() - started) * 1000, 3)
        return subprocess.CompletedProcess(
            ["docker", *args],
            124,
            stdout=(exc.stdout or "") if isinstance(exc.stdout, str) else "",
            stderr=f"docker command timed out after {duration} ms",
        )
