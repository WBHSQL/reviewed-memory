"""Ephemeral retrieval over recent flomo memos; raw text is never persisted."""

from __future__ import annotations

from dataclasses import dataclass
from html import unescape
import re
from typing import Any, Iterable

from .context_builder import AgentContextBuilder


@dataclass(frozen=True)
class FreshMemo:
    created_at: str
    text: str
    tags: tuple[str, ...]
    score: float


def _plain_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return unescape(text).strip()


def _created_at(memo: dict[str, Any]) -> str:
    return str(memo.get("created_at") or "")


def rank_recent_memos(
    memos: Iterable[dict[str, Any]], question: str, *, limit: int = 6
) -> tuple[FreshMemo, ...]:
    terms = AgentContextBuilder._terms(question)
    wants_recent = bool(re.search(r"最近|现在|目前|这阵子|忙什么|在做什么", question))
    ordered = sorted(memos, key=_created_at, reverse=True)
    if wants_recent:
        ordered = ordered[: max(12, limit * 3)]
    ranked: list[FreshMemo] = []
    for index, memo in enumerate(ordered):
        text = _plain_text(memo.get("content"))
        if not text:
            continue
        tags = tuple(str(tag) for tag in memo.get("tags") or ())
        haystack = f"{text} {' '.join(tags)}".lower()
        score = max(0.0, 8.0 - index * 0.6) if wants_recent else 0.0
        for term in terms:
            if term.lower() in haystack:
                score += 0.75 if wants_recent else 3.0
        if score <= 0 and not wants_recent:
            continue
        ranked.append(FreshMemo(_created_at(memo), text, tags, score))
    ranked.sort(key=lambda item: (item.score, item.created_at), reverse=True)
    return tuple(ranked[:limit])
