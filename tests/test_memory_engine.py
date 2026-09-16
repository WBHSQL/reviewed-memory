"""CRUD, storage-boundary, and V2.3.1 trust-gate tests."""

from __future__ import annotations

import tempfile
import unittest
import sqlite3
from pathlib import Path

try:
    from memory_engine import (
        CandidateReview,
        CandidateReviewService,
        CandidateStagingService,
        FlomoEvidenceCatalogBuilder,
        MemoryFragment,
        MemoryRepository,
        MemoryValidationError,
    )
    from memory_engine.service import MemoryService
except ModuleNotFoundError:
    from flomo_poc.memory_engine import (
        CandidateReview,
        CandidateReviewService,
        CandidateStagingService,
        FlomoEvidenceCatalogBuilder,
        MemoryFragment,
        MemoryRepository,
        MemoryValidationError,
    )
    from flomo_poc.memory_engine.service import MemoryService


class MemoryEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = MemoryRepository(":memory:")
        self.service = MemoryService(self.repository)

    def tearDown(self) -> None:
        self.repository.close()

    def _promote(
        self,
        *,
        fragment_type: str,
        statement: str,
        source_slug: str,
        observed_at: str = "2026-08-26T10:00:00+08:00",
    ):
        catalog = FlomoEvidenceCatalogBuilder.from_api_response(
            [
                {
                    "slug": source_slug,
                    "created_at": observed_at,
                    "context": "测试",
                }
            ]
        )
        staging = CandidateStagingService(self.repository, catalog)
        candidate = staging.stage_batch(
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

    def test_schema_contains_required_tables_and_no_raw_memo_column(self) -> None:
        tables = {
            row[0]
            for row in self.repository.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        self.assertTrue(
            {
                "memory_fragments",
                "user_profile",
                "current_memory",
                "open_loops",
                "memory_candidates",
                "candidate_provenance",
                "promotion_lineage",
            }
            <= tables
        )

        for table in tables & {"memory_fragments", "user_profile", "current_memory", "open_loops"}:
            columns = {
                row[1]
                for row in self.repository.connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            self.assertNotIn("memo_body", columns)
            self.assertNotIn("raw_text", columns)
            self.assertIn("source_slugs", columns)
        provenance_columns = {
            row[1]
            for row in self.repository.connection.execute(
                "PRAGMA table_info(candidate_provenance)"
            ).fetchall()
        }
        self.assertTrue(
            {
                "candidate_id",
                "proof_marker",
                "origin",
                "source_slugs",
                "observed_at",
                "last_supported_at",
                "evidence_refs",
            }
            <= provenance_columns
        )
        self.assertNotIn("memo_body", provenance_columns)
        self.assertNotIn("raw_text", provenance_columns)

    def test_formal_fragment_reads_are_traceable_after_promotion(self) -> None:
        fragment = self._promote(
            fragment_type="preference",
            statement="偏好把复杂任务拆成可验证的小步骤",
            source_slug="memo-001",
        )
        self.assertIsNotNone(fragment.id)
        self.assertEqual(("memo-001",), fragment.source_slugs)
        self.assertEqual(fragment, self.service.get_fragment(fragment.id))
        self.assertEqual([fragment], self.service.list_fragments(fragment_type="preference"))

    def test_direct_fragment_write_cannot_bypass_review_lineage(self) -> None:
        with self.assertRaises(MemoryValidationError):
            self.service.create_fragment(
                fragment_type="fact",
                statement="未经审核的正式事实",
                source_slugs=["direct-service-001"],
            )
        with self.assertRaises(MemoryValidationError):
            self.service.update_fragment(self._fragment_fixture("direct-update-001"))
        with self.assertRaises(PermissionError):
            self.repository.create_fragment(
                self._fragment_fixture("direct-repository-001")
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.repository.connection.execute(
                """
                INSERT INTO memory_fragments
                    (type, statement, topic, confidence, status, valid_from,
                     last_supported_at, source_slugs, created_at, updated_at,
                     temporal_scope, observed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "fact",
                    "raw SQL active fragment",
                    "",
                    0.5,
                    "active",
                    "2026-08-26",
                    "2026-08-26",
                    '["direct-sql-001"]',
                    "2026-08-26",
                    "2026-08-26",
                    "episodic",
                    "2026-08-26",
                ),
            )
        self.assertEqual([], self.repository.list_fragments())
        self.assertEqual([], self.repository.list_active_fragments_without_lineage())

    def test_package_api_does_not_export_memory_service_writer(self) -> None:
        try:
            import memory_engine as package
        except ModuleNotFoundError:
            import flomo_poc.memory_engine as package
        self.assertNotIn("MemoryService", package.__all__)
        self.assertFalse(hasattr(package, "MemoryService"))

    @staticmethod
    def _fragment_fixture(source_slug: str):
        return MemoryFragment(
            id=None,
            type="fact",
            statement="未经审核的正式事实",
            topic="",
            confidence=0.9,
            status="active",
            valid_from="2026-08-26T10:00:00+08:00",
            last_supported_at="2026-08-26T10:00:00+08:00",
            source_slugs=(source_slug,),
            created_at="2026-08-26T10:00:00+08:00",
            updated_at="2026-08-26T10:00:00+08:00",
            temporal_scope="episodic",
            observed_at="2026-08-26T10:00:00+08:00",
        )

    def test_user_profile_upsert_and_delete(self) -> None:
        first = self.service.upsert_profile(
            attribute="communication_style",
            value="默认使用中文，结论先行",
            confidence=0.8,
            source_slugs=["memo-profile-1"],
        )
        second = self.service.upsert_profile(
            attribute="communication_style",
            value="默认使用中文，结论先行，并标注证据不足",
            confidence=0.95,
            source_slugs=["memo-profile-2"],
        )
        self.assertEqual(first.id, second.id)
        self.assertEqual(second, self.service.get_profile("communication_style"))
        self.assertEqual(1, len(self.service.list_profiles()))
        self.assertTrue(self.service.delete_profile("communication_style"))

    def test_current_memory_crud(self) -> None:
        item = self.service.create_current_memory(
            item_type="project",
            statement="实现 Memory Engine V1",
            topic="flomo_poc",
            priority=10,
            source_slugs=["memo-project-1"],
        )
        self.assertEqual([item], self.service.list_current_memory(item_type="project"))
        updated = self.service.update_current_memory(
            item.__class__(
                id=item.id,
                type=item.type,
                statement="完成 Memory Engine V1 的 schema 和 CRUD",
                topic=item.topic,
                status="active",
                priority=item.priority,
                source_slugs=item.source_slugs,
                created_at=item.created_at,
                updated_at=item.updated_at,
            )
        )
        self.assertIn("CRUD", updated.statement)
        self.assertTrue(self.service.delete_current_memory(item.id))

    def test_open_loop_crud(self) -> None:
        loop = self.service.create_open_loop(
            statement="下一阶段如何设计人工确认后的 extraction 接口？",
            topic="Memory Engine",
            source_slugs=["memo-loop-1"],
        )
        self.assertEqual([loop], self.service.list_open_loops(status="open"))
        updated = self.service.update_open_loop(
            loop.__class__(
                id=loop.id,
                statement=loop.statement,
                topic=loop.topic,
                status="resolved",
                source_slugs=loop.source_slugs,
                last_reviewed_at="2026-08-26T00:00:00+00:00",
                created_at=loop.created_at,
                updated_at=loop.updated_at,
            )
        )
        self.assertEqual("resolved", updated.status)
        self.assertTrue(self.service.delete_open_loop(loop.id))

    def test_all_derived_records_require_source_slug(self) -> None:
        with self.assertRaises(MemoryValidationError):
            self.service.create_fragment(
                fragment_type="fact", statement="没有来源的记录", source_slugs=[]
            )
        with self.assertRaises(MemoryValidationError):
            self.service.create_open_loop(statement="没有来源的问题", source_slugs=[])

    def test_database_file_does_not_contain_full_memo_body(self) -> None:
        full_memo_body = "这是不应被永久保存的 flomo 原始 memo 正文。"
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "derived_memory.sqlite3"
            repository = MemoryRepository(db_path)
            try:
                self._promote_in_repository(
                    repository,
                    fragment_type="fact",
                    statement="提炼后的事实",
                    source_slug="memo-boundary-1",
                )
            finally:
                repository.close()
            self.assertNotIn(full_memo_body.encode("utf-8"), db_path.read_bytes())

    @staticmethod
    def _promote_in_repository(repository, *, fragment_type, statement, source_slug):
        catalog = FlomoEvidenceCatalogBuilder.from_api_response(
            [{"slug": source_slug, "created_at": "2026-08-26T10:00:00+00:00"}]
        )
        candidate = CandidateStagingService(repository, catalog).stage_batch(
            [
                {
                    "type": fragment_type,
                    "statement": statement,
                    "confidence": 0.9,
                    "source_slugs": [source_slug],
                    "evidence_kind": "explicit",
                    "proposed_action": "accept",
                }
            ]
        )[0]
        return CandidateReviewService(repository).review_batch(
            [CandidateReview(candidate.id, "accept")]
        )

    def test_invalid_confidence_is_rejected(self) -> None:
        with self.assertRaises(MemoryValidationError):
            self.service.create_fragment(
                fragment_type="fact",
                statement="事实",
                confidence=1.1,
                source_slugs=["memo-invalid-1"],
            )

    def test_v23_temporal_fields_are_present_on_formal_fragments(self) -> None:
        fragment = self._promote(
            fragment_type="project",
            statement="正在进行一个有时间边界的项目",
            source_slug="memo-temporal-1",
            observed_at="2026-08-26T10:00:00+08:00",
        )
        self.assertEqual("current", fragment.temporal_scope)
        self.assertEqual("2026-08-26T10:00:00+08:00", fragment.observed_at)

    def test_v23_formal_temporal_defaults_are_conservative(self) -> None:
        expectations = (
            ("fact", "一次性事实", "episodic"),
            ("experience", "一次经历", "episodic"),
            ("decision", "做出一次决定", "episodic"),
            ("belief", "相信先写规则", "current"),
            ("preference", "偏好结论先行", "current"),
            ("value", "重视可追溯性", "current"),
            ("project", "当前项目", "current"),
            ("goal", "当前目标", "current"),
        )
        fragments = [
            self._promote(
                fragment_type=fragment_type,
                statement=statement,
                source_slug=f"temporal-fragment-{index}",
            )
            for index, (fragment_type, statement, _expected) in enumerate(expectations)
        ]
        self.assertEqual(
            [expected for _type, _statement, expected in expectations],
            [fragment.temporal_scope for fragment in fragments],
        )
        with self.assertRaises(MemoryValidationError):
            self.service.create_fragment(
                fragment_type="fact",
                statement="用户使用 SQLite",
                source_slugs=["temporal-fragment-invalid"],
                temporal_scope="current",
            )

    def test_v23_migrates_v2_tables_without_raw_body_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "old-v2.sqlite3"
            connection = sqlite3.connect(db_path)
            connection.executescript(
                """
                CREATE TABLE memory_fragments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT NOT NULL,
                    statement TEXT NOT NULL,
                    topic TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL,
                    status TEXT NOT NULL,
                    valid_from TEXT NOT NULL,
                    last_supported_at TEXT NOT NULL,
                    source_slugs TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE memory_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT NOT NULL,
                    statement TEXT NOT NULL,
                    topic TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL,
                    source_slugs TEXT NOT NULL,
                    evidence_kind TEXT NOT NULL,
                    review_status TEXT NOT NULL,
                    proposed_action TEXT NOT NULL,
                    review_note TEXT,
                    reviewed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO memory_candidates
                    (type, statement, topic, confidence, source_slugs, evidence_kind,
                     review_status, proposed_action, review_note, reviewed_at,
                     created_at, updated_at)
                VALUES
                    ('fact', '迁移中的候选', '', 0.8, '["migration-source"]',
                     'explicit', 'pending', 'accept', NULL, NULL,
                     '2026-01-01', '2026-01-01');
                """
            )
            connection.close()

            repository = MemoryRepository(db_path)
            fragment_columns = {
                row[1]
                for row in repository.connection.execute(
                    "PRAGMA table_info(memory_fragments)"
                ).fetchall()
            }
            candidate_columns = {
                row[1]
                for row in repository.connection.execute(
                    "PRAGMA table_info(memory_candidates)"
                ).fetchall()
            }
            self.assertTrue({"temporal_scope", "observed_at"} <= fragment_columns)
            self.assertTrue(
                {"temporal_scope", "observed_at", "last_supported_at", "evidence_refs"}
                <= candidate_columns
            )
            self.assertEqual(
                6,
                repository.connection.execute("PRAGMA user_version").fetchone()[0],
            )
            self.assertTrue(
                {
                    "dedupe_key",
                    "protocol_name",
                    "protocol_version",
                    "revision_of_candidate_id",
                    "revision_reason",
                    "trusted_staging_proof",
                }
                <= candidate_columns
            )
            migrated_candidate = repository.get_candidate(1)
            self.assertEqual("2.1", migrated_candidate.protocol_version)
            self.assertEqual(64, len(migrated_candidate.dedupe_key))
            self.assertIsNone(migrated_candidate.revision_of_candidate_id)
            repository.close()


if __name__ == "__main__":
    unittest.main()
