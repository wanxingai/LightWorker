"""Configuration loading with safe, explicit defaults."""

from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import Any, Literal

import yaml
from platformdirs import user_data_path
from pydantic import BaseModel, Field, SecretStr, field_validator

from .models import GoalBudget, RuntimeMode, VerificationCommand, VerificationKind

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
    search_enabled: bool = True
    search_max_results: int = Field(default=8, ge=1, le=20)
    max_concurrent_per_host: int = Field(default=2, ge=1, le=16)


class RuntimeConfig(BaseModel):
    mode: RuntimeMode = RuntimeMode.AGENTIC
    max_tool_iterations: int = Field(default=16, ge=1, le=100)
    context_window_tokens: int = Field(default=64_000, ge=4_096)
    compression_ratio: float = Field(default=0.75, gt=0.4, lt=0.95)
    goal_budget: GoalBudget = Field(default_factory=GoalBudget)
    no_progress_limit: int = Field(default=4, ge=2, le=20)


class SchedulerConfig(BaseModel):
    max_tasks: int = Field(default=2, ge=1, le=16)
    max_model_calls: int = Field(default=4, ge=1, le=32)
    max_browsers: int = Field(default=2, ge=1, le=8)
    max_containers: int = Field(default=4, ge=1, le=16)
    max_subagents: int = Field(default=8, ge=1, le=32)


class ShellConfig(BaseModel):
    enabled: bool = True
    allowed_programs: list[str] = Field(
        default_factory=lambda: [
            "git",
            "python",
            "python3",
            "pytest",
            "ruff",
            "mypy",
            "uv",
            "pip",
            "pip3",
        ]
    )
    max_argv_items: int = Field(default=128, ge=1, le=512)


class BrowserConfig(BaseModel):
    enabled: bool = True
    backend: Literal["playwright", "drissionpage"] = "playwright"
    headless: bool = True
    timeout_seconds: int = Field(default=30, ge=1, le=180)
    max_pages: int = Field(default=8, ge=1, le=32)
    allow_downloads: bool = False
    allowed_hosts: list[str] = Field(default_factory=list)
    persistent_profiles: bool = False


class MemoryConfig(BaseModel):
    enabled: bool = True
    user_agents_file: Path = Field(default_factory=lambda: Path.home() / ".lightworker" / "AGENTS.md")
    max_instruction_bytes: int = Field(default=131_072, ge=4_096, le=1_048_576)
    candidate_ttl_days: int = Field(default=30, ge=1, le=3650)


class SkillsConfig(BaseModel):
    enabled: bool = True
    user_directories: list[Path] = Field(default_factory=lambda: [Path.home() / ".lightworker" / "skills"])
    managed_directories: list[Path] = Field(default_factory=list)
    max_skill_bytes: int = Field(default=262_144, ge=4_096, le=2_097_152)
    allow_scripts: bool = True


class MCPServerConfig(BaseModel):
    transport: Literal["stdio", "sse", "streamable_http"] = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    disabled: bool = False
    allowed_tools: list[str] = Field(default_factory=list)
    read_only_tools: list[str] = Field(default_factory=list)
    timeout_seconds: int = Field(default=60, ge=1, le=600)


class MCPConfig(BaseModel):
    enabled: bool = True
    allowed_hosts: list[str] = Field(default_factory=list)
    servers: dict[str, MCPServerConfig] = Field(default_factory=dict)


class RAGConfig(BaseModel):
    enabled: bool = True
    chunk_tokens: int = Field(default=1000, ge=200, le=4000)
    chunk_overlap_tokens: int = Field(default=120, ge=0, le=1000)
    max_results: int = Field(default=8, ge=1, le=50)
    embeddings_enabled: bool = False
    embedding_model: str = "text-embedding-3-small"
    embedding_base_url: str | None = None
    embedding_api_key_env: str = "LIGHTWORKER_EMBEDDING_API_KEY"
    embedding_batch_size: int = Field(default=64, ge=1, le=256)


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
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    shell: ShellConfig = Field(default_factory=ShellConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    rag: RAGConfig = Field(default_factory=RAGConfig)
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
