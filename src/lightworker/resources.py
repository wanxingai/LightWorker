"""Process-wide bounded resources shared by concurrently scheduled tasks."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from .config import SchedulerConfig


class ResourcePool:
    def __init__(self, config: SchedulerConfig):
        self.model = threading.BoundedSemaphore(config.max_model_calls)
        self.browser = threading.BoundedSemaphore(config.max_browsers)
        self.container = threading.BoundedSemaphore(config.max_containers)


_POOLS: dict[str, ResourcePool] = {}
_LOCK = threading.RLock()


def resource_pool(state_dir: Path, config: SchedulerConfig) -> ResourcePool:
    key = str(state_dir.expanduser().resolve())
    with _LOCK:
        pool = _POOLS.get(key)
        if pool is None:
            pool = ResourcePool(config)
            _POOLS[key] = pool
        return pool


class _CompletionProxy:
    def __init__(self, target: Any, semaphore: threading.Semaphore):
        self._target = target
        self._semaphore = semaphore

    def create(self, *args: Any, **kwargs: Any) -> Any:
        with self._semaphore:
            return self._target.create(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target, name)


def limit_agent_model_calls(agent: Any, semaphore: threading.Semaphore) -> Any:
    """Limit each actual provider request without holding a slot while tools or subagents run."""
    try:
        completions = agent.client.chat.completions
        if not isinstance(completions, _CompletionProxy):
            agent.client.chat.completions = _CompletionProxy(completions, semaphore)
    except (AttributeError, TypeError):
        # Test doubles and custom providers may not expose the OpenAI-compatible resource tree.
        pass
    return agent
