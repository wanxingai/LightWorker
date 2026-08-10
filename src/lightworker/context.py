"""Token-aware deterministic context compression for long conversations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .policy import redact_text


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
