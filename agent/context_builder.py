"""Build a small, evidence-grounded context pack for the personal agent."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterable

from memory_engine.models import MemoryFragment
from memory_engine.repository import MemoryRepository

_PROMPT_PATH = Path(__file__).with_name("default_prompt.md")


@dataclass(frozen=True)
class RetrievedMemory:
    fragment: MemoryFragment
    score: float
    reasons: tuple[str, ...]


class AgentContextBuilder:
    """Retrieve reviewed memories without adding a vector database yet."""

    def __init__(self, repository: MemoryRepository) -> None:
        self.repository = repository
    @staticmethod
    def _intent(question: str) -> set[str]:
        intents: set[str] = set()
        if re.search(r"最近|现在|目前|这阵子|忙什么|在做什么", question):
            intents.add("current")
        if re.search(r"什么样的人|性格|特点|习惯|经常|总是|做事|怎么想", question):
            intents.add("identity")
        if re.search(r"变化|以前|过去|后来|当时|越来越|相比", question):
            intents.add("change")
        if re.search(r"纠结|困扰|担心|问题|为什么|卡住", question):
            intents.add("tension")
        return intents

    @staticmethod
    def _terms(question: str) -> tuple[str, ...]:
        latin = re.findall(r"[A-Za-z0-9_+-]{2,}", question.lower())
        chinese_runs = re.findall(r"[\u4e00-\u9fff]{2,}", question)
        pieces: list[str] = list(latin)
        for run in chinese_runs:
            if len(run) <= 4:
                pieces.append(run)
            pieces.extend(run[i : i + 2] for i in range(len(run) - 1))
        stop = {"什么", "怎么", "觉得", "我的", "我是", "一个", "最近", "现在"}
        return tuple(dict.fromkeys(piece for piece in pieces if piece not in stop))
    def retrieve(self, question: str, *, limit: int = 12) -> tuple[RetrievedMemory, ...]:
        intents = self._intent(question)
        terms = self._terms(question)
        ranked: list[RetrievedMemory] = []
        for fragment in self.repository.list_fragments(status="active"):
            score, reasons = self._score(fragment, intents, terms)
            if score > 0:
                ranked.append(RetrievedMemory(fragment, score, tuple(reasons)))
        ranked.sort(
            key=lambda item: (
                item.score,
                item.fragment.last_supported_at,
                item.fragment.confidence,
                item.fragment.id or 0,
            ),
            reverse=True,
        )
        return tuple(ranked[:limit])

    @staticmethod
    def _score(
        fragment: MemoryFragment, intents: set[str], terms: Iterable[str]
    ) -> tuple[float, list[str]]:
        haystack = f"{fragment.topic} {fragment.statement}".lower()
        score = max(0.0, min(fragment.confidence, 1.0))
        reasons: list[str] = []
        for term in terms:
            if term.lower() in haystack:
                score += 3.0 if term.lower() in fragment.statement.lower() else 1.5
                reasons.append(f"match:{term}")
        if "current" in intents:
            if fragment.temporal_scope == "current":
                score += 4.0
                reasons.append("current-scope")
            if fragment.type in {"project", "goal", "decision", "fact"}:
                score += 2.5
                reasons.append(f"current-type:{fragment.type}")
        if "identity" in intents:
            if fragment.type in {"preference", "value", "belief", "pattern"}:
                score += 4.0
                reasons.append(f"identity-type:{fragment.type}")
            elif fragment.type in {"experience", "decision"}:
                score += 1.0
                reasons.append(f"behavior-evidence:{fragment.type}")
        if "change" in intents and fragment.type in {
            "preference", "value", "belief", "project", "goal", "decision"
        }:
            score += 2.0
            reasons.append("change-relevant")
        if "tension" in intents and fragment.type in {"goal", "decision", "belief", "experience"}:
            score += 1.5
            reasons.append("tension-relevant")
        if not intents and not tuple(terms):
            score += 1.0
        return score, reasons
    def build_prompt(self, question: str, *, limit: int = 12) -> str:
        memories = self.retrieve(question, limit=limit)
        instructions = _PROMPT_PATH.read_text(encoding="utf-8").strip()
        lines = [instructions, "", "## 用户问题", question.strip(), "", "## 可用记忆证据"]
        if not memories:
            lines.append("没有检索到足够相关的已审核记忆。请明确承认证据不足。")
        else:
            for item in memories:
                fragment = item.fragment
                lines.append(
                    "- "
                    f"[memory:{fragment.id}] type={fragment.type}; "
                    f"topic={fragment.topic or '未分类'}; "
                    f"scope={fragment.temporal_scope}; "
                    f"observed={fragment.observed_at}; "
                    f"supported={fragment.last_supported_at}; "
                    f"confidence={fragment.confidence:.2f}; "
                    f"sources={len(fragment.source_slugs)} | {fragment.statement}"
                )
        lines.extend(
            [
                "",
                "## 回答要求",
                "直接回答用户。证据不足就说不足；不要补写记忆里没有的故事。",
            ]
        )
        return "\n".join(lines)
