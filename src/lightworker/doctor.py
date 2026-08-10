"""Environment diagnostics used by the CLI and tests."""

from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
from dataclasses import dataclass

from .config import WorkerConfig
from .sandbox import DockerSandbox


@dataclass(frozen=True)
class Diagnostic:
    name: str
    ok: bool
    message: str
    required: bool = True


def run_diagnostics(config: WorkerConfig) -> list[Diagnostic]:
    diagnostics = [
        Diagnostic("Python", sys.version_info >= (3, 11), sys.version.split()[0]),
        _command_diagnostic("Git", "git", ["--version"]),
        _command_diagnostic("Docker CLI", "docker", ["--version"]),
    ]
    daemon = DockerSandbox.daemon_available() if shutil.which("docker") else False
    diagnostics.append(Diagnostic("Docker daemon", daemon, "available" if daemon else "not running"))
    image = daemon and DockerSandbox.image_exists(config.image)
    diagnostics.append(
        Diagnostic(
            "Sandbox image",
            image,
            config.image if image else f"{config.image} is not built yet",
            required=False,
        )
    )
    dockerfile = config.dockerfile
    context = config.docker_context
    build_files_ok = bool(
        dockerfile and dockerfile.is_file() and context and (context / "sandbox_helper.py").is_file()
    )
    diagnostics.append(
        Diagnostic(
            "Sandbox build files",
            build_files_ok,
            str(dockerfile) if build_files_ok else "Dockerfile or sandbox_helper.py is missing",
        )
    )
    diagnostics.append(_lightagent_diagnostic())
    diagnostics.append(
        Diagnostic(
            "Model",
            bool(config.model.model),
            config.model.model or "set LIGHTWORKER_MODEL",
        )
    )
    diagnostics.append(
        Diagnostic(
            "Model API key",
            bool(config.model.resolved_api_key),
            "configured (value hidden)" if config.model.resolved_api_key else "not configured",
            required=False,
        )
    )
    return diagnostics


def _command_diagnostic(name: str, executable: str, args: list[str]) -> Diagnostic:
    path = shutil.which(executable)
    if not path:
        return Diagnostic(name, False, f"{executable} is not installed")
    try:
        result = subprocess.run(
            [path, *args],
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Diagnostic(name, False, str(exc))
    message = (result.stdout or result.stderr).strip()
    return Diagnostic(name, result.returncode == 0, message)


def _lightagent_diagnostic() -> Diagnostic:
    try:
        module = importlib.import_module("LightAgent")
    except Exception as exc:
        return Diagnostic("LightAgent", False, f"import failed: {exc}")
    required = ["LightAgent", "LightFlow", "JsonLightFlowStore", "PolicyHook", "RunResult"]
    missing = [name for name in required if not hasattr(module, name)]
    version = str(getattr(module, "__version__", "unknown"))
    if missing:
        return Diagnostic("LightAgent", False, f"{version}; missing APIs: {', '.join(missing)}")
    return Diagnostic("LightAgent", version.startswith("0.9."), f"version {version}")
