"""Risk-aware tool metadata, approval brokerage, and auditable dispatch wrappers."""

from __future__ import annotations

import functools
import hashlib
import inspect
import json
import threading
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from .models import ApprovalPolicy, ToolCategory, ToolMetadata
from .policy import redact_text, redact_value
from .storage import RunStore

ApprovalCheck = Callable[[dict[str, Any]], bool]


def tool_info(
    name: str,
    description: str,
    params: list[dict[str, Any]],
    *,
    category: ToolCategory | str = ToolCategory.WORKSPACE,
    is_read_only: bool = True,
    is_write: bool = False,
    is_destructive: bool = False,
    external_side_effect: bool = False,
    concurrency_safe: bool = True,
    sandbox_required: bool = False,
    network_required: bool = False,
    credential_scope: str | None = None,
    approval_policy: ApprovalPolicy | str = ApprovalPolicy.NEVER,
    timeout_seconds: int = 120,
    output_limit_bytes: int = 32_768,
    approval_check: ApprovalCheck | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorate a LightAgent tool with machine-readable LightWorker policy metadata."""

    metadata = ToolMetadata(
        category=category,
        is_read_only=is_read_only,
        is_write=is_write,
        is_destructive=is_destructive,
        external_side_effect=external_side_effect,
        concurrency_safe=concurrency_safe,
        sandbox_required=sandbox_required,
        network_required=network_required,
        credential_scope=credential_scope,
        approval_policy=approval_policy,
        timeout_seconds=timeout_seconds,
        output_limit_bytes=output_limit_bytes,
    )

    def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
        function.tool_info = {  # type: ignore[attr-defined]
            "tool_name": name,
            "tool_title": name.replace("_", " ").title(),
            "tool_description": description,
            "tool_params": params,
            "lightworker": metadata.model_dump(mode="json"),
            "approval_check": approval_check,
        }
        return function

    return decorate


def metadata_for(tool: Callable[..., Any]) -> ToolMetadata:
    info = getattr(tool, "tool_info", {})
    return ToolMetadata.model_validate(info.get("lightworker") or {})


class EventLog:
    """Append-only, redacted event stream used by the UI and recovery diagnostics."""

    def __init__(self, store: RunStore, run_id: str):
        self.store = store
        self.run_id = run_id
        self._lock = threading.RLock()
        self.session = store.session(run_id)
        self._migrate()

    def emit(self, event_type: str, data: dict[str, Any] | None = None, **fields: Any) -> dict[str, Any]:
        with self.session.store.transaction():
            native = self.session.append(
                "lightworker.event", {"ui_type": event_type, "payload": {**(data or {}), **fields}}
            )
            event = {
                "sequence": native.sequence,
                "type": event_type,
                "timestamp": native.timestamp,
                "data": redact_value({**(data or {}), **fields}),
            }
            self.store.append_text(
                self.run_id,
                "events.jsonl",
                json.dumps(event, ensure_ascii=False, default=str) + "\n",
            )
            return event

    def read(self, *, after: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        return [
            {
                "sequence": e.sequence,
                "type": e.data["ui_type"],
                "timestamp": e.timestamp,
                "data": e.data["payload"],
            }
            for e in self.session.events()
            if e.type == "lightworker.event" and e.sequence > after
        ][:limit]

    def _migrate(self) -> None:
        path = self.store.artifact_path(self.run_id, "events.jsonl")
        if not path.is_file():
            return
        with self.session.store.transaction():
            if any(e.type == "lightworker.events.migrated" for e in self.session.events()):
                return
            if any(e.type == "lightworker.event" for e in self.session.events()):
                self.session.append("lightworker.events.migrated", {"source": "compatibility-cache"})
                return
            self._import_legacy(path)
            self.session.append("lightworker.events.migrated", {"source": "events.jsonl"})

    def _import_legacy(self, path) -> None:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                self.session.append(
                    "lightworker.event",
                    {"ui_type": value.get("type"), "payload": value.get("data", {})},
                    idempotency_key=f"legacy-ui-{value.get('sequence')}",
                )


class ApprovalBroker:
    """Durable exact-argument approvals; changed arguments always require a new decision."""

    def __init__(self, store: RunStore, run_id: str, events: EventLog | None = None):
        self.store = store
        self.run_id = run_id
        self.events = events
        self._lock = threading.RLock()

    @staticmethod
    def fingerprint(tool_name: str, arguments: dict[str, Any]) -> str:
        canonical = json.dumps(
            arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        )
        return hashlib.sha256(f"{tool_name}\n{canonical}".encode()).hexdigest()

    def request(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        metadata: ToolMetadata,
    ) -> dict[str, Any]:
        with self.store.transaction(self.run_id):
            payload = self._load()
            fingerprint = self.fingerprint(tool_name, arguments)
            existing = next(
                (item for item in payload["requests"] if item.get("fingerprint") == fingerprint),
                None,
            )
            if existing is not None:
                return existing
            request = {
                "request_id": uuid4().hex,
                "fingerprint": fingerprint,
                "tool": tool_name,
                "arguments": redact_value(arguments),
                "category": metadata.category.value,
                "reason": _approval_reason(metadata),
                "status": "pending",
                "created_at": datetime.now(UTC).isoformat(),
            }
            payload["requests"].append(request)
            self._save(payload)
            if self.events:
                self.events.emit("approval_requested", request)
            return request

    def decision(self, tool_name: str, arguments: dict[str, Any]) -> str | None:
        fingerprint = self.fingerprint(tool_name, arguments)
        payload = self._load()
        value = payload["decisions"].get(fingerprint)
        return str(value.get("decision")) if isinstance(value, dict) else None

    def decide(self, request_id: str, decision: str, note: str = "") -> dict[str, Any]:
        if decision not in {"approved", "rejected"}:
            raise ValueError("decision must be approved or rejected")
        with self.store.transaction(self.run_id):
            payload = self._load()
            request = next(
                (item for item in payload["requests"] if item.get("request_id") == request_id),
                None,
            )
            if request is None:
                raise ValueError("approval request not found")
            request["status"] = decision
            request["decided_at"] = datetime.now(UTC).isoformat()
            request["note"] = redact_text(note)
            payload["decisions"][request["fingerprint"]] = {
                "decision": decision,
                "request_id": request_id,
                "note": redact_text(note),
                "decided_at": request["decided_at"],
            }
            self._save(payload)
            if self.events:
                self.events.emit("approval_decided", request)
            return request

    def pending(self) -> list[dict[str, Any]]:
        return [item for item in self._load()["requests"] if item.get("status") == "pending"]

    def _load(self) -> dict[str, Any]:
        try:
            value = self.store.read_json(self.run_id, "approvals.json")
        except (OSError, json.JSONDecodeError):
            return {"requests": [], "decisions": {}}
        if not isinstance(value, dict):
            return {"requests": [], "decisions": {}}
        return {
            "requests": list(value.get("requests") or []),
            "decisions": dict(value.get("decisions") or {}),
        }

    def _save(self, value: dict[str, Any]) -> None:
        self.store.write_json(self.run_id, "approvals.json", value)


class ToolCatalog:
    """Wrap tools once so every invocation shares policy, approval, limits, and events."""

    def __init__(
        self,
        *,
        broker: ApprovalBroker,
        events: EventLog,
        control_check: Callable[[], str | None] | None = None,
        max_tool_calls: int = 80,
        max_repeat_calls: int = 4,
    ):
        self.broker = broker
        self.events = events
        self.control_check = control_check
        self.max_tool_calls = max_tool_calls
        self.max_repeat_calls = max_repeat_calls
        self.call_count = 0
        self._fingerprint_counts: dict[str, int] = {}
        self._lock = threading.RLock()

    def wrap_all(self, tools: Iterable[Callable[..., Any]]) -> list[Callable[..., Any]]:
        return [self.wrap(tool) for tool in tools]

    def wrap(self, tool: Callable[..., Any]) -> Callable[..., Any]:
        info = dict(getattr(tool, "tool_info", {}))
        if not info.get("tool_name"):
            raise ValueError("tool is missing tool_info")
        metadata = metadata_for(tool)
        name = str(info["tool_name"])
        approval_check = info.get("approval_check")

        @functools.wraps(tool)
        def guarded(*args: Any, **arguments: Any) -> Any:
            if args:
                arguments = dict(inspect.signature(tool).bind(*args, **arguments).arguments)
            with self._lock:
                self.call_count += 1
                call_number = self.call_count
                fingerprint = self.broker.fingerprint(name, arguments)
                repeat_count = self._fingerprint_counts.get(fingerprint, 0) + 1
                self._fingerprint_counts[fingerprint] = repeat_count
            if call_number > self.max_tool_calls:
                self.events.emit("budget_exceeded", {"kind": "tool_calls", "limit": self.max_tool_calls})
                return json.dumps({"ok": False, "error": "tool call budget exceeded"}, ensure_ascii=False)
            if repeat_count > self.max_repeat_calls:
                self.events.emit(
                    "no_progress_detected",
                    {
                        "tool": name,
                        "repeat_count": repeat_count,
                        "limit": self.max_repeat_calls,
                    },
                )
                return json.dumps(
                    {"ok": False, "error": "repeated identical tool call limit exceeded"},
                    ensure_ascii=False,
                )
            if self.control_check:
                reason = self.control_check()
                if reason:
                    self.events.emit("tool_blocked", {"tool": name, "reason": reason})
                    return json.dumps({"ok": False, "error": reason}, ensure_ascii=False)
            if metadata.approval_policy == ApprovalPolicy.DISABLED:
                return json.dumps({"ok": False, "error": f"tool is disabled by policy: {name}"})
            requires_approval = metadata.approval_policy == ApprovalPolicy.ALWAYS
            if metadata.approval_policy == ApprovalPolicy.CONDITIONAL:
                requires_approval = bool(approval_check(arguments)) if callable(approval_check) else True
            if requires_approval:
                decision = self.broker.decision(name, arguments)
                if decision == "rejected":
                    self.events.emit("tool_blocked", {"tool": name, "reason": "approval rejected"})
                    return json.dumps({"ok": False, "error": "user rejected this exact tool call"})
                if decision != "approved":
                    request = self.broker.request(name, arguments, metadata)
                    return json.dumps(
                        {
                            "ok": False,
                            "approval_required": True,
                            "request_id": request["request_id"],
                            "tool": name,
                            "reason": request["reason"],
                        },
                        ensure_ascii=False,
                    )
            self.events.emit(
                "tool_started",
                {"tool": name, "arguments": arguments, "call_number": call_number},
            )
            started = time.monotonic()
            try:
                result = tool(**arguments)
            except Exception as exc:
                self.events.emit(
                    "tool_failed",
                    {
                        "tool": name,
                        "error": redact_text(str(exc)),
                        "error_type": type(exc).__name__,
                        "elapsed_seconds": time.monotonic() - started,
                    },
                )
                raise
            from .evidence import ArtifactRegistry, EvidenceStore

            captured = EvidenceStore(self.events.store, self.events.run_id).capture(name, result)
            if captured and isinstance(result, str):
                try:
                    structured = json.loads(result)
                    if isinstance(structured, dict):
                        structured["evidence_ids"] = [item["evidence_id"] for item in captured]
                        result = json.dumps(structured, ensure_ascii=False)
                except ValueError:
                    pass
            artifact: str | None = None
            serialized = (
                result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
            )
            if len(serialized.encode("utf-8")) > metadata.output_limit_bytes:
                safe_name = "".join(char if char.isalnum() or char in "_-" else "_" for char in name)
                artifact = f"logs/tool-{call_number}-{safe_name}.log"
                self.events.store.write_text(
                    self.events.run_id,
                    artifact,
                    redact_text(serialized),
                )
                ArtifactRegistry(self.events.store, self.events.run_id).register(
                    artifact, kind="tool_output", evidence_ids=[e["evidence_id"] for e in captured]
                )
            safe_result = _cap_result(result, metadata.output_limit_bytes)
            self.events.emit(
                "tool_completed",
                {
                    "tool": name,
                    "output": safe_result,
                    "full_output_artifact": artifact,
                    "evidence_ids": [e["evidence_id"] for e in captured],
                    "elapsed_seconds": time.monotonic() - started,
                },
            )
            return safe_result

        guarded.tool_info = info  # type: ignore[attr-defined]
        guarded._lightworker_guarded = True
        return guarded


def _cap_result(value: Any, limit_bytes: int) -> Any:
    if not isinstance(value, str):
        return redact_value(value)
    safe = redact_text(value)
    encoded = safe.encode("utf-8")
    if len(encoded) <= limit_bytes:
        return safe
    excerpt = encoded[: max(0, limit_bytes - 256)].decode("utf-8", errors="ignore")
    try:
        json.loads(safe)
    except (ValueError, TypeError):
        return excerpt + "\n…[tool output truncated; full output is in the artifact log]"
    return json.dumps(
        {
            "ok": True,
            "truncated": True,
            "excerpt": excerpt,
            "note": "Read the full output artifact for complete structured data.",
        },
        ensure_ascii=False,
    )


def _approval_reason(metadata: ToolMetadata) -> str:
    reasons: list[str] = []
    if metadata.is_destructive:
        reasons.append("destructive operation")
    if metadata.external_side_effect:
        reasons.append("external side effect")
    if metadata.is_write:
        reasons.append("write operation")
    if metadata.network_required:
        reasons.append("network access")
    return ", ".join(reasons) or "policy requires confirmation"
