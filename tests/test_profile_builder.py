"""Tests for conservative, review-only Profile Builder V0."""

from __future__ import annotations

import unittest

try:
    from memory_engine import (
        CandidateReview,
        CandidateReviewService,
        CandidateStagingService,
        FlomoEvidenceCatalogBuilder,
        MemoryRepository,
    )
    from memory_engine.profile_builder import ProfileBuilderError, ProfileBuilderV0
except ModuleNotFoundError:
    from flomo_poc.memory_engine import (
        CandidateReview,
        CandidateReviewService,
        CandidateStagingService,
        FlomoEvidenceCatalogBuilder,
        MemoryRepository,
    )
    from flomo_poc.memory_engine.profile_builder import ProfileBuilderError, ProfileBuilderV0


class ProfileBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = MemoryRepository(":memory:")
        self.builder = ProfileBuilderV0(self.repository)

    def tearDown(self) -> None:
        self.repository.close()

    def _promote(
        self, *, fragment_type: str, statement: str, source_slug: str, observed_at: str
    ):
        catalog = FlomoEvidenceCatalogBuilder.from_api_response(
            [{"slug": source_slug, "created_at": observed_at, "context": "测试"}]
        )
        candidate = CandidateStagingService(self.repository, catalog).stage_batch(
            [
                {
                    "type": fragment_type,
                    "statement": statement,
                    "topic": "测试",
                    "confidence": 0.9,
                    "source_slugs": [source_slug],
                    "evidence_kind": "explicit",
                    "proposed_action": "accept",
                }
            ]
        )[0]
        result = CandidateReviewService(self.repository).review_batch(
            [CandidateReview(candidate.id, "accept")]
        )
        return self.repository.get_fragment(result.results[0].promoted_fragment_id)

    def test_supported_candidate_derives_provenance_without_writing_profile(self) -> None:
        first = self._promote(
            fragment_type="belief",
            statement="我曾认同实践复习",
            source_slug="a",
            observed_at="2026-01-01T00:00:00+00:00",
        )
        second = self._promote(
            fragment_type="belief",
            statement="我曾认同复盘循环",
            source_slug="b",
            observed_at="2026-02-01T00:00:00+00:00",
        )
        result = self.builder.build_from_specs(
            [{
                "attribute": "learning:method",
                "value": "用户曾多次认同实践与复盘式学习。",
                "topic": "学习方法",
                "state": "supported",
                "supporting_fragment_ids": [first.id, second.id],
            }]
        )[0]
        self.assertEqual(result.source_count, 2)
        self.assertEqual(result.source_slugs, ("a", "b"))
        self.assertEqual(result.first_observed_at, "2026-01-01T00:00:00+00:00")
        self.assertEqual(self.repository.list_profiles(), [])

    def test_supported_requires_two_independent_sources(self) -> None:
        fragment = self._promote(
            fragment_type="belief",
            statement="我曾认同某种方法",
            source_slug="only",
            observed_at="2026-01-01T00:00:00+00:00",
        )
        with self.assertRaises(ProfileBuilderError):
            self.builder.build_from_specs(
                [{
                    "attribute": "working_style:test",
                    "value": "测试",
                    "topic": "测试",
                    "state": "supported",
                    "supporting_fragment_ids": [fragment.id],
                }]
            )

    def test_single_experience_cannot_become_profile_claim(self) -> None:
        fragment = self._promote(
            fragment_type="experience",
            statement="我做过一次测试",
            source_slug="event",
            observed_at="2026-01-01T00:00:00+00:00",
        )
        with self.assertRaises(ProfileBuilderError):
            self.builder.build_from_specs(
                [{
                    "attribute": "identity:trait",
                    "value": "用户总是这样做",
                    "topic": "测试",
                    "state": "tentative",
                    "supporting_fragment_ids": [fragment.id],
                }]
            )

    def test_builder_rejects_raw_or_forged_fields(self) -> None:
        fragment = self._promote(
            fragment_type="preference",
            statement="我喜欢测试",
            source_slug="pref",
            observed_at="2026-01-01T00:00:00+00:00",
        )
        with self.assertRaises(ProfileBuilderError):
            self.builder.build_from_specs(
                [{
                    "attribute": "preference:test",
                    "value": "喜欢测试",
                    "topic": "测试",
                    "state": "tentative",
                    "supporting_fragment_ids": [fragment.id],
                    "raw_text": "forbidden",
                }]
            )
