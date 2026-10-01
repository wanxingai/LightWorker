"""Provider-backed durable child conversations and FIFO activations."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from LightAgent import HookDecision, PolicyHook

from .jobs import JobInterrupted, JobRegistry
from .models import ToolCategory
from .resources import limit_agent_model_calls
from .subagents import ROLES
from .tool_protocol import metadata_for, tool_info

_active_services = {}


def active_agents(store, run_id):
    return _active_services.get((str(store.state_dir), run_id))


@dataclass(frozen=True)
class SubagentProvider:
    name: str
    continuation: bool = True
    fork: bool = False
    factory: Any = None


class ContinuableAgents:
    def __init__(
        self,
        *,
        store,
        run_id,
        agent_factory,
        tools,
        max_agents=8,
        max_depth=2,
        parent_id="supervisor",
        depth=0,
        jobs=None,
        model_semaphore=None,
    ):
        self.store, self.run_id, self.agent_factory = store, run_id, agent_factory
        self.read_tools = [t for t in tools if metadata_for(t).is_read_only]
        self.max_agents, self.max_depth, self.depth, self.parent_id = max_agents, max_depth, depth, parent_id
        self.jobs = jobs or JobRegistry(store, run_id)
        self.model_semaphore = model_semaphore
        _active_services[(str(store.state_dir), run_id)] = self
        self.providers = {"spawn": SubagentProvider("spawn"), "fork": SubagentProvider("fork", fork=True)}
        self.jobs.register_handler("subagent", lambda job, ctx: self._drive(job["payload"]["agent_id"], ctx))
        self.tools = [
            self.spawn_agent,
            self.send_message,
            self.list_agents,
            self.interrupt_agent,
            self.resume_agent,
        ]

    def close(self):
        _active_services.pop((str(self.store.state_dir), self.run_id), None)

    def recover(self):
        self.jobs.recover()
        for agent in self._load()["agents"].values():
            job = self.jobs.get(agent["job_id"]) if agent.get("job_id") else None
            has_pending = any(m["status"] in {"pending", "claimed"} for m in agent["messages"])
            if has_pending and (
                not job or job["status"] in {"interrupted", "paused", "pending", "completed", "failed"}
            ):
                self.resume_agent(agent["agent_id"])

    def register_provider(self, provider):
        if provider.name in self.providers:
            raise ValueError("subagent provider already registered")
        self.providers[provider.name] = provider

    def _load(self):
        try:
            return self.store.read_json(self.run_id, "agents.json")
        except FileNotFoundError:
            return {"agents": {}}

    def _save(self, state, kind, agent):
        self.store.write_json(self.run_id, "agents.json", state)
        self.store.session(self.run_id).append(kind, {"agent": agent}, agent_id=agent["agent_id"])

    @tool_info(
        "spawn_agent",
        "Start a continuable read-only child with its own durable Session. "
        "Use spawn for independent work or fork for parent history. Returns agent/job IDs.",
        [
            {"name": "role", "type": "string", "required": True},
            {"name": "task", "type": "string", "required": True},
            {"name": "provider", "type": "string", "required": False},
        ],
        category=ToolCategory.AGENT,
        is_read_only=False,
        is_write=True,
    )
    def spawn_agent(self, role, task, provider="spawn"):
        if role not in ROLES or not str(task).strip():
            raise ValueError("invalid specialist role or task")
        descriptor = self.providers.get(provider)
        if descriptor is None or not descriptor.continuation:
            raise ValueError("unsupported continuation provider")
        with self.store.transaction(self.run_id):
            state = self._load()
            legacy = self.store.session(self.run_id).state("agent-tree.json") or {}
            total = len(state["agents"]) + len(legacy.get("agents", []))
            if total >= self.max_agents or self.depth >= self.max_depth:
                raise ValueError("agent tree/depth limit reached")
            agent_id = uuid4().hex[:12]
            session_id = f"{self.run_id}-child-{agent_id}"
            allowed = sorted(t.tool_info["tool_name"] for t in self.read_tools)
            agent = {
                "agent_id": agent_id,
                "session_id": session_id,
                "role": role,
                "provider": provider,
                "parent_id": self.parent_id,
                "depth": self.depth + 1,
                "status": "pending",
                "messages": [],
                "allowed_tools": allowed,
                "job_id": None,
                "result": None,
            }
            if descriptor.fork:
                parent = self.store.session(self.run_id).store.get(self.run_id)
                if parent:
                    fork = parent.fork(
                        new_session_id=session_id, metadata={"parent_agent_id": self.parent_id}
                    )
                    fork.events = [
                        e
                        for e in fork.events
                        if not e.type.startswith("lightworker.")
                        and not e.type.startswith(("job.", "agent.", "goal.", "control."))
                    ]
                    for sequence, event in enumerate(fork.events, 1):
                        event.sequence = sequence
                    self.store.session(self.run_id).store.create(fork)
            state["agents"][agent_id] = agent
            self._save(state, "agent.created", agent)
        return self.send_message(agent_id, task)

    @tool_info(
        "send_message",
        "Queue the next FIFO turn for a child. A running child consumes it "
        "after its current turn. An idle child starts a new activation.",
        [
            {"name": "agent_id", "type": "string", "required": True},
            {"name": "message", "type": "string", "required": True},
        ],
        category=ToolCategory.AGENT,
        is_read_only=False,
        is_write=True,
    )
    def send_message(self, agent_id, message):
        if not str(message).strip():
            raise ValueError("message cannot be blank")
        with self.store.transaction(self.run_id):
            state = self._load()
            agent = state["agents"][agent_id]
            agent["messages"].append({"id": uuid4().hex, "message": message, "status": "pending"})
            self._save(state, "agent.message", agent)
            if agent["status"] not in {"running", "paused", "interrupted"}:
                self._activate(agent_id)
            agent = self._load()["agents"][agent_id]
        return json.dumps({"agent_id": agent_id, "job_id": agent["job_id"], "queued": True})

    def _activate(self, agent_id):
        state = self._load()
        agent = state["agents"][agent_id]
        job = self.jobs.create("subagent", {"agent_id": agent_id}, agent_id=agent_id)
        agent.update(status="running", job_id=job["job_id"])
        self._save(state, "agent.activated", agent)
        self.jobs.start(job["job_id"], lambda ctx: self._drive(agent_id, ctx))

    def _drive(self, agent_id, context):
        try:
            while True:
                context.checkpoint()
                with self.store.transaction(self.run_id):
                    state = self._load()
                    agent = state["agents"][agent_id]
                    message = next(
                        (m for m in agent["messages"] if m["status"] in {"pending", "claimed"}), None
                    )
                    if message is None:
                        agent["status"] = "idle"
                        self._save(state, "agent.idle", agent)
                        return agent["result"]
                    message["status"] = "claimed"
                    message_id = message["id"]
                    text = message["message"]
                    self._save(state, "agent.turn.started", agent)
                provider = self.providers.get(agent["provider"])
                if provider is None:
                    raise ValueError("configured child provider is unavailable")
                factory = provider.factory or self.agent_factory
                allowed = set(agent["allowed_tools"])
                functions = [t for t in self.read_tools if t.tool_info["tool_name"] in allowed]
                child = factory.specialist(agent["role"], allowed_tools=allowed)

                def safe_boundary(ctx):
                    context.checkpoint()
                    return HookDecision.continue_()

                hook = PolicyHook(
                    safe_boundary,
                    phases={"before_tool_call", "before_model_request"},
                    failure_mode="block",
                    name="child_job_control",
                )
                hooks = getattr(child, "hooks", None)
                if hasattr(hooks, "hooks"):
                    hooks.hooks.append(hook)
                elif isinstance(hooks, list):
                    hooks.append(hook)

                # Also guard the functions for custom agents that do not run native hooks.
                def guarded(tool):
                    def invoke(**arguments):
                        context.checkpoint()
                        return tool(**arguments)

                    invoke.tool_info = tool.tool_info
                    return invoke

                if self.model_semaphore:
                    limit_agent_model_calls(child, self.model_semaphore)
                result = child.run(
                    text,
                    tools=[guarded(t) for t in functions],
                    trace=True,
                    result_format="object",
                    max_retry=4,
                    max_tool_iterations=8,
                    session_id=agent["session_id"],
                    run_group_id=self.run_id,
                    use_skills=False,
                )
                context.checkpoint()
                if getattr(result, "error", None):
                    raise RuntimeError(str(result.error))
                content = str(getattr(result, "content", result))
                if not content.strip() or content.startswith("Failed to generate"):
                    raise RuntimeError("child did not produce a valid response")
                with self.store.transaction(self.run_id):
                    context.checkpoint()
                    state = self._load()
                    agent = state["agents"][agent_id]
                    item = next(m for m in agent["messages"] if m["id"] == message_id)
                    item.update(status="completed", result=content)
                    agent["result"] = content
                    self._save(state, "agent.report", agent)
                context.output({"agent_id": agent_id, "message_id": message_id, "content": content})
        except JobInterrupted:
            with self.store.transaction(self.run_id):
                state = self._load()
                agent = state["agents"][agent_id]
                if agent["job_id"] == context.job_id:
                    job = self.jobs.get(context.job_id)
                    agent["status"] = (
                        job["status"] if job["status"] in {"paused", "cancelled"} else "interrupted"
                    )
                    self._save(state, "agent.interrupted", agent)
            raise
        except Exception:
            with self.store.transaction(self.run_id):
                state = self._load()
                agent = state["agents"][agent_id]
                agent["status"] = "failed"
                self._save(state, "agent.failed", agent)
            raise

    @tool_info(
        "list_agents",
        "List durable child conversations, jobs, results and pending turns.",
        [],
        category=ToolCategory.AGENT,
    )
    def list_agents(self):
        return json.dumps(list(self._load()["agents"].values()), ensure_ascii=False)

    @tool_info(
        "interrupt_agent",
        "Pause a child at its next safe boundary.",
        [{"name": "agent_id", "type": "string", "required": True}],
        category=ToolCategory.AGENT,
        is_read_only=False,
        is_write=True,
    )
    def interrupt_agent(self, agent_id):
        with self.store.transaction(self.run_id):
            state = self._load()
            agent = state["agents"][agent_id]
            self.jobs.control(agent["job_id"], "pause")
            agent["status"] = "paused"
            self._save(state, "agent.paused", agent)
        return json.dumps({"agent_id": agent_id, "status": "paused"})

    @tool_info(
        "resume_agent",
        "Resume a paused/interrupted child with the same durable Session.",
        [{"name": "agent_id", "type": "string", "required": True}],
        category=ToolCategory.AGENT,
        is_read_only=False,
        is_write=True,
    )
    def resume_agent(self, agent_id):
        with self.store.transaction(self.run_id):
            state = self._load()
            agent = state["agents"][agent_id]
            job = self.jobs.get(agent["job_id"])
            if job["status"] == "running" and job["lease_until"] > time.time():
                raise ValueError("child still has a live activation")
            agent["status"] = "interrupted"
            self._save(state, "agent.resumed", agent)
            self._activate(agent_id)
        return json.dumps({"agent_id": agent_id, "resumed": True})
