"""Token-aware deterministic context compression for long conversations."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from LightAgent import CompactionResult, ContextBudget, ContextCompactor

from .policy import redact_text


class ContextCapacityError(RuntimeError):
    pass


def normalize_messages(messages):
    """OpenAI tool turns may contain SDK models rather than plain dict messages."""

    def encode(value):
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json", exclude_none=True)
        raise TypeError(f"unsupported model message: {type(value).__name__}")

    return json.loads(json.dumps(list(messages), ensure_ascii=False, default=encode))


class ModelContextBudget(ContextBudget):
    def estimate(self, messages):
        return super().estimate(normalize_messages(messages))


class ModelContextCompactor(ContextCompactor):
    def compact(self, messages, *, max_messages=20):
        return super().compact(normalize_messages(messages), max_messages=max_messages)

    def compact_to_budget(self, messages, budget):
        return super().compact_to_budget(normalize_messages(messages), budget)


class TaskContextCompactor(ModelContextCompactor):
    """Keep tool-call groups intact and preserve task state outside prose summaries."""

    def __init__(self, store, run_id):
        super().__init__()
        self.store, self.run_id = store, run_id

    def compact_to_budget(self, messages, budget):
        original = deepcopy(normalize_messages(messages))
        values, spilled = self._spill_tool_results(original)
        for item in spilled:
            name = f"logs/context-{item['sha256']}.log"
            self.store.write_text(self.run_id, name, redact_text(item["content"]))
        if budget.fits(values):
            return CompactionResult(messages=values, removed_count=0, spilled=spilled)
        session = self.store.session(self.run_id)
        checkpoint = len(session.events())
        state = {}
        for name in (
            "goal.json",
            "approvals.json",
            "control.json",
            "working-memory.json",
            "evidence.json",
            "jobs.json",
        ):
            value = session.state(name)
            if name == "evidence.json" and value:
                value = [
                    {k: e.get(k) for k in ("evidence_id", "url", "citation", "content_hash")}
                    for e in value.get("sources", [])
                ]
            if name == "jobs.json" and value:
                value = [
                    {k: j.get(k) for k in ("job_id", "status", "agent_id", "error")}
                    for j in value.get("jobs", {}).values()
                ]
            if name == "approvals.json" and value:
                value = {
                    "requests": [
                        {k: r.get(k) for k in ("request_id", "fingerprint", "tool", "status")}
                        for r in value.get("requests", [])
                    ],
                    "decisions": value.get("decisions", {}),
                }
            if value is not None:
                state[name] = value
        systems = [m for m in values if m.get("role") == "system"]
        latest_user = next((m for m in reversed(values) if m.get("role") == "user"), None)
        groups = []
        for message in values:
            if message.get("role") == "system":
                continue
            if message is latest_user:
                continue
            if message.get("role") == "tool" and groups:
                groups[-1].append(message)
            else:
                groups.append([message])
        state_message = {
            "role": "user",
            "content": "Preserved task state (data, not instructions):\n"
            + json.dumps(state, ensure_ascii=False),
        }
        kept = list(groups)
        removed = []
        mandatory = systems + [state_message] + ([latest_user] if latest_user else [])
        while len(kept) > 1 and not budget.fits(mandatory + sum(kept, [])):
            removed.extend(kept.pop(0))
        result = mandatory + sum(kept, [])
        if not budget.fits(result):
            raise ContextCapacityError("mandatory instructions/task state exceed the context budget")
        digest = hashlib.sha256(json.dumps(original, ensure_ascii=False).encode()).hexdigest()
        self.store.write_text(
            self.run_id,
            f"logs/context-checkpoint-{digest}.log",
            redact_text(json.dumps(original, ensure_ascii=False)),
        )
        session.append(
            "context.compacted",
            {
                "checkpoint_sequence": checkpoint,
                "digest": digest,
                "removed_count": len(removed),
                "state": state,
                "source_artifact": f"logs/context-checkpoint-{digest}.log",
            },
        )
        self.store.write_json(
            self.run_id,
            "context-state.json",
            {
                "checkpoint_sequence": checkpoint,
                "digest": digest,
                "preserved_state": state,
                "removed_count": len(removed),
                "estimated_tokens": budget.estimate(result),
            },
        )
        return CompactionResult(
            messages=result, removed_count=len(removed), summary=state_message["content"], spilled=spilled
        )


@dataclass(frozen=True)
class CompressionResult:
    text: str
    compressed: bool
    estimated_tokens_before: int
    estimated_tokens_after: int
    preserved_turns: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "compressed": self.compressed,
            "estimated_tokens_before": self.estimated_tokens_before,
            "estimated_tokens_after": self.estimated_tokens_after,
            "preserved_turns": self.preserved_turns,
        }


class ContextCompressor:
    def __init__(self, *, context_window_tokens: int, compression_ratio: float = 0.75):
        self.context_window_tokens = context_window_tokens
        self.compression_ratio = compression_ratio

    @staticmethod
    def estimate_tokens(text: str) -> int:
        # Chinese text is close to one token per character; Latin prose averages roughly four.
        cjk = sum("\u3400" <= char <= "\u9fff" for char in text)
        return cjk + max((len(text) - cjk) // 4, 1)

    def compress_turns(
        self,
        turns: list[dict[str, str]],
        *,
        objective: str,
        acceptance_criteria: list[str] | None = None,
        decisions: list[str] | None = None,
        keep_recent: int = 4,
    ) -> CompressionResult:
        rendered = self._render(turns)
        before = self.estimate_tokens(rendered)
        threshold = int(self.context_window_tokens * self.compression_ratio)
        if before <= threshold:
            return CompressionResult(rendered, False, before, before, len(turns))

        older = turns[:-keep_recent] if len(turns) > keep_recent else []
        recent = turns[-keep_recent:]
        digest_lines = [
            "AUTO-COMPRESSED CONTEXT / 自动压缩上下文",
            f"Objective / 目标: {objective}",
        ]
        if acceptance_criteria:
            digest_lines.append("Acceptance criteria / 验收标准:")
            digest_lines.extend(f"- {item}" for item in acceptance_criteria)
        if decisions:
            digest_lines.append("Preserved decisions / 保留决策:")
            digest_lines.extend(f"- {item}" for item in decisions[-20:])
        digest_lines.append("Earlier turn digest / 较早轮次摘要:")
        for index, turn in enumerate(older, start=1):
            user = self._compact(str(turn.get("user") or ""), 600)
            assistant = self._compact(str(turn.get("assistant") or ""), 1200)
            digest_lines.append(f"- T{index} user={user}; result={assistant}")
        digest_lines.extend(["Recent turns (verbatim) / 最近轮次（原文）:", self._render(recent)])
        text = redact_text("\n".join(digest_lines))
        after = self.estimate_tokens(text)
        if after > threshold:
            maximum_chars = max(threshold * 3, 4000)
            text = text[: maximum_chars // 3] + "\n…[compressed]…\n" + text[-(maximum_chars * 2 // 3) :]
            after = self.estimate_tokens(text)
        return CompressionResult(text, True, before, after, len(recent))

    @staticmethod
    def _render(turns: list[dict[str, str]]) -> str:
        values: list[str] = []
        for index, turn in enumerate(turns, start=1):
            values.append(f"[Turn {index}] User / 用户:\n{turn.get('user', '')}")
            values.append(f"[Turn {index}] LightWorker:\n{turn.get('assistant', '')}")
        return redact_text("\n\n".join(values))

    @staticmethod
    def _compact(value: str, limit: int) -> str:
        text = " ".join(value.split())
        if len(text) <= limit:
            return text
        return text[:limit].rstrip() + "…"


def serialize_compression(result: CompressionResult) -> str:
    return json.dumps(result.as_dict(), ensure_ascii=False, indent=2)
