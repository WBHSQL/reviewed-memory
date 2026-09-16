"""Combine recent flomo context with reviewed long-term memory."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .context_builder import AgentContextBuilder
from .live_context import rank_recent_memos

_PROMPT_PATH = Path(__file__).with_name("living_companion.md")


class PersonalAgentPromptBuilder:
    def __init__(self, repository, flomo_client: Any | None = None) -> None:
        self.context_builder = AgentContextBuilder(repository)
        self.flomo_client = flomo_client

    def build(self, question: str, *, memory_limit: int = 12, live_limit: int = 6) -> str:
        lines = [_PROMPT_PATH.read_text(encoding="utf-8").strip(), ""]
        lines.extend(["## 用户问题", question.strip(), ""])
        fresh = ()
        if self.flomo_client is not None:
            memos = self.flomo_client.get_latest_memos(limit=max(20, live_limit * 3))
            fresh = rank_recent_memos(memos, question, limit=live_limit)
        lines.append("## 最近 flomo（临时读取，不写入本地 memory）")
        if not fresh:
            lines.append("- 没有匹配到近期记录。")
        else:
            for memo in fresh:
                tags = ",".join(memo.tags) if memo.tags else "无标签"
                lines.append(f"- [{memo.created_at}] tags={tags} | {memo.text}")
        lines.extend(["", "## 已审核长期记忆"])
        memories = self.context_builder.retrieve(question, limit=memory_limit)
        if not memories:
            lines.append("- 没有检索到足够相关的长期记忆。")
        else:
            for item in memories:
                fragment = item.fragment
                lines.append(
                    "- "
                    f"[memory:{fragment.id}] type={fragment.type}; "
                    f"topic={fragment.topic or '未分类'}; "
                    f"scope={fragment.temporal_scope}; "
                    f"supported={fragment.last_supported_at}; "
                    f"confidence={fragment.confidence:.2f}; "
                    f"sources={len(fragment.source_slugs)} | {fragment.statement}"
                )
        lines.extend(["", "## 回答要求"])
        lines.append("优先使用最新记录理解当前状态，用长期记忆理解反复模式。")
        lines.append("如果两者冲突，把它理解成可能发生了变化，不要用旧记忆覆盖新记录。")
        lines.append("直接回答；证据不足就明确说不足，不虚构。")
        return "\n".join(lines)
