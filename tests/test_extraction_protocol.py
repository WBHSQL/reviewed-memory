"""Synthetic fixtures for Memory Engine V2.3.1 extraction and review rules."""

from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import patch

try:
    from memory_engine import (
        CandidateReview,
        CandidateReviewError,
        CandidateRevisionRequiredError,
        CandidateReviewService,
        CandidateStagingService,
        EvidenceCatalogRequiredError,
        ExtractionProtocol,
        ExtractionProtocolError,
        EvidenceRef,
        MemoryRepository,
        MemoryCandidate,
        TrustedEvidence,
        TrustedEvidenceCatalog,
        TrustedEvidenceError,
        FlomoEvidenceCatalogBuilder,
    )
except ModuleNotFoundError:
    from flomo_poc.memory_engine import (
        CandidateReview,
        CandidateReviewError,
        CandidateRevisionRequiredError,
        CandidateReviewService,
        CandidateStagingService,
        EvidenceCatalogRequiredError,
        ExtractionProtocol,
        ExtractionProtocolError,
        EvidenceRef,
        MemoryRepository,
        MemoryCandidate,
        TrustedEvidence,
        TrustedEvidenceCatalog,
        TrustedEvidenceError,
        FlomoEvidenceCatalogBuilder,
    )

try:
    from memory_engine.repository import _STAGING_WRITE_TOKEN
except ModuleNotFoundError:
    from flomo_poc.memory_engine.repository import _STAGING_WRITE_TOKEN


class SyntheticEvidenceCatalog(TrustedEvidenceCatalog):
    """Provide explicit metadata for pattern fixtures and safe defaults elsewhere."""

    def __init__(self, records):
        # Keep fixtures on the same production-authorized path as a real API
        # response; the fallback below only supplies deterministic test data.
        projected = FlomoEvidenceCatalogBuilder.from_api_response(
            [
                {
                    "slug": record.source_slug,
                    "created_at": record.observed_at,
                    "context": record.context,
                    "tags": list(record.tags),
                }
                for record in records
            ]
        )
        self._records = projected._records
        self._is_flomo_api_catalog = projected.is_flomo_api_catalog

    def get(self, source_slug: str):
        existing = super().get(source_slug)
        if existing is not None:
            return existing
        return TrustedEvidence(
            source_slug=source_slug,
            observed_at="2026-01-01T00:00:00+00:00",
            context=source_slug,
        )


class ExtractionProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = MemoryRepository(":memory:")
        self.catalog = SyntheticEvidenceCatalog(
            [
                TrustedEvidence("pattern-001", "2026-01-01", "学习"),
                TrustedEvidence("pattern-002", "2026-01-15", "项目"),
                TrustedEvidence("pattern-003", "2026-02-01", "复盘"),
                TrustedEvidence("roundtrip-001", "2026-08-26T09:00:00+08:00", "工程"),
                TrustedEvidence(
                    "temporal-project", "2026-08-20T10:00:00+08:00", "项目"
                ),
                TrustedEvidence(
                    "temporal-experience", "2026-08-21T10:00:00+08:00", "经历"
                ),
            ]
        )
        self.staging = CandidateStagingService(self.repository, self.catalog)
        self.review = CandidateReviewService(self.repository, self.catalog)

    def tearDown(self) -> None:
        self.repository.close()

    @staticmethod
    def payload(
        *,
        candidate_type: str,
        statement: str,
        source_slugs: list[str],
        evidence_kind: str = "explicit",
        proposed_action: str = "accept",
        topic: str = "",
        confidence: float = 0.9,
        temporal_scope: str | None = None,
        observed_at: str | None = None,
        last_supported_at: str | None = None,
        evidence_refs: list[dict[str, str]] | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "type": candidate_type,
            "statement": statement,
            "topic": topic,
            "confidence": confidence,
            "source_slugs": source_slugs,
            "evidence_kind": evidence_kind,
            "proposed_action": proposed_action,
        }
        if temporal_scope is not None:
            payload["temporal_scope"] = temporal_scope
        if observed_at is not None:
            payload["observed_at"] = observed_at
        if last_supported_at is not None:
            payload["last_supported_at"] = last_supported_at
        if evidence_refs is not None:
            payload["evidence_refs"] = evidence_refs
        return payload

    @staticmethod
    def manual_candidate(source_slug: str = "manual-candidate-001") -> MemoryCandidate:
        return MemoryCandidate(
            id=None,
            type="fact",
            statement="手工写入的候选事实",
            topic="测试",
            confidence=0.8,
            source_slugs=(source_slug,),
            evidence_kind="explicit",
            review_status="pending",
            proposed_action="accept",
            created_at="2026-08-26T10:00:00+00:00",
            updated_at="2026-08-26T10:00:00+00:00",
            temporal_scope="episodic",
            observed_at="2026-08-26T10:00:00+00:00",
            last_supported_at="2026-08-26T10:00:00+00:00",
            evidence_refs=(
                EvidenceRef(
                    source_slug=source_slug,
                    observed_at="2026-08-26T10:00:00+00:00",
                    context="测试",
                ),
            ),
        )

    def test_explicit_fact_is_staged_then_requires_batch_accept(self) -> None:
        candidates = self.staging.stage_batch(
            [
                self.payload(
                    candidate_type="fact",
                    statement="用户明确表示本阶段不修改 flomo 线上数据",
                    source_slugs=["fact-001"],
                )
            ]
        )
        self.assertEqual(1, len(candidates))
        self.assertEqual("pending", candidates[0].review_status)
        self.assertEqual([], self.repository.list_fragments())

        result = self.review.review_batch([CandidateReview(candidates[0].id, "accept")])
        self.assertEqual("accepted", result.results[0].review_status)
        fragments = self.repository.list_fragments()
        self.assertEqual(1, len(fragments))
        self.assertEqual(("fact-001",), fragments[0].source_slugs)
        self.assertEqual([], self.repository.list_profiles())
        self.assertEqual([], self.repository.list_current_memory())

    def test_staging_persists_body_free_trusted_provenance(self) -> None:
        candidate = self.staging.stage_batch(
            [
                self.payload(
                    candidate_type="fact",
                    statement="trusted proof 只保存来源元数据",
                    source_slugs=["proof-001"],
                )
            ]
        )[0]
        proof = self.repository.get_candidate_provenance(candidate.id)
        self.assertIsNotNone(proof)
        self.assertEqual(candidate.trusted_staging_proof, proof.proof_marker)
        self.assertEqual("flomo_trusted_staging", proof.origin)
        self.assertEqual(("proof-001",), proof.source_slugs)
        self.assertEqual("2026-01-01T00:00:00+00:00", proof.observed_at)
        self.assertNotIn("正文", vars(proof))

    def test_accept_revalidates_persisted_provenance_against_candidate(self) -> None:
        candidate = self.staging.stage_batch(
            [
                self.payload(
                    candidate_type="fact",
                    statement="晋级前必须重新校验 source proof",
                    source_slugs=["proof-tamper-001"],
                )
            ]
        )[0]
        self.repository.connection.execute(
            "UPDATE memory_candidates SET observed_at = ? WHERE id = ?",
            ("2099-01-01T00:00:00+00:00", candidate.id),
        )
        self.repository.connection.commit()
        tampered = self.repository.get_candidate(candidate.id)
        with self.assertRaises(CandidateReviewError):
            self.review.review_batch([CandidateReview(tampered.id, "accept")])
        self.assertEqual([], self.repository.list_fragments())
        self.assertEqual("pending", self.repository.get_candidate(candidate.id).review_status)

    def test_manual_repository_candidate_cannot_forge_trusted_candidate(self) -> None:
        candidate = self.repository.create_candidate(self.manual_candidate())
        with self.assertRaises(CandidateReviewError):
            self.review.review_batch([CandidateReview(candidate.id, "accept")])
        self.assertEqual([], self.repository.list_fragments())
        self.assertIsNone(self.repository.get_candidate_provenance(candidate.id))

        with self.assertRaises(ValueError):
            self.repository.create_candidate(
                replace(self.manual_candidate("manual-forged-001"), trusted_staging_proof="forged")
            )

    def test_candidate_without_trusted_provenance_cannot_promote(self) -> None:
        old_candidate = self.staging.stage_batch(
            [
                self.payload(
                    candidate_type="decision",
                    statement="先建立一个可被 supersede 的旧决定",
                    source_slugs=["missing-proof-old-001"],
                )
            ]
        )[0]
        old_result = self.review.review_batch([CandidateReview(old_candidate.id, "accept")])
        candidate = self.repository.create_candidate(
            self.manual_candidate("missing-proof-001")
        )
        with self.assertRaises(CandidateReviewError):
            self.review.review_batch(
                [
                    CandidateReview(
                        candidate.id,
                        "supersede",
                        supersedes_fragment_id=old_result.results[0].promoted_fragment_id,
                    )
                ]
            )
        self.assertEqual(1, len(self.repository.list_fragments()))
        self.assertEqual("active", self.repository.list_fragments()[0].status)

    def test_single_source_pattern_cannot_enter_formal_memory(self) -> None:
        candidate = self.repository._create_candidate_from_trusted_staging(
            replace(
                self.manual_candidate("pattern-promote-001"),
                type="pattern",
                statement="用户在任务中形成一个模式",
                evidence_kind="repeated",
                temporal_scope="current",
                observed_at="2026-01-01",
                last_supported_at="2026-01-01",
                evidence_refs=(
                    EvidenceRef("pattern-promote-001", "2026-01-01", "单一上下文"),
                ),
            ),
            source_slugs=("pattern-promote-001",),
            observed_at="2026-01-01",
            last_supported_at="2026-01-01",
            evidence_refs=(
                EvidenceRef("pattern-promote-001", "2026-01-01", "单一上下文"),
            ),
            _staging_token=_STAGING_WRITE_TOKEN,
        )
        with self.assertRaises(CandidateReviewError):
            self.review.review_batch([CandidateReview(candidate.id, "accept")])
        self.assertEqual([], self.repository.list_fragments())

    def test_every_active_fragment_has_promotion_lineage(self) -> None:
        candidate = self.staging.stage_batch(
            [
                self.payload(
                    candidate_type="project",
                    statement="当前正在验证 promotion gate",
                    source_slugs=["lineage-invariant-001"],
                )
            ]
        )[0]
        self.review.review_batch([CandidateReview(candidate.id, "accept")])
        active = self.repository.list_fragments(status="active")
        self.assertTrue(active)
        for fragment in active:
            self.assertTrue(
                any(
                    lineage.promoted_fragment_id == fragment.id
                    for lineage in self.repository.list_promotion_lineage()
                )
            )
        self.assertEqual([], self.repository.list_active_fragments_without_lineage())

    def test_candidate_schema_has_required_fields_without_raw_body(self) -> None:
        columns = {
            row[1]
            for row in self.repository.connection.execute(
                "PRAGMA table_info(memory_candidates)"
            ).fetchall()
        }
        required = {
            "id",
            "type",
            "statement",
            "topic",
            "confidence",
            "source_slugs",
            "evidence_kind",
            "review_status",
            "proposed_action",
        }
        self.assertTrue(required <= columns)
        self.assertNotIn("memo_body", columns)
        self.assertNotIn("raw_text", columns)
        self.assertTrue(
            {
                "temporal_scope",
                "observed_at",
                "last_supported_at",
                "evidence_refs",
                "dedupe_key",
                "protocol_name",
                "protocol_version",
                "trusted_staging_proof",
            }
            <= columns
        )

    def test_explicit_preference_can_be_candidate(self) -> None:
        candidates = self.staging.stage_batch(
            [
                self.payload(
                    candidate_type="preference",
                    statement="用户偏好结论先行",
                    source_slugs=["preference-001"],
                )
            ]
        )
        self.assertEqual("preference", candidates[0].type)
        self.assertEqual(("preference-001",), candidates[0].source_slugs)

    def test_transient_emotion_cannot_be_long_term_preference(self) -> None:
        with self.assertRaises(ExtractionProtocolError):
            self.staging.stage_batch(
                [
                    self.payload(
                        candidate_type="preference",
                        statement="用户今天感到焦虑",
                        source_slugs=["emotion-001"],
                        evidence_kind="transient_emotion",
                    )
                ]
            )
        self.assertEqual([], self.staging.list())

    def test_single_source_cannot_form_pattern(self) -> None:
        with self.assertRaises(ExtractionProtocolError):
            self.staging.stage_batch(
                [
                    self.payload(
                        candidate_type="pattern",
                        statement="用户总是先定义规则再执行",
                        source_slugs=["pattern-001"],
                        evidence_kind="corroborated",
                    )
                ]
            )
        self.assertEqual([], self.staging.list())

    def test_multiple_sources_can_form_pattern_candidate(self) -> None:
        payload = self.payload(
            candidate_type="pattern",
            statement="用户在复杂任务中倾向先定义规则再执行",
            source_slugs=["pattern-001", "pattern-002", "pattern-003"],
            evidence_kind="corroborated",
            evidence_refs=[
                {
                    "source_slug": "pattern-001",
                    "observed_at": "2026-01-01",
                    "context": "学习",
                },
                {
                    "source_slug": "pattern-002",
                    "observed_at": "2026-01-15",
                    "context": "项目",
                },
                {
                    "source_slug": "pattern-003",
                    "observed_at": "2026-02-01",
                    "context": "复盘",
                },
            ],
        )
        parsed = ExtractionProtocol.parse_candidate(payload, allow_pattern=True)
        self.assertIsNotNone(parsed)
        self.assertEqual("pattern", parsed.type)
        self.assertEqual(3, len(parsed.source_slugs))
        self.assertEqual(3, len(parsed.evidence_refs))
        with self.assertRaises(ExtractionProtocolError):
            self.staging.stage_batch([payload])

    def test_pattern_requires_three_independent_sources(self) -> None:
        with self.assertRaises(ExtractionProtocolError):
            ExtractionProtocol.parse_candidate(
                self.payload(
                    candidate_type="pattern",
                    statement="用户在复杂任务中倾向先定义规则再执行",
                    source_slugs=["pattern-001", "pattern-002"],
                    evidence_kind="corroborated",
                    evidence_refs=[
                        {
                            "source_slug": "pattern-001",
                            "observed_at": "2026-01-01",
                            "context": "学习",
                        },
                        {
                            "source_slug": "pattern-002",
                            "observed_at": "2026-01-15",
                            "context": "项目",
                        },
                    ],
                ),
                allow_pattern=True,
            )

    def test_pattern_requires_distinct_dates_and_contexts(self) -> None:
        base = [
            {"source_slug": "pattern-001", "observed_at": "2026-01-01", "context": "学习"},
            {"source_slug": "pattern-002", "observed_at": "2026-01-01", "context": "学习"},
            {"source_slug": "pattern-003", "observed_at": "2026-01-01", "context": "项目"},
        ]
        with self.assertRaises(ExtractionProtocolError):
            ExtractionProtocol.parse_candidate(
                self.payload(
                    candidate_type="pattern",
                    statement="用户在复杂任务中倾向先定义规则再执行",
                    source_slugs=["pattern-001", "pattern-002", "pattern-003"],
                    evidence_kind="repeated",
                    evidence_refs=base,
                ),
                allow_pattern=True,
            )

        different_dates_same_context = [
            {"source_slug": "pattern-001", "observed_at": "2026-01-01", "context": "学习"},
            {"source_slug": "pattern-002", "observed_at": "2026-01-15", "context": "学习"},
            {"source_slug": "pattern-003", "observed_at": "2026-02-01", "context": "学习"},
        ]
        with self.assertRaises(ExtractionProtocolError):
            ExtractionProtocol.parse_candidate(
                self.payload(
                    candidate_type="pattern",
                    statement="用户在复杂任务中倾向先定义规则再执行",
                    source_slugs=["pattern-001", "pattern-002", "pattern-003"],
                    evidence_kind="repeated",
                    evidence_refs=different_dates_same_context,
                ),
                allow_pattern=True,
            )

    def test_atomic_candidate_rejects_event_with_interpretation(self) -> None:
        with self.assertRaises(ExtractionProtocolError):
            self.staging.stage_batch(
                [
                    self.payload(
                        candidate_type="experience",
                        statement="我把交易对看错了，因此说明我做事很粗心",
                        source_slugs=["atomic-001"],
                    )
                ]
            )

    def test_atomic_candidate_rejects_fact_with_pattern(self) -> None:
        with self.assertRaises(ExtractionProtocolError):
            self.staging.stage_batch(
                [
                    self.payload(
                        candidate_type="fact",
                        statement="我今天完成了任务，通常会先列清单",
                        source_slugs=["atomic-002"],
                    )
                ]
            )

    def test_project_and_experience_have_temporal_defaults(self) -> None:
        candidates = self.staging.stage_batch(
            [
                self.payload(
                    candidate_type="project",
                    statement="正在开发一个交易策略",
                    source_slugs=["temporal-project"],
                    observed_at="2026-08-20T10:00:00+08:00",
                ),
                self.payload(
                    candidate_type="experience",
                    statement="曾经误读一次交易对",
                    source_slugs=["temporal-experience"],
                    observed_at="2026-08-21T10:00:00+08:00",
                ),
            ]
        )
        by_type = {candidate.type: candidate for candidate in candidates}
        self.assertEqual("current", by_type["project"].temporal_scope)
        self.assertEqual("episodic", by_type["experience"].temporal_scope)
        self.assertEqual("2026-08-20T10:00:00+08:00", by_type["project"].observed_at)

    def test_v23_temporal_defaults_are_conservative(self) -> None:
        payloads = [
            self.payload(
                candidate_type="fact",
                statement="用户完成过一次数据迁移",
                source_slugs=["temporal-fact-v23"],
            ),
            self.payload(
                candidate_type="experience",
                statement="用户经历过一次线上故障",
                source_slugs=["temporal-experience-v23"],
            ),
            self.payload(
                candidate_type="decision",
                statement="用户决定采用 SQLite",
                source_slugs=["temporal-decision-v23"],
            ),
            self.payload(
                candidate_type="belief",
                statement="用户相信先写规则再执行",
                source_slugs=["temporal-belief-v23"],
            ),
            self.payload(
                candidate_type="preference",
                statement="用户偏好结论先行",
                source_slugs=["temporal-preference-v23"],
            ),
            self.payload(
                candidate_type="value",
                statement="用户重视可追溯性",
                source_slugs=["temporal-value-v23"],
            ),
            self.payload(
                candidate_type="project",
                statement="实现一个有边界的项目",
                source_slugs=["temporal-project-v23"],
            ),
            self.payload(
                candidate_type="goal",
                statement="完成这一轮审核",
                source_slugs=["temporal-goal-v23"],
            ),
        ]
        candidates = self.staging.stage_batch(payloads)
        by_type = {candidate.type: candidate for candidate in candidates}
        self.assertEqual(
            {"fact", "experience", "decision"},
            {candidate_type for candidate_type in by_type if by_type[candidate_type].temporal_scope == "episodic"},
        )
        self.assertEqual(
            {"belief", "preference", "value", "project", "goal"},
            {candidate_type for candidate_type in by_type if by_type[candidate_type].temporal_scope == "current"},
        )

    def test_fact_can_be_current_only_with_explicit_ongoing_language(self) -> None:
        current = self.staging.stage_batch(
            [
                self.payload(
                    candidate_type="fact",
                    statement="用户目前仍在使用 SQLite",
                    source_slugs=["temporal-current-fact"],
                    temporal_scope="current",
                )
            ]
        )[0]
        self.assertEqual("current", current.temporal_scope)

        with self.assertRaises(ExtractionProtocolError):
            self.staging.stage_batch(
                [
                    self.payload(
                        candidate_type="fact",
                        statement="用户使用 SQLite",
                        source_slugs=["temporal-invalid-current-fact"],
                        temporal_scope="current",
                    )
                ]
            )

    def test_v23_old_source_does_not_get_extraction_time_or_current_fact_scope(self) -> None:
        catalog = FlomoEvidenceCatalogBuilder.from_api_response(
            [
                {
                    "slug": "old-source-v23",
                    "created_at": "2019-01-02T03:04:05+00:00",
                    "updated_at": "2026-08-27T00:00:00+00:00",
                    "content": "这段正文只能在处理时存在",
                    "tags": ["历史"],
                }
            ]
        )
        candidate = self.staging.stage_batch(
            [
                self.payload(
                    candidate_type="fact",
                    statement="用户曾完成一次迁移",
                    source_slugs=["old-source-v23"],
                )
            ],
            evidence_catalog=catalog,
        )[0]
        self.assertEqual("episodic", candidate.temporal_scope)
        self.assertEqual("2019-01-02T03:04:05+00:00", candidate.observed_at)
        self.assertNotEqual(candidate.created_at, candidate.observed_at)

    def test_flomo_catalog_builder_projects_metadata_without_memo_body(self) -> None:
        catalog = FlomoEvidenceCatalogBuilder.from_api_response(
            {
                "code": 0,
                "data": [
                    {
                        "slug": "api-source-v23",
                        "created_at": 1_700_000_000_000,
                        "updated_at": 1_800_000_000,
                        "content": "不应进入 catalog",
                        "tags": [{"name": "工程", "content": "忽略"}, "V2.3"],
                        "context": "项目",
                    }
                ],
            }
        )
        evidence = catalog.get("api-source-v23")
        self.assertTrue(catalog.is_flomo_api_catalog)
        self.assertEqual(("工程", "V2.3"), evidence.tags)
        self.assertEqual("项目", evidence.context)
        self.assertNotIn("content", vars(evidence))
        self.assertNotIn("memo", vars(evidence))
        self.assertEqual(
            1,
            len(catalog.provenance(("api-source-v23",)).evidence_refs),
        )

        with self.assertRaises(TrustedEvidenceError):
            FlomoEvidenceCatalogBuilder.from_api_response(
                [{"slug": "missing-source-time", "updated_at": 123}]
            )

    def test_manually_constructed_catalog_cannot_cross_staging_boundary(self) -> None:
        manual_catalog = TrustedEvidenceCatalog.from_records(
            [TrustedEvidence("manual-source-v23", "2026-01-01", "手工")]
        )
        with self.assertRaises(EvidenceCatalogRequiredError):
            self.staging.stage_batch(
                [
                    self.payload(
                        candidate_type="fact",
                        statement="手工 catalog 不能进入 bootstrap staging",
                        source_slugs=["manual-source-v23"],
                    )
                ],
                evidence_catalog=manual_catalog,
            )

    def test_single_belief_or_value_cannot_be_stable(self) -> None:
        for candidate_type in ("belief", "value"):
            with self.assertRaises(ExtractionProtocolError):
                self.staging.stage_batch(
                    [
                        self.payload(
                            candidate_type=candidate_type,
                            statement="一次表达",
                            source_slugs=[f"stable-{candidate_type}"],
                            temporal_scope="stable",
                        )
                    ]
                )

    def test_method_endorsement_is_not_a_preference_by_default(self) -> None:
        with self.assertRaises(ExtractionProtocolError):
            self.staging.stage_batch(
                [
                    self.payload(
                        candidate_type="preference",
                        statement="认同无视中断的方法",
                        source_slugs=["method-001"],
                        evidence_kind="method_endorsement",
                    )
                ]
            )

    def test_method_endorsement_wording_is_not_a_preference_by_default(self) -> None:
        with self.assertRaises(ExtractionProtocolError):
            self.staging.stage_batch(
                [
                    self.payload(
                        candidate_type="preference",
                        statement="用户喜欢‘坚持的方法之一就是无视中断’这句话，主张第二天照常继续",
                        source_slugs=["method-002"],
                    )
                ]
            )

    def test_atomicity_rejects_compound_project_statement(self) -> None:
        with self.assertRaises(ExtractionProtocolError):
            self.staging.stage_batch(
                [
                    self.payload(
                        candidate_type="project",
                        statement="用户正在开发一个交易策略，包含回测和风控功能",
                        source_slugs=["atomic-project-001"],
                    )
                ]
            )

    def test_atomicity_rejects_conjoined_facts_and_beliefs(self) -> None:
        for statement in (
            "用户有健身习惯且体重曾达到110斤",
            "用户认为一切矛盾最终落到能力问题上，行业顶尖者不会过得差",
            "用户既不能因完美主义拖延也不能拖着不改正问题",
            "用户认为能力重要，但对学习方向仍没有结论",
        ):
            with self.assertRaises(ExtractionProtocolError):
                self.staging.stage_batch(
                    [
                        self.payload(
                            candidate_type="fact",
                            statement=statement,
                            source_slugs=["atomic-conjoined"],
                        )
                    ]
                )

    def test_atomicity_rejects_self_assessment_with_recommendation(self) -> None:
        with self.assertRaises(ExtractionProtocolError):
            self.staging.stage_batch(
                [
                    self.payload(
                        candidate_type="belief",
                        statement="用户认为自己经历太少、语言边界匮乏，需要多去感受人性",
                        source_slugs=["atomic-recommendation"],
                    )
                ]
            )

    def test_one_time_self_motivation_cannot_be_stable_value(self) -> None:
        with self.assertRaises(ExtractionProtocolError):
            self.staging.stage_batch(
                [
                    self.payload(
                        candidate_type="value",
                        statement="今天我要相信自己并继续前进",
                        source_slugs=["motivation-001"],
                        evidence_kind="transient_emotion",
                        temporal_scope="stable",
                    )
                ]
            )

    def test_suppression_outcomes_are_returned_without_candidate(self) -> None:
        result = self.staging.stage_batch_with_audit(
            [
                self.payload(
                    candidate_type="experience",
                    statement="今天喝了咖啡",
                    source_slugs=["suppressed-low-value"],
                    evidence_kind="low_value",
                    proposed_action="reject",
                )
            ],
            [
                {
                    "source_slugs": ["suppressed-transient"],
                    "suppression_reason": "transient",
                },
                {
                    "source_slugs": ["suppressed-external"],
                    "suppression_reason": "pure_external_content",
                },
            ],
        )
        self.assertEqual((), result.candidates)
        self.assertEqual(
            {"low_value", "transient", "pure_external_content"},
            {item.suppression_reason for item in result.suppressions},
        )
        self.assertEqual([], self.repository.list_fragments())

    def test_all_supported_suppression_reasons_are_valid(self) -> None:
        reasons = {
            "low_value",
            "transient",
            "pure_external_content",
            "insufficient_personal_signal",
            "duplicate",
            "no_extractable_memory",
        }
        result = self.staging.stage_batch_with_audit(
            [],
            [
                {"source_slugs": [f"suppressed-{reason}"], "suppression_reason": reason}
                for reason in sorted(reasons)
            ],
        )
        self.assertEqual(reasons, {item.suppression_reason for item in result.suppressions})
        self.assertEqual([], self.repository.list_candidates())

    def test_temporal_and_evidence_refs_round_trip_and_accept_later(self) -> None:
        candidate = self.staging.stage_batch(
            [
                self.payload(
                    candidate_type="project",
                    statement="正在开发 Memory Engine V2.1",
                    source_slugs=["roundtrip-001"],
                    observed_at="2026-08-26T09:00:00+08:00",
                    last_supported_at="2026-08-26T09:00:00+08:00",
                    topic="工程",
                )
            ]
        )[0]
        loaded = self.staging.get(candidate.id)
        self.assertEqual("current", loaded.temporal_scope)
        self.assertEqual("2026-08-26T09:00:00+08:00", loaded.observed_at)
        self.assertEqual("2026-08-26T09:00:00+08:00", loaded.last_supported_at)
        self.assertEqual("roundtrip-001", loaded.evidence_refs[0].source_slug)

        self.review.review_batch([CandidateReview(candidate.id, "accept")])
        fragment = self.repository.list_fragments()[0]
        self.assertEqual("current", fragment.temporal_scope)
        self.assertEqual("2026-08-26T09:00:00+08:00", fragment.observed_at)

    def test_contradiction_can_be_staged_as_unresolved(self) -> None:
        candidates = self.staging.stage_batch(
            [
                self.payload(
                    candidate_type="belief",
                    statement="关于该问题的前后表达存在冲突，暂不确定当前立场",
                    source_slugs=["conflict-001", "conflict-002"],
                    evidence_kind="contradictory",
                    proposed_action="unresolved",
                )
            ]
        )
        result = self.review.review_batch([CandidateReview(candidates[0].id, "unresolved")])
        self.assertEqual("unresolved", result.results[0].review_status)
        self.assertEqual([], self.repository.list_fragments())

    def test_insufficient_evidence_cannot_propose_accept(self) -> None:
        with self.assertRaises(ExtractionProtocolError):
            self.staging.stage_batch(
                [
                    self.payload(
                        candidate_type="belief",
                        statement="模型猜测用户相信某个观点",
                        source_slugs=["inference-001"],
                        evidence_kind="insufficient",
                        proposed_action="accept",
                    )
                ]
            )
        self.assertEqual([], self.staging.list())

    def test_edit_stays_in_staging_until_a_later_accept(self) -> None:
        candidates = self.staging.stage_batch(
            [
                self.payload(
                    candidate_type="goal",
                    statement="完成一个模糊目标",
                    source_slugs=["edit-001"],
                )
            ]
        )
        edited = self.review.review_batch(
            [
                CandidateReview(
                    candidates[0].id,
                    "edit",
                    edited_statement="完成 Memory Engine V2 的候选审核接口",
                    edited_proposed_action="accept",
                )
            ]
        )
        self.assertEqual("edited", edited.results[0].review_status)
        self.assertEqual([], self.repository.list_fragments())
        self.assertEqual(
            "完成 Memory Engine V2 的候选审核接口",
            self.staging.get(candidates[0].id).statement,
        )

        accepted = self.review.review_batch([CandidateReview(candidates[0].id, "accept")])
        self.assertEqual("accepted", accepted.results[0].review_status)
        self.assertEqual(1, len(self.repository.list_fragments()))

    def test_reject_does_not_write_formal_memory(self) -> None:
        candidate = self.staging.stage_batch(
            [
                self.payload(
                    candidate_type="experience",
                    statement="一次无长期意义的经历描述",
                    source_slugs=["reject-001"],
                )
            ]
        )[0]
        result = self.review.review_batch([CandidateReview(candidate.id, "reject")])
        self.assertEqual("rejected", result.results[0].review_status)
        self.assertEqual([], self.repository.list_fragments())

    def test_supersede_promotes_new_fragment_and_marks_old_fragment(self) -> None:
        old_candidate = self.staging.stage_batch(
            [
                self.payload(
                    candidate_type="decision",
                    statement="采用旧方案",
                    source_slugs=["supersede-old"],
                )
            ]
        )[0]
        old_result = self.review.review_batch([CandidateReview(old_candidate.id, "accept")])
        old_fragment_id = old_result.results[0].promoted_fragment_id

        new_candidate = self.staging.stage_batch(
            [
                self.payload(
                    candidate_type="decision",
                    statement="改用新方案",
                    source_slugs=["supersede-new"],
                )
            ]
        )[0]
        result = self.review.review_batch(
            [
                CandidateReview(
                    new_candidate.id,
                    "supersede",
                    supersedes_fragment_id=old_fragment_id,
                )
            ]
        )
        self.assertEqual("superseded", result.results[0].review_status)
        fragments = self.repository.list_fragments()
        self.assertEqual(2, len(fragments))
        self.assertEqual({"active", "superseded"}, {fragment.status for fragment in fragments})

    def test_low_value_log_is_suppressed_without_candidate(self) -> None:
        candidates = self.staging.stage_batch(
            [
                self.payload(
                    candidate_type="experience",
                    statement="今天喝了咖啡，天气普通，记录结束",
                    source_slugs=["log-001"],
                    evidence_kind="low_value",
                    proposed_action="reject",
                )
            ]
        )
        self.assertEqual((), candidates)
        self.assertEqual([], self.staging.list())
        self.assertEqual([], self.repository.list_fragments())

    def test_protocol_rejects_raw_memo_fields(self) -> None:
        payload = self.payload(
            candidate_type="fact",
            statement="提炼后的事实",
            source_slugs=["raw-001"],
        )
        payload["memo_body"] = "原始正文不应进入 protocol"
        with self.assertRaises(ExtractionProtocolError):
            ExtractionProtocol.parse_candidate(payload)

    def test_batch_validation_happens_before_any_candidate_is_written(self) -> None:
        valid = self.payload(
            candidate_type="fact",
            statement="先暂存的事实",
            source_slugs=["batch-valid"],
        )
        invalid = self.payload(
            candidate_type="pattern",
            statement="不合法的单源 pattern",
            source_slugs=["batch-invalid"],
            evidence_kind="corroborated",
        )
        with self.assertRaises(ExtractionProtocolError):
            self.staging.stage_batch([valid, invalid])
        self.assertEqual([], self.staging.list())

    def test_staging_requires_trusted_evidence_catalog(self) -> None:
        staging = CandidateStagingService(self.repository)
        with self.assertRaises(EvidenceCatalogRequiredError):
            staging.stage_batch(
                [
                    self.payload(
                        candidate_type="fact",
                        statement="没有 trusted catalog 不能暂存",
                        source_slugs=["catalog-required-001"],
                    )
                ]
            )
        self.assertEqual([], self.repository.list_candidates())

    def test_forged_evidence_metadata_is_rejected(self) -> None:
        catalog = FlomoEvidenceCatalogBuilder.from_api_response(
            [
                {
                    "slug": "forged-001",
                    "created_at": "2020-01-01",
                    "context": "真实上下文",
                }
            ]
        )
        with self.assertRaises(TrustedEvidenceError):
            self.staging.stage_batch(
                [
                    self.payload(
                        candidate_type="fact",
                        statement="来源元数据必须可信",
                        source_slugs=["forged-001"],
                        observed_at="2099-01-01",
                        evidence_refs=[
                            {
                                "source_slug": "forged-001",
                                "observed_at": "2099-01-01",
                                "context": "伪造上下文",
                            }
                        ],
                    )
                ],
                evidence_catalog=catalog,
            )
        self.assertEqual([], self.repository.list_candidates())

    def test_source_metadata_controls_temporal_provenance(self) -> None:
        catalog = FlomoEvidenceCatalogBuilder.from_api_response(
            [
                {"slug": "old-001", "created_at": "2020-01-03", "context": "工作"},
                {"slug": "old-002", "created_at": "2020-02-10", "context": "复盘"},
            ]
        )
        candidate = self.staging.stage_batch(
            [
                self.payload(
                    candidate_type="fact",
                    statement="候选时间来自原始来源元数据",
                    source_slugs=["old-001", "old-002"],
                )
            ],
            evidence_catalog=catalog,
        )[0]
        self.assertEqual("2020-01-03", candidate.observed_at)
        self.assertEqual("2020-02-10", candidate.last_supported_at)
        self.assertNotEqual(candidate.created_at, candidate.observed_at)
        self.assertEqual(
            ("2020-01-03", "2020-02-10"),
            tuple(ref.observed_at for ref in candidate.evidence_refs),
        )
        self.review.review_batch([CandidateReview(candidate.id, "accept")])
        fragment = self.repository.list_fragments()[0]
        self.assertEqual("2020-01-03", fragment.valid_from)
        self.assertEqual("2020-01-03", fragment.observed_at)
        self.assertEqual("2020-02-10", fragment.last_supported_at)

    def test_repeated_extraction_is_idempotent_for_staging_and_accept(self) -> None:
        first_payload = self.payload(
            candidate_type="fact",
            statement="同一原子命题只产生一个候选",
            source_slugs=["idem-001", "idem-002"],
        )
        second_payload = self.payload(
            candidate_type="fact",
            statement="  同一原子命题只产生一个候选  ",
            source_slugs=["idem-002", "idem-001"],
        )
        first = self.staging.stage_batch([first_payload])[0]
        second = self.staging.stage_batch([second_payload])[0]
        self.assertEqual(first.id, second.id)
        self.assertEqual(1, len(self.repository.list_candidates()))

        self.review.review_batch([CandidateReview(first.id, "accept")])
        repeated = self.staging.stage_batch([first_payload])[0]
        self.assertEqual(first.id, repeated.id)
        self.assertEqual("accepted", repeated.review_status)
        self.assertEqual(1, len(self.repository.list_fragments()))
        self.assertEqual(1, len(self.repository.list_promotion_lineage()))

    def test_protocol_upgrade_creates_explicit_revision_and_not_duplicate_fragment(self) -> None:
        payload = self.payload(
            candidate_type="fact",
            statement="协议升级仍指向同一个原子命题",
            source_slugs=["revision-source-001"],
        )
        old_candidate = self.staging.stage_batch([payload])[0]
        self.repository.update_candidate(
            replace(old_candidate, protocol_version="2.2", dedupe_key=""),
            _provenance_token=_STAGING_WRITE_TOKEN,
        )

        new_candidate = self.staging.stage_batch([payload])[0]
        self.assertNotEqual(old_candidate.id, new_candidate.id)
        self.assertEqual("2.3", new_candidate.protocol_version)
        self.assertEqual(old_candidate.id, new_candidate.revision_of_candidate_id)
        self.assertEqual("protocol_upgrade", new_candidate.revision_reason)

        old_result = self.review.review_batch([CandidateReview(old_candidate.id, "accept")])
        new_result = self.review.review_batch([CandidateReview(new_candidate.id, "accept")])
        self.assertEqual(
            old_result.results[0].promoted_fragment_id,
            new_result.results[0].promoted_fragment_id,
        )
        self.assertEqual(1, len(self.repository.list_fragments()))
        self.assertEqual(2, len(self.repository.list_promotion_lineage()))

    def test_manual_reclassification_requires_explicit_review_edit(self) -> None:
        payload = self.payload(
            candidate_type="fact",
            statement="用户偏好先定义规则再执行",
            source_slugs=["reclassify-source-001"],
        )
        candidate = self.staging.stage_batch([payload])[0]
        with self.assertRaises(CandidateRevisionRequiredError):
            self.staging.stage_batch(
                [
                    self.payload(
                        candidate_type="preference",
                        statement=payload["statement"],
                        source_slugs=list(payload["source_slugs"]),
                    )
                ]
            )

        edited = self.review.review_batch(
            [CandidateReview(candidate.id, "edit", edited_type="preference")]
        )
        self.assertEqual("edited", edited.results[0].review_status)
        saved = self.staging.get(candidate.id)
        self.assertEqual("preference", saved.type)
        self.assertEqual("manual_reclassification", saved.revision_reason)
        self.review.review_batch([CandidateReview(candidate.id, "accept")])
        self.assertEqual(1, len(self.repository.list_fragments()))
        self.assertEqual("preference", self.repository.list_fragments()[0].type)

    def test_review_batch_rolls_back_all_items_when_later_item_fails(self) -> None:
        candidates = self.staging.stage_batch(
            [
                self.payload(
                    candidate_type="fact",
                    statement="批量事务第一项",
                    source_slugs=["rollback-001"],
                ),
                self.payload(
                    candidate_type="fact",
                    statement="批量事务第二项",
                    source_slugs=["rollback-002"],
                ),
            ]
        )
        original_create = self.review.memory_service.create_fragment
        call_count = 0

        def fail_on_second(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("injected second-item failure")
            return original_create(**kwargs)

        with patch.object(
            self.review.memory_service,
            "create_fragment",
            side_effect=fail_on_second,
        ):
            with self.assertRaises(RuntimeError):
                self.review.review_batch(
                    [
                        CandidateReview(candidates[0].id, "accept"),
                        CandidateReview(candidates[1].id, "accept"),
                    ]
                )

        self.assertEqual([], self.repository.list_fragments())
        self.assertEqual([], self.repository.list_promotion_lineage())
        self.assertEqual(
            ["pending", "pending"],
            [candidate.review_status for candidate in self.staging.list()],
        )

    def test_supersede_rolls_back_when_supersede_step_fails(self) -> None:
        old_candidate = self.staging.stage_batch(
            [
                self.payload(
                    candidate_type="decision",
                    statement="回滚测试旧决定",
                    source_slugs=["rollback-old"],
                )
            ]
        )[0]
        old_result = self.review.review_batch([CandidateReview(old_candidate.id, "accept")])
        old_fragment_id = old_result.results[0].promoted_fragment_id
        new_candidate = self.staging.stage_batch(
            [
                self.payload(
                    candidate_type="decision",
                    statement="回滚测试新决定",
                    source_slugs=["rollback-new"],
                )
            ]
        )[0]

        original_update = self.review.memory_service.update_fragment

        def update_then_fail(fragment, **kwargs):
            result = original_update(fragment, **kwargs)
            raise RuntimeError("injected supersede failure")

        with patch.object(
            self.review.memory_service,
            "update_fragment",
            side_effect=update_then_fail,
        ):
            with self.assertRaises(RuntimeError):
                self.review.review_batch(
                    [
                        CandidateReview(
                            new_candidate.id,
                            "supersede",
                            supersedes_fragment_id=old_fragment_id,
                        )
                    ]
                )

        fragments = self.repository.list_fragments()
        self.assertEqual(1, len(fragments))
        self.assertEqual("active", fragments[0].status)
        self.assertEqual("pending", self.staging.get(new_candidate.id).review_status)
        self.assertEqual(1, len(self.repository.list_promotion_lineage()))

    def test_promotion_provenance_is_queryable_for_accept_and_supersede(self) -> None:
        old_candidate = self.staging.stage_batch(
            [
                self.payload(
                    candidate_type="decision",
                    statement="可回查的旧决定",
                    source_slugs=["lineage-old"],
                )
            ]
        )[0]
        old_result = self.review.review_batch([CandidateReview(old_candidate.id, "accept")])
        old_fragment_id = old_result.results[0].promoted_fragment_id
        old_lineage = self.repository.get_promotion_for_candidate(old_candidate.id)
        self.assertEqual(old_fragment_id, old_lineage.promoted_fragment_id)
        self.assertEqual("accept", old_lineage.action)
        self.assertEqual("2.3", old_lineage.protocol_version)

        new_candidate = self.staging.stage_batch(
            [
                self.payload(
                    candidate_type="decision",
                    statement="可回查的新决定",
                    source_slugs=["lineage-new"],
                )
            ]
        )[0]
        new_result = self.review.review_batch(
            [
                CandidateReview(
                    new_candidate.id,
                    "supersede",
                    supersedes_fragment_id=old_fragment_id,
                )
            ]
        )
        new_lineage = self.repository.get_promotion_for_candidate(new_candidate.id)
        self.assertEqual(new_result.results[0].promoted_fragment_id, new_lineage.promoted_fragment_id)
        self.assertEqual(old_fragment_id, new_lineage.superseded_fragment_id)
        self.assertEqual("supersede", new_lineage.action)
        self.assertEqual("flomo-memory-extraction", new_lineage.protocol_name)

    def test_raw_and_oversized_derived_text_is_rejected(self) -> None:
        oversized_statement = self.payload(
            candidate_type="fact",
            statement="x" * 1001,
            source_slugs=["oversized-statement"],
        )
        with self.assertRaises(ExtractionProtocolError):
            self.staging.stage_batch([oversized_statement])

        oversized_topic = self.payload(
            candidate_type="fact",
            statement="合法候选",
            topic="x" * 201,
            source_slugs=["oversized-topic"],
        )
        with self.assertRaises(ExtractionProtocolError):
            self.staging.stage_batch([oversized_topic])

        oversized_evidence = self.payload(
            candidate_type="fact",
            statement="合法候选",
            source_slugs=["oversized-evidence"],
            evidence_refs=[
                {
                    "source_slug": "oversized-evidence",
                    "observed_at": "2026-01-01",
                    "context": "x" * 301,
                }
            ],
        )
        with self.assertRaises(ExtractionProtocolError):
            self.staging.stage_batch([oversized_evidence])

        raw_payload = self.payload(
            candidate_type="fact",
            statement="合法候选",
            source_slugs=["raw-body-002"],
        )
        raw_payload["text"] = "memo body"
        with self.assertRaises(ExtractionProtocolError):
            self.staging.stage_batch([raw_payload])

        candidate = self.staging.stage_batch(
            [
                self.payload(
                    candidate_type="fact",
                    statement="审核备注也必须有上限",
                    source_slugs=["oversized-note"],
                )
            ]
        )[0]
        with self.assertRaises(CandidateReviewError):
            self.review.review_batch(
                [CandidateReview(candidate.id, "reject", review_note="x" * 501)]
            )
        self.assertEqual("pending", self.staging.get(candidate.id).review_status)

    def test_review_batch_rejects_duplicate_candidate_ids(self) -> None:
        candidate = self.staging.stage_batch(
            [
                self.payload(
                    candidate_type="fact",
                    statement="重复审核测试",
                    source_slugs=["duplicate-001"],
                )
            ]
        )[0]
        with self.assertRaises(CandidateReviewError):
            self.review.review_batch(
                [
                    CandidateReview(candidate.id, "reject"),
                    CandidateReview(candidate.id, "unresolved"),
                ]
            )
        self.assertEqual("pending", self.staging.get(candidate.id).review_status)


if __name__ == "__main__":
    unittest.main()
