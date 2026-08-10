"""Configuration loading with safe, explicit defaults."""

from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import Any

import yaml
from platformdirs import user_data_path
from pydantic import BaseModel, Field, SecretStr, field_validator

from .models import VerificationCommand, VerificationKind

DEFAULT_PROTECTED_PATTERNS = [
    ".git/**",
    ".env",
    ".env.*",
    ".github/workflows/**",
    "Dockerfile",
    "Dockerfile.*",
    "docker-compose*.yml",
    "docker-compose*.yaml",
    "compose*.yml",
    "compose*.yaml",
    "requirements*.txt",
    "pyproject.toml",
    "poetry.lock",
    "uv.lock",
    "Pipfile*",
    ".circleci/**",
    ".gitlab-ci.yml",
    "azure-pipelines*.yml",
    "azure-pipelines*.yaml",
    "Jenkinsfile*",
    ".devcontainer/**",
    "k8s/**",
    "kubernetes/**",
    "helm/**",
    "charts/**",
    "terraform/**",
    "*.tf",
    "CODEOWNERS",
]

DEFAULT_SENSITIVE_READ_PATTERNS = [
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    ".npmrc",
    ".pypirc",
    ".netrc",
    "credentials.json",
    "secrets/**",
]


class ResourceLimits(BaseModel):
    cpus: float = Field(default=2.0, gt=0, le=16)
    memory: str = "4g"
    pids: int = Field(default=256, ge=32, le=4096)
    task_timeout_seconds: int = Field(default=3600, ge=60)
    command_timeout_seconds: int = Field(default=900, ge=1)
    max_patch_bytes: int = Field(default=1_048_576, ge=1024)
    max_changed_files: int = Field(default=50, ge=1)
    max_read_bytes: int = Field(default=524_288, ge=1024)
    max_tool_output_bytes: int = Field(default=32_768, ge=1024)


class ModelConfig(BaseModel):
    model: str | None = None
    base_url: str | None = None
    provider: str | None = None
    api_key: SecretStr | None = None
    api_key_env: str = "LIGHTWORKER_API_KEY"

    @property
    def resolved_api_key(self) -> str | None:
        if self.api_key is not None:
            return self.api_key.get_secret_value()
        return os.getenv(self.api_key_env) or os.getenv("OPENAI_API_KEY")


class AnalysisConfig(BaseModel):
    enabled: bool = True
    allow_http: bool = True
    allowed_hosts: list[str] = Field(default_factory=list)
    max_requests: int = Field(default=12, ge=1, le=50)
    request_timeout_seconds: int = Field(default=30, ge=1, le=120)
    max_response_bytes: int = Field(default=262_144, ge=4096, le=2_097_152)


class WorkerConfig(BaseModel):
    state_dir: Path = Field(default_factory=lambda: user_data_path("lightworker", ensure_exists=False))
    image: str = "lightworker-python:3.11"
    dockerfile: Path | None = None
    docker_context: Path | None = None
    language: str = "bilingual"
    max_repairs: int = Field(default=2, ge=0, le=3)
    protected_patterns: list[str] = Field(default_factory=lambda: list(DEFAULT_PROTECTED_PATTERNS))
    sensitive_read_patterns: list[str] = Field(default_factory=lambda: list(DEFAULT_SENSITIVE_READ_PATTERNS))
    limits: ResourceLimits = Field(default_factory=ResourceLimits)
    model: ModelConfig = Field(default_factory=ModelConfig)
    analysis: AnalysisConfig = Field(default_factory=AnalysisConfig)
    verification: list[VerificationCommand] = Field(default_factory=list)
    pip_index_url: str = "https://pypi.org/simple"
    max_pip_requirements: int = Field(default=10, ge=1, le=50)

    @field_validator("language")
    @classmethod
    def validate_language(cls, value: str) -> str:
        if value not in {"bilingual", "zh-CN", "en"}:
            raise ValueError("language must be bilingual, zh-CN, or en")
        return value

    @classmethod
    def load(cls, path: Path | None = None, **overrides: Any) -> WorkerConfig:
        payload: dict[str, Any] = {}
        if path:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, dict):
                raise ValueError("configuration root must be a mapping")
            payload.update(raw)
        payload = _deep_merge(payload, {key: value for key, value in overrides.items() if value is not None})
        config = cls.model_validate(payload)
        if config.dockerfile is None:
            source_dockerfile = Path(__file__).resolve().parents[2] / "docker" / "Dockerfile.python311"
            packaged_dockerfile = Path(__file__).resolve().parent / "docker" / "Dockerfile.python311"
            config.dockerfile = source_dockerfile if source_dockerfile.is_file() else packaged_dockerfile
        if config.docker_context is None:
            config.docker_context = Path(__file__).resolve().parent
        if config.model.model is None:
            config.model.model = os.getenv("LIGHTWORKER_MODEL") or os.getenv("OPENAI_MODEL")
        if config.model.base_url is None:
            config.model.base_url = os.getenv("LIGHTWORKER_BASE_URL") or os.getenv("OPENAI_BASE_URL")
        if config.model.provider is None:
            config.model.provider = os.getenv("LIGHTWORKER_PROVIDER")
        return config


def parse_verification_command(raw: str, *, kind: VerificationKind, index: int) -> VerificationCommand:
    argv = shlex.split(raw)
    return VerificationCommand(name=f"{kind.value}-{index}", argv=argv, kind=kind)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result
