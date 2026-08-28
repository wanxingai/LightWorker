"""Safe-boundary synchronization with LightAgent's native runtime state."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from .models import RunStatus

try:
    from LightAgent import (
        AgentRuntime,
        InboxMessageStatus,
        InboxMessageType,
        PolicyHook,
    )
    from LightAgent import GoalStatus as NativeGoalStatus
except ImportError:  # pragma: no cover
    AgentRuntime = None  # type: ignore[assignment]
    NativeGoalStatus = None  # type: ignore[assignment]
    InboxMessageStatus = None  # type: ignore[assignment]
    InboxMessageType = None  # type: ignore[assignment]
    PolicyHook = None  # type: ignore[assignment]


class NativeRuntimeLifecycle:
    """Mirror LightWorker lifecycle facts into the active LightAgent Session.

    Mutations made while a model run is active always use that agent's own
    ``AgentRuntime``. Terminal synchronization opens a fresh runtime only after
    model execution has stopped, avoiding stale read-modify-write races.
    """

    def __init__(
        self,
        *,
        session_id: str,
        run_id: str,
        objective: str,
        acceptance_criteria: list[str],
        parent_run_id: str | None = None,
        root_run_id: str | None = None,
        queue_item_id: str | None = None,
        local_goal_id: str | None = None,
    ):
        self.session_id = session_id
        self.run_id = run_id
        self.objective = objective
        self.acceptance_criteria = list(acceptance_criteria)
        self.parent_run_id = parent_run_id
        self.root_run_id = root_run_id or run_id
        self.queue_item_id = queue_item_id
        self.local_goal_id = local_goal_id
        self.agent: Any | None = None
        self.native_goal_id: str | None = None
        self.followup_message_id: str | None = None
        self._initialized = False

    def bind(self, agent: Any) -> None:
        self.agent = agent

    def hook(self) -> Any:
        if PolicyHook is None:
            raise RuntimeError(
                "LightAgent native lifecycle API is unavailable; install LightAgent>=0.10,<0.16"
            )
        return PolicyHook(
            self._handle_hook,
            phases={"before_run", "before_model_request"},
            failure_mode="continue",
            name="lightworker_native_runtime_lifecycle",
        )

    def _handle_hook(self, context: Any) -> None:
        if context.phase == "before_model_request":
            steering = context.payload.get("lightworker_steering") or []
            if isinstance(steering, list):
                self.deliver_steering([str(message) for message in steering if str(message)])
            return
        if self._initialized:
            return
        runtime = self._active_runtime()
        if runtime is None:
            return
        self._ensure_goal(runtime)
        self._ensure_followup(runtime)
        self._initialized = True

    def deliver_steering(self, messages: list[str]) -> None:
        """Record steering only when it is being delivered to a model request."""
        runtime = self._active_runtime()
        if runtime is None or InboxMessageType is None:
            return
        for content in messages:
            message = runtime.inbox.enqueue(
                InboxMessageType.STEERING,
                content,
                message_id=f"steering-{self.run_id}-{uuid4().hex}",
                correlation_id=self.root_run_id,
                metadata={"lightworker_run_id": self.run_id, "delivery": "before_model_request"},
            )
            claimed = runtime.inbox.claim_next(safe_boundary=True)
            if claimed is not None and claimed.message_id == message.message_id:
                runtime.inbox.complete(
                    claimed.message_id,
                    result={"delivered": True, "phase": "before_model_request"},
                )

    def finalize(
        self,
        session_store: Any,
        status: RunStatus | str,
        reason: str = "",
        *,
        budget_limits: Any | None = None,
    ) -> dict[str, Any] | None:
        """Synchronize a stopped LightWorker run and return a native snapshot."""
        if session_store is None or AgentRuntime is None:
            return None
        runtime = AgentRuntime(session_store=session_store, budget_limits=budget_limits)
        session = runtime.open_session(self.session_id)
        if session is None:
            return None
        resolved = RunStatus(status)
        goal = self._find_goal(runtime)
        if goal is not None and NativeGoalStatus is not None:
            if resolved == RunStatus.SUCCEEDED:
                if goal.status != NativeGoalStatus.COMPLETED:
                    runtime.goals.complete(
                        goal.goal_id,
                        evidence=[{"lightworker_run_id": self.run_id, "status": resolved.value}],
                    )
            elif resolved == RunStatus.CANCELLED:
                if goal.status != NativeGoalStatus.CANCELLED:
                    runtime.goals.cancel(goal.goal_id, reason or "task cancelled by user")
            elif goal.status not in {NativeGoalStatus.COMPLETED, NativeGoalStatus.CANCELLED}:
                runtime.goals.block(goal.goal_id, reason or f"LightWorker status: {resolved.value}")

        message_id = self.followup_message_id or self._followup_id()
        message = runtime.inbox.get(message_id) if message_id else None
        if message is not None and InboxMessageStatus is not None:
            if resolved == RunStatus.SUCCEEDED and message.status == InboxMessageStatus.CLAIMED:
                runtime.inbox.complete(message.message_id, result={"run_status": resolved.value})
            elif resolved == RunStatus.CANCELLED and message.status in {
                InboxMessageStatus.PENDING,
                InboxMessageStatus.CLAIMED,
            }:
                runtime.inbox.reject(message.message_id, reason or "task cancelled by user")

        paused = {
            RunStatus.FAILED,
            RunStatus.INTERRUPTED,
            RunStatus.NEEDS_ATTENTION,
            RunStatus.PAUSED,
            RunStatus.WAITING_INPUT,
            RunStatus.WAITING_APPROVAL,
            RunStatus.BUDGET_LIMITED,
        }
        if resolved in paused:
            runtime.pause(reason or f"LightWorker status: {resolved.value}")
        elif resolved == RunStatus.CANCELLED:
            runtime.cancel(reason or "task cancelled by user")
        return runtime.snapshot()

    def _active_runtime(self) -> Any | None:
        runtime = getattr(self.agent, "runtime", None)
        if runtime is None or getattr(runtime, "session", None) is None:
            return None
        return runtime

    def _ensure_goal(self, runtime: Any) -> None:
        goal = self._find_goal(runtime)
        if goal is None:
            goal = runtime.goals.create(
                self.objective,
                acceptance_criteria=self.acceptance_criteria,
                metadata={
                    "lightworker_run_id": self.run_id,
                    "lightworker_goal_id": self.local_goal_id,
                    "root_run_id": self.root_run_id,
                },
            )
        self.native_goal_id = goal.goal_id
        if NativeGoalStatus is not None and goal.status in {
            NativeGoalStatus.PENDING,
            NativeGoalStatus.BLOCKED,
        }:
            was_blocked = goal.status == NativeGoalStatus.BLOCKED
            runtime.goals.activate(goal.goal_id)
            if was_blocked:
                runtime.resume("LightWorker execution resumed")

    def _find_goal(self, runtime: Any) -> Any | None:
        for goal in runtime.goals.list():
            if goal.metadata.get("lightworker_run_id") == self.run_id:
                return goal
        return None

    def _ensure_followup(self, runtime: Any) -> None:
        message_id = self._followup_id()
        if message_id is None or InboxMessageType is None or InboxMessageStatus is None:
            return
        self.followup_message_id = message_id
        message = runtime.inbox.enqueue(
            InboxMessageType.FOLLOWUP,
            self.objective,
            message_id=message_id,
            correlation_id=self.root_run_id,
            metadata={
                "lightworker_run_id": self.run_id,
                "parent_run_id": self.parent_run_id,
                "queue_item_id": self.queue_item_id,
            },
        )
        if message.status == InboxMessageStatus.PENDING:
            runtime.inbox.claim_next(safe_boundary=True)

    def _followup_id(self) -> str | None:
        if not self.parent_run_id:
            return None
        return self.queue_item_id or f"followup-{self.run_id}"


__all__ = ["NativeRuntimeLifecycle"]
