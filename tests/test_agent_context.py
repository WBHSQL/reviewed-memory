import unittest

from agent.context_builder import AgentContextBuilder
from memory_engine.models import MemoryFragment


class _FakeRepository:
    def __init__(self, fragments):
        self._fragments = fragments

    def list_fragments(self, **_kwargs):
        return list(self._fragments)


def _fragment(fid, ftype, statement, topic, scope="current", confidence=0.9):
    return MemoryFragment(
        id=fid,
        type=ftype,
        statement=statement,
        topic=topic,
        confidence=confidence,
        status="active",
        valid_from="2026-08-01T00:00:00+08:00",
        last_supported_at="2026-08-30T00:00:00+08:00",
        source_slugs=(f"secret-{fid}",),
        created_at="2026-08-30T00:00:00+08:00",
        updated_at="2026-08-30T00:00:00+08:00",
        temporal_scope=scope,
        observed_at="2026-08-30T00:00:00+08:00",
    )


class AgentContextBuilderTests(unittest.TestCase):
    def setUp(self):
        self.fragments = [
            _fragment(1, "project", "用户目前正在开发 flomo Personal Agent。", "flomo"),
            _fragment(2, "preference", "用户偏好先验证真实价值再扩展工程。", "项目方法"),
            _fragment(3, "experience", "用户曾停止一个实际价值不足的工具项目。", "项目经历", "episodic"),
        ]
        self.builder = AgentContextBuilder(_FakeRepository(self.fragments))

    def test_current_question_prefers_current_project(self):
        results = self.builder.retrieve("我最近在做什么？", limit=3)
        self.assertEqual(results[0].fragment.id, 1)

    def test_identity_question_prefers_preference(self):
        results = self.builder.retrieve("我做事情有什么特点？", limit=3)
        self.assertEqual(results[0].fragment.id, 2)

    def test_prompt_uses_derived_memory_without_exposing_source_slug(self):
        prompt = self.builder.build_prompt("我最近在做什么？", limit=2)
        self.assertIn("flomo Personal Agent", prompt)
        self.assertIn("证据不足", prompt)
        self.assertNotIn("secret-1", prompt)


if __name__ == "__main__":
    unittest.main()


class LiveContextTests(unittest.TestCase):
    def test_recent_memo_ranking_prefers_newest_for_current_question(self):
        from agent.live_context import rank_recent_memos

        memos = [
            {"content": "<p>今天在继续开发 flomo Agent</p>", "created_at": "2026-08-30 10:00:00", "tags": ["项目"]},
            {"content": "<p>旧的英语学习记录</p>", "created_at": "2026-07-01 10:00:00", "tags": []},
        ]
        ranked = rank_recent_memos(memos, "我最近在做什么？", limit=2)
        self.assertEqual(ranked[0].text, "今天在继续开发 flomo Agent")
        self.assertNotIn("<p>", ranked[0].text)
