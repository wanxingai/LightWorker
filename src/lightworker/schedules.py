"""Persistent schedules claimed transactionally by the local control plane."""

from __future__ import annotations

import hashlib
import json
import time
from uuid import uuid4

from .models import ApprovalPolicy, ToolCategory
from .tool_protocol import tool_info


class ScheduleStore:
    def __init__(self, store):
        self.store = store
        self.run_id = "lightworker-scheduler"

    def _load(self):
        try:
            return self.store.read_json(self.run_id, "schedules.json")
        except FileNotFoundError:
            return {"schedules": {}}

    def list(self):
        return list(self._load()["schedules"].values())

    def create(self, *, task, source_run_id, next_at=None, interval_seconds=None):
        if not str(task).strip() or (interval_seconds is not None and interval_seconds < 60):
            raise ValueError("task is required; recurring interval must be at least 60 seconds")
        self.store.load(source_run_id)
        with self.store.transaction(self.run_id):
            state = self._load()
            schedule = {
                "schedule_id": uuid4().hex,
                "task": task,
                "source_run_id": source_run_id,
                "next_at": next_at or time.time(),
                "interval_seconds": interval_seconds,
                "status": "active",
                "occurrence": 0,
                "lease_until": 0,
                "lease_epoch": 0,
                "pending_run_id": None,
                "last_run_id": None,
                "last_error": None,
            }
            state["schedules"][schedule["schedule_id"]] = schedule
            self.store.write_json(self.run_id, "schedules.json", state)
            self.store.session(self.run_id).append("schedule.created", {"schedule": schedule})
        return schedule

    def control(self, schedule_id, action):
        status = {"pause": "paused", "resume": "active", "delete": "deleted"}.get(action)
        if not status:
            raise ValueError("invalid schedule action")
        with self.store.transaction(self.run_id):
            state = self._load()
            value = state["schedules"][schedule_id]
            value["status"] = status
            value["lease_epoch"] = value.get("lease_epoch", 0) + 1
            value["lease_until"] = 0
            self.store.write_json(self.run_id, "schedules.json", state)
            self.store.session(self.run_id).append("schedule.updated", {"schedule": value})
        return value

    def due(self, now=None):
        now = now or time.time()
        claimed = []
        with self.store.transaction(self.run_id):
            state = self._load()
            for item in state["schedules"].values():
                if item["status"] != "active" or item["next_at"] > now or item["lease_until"] > now:
                    continue
                occurrence = item["occurrence"] + 1
                identity = f"schedule:{item['schedule_id']}:{occurrence}"
                item.update(
                    lease_until=now + 60,
                    lease_epoch=item.get("lease_epoch", 0) + 1,
                    pending_run_id=hashlib.sha256(identity.encode()).hexdigest()[:32],
                )
                claimed.append(dict(item))
            if claimed:
                self.store.write_json(self.run_id, "schedules.json", state)
        return claimed

    def dispatched(self, schedule_id, *, epoch, error=None):
        with self.store.transaction(self.run_id):
            state = self._load()
            item = state["schedules"][schedule_id]
            if item["status"] != "active" or item["lease_epoch"] != epoch:
                raise ValueError("stale schedule claim")
            if error:
                item.update(last_error=str(error), lease_until=0, next_at=time.time() + 60)
            else:
                item.update(
                    occurrence=item["occurrence"] + 1,
                    last_run_id=item["pending_run_id"],
                    pending_run_id=None,
                    lease_until=0,
                    last_error=None,
                )
                if item["interval_seconds"]:
                    item["next_at"] = time.time() + item["interval_seconds"]
                else:
                    item["status"] = "completed"
            self.store.write_json(self.run_id, "schedules.json", state)
            self.store.session(self.run_id).append("schedule.dispatched", {"schedule": item})


class ScheduleTools:
    def __init__(self, schedules, run_id):
        self.schedules, self.run_id = schedules, run_id
        self.tools = [self.schedule_create, self.schedule_list, self.schedule_control]

    @tool_info(
        "schedule_create",
        "Schedule a future task in this workspace. Human approval is required.",
        [
            {"name": "task", "type": "string", "required": True},
            {"name": "next_at", "type": "number", "required": False},
            {"name": "interval_seconds", "type": "integer", "required": False},
        ],
        category=ToolCategory.GOAL,
        is_read_only=False,
        is_write=True,
        approval_policy=ApprovalPolicy.ALWAYS,
    )
    def schedule_create(self, task, next_at=None, interval_seconds=None):
        return json.dumps(
            self.schedules.create(
                task=task, source_run_id=self.run_id, next_at=next_at, interval_seconds=interval_seconds
            )
        )

    @tool_info("schedule_list", "List schedules in the current workspace.", [], category=ToolCategory.GOAL)
    def schedule_list(self):
        return json.dumps([s for s in self.schedules.list() if s["source_run_id"] == self.run_id])

    @tool_info(
        "schedule_control",
        "Pause, resume or delete an owned schedule.",
        [
            {"name": "schedule_id", "type": "string", "required": True},
            {"name": "action", "type": "string", "required": True},
        ],
        category=ToolCategory.GOAL,
        is_read_only=False,
        is_write=True,
        approval_policy=ApprovalPolicy.ALWAYS,
    )
    def schedule_control(self, schedule_id, action):
        value = next((s for s in self.schedules.list() if s["schedule_id"] == schedule_id), None)
        if not value or value["source_run_id"] != self.run_id:
            raise ValueError("schedule is not owned by this task")
        return json.dumps(self.schedules.control(schedule_id, action))
