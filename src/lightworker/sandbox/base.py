"""Sandbox backend contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class SandboxError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandResult:
    argv: list[str]
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: float
    timed_out: bool = False

    @property
    def output(self) -> str:
        if self.stdout and self.stderr:
            return f"{self.stdout.rstrip()}\n{self.stderr.rstrip()}\n"
        return self.stdout or self.stderr


class SandboxBackend(ABC):
    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def call(self, action: str, params: dict[str, Any], *, timeout: int | None = None) -> dict[str, Any]: ...

    @abstractmethod
    def install_requirements(self, requirements: list[str], *, timeout: int = 900) -> dict[str, Any]: ...

    @classmethod
    @abstractmethod
    def image_exists(cls, image: str) -> bool: ...

    @classmethod
    @abstractmethod
    def build_image(cls, image: str, dockerfile: Path, context: Path) -> None: ...
