"""Durable, epoch-fenced background jobs with cooperative cancellation."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import uuid4

from .control import ControlStore
from .models import ToolCategory
from .policy import redact_text, redact_value
from .storage import RunStore
from .tool_protocol import EventLog, tool_info

TERMINAL = {"completed", "failed", "cancelled"}
_executors: dict[str, ThreadPoolExecutor] = {}
_executor_lock = threading.RLock()
_handlers: dict[tuple[str, str, str], Callable] = {}


class JobInterrupted(RuntimeError):
    pass


class JobContext:
    def __init__(self, registry, job_id, epoch):
        self.registry, self.job_id, self.epoch = registry, job_id, epoch

    def checkpoint(self):
        job = self.registry.get(self.job_id)
        control = ControlStore(self.registry.store, self.registry.run_id).state()
        if (
            job["lease_epoch"] != self.epoch
            or job["status"] != "running"
            or control["state"] != "running"
            or job["lease_until"] < time.time()
        ):
            raise JobInterrupted("job interrupted, paused, cancelled, or lease lost")

    def output(self, value):
        self.checkpoint()
        self.registry.output(self.job_id, value, epoch=self.epoch)


class JobRegistry:
    def __init__(self, store: RunStore, run_id: str, *, max_workers=4, lease_seconds=60):
        self.store, self.run_id = store, run_id
        self.lease_seconds = lease_seconds
        self.owner = uuid4().hex
        key = str(store.state_dir)
        with _executor_lock:
            self.executor = _executors.setdefault(
                key, ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="lightworker-job")
            )

    def _load(self):
        try:
            return self.store.read_json(self.run_id, "jobs.json")
        except FileNotFoundError:
            return {"jobs": {}}

    def _save(self, value, event, job):
        self.store.write_json(self.run_id, "jobs.json", value)
        self.store.session(self.run_id).append("lightworker." + event, {"job": job})
        EventLog(self.store, self.run_id).emit(event.replace(".", "_"), {"job": job})

    def register_handler(self, kind, handler):
        _handlers[(str(self.store.state_dir), self.run_id, kind)] = handler

    def unregister_handlers(self):
        for key in list(_handlers):
            if key[:2] == (str(self.store.state_dir), self.run_id):
                _handlers.pop(key, None)

    def dispatch(self, job_id):
        job = self.get(job_id)
        handler = _handlers.get((str(self.store.state_dir), self.run_id, job["kind"]))
        if handler is None:
            raise ValueError("job provider is not active; resume its parent task first")
        return self.start(job_id, lambda context: handler(job, context))

    def create(
        self, kind: str, payload: dict[str, Any], *, agent_id=None, parent_job_id=None, idempotency_key=None
    ):
        with self.store.transaction(self.run_id):
            value = self._load()
            if idempotency_key:
                existing = next(
                    (j for j in value["jobs"].values() if j.get("idempotency_key") == idempotency_key), None
                )
                if existing:
                    return dict(existing)
            job = {
                "job_id": uuid4().hex,
                "kind": kind,
                "payload": redact_value(payload),
                "status": "pending",
                "agent_id": agent_id,
                "parent_job_id": parent_job_id,
                "lease_epoch": 0,
                "lease_until": 0,
                "owner": None,
                "process_id": os.getpid(),
                "created_at": time.time(),
                "output": [],
                "output_cursor": 0,
                "result": None,
                "error": None,
                "idempotency_key": idempotency_key,
            }
            value["jobs"][job["job_id"]] = job
            self._save(value, "job.created", job)
            return dict(job)

    def get(self, job_id):
        job = self._load()["jobs"].get(job_id)
        if job is None:
            raise KeyError(job_id)
        return dict(job)

    def list(self):
        return list(self._load()["jobs"].values())

    def claim(self, job_id):
        with self.store.transaction(self.run_id):
            value = self._load()
            job = value["jobs"][job_id]
            if job["status"] == "running" and job["lease_until"] > time.time():
                return None
            if job["status"] not in {"pending", "interrupted", "paused", "running"}:
                return None
            job.update(
                status="running",
                owner=self.owner,
                process_id=os.getpid(),
                lease_epoch=job["lease_epoch"] + 1,
                lease_until=time.time() + self.lease_seconds,
                started_at=time.time(),
            )
            self._save(value, "job.started", job)
            return dict(job)

    def renew(self, job_id, epoch):
        with self.store.transaction(self.run_id):
            value = self._load()
            job = value["jobs"][job_id]
            if (
                job["status"] != "running"
                or job["lease_epoch"] != epoch
                or job["owner"] != self.owner
                or job["lease_until"] < time.time()
            ):
                raise JobInterrupted("stale job lease")
            job["lease_until"] = time.time() + self.lease_seconds
            self.store.write_json(self.run_id, "jobs.json", value)

    def output(self, job_id, output, *, epoch):
        with self.store.transaction(self.run_id):
            value = self._load()
            job = value["jobs"][job_id]
            self._fence(job, epoch)
            rendered = (
                output if isinstance(output, str) else json.dumps(output, ensure_ascii=False, default=str)
            )
            artifact = f"logs/job-{job_id}.log"
            self.store.append_text(self.run_id, artifact, redact_text(rendered) + "\n")
            cursor = job.get("output_cursor", len(job["output"])) + 1
            item = {"cursor": cursor, "value": redact_value(output)}
            if len(rendered) > 4096:
                item["value"] = redact_text(rendered[:4096]) + "…[full output in job log]"
            job["output"].append(item)
            job["output"] = job["output"][-64:]
            job["output_cursor"] = cursor
            job["output_artifact"] = artifact
            self._save(value, "job.output", job)

    def finish(self, job_id, epoch, result=None, error=None):
        with self.store.transaction(self.run_id):
            value = self._load()
            job = value["jobs"][job_id]
            self._fence(job, epoch)
            job.update(
                status="failed" if error else "completed",
                result=redact_value(result),
                error=str(error) if error else None,
                completed_at=time.time(),
            )
            self._save(value, "job.failed" if error else "job.completed", job)

    def _fence(self, job, epoch):
        if (
            job["status"] != "running"
            or job["lease_epoch"] != epoch
            or job["owner"] != self.owner
            or job["lease_until"] < time.time()
        ):
            raise JobInterrupted("stale job cannot publish output or completion")

    def control(self, job_id, action):
        target = {"pause": "paused", "cancel": "cancelled", "resume": "pending"}.get(action)
        if target is None:
            raise ValueError("unsupported job action")
        with self.store.transaction(self.run_id):
            value = self._load()
            job = value["jobs"][job_id]
            if job["status"] in TERMINAL:
                raise ValueError("terminal job cannot be controlled")
            if action == "resume" and job["status"] not in {"paused", "interrupted"}:
                raise ValueError("only paused/interrupted jobs can resume")
            if action == "resume" and (str(self.store.state_dir), self.run_id, job["kind"]) not in _handlers:
                raise ValueError("job provider is not active; resume its parent task first")
            job.update(status=target, lease_epoch=job["lease_epoch"] + 1, lease_until=0)
            self._save(value, f"job.{target}", job)
            if action in {"cancel", "pause"}:
                children = [
                    j["job_id"]
                    for j in value["jobs"].values()
                    if j.get("parent_job_id") == job_id and j["status"] not in TERMINAL
                ]
                for child in children:
                    self.control(child, action)
            if action == "resume":
                self.dispatch(job_id)
            return dict(job)

    def recover(self):
        with self.store.transaction(self.run_id):
            value = self._load()
            for job in value["jobs"].values():
                alive = True
                try:
                    if job.get("process_id"):
                        os.kill(job["process_id"], 0)
                except ProcessLookupError:
                    alive = False
                if (job["status"] == "running" and job["lease_until"] <= time.time()) or (
                    job["status"] in {"pending", "running"} and not alive
                ):
                    job.update(status="interrupted", error="worker lease expired", lease_until=0)
                    self._save(value, "job.interrupted", job)
        return self.list()

    def start(self, job_id, operation: Callable[[JobContext], Any]):
        claimed = self.claim(job_id)
        if claimed is None:
            return None
        epoch = claimed["lease_epoch"]
        return self.executor.submit(self._drive, job_id, epoch, operation)

    def _drive(self, job_id, epoch, operation):
        stop = threading.Event()

        def heartbeat():
            while not stop.wait(max(0.01, self.lease_seconds / 3)):
                try:
                    self.renew(job_id, epoch)
                except JobInterrupted:
                    return

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        context = JobContext(self, job_id, epoch)
        try:
            context.checkpoint()
            result = operation(context)
            context.checkpoint()
            self.finish(job_id, epoch, result=result)
        except JobInterrupted:
            with self.store.transaction(self.run_id):
                state = self._load()
                job = state["jobs"][job_id]
                if job["status"] == "running" and job["lease_epoch"] == epoch and job["owner"] == self.owner:
                    control = ControlStore(self.store, self.run_id).state()["state"]
                    job.update(
                        status=control if control in {"paused", "cancelled"} else "interrupted", lease_until=0
                    )
                    self._save(state, "job.interrupted", job)
                    if job["kind"] == "task" and control in {"paused", "cancelled"}:
                        from .models import RunStatus

                        try:
                            record = self.store.load(self.run_id)
                        except FileNotFoundError:
                            pass
                        else:
                            if record.status in {RunStatus.CREATED, RunStatus.PREPARING, RunStatus.RUNNING}:
                                record.status = RunStatus(control)
                                record.error = f"task {control} before its activation completed"
                                self.store.save(record)
        except Exception as exc:
            try:
                self.finish(job_id, epoch, error=str(exc))
            except JobInterrupted:
                pass
        finally:
            stop.set()
        return self.get(job_id)


class JobTools:
    def __init__(self, registry):
        self.registry = registry
        self.tools = [self.job_list, self.job_output, self.job_control]

    @tool_info("job_list", "List durable background jobs and their status.", [], category=ToolCategory.AGENT)
    def job_list(self):
        return json.dumps([j for j in self.registry.recover() if j["kind"] != "task"], ensure_ascii=False)

    @tool_info(
        "job_output",
        "Read a job result and output after a cursor.",
        [
            {"name": "job_id", "type": "string", "required": True},
            {"name": "after", "type": "integer", "required": False},
        ],
        category=ToolCategory.AGENT,
    )
    def job_output(self, job_id, after=0):
        job = self.registry.get(job_id)
        job["output_dropped_before"] = job["output"][0]["cursor"] - 1 if job["output"] else 0
        job["output"] = [item for item in job["output"] if item["cursor"] > after]
        return json.dumps(job, ensure_ascii=False)

    @tool_info(
        "job_control",
        "Pause, resume or cancel an owned background job.",
        [
            {"name": "job_id", "type": "string", "required": True},
            {"name": "action", "type": "string", "required": True},
        ],
        category=ToolCategory.AGENT,
        is_read_only=False,
        is_write=True,
    )
    def job_control(self, job_id, action):
        if self.registry.get(job_id)["kind"] == "task":
            raise ValueError("control the parent task through its task controls, not a background tool")
        return json.dumps(self.registry.control(job_id, action), ensure_ascii=False)
