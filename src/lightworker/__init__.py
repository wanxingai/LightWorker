"""LightWorker public package."""

from .config import WorkerConfig
from .models import (
    RunRecord,
    RunStatus,
    TaskSpec,
    VerificationCommand,
    VerificationResult,
)

__all__ = [
    "RunRecord",
    "RunStatus",
    "TaskSpec",
    "VerificationCommand",
    "VerificationResult",
    "WorkerConfig",
]

__version__ = "0.4.0"
