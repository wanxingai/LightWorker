"""Persistent goal-mode state and agent-facing goal tools."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from .models import GoalBudget, GoalState, GoalStatus, Subgoal, SubgoalStatus, ToolCategory
from .storage import RunStore
from .tool_protocol import tool_info


class GoalManager:
    def __init__(self, store: RunStore, run_id: str):
        self.store = store
        self.run_id = run_id

    def create(
        self,
        objective: str,
        *,
        acceptance_criteria: list[str] | None = None,
        budget: GoalBudget | None = None,
    ) -> GoalState:
        path = self.store.artifact_path(self.run_id, "goal.json")
        if path.is_file():
            return self.load()
        goal = GoalState(
            run_id=self.run_id,
            objective=objective,
            acceptance_criteria=list(acceptance_criteria or []),
            budget=budget or GoalBudget(),
        )
        self.save(goal)
        return goal

    def load(self) -> GoalState:
        return GoalState.model_validate(self.store.read_json(self.run_id, "goal.json"))

    def save(self, goal: GoalState) -> GoalState:
        goal.updated_at = datetime.now(UTC)
        self.store.write_json(self.run_id, "goal.json", goal.model_dump(mode="json"))
        return goal

    def update_status(self, status: GoalStatus, reason: str | None = None) -> GoalState:
        goal = self.load()
        goal.status = status
        goal.waiting_reason = reason
        return self.save(goal)

    def add_subgoal(self, objective: str, owner: str | None = None) -> Subgoal:
        goal = self.load()
        subgoal = Subgoal(objective=objective, owner=owner)
        goal.subgoals.append(subgoal)
        self.save(goal)
        return subgoal

    def update_subgoal(
        self,
        subgoal_id: str,
        status: SubgoalStatus,
        result: str | None = None,
    ) -> Subgoal:
        goal = self.load()
        subgoal = next((item for item in goal.subgoals if item.id == subgoal_id), None)
        if subgoal is None:
            raise ValueError("subgoal not found")
        subgoal.status = status
        subgoal.result = result
        self.save(goal)
        return subgoal

    def add_usage(
        self,
        *,
        turns: int = 0,
        tool_calls: int = 0,
        model_calls: int = 0,
        tokens: int = 0,
        elapsed_seconds: float = 0,
    ) -> GoalState:
        goal = self.load()
        goal.usage.turns += turns
        goal.usage.tool_calls += tool_calls
        goal.usage.model_calls += model_calls
        goal.usage.tokens += tokens
        goal.usage.elapsed_seconds += elapsed_seconds
        return self.save(goal)

    def exceeded_budget(self) -> str | None:
        goal = self.load()
        pairs = (
            ("turns", goal.usage.turns, goal.budget.max_turns),
            ("tool_calls", goal.usage.tool_calls, goal.budget.max_tool_calls),
            ("model_calls", goal.usage.model_calls, goal.budget.max_model_calls),
            ("seconds", goal.usage.elapsed_seconds, goal.budget.max_seconds),
        )
        for name, used, limit in pairs:
            if used >= limit:
                return f"goal budget exceeded: {name} {used}/{limit}"
        if goal.budget.max_tokens is not None and goal.usage.tokens >= goal.budget.max_tokens:
            return f"goal budget exceeded: tokens {goal.usage.tokens}/{goal.budget.max_tokens}"
        return None


class GoalTools:
    def __init__(self, manager: GoalManager):
        self.manager = manager
        self.tools = [self.goal_get, self.goal_add_subgoal, self.goal_update_subgoal]

    @tool_info(
        "goal_get",
        "Read the current objective, acceptance criteria, budget, and subgoals.",
        [],
        category=ToolCategory.GOAL,
    )
    def goal_get(self) -> str:
        return self.manager.load().model_dump_json()

    @tool_info(
        "goal_add_subgoal",
        "Add a concrete subgoal when the task benefits from explicit decomposition.",
        [
            {"name": "objective", "description": "Subgoal objective", "type": "string", "required": True},
            {"name": "owner", "description": "Optional agent owner", "type": "string", "required": False},
        ],
        category=ToolCategory.GOAL,
        is_read_only=False,
        is_write=True,
    )
    def goal_add_subgoal(self, objective: str, owner: str = "") -> str:
        return self.manager.add_subgoal(objective, owner or None).model_dump_json()

    @tool_info(
        "goal_update_subgoal",
        "Update a subgoal with tool-confirmed progress or a blocking condition.",
        [
            {"name": "subgoal_id", "description": "Subgoal identifier", "type": "string", "required": True},
            {
                "name": "status",
                "description": "pending, active, completed, blocked, or cancelled",
                "type": "string",
                "required": True,
            },
            {"name": "result", "description": "Evidence-bounded result", "type": "string", "required": False},
        ],
        category=ToolCategory.GOAL,
        is_read_only=False,
        is_write=True,
    )
    def goal_update_subgoal(self, subgoal_id: str, status: str, result: str = "") -> str:
        try:
            state = SubgoalStatus(status)
            value = self.manager.update_subgoal(subgoal_id, state, result or None)
            return value.model_dump_json()
        except ValueError as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
