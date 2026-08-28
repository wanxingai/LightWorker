"""Domain models shared by the CLI, runner, and artifact store."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class RunStatus(StrEnum):
    CREATED = "created"
    PREPARING = "preparing"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    NEEDS_ATTENTION = "needs_attention"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    PAUSED = "paused"
    WAITING_INPUT = "waiting_input"
    WAITING_APPROVAL = "waiting_approval"
    BUDGET_LIMITED = "budget_limited"
    CANCELLED = "cancelled"


class RuntimeMode(StrEnum):
    AGENTIC = "agentic"
    WORKFLOW = "workflow"


class GoalStatus(StrEnum):
    ACTIVE = "active"
    WAITING_INPUT = "waiting_input"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    BUDGET_LIMITED = "budget_limited"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SubgoalStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class ToolCategory(StrEnum):
    WORKSPACE = "workspace"
    SHELL = "shell"
    NETWORK = "network"
    BROWSER = "browser"
    MEMORY = "memory"
    SKILL = "skill"
    MCP = "mcp"
    RAG = "rag"
    AGENT = "agent"
    GOAL = "goal"


class ApprovalPolicy(StrEnum):
    NEVER = "never"
    CONDITIONAL = "conditional"
    ALWAYS = "always"
    DISABLED = "disabled"


class VerificationKind(StrEnum):
    TEST = "test"
    LINT = "lint"
    TYPECHECK = "typecheck"
    BUILD = "build"


class VerificationCommand(BaseModel):
    name: str
    argv: list[str]
    kind: VerificationKind = VerificationKind.TEST
    timeout_seconds: int = 900
    required: bool = True

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, value: list[str]) -> list[str]:
        if not value or any(not isinstance(item, str) or not item for item in value):
            raise ValueError("verification argv must contain non-empty strings")
        return value


class VerificationResult(BaseModel):
    name: str
    kind: VerificationKind
    argv: list[str]
    exit_code: int
    passed: bool
    timed_out: bool = False
    duration_ms: float = 0
    output_excerpt: str = ""
    log_path: str | None = None
    required: bool = True


class TaskSpec(BaseModel):
    task: str = Field(min_length=1)
    repo: Path
    run_id: str = Field(default_factory=lambda: uuid4().hex)
    include_dirty: bool = False
    language: Literal["bilingual", "zh-CN", "en"] = "bilingual"
    verification: list[VerificationCommand] = Field(default_factory=list)
    max_repairs: int = Field(default=2, ge=0, le=3)
    image: str | None = None
    source_mode: Literal["existing", "empty"] = "existing"
    parent_run_id: str | None = None
    root_run_id: str | None = None
    queue_item_id: str | None = None
    conversation_context: str | None = None
    runtime_mode: RuntimeMode = RuntimeMode.AGENTIC
    goal_mode: bool = True


class StepRecord(BaseModel):
    name: str
    status: str
    started_at: datetime | None = None
    ended_at: datetime | None = None
    error: str | None = None


class InstalledRequirement(BaseModel):
    requested: list[str]
    frozen: list[str] = Field(default_factory=list)
    installed_at: datetime = Field(default_factory=utc_now)


class RunRecord(BaseModel):
    run_id: str
    task: str
    repo: str
    workspace: str | None = None
    status: RunStatus = RunStatus.CREATED
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    current_step: str | None = None
    error: str | None = None
    trace_id: str | None = None
    verification: list[VerificationResult] = Field(default_factory=list)
    installed_requirements: list[InstalledRequirement] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolMetadata(BaseModel):
    category: ToolCategory = ToolCategory.WORKSPACE
    is_read_only: bool = True
    is_write: bool = False
    is_destructive: bool = False
    external_side_effect: bool = False
    concurrency_safe: bool = True
    sandbox_required: bool = False
    network_required: bool = False
    credential_scope: str | None = None
    approval_policy: ApprovalPolicy = ApprovalPolicy.NEVER
    timeout_seconds: int = Field(default=120, ge=1, le=3600)
    output_limit_bytes: int = Field(default=32_768, ge=1024, le=2_097_152)


class GoalBudget(BaseModel):
    max_turns: int = Field(default=24, ge=1, le=500)
    max_tool_calls: int = Field(default=80, ge=1, le=2000)
    max_model_calls: int = Field(default=32, ge=1, le=500)
    max_tokens: int | None = Field(default=None, ge=256)
    max_seconds: int = Field(default=3600, ge=30, le=604_800)


class GoalUsage(BaseModel):
    turns: int = 0
    tool_calls: int = 0
    model_calls: int = 0
    tokens: int = 0
    elapsed_seconds: float = 0


class Subgoal(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    objective: str
    status: SubgoalStatus = SubgoalStatus.PENDING
    owner: str | None = None
    result: str | None = None


class GoalState(BaseModel):
    goal_id: str = Field(default_factory=lambda: uuid4().hex)
    run_id: str
    objective: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    status: GoalStatus = GoalStatus.ACTIVE
    subgoals: list[Subgoal] = Field(default_factory=list)
    budget: GoalBudget = Field(default_factory=GoalBudget)
    usage: GoalUsage = Field(default_factory=GoalUsage)
    waiting_reason: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class BilingualText(BaseModel):
    zh: str
    en: str


class PlanItem(BaseModel):
    id: str
    description: BilingualText
    files: list[str] = Field(default_factory=list)


class CodingPlan(BaseModel):
    task_type: str
    risk: Literal["low", "medium", "high"] = "medium"
    summary: BilingualText
    items: list[PlanItem]
    verification: list[str] = Field(default_factory=list)


class ReviewReport(BaseModel):
    summary: BilingualText
    changes: list[BilingualText] = Field(default_factory=list)
    verification: list[BilingualText] = Field(default_factory=list)
    residual_risks: list[BilingualText] = Field(default_factory=list)
