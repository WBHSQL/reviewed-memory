"""SQLite persistence for derived memory records.

The repository has no API for raw flomo memo objects or memo bodies. It only
persists the structured fields represented by the models in this package.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator, List, Optional

from .constants import (
    MAX_EVIDENCE_CONTEXT_LENGTH,
    MAX_REVISION_REASON_LENGTH,
    MAX_REVIEW_NOTE_LENGTH,
    MAX_SOURCE_SLUG_LENGTH,
    MAX_STATEMENT_LENGTH,
    MAX_TRUSTED_STAGING_PROOF_LENGTH,
    MAX_TOPIC_LENGTH,
)
from .identity import candidate_dedupe_key, candidate_semantic_key
from .models import (
    CurrentMemoryItem,
    CandidateProvenance,
    EvidenceRef,
    EvidenceRefs,
    MemoryCandidate,
    MemoryFragment,
    OpenLoop,
    PromotionLineage,
    SourceSlugs,
    UserProfileEntry,
)


_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class CandidateIdentityConflictError(ValueError):
    """Raised when one protocol identity is reused with a new classification."""


class DirectFragmentWriteError(PermissionError):
    """Raised when formal fragments are written outside internal promotion."""


# These capabilities are module-private. Candidate staging and review import
# them through implementation modules; they are intentionally not package API.
_FORMAL_WRITE_TOKEN = object()
_STAGING_WRITE_TOKEN = object()
_STAGING_ORIGIN = "flomo_trusted_staging"


def _encode_source_slugs(source_slugs: SourceSlugs) -> str:
    if not source_slugs:
        raise ValueError("source_slugs must contain at least one slug")
    if any(
        not isinstance(slug, str)
        or not slug.strip()
        or len(slug.strip()) > MAX_SOURCE_SLUG_LENGTH
        for slug in source_slugs
    ):
        raise ValueError("source_slugs must contain non-empty strings")
    return json.dumps(list(source_slugs), ensure_ascii=False, separators=(",", ":"))


def _decode_source_slugs(raw_value: str) -> SourceSlugs:
    try:
        values = json.loads(raw_value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise sqlite3.DatabaseError("source_slugs 数据损坏") from exc
    if not isinstance(values, list) or not values or any(
        not isinstance(value, str) or not value.strip() for value in values
    ):
        raise sqlite3.DatabaseError("source_slugs 数据损坏")
    return tuple(values)


def _encode_evidence_refs(evidence_refs: EvidenceRefs) -> str:
    encoded = []
    for evidence_ref in evidence_refs:
        if not isinstance(evidence_ref, EvidenceRef):
            raise ValueError("evidence_refs must contain EvidenceRef values")
        if (
            not isinstance(evidence_ref.source_slug, str)
            or not evidence_ref.source_slug.strip()
            or len(evidence_ref.source_slug.strip()) > MAX_SOURCE_SLUG_LENGTH
            or not isinstance(evidence_ref.observed_at, str)
            or not evidence_ref.observed_at.strip()
        ):
            raise ValueError("evidence_refs must contain source slug and observed_at")
        if not isinstance(evidence_ref.context, str):
            raise ValueError("evidence_ref context must be text")
        if len(evidence_ref.context.strip()) > MAX_EVIDENCE_CONTEXT_LENGTH:
            raise ValueError("evidence_ref context exceeds the maximum length")
        encoded.append(
            {
                "source_slug": evidence_ref.source_slug,
                "observed_at": evidence_ref.observed_at,
                "context": evidence_ref.context,
            }
        )
    return json.dumps(encoded, ensure_ascii=False, separators=(",", ":"))


def _decode_evidence_refs(raw_value: str) -> EvidenceRefs:
    try:
        values = json.loads(raw_value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise sqlite3.DatabaseError("evidence_refs 数据损坏") from exc
    if not isinstance(values, list):
        raise sqlite3.DatabaseError("evidence_refs 数据损坏")
    decoded = []
    for value in values:
        if not isinstance(value, dict) or set(value) != {"source_slug", "observed_at", "context"}:
            raise sqlite3.DatabaseError("evidence_refs 数据损坏")
        if (
            not isinstance(value["source_slug"], str)
            or not value["source_slug"].strip()
            or len(value["source_slug"].strip()) > MAX_SOURCE_SLUG_LENGTH
        ):
            raise sqlite3.DatabaseError("evidence_refs 数据损坏")
        if not isinstance(value["observed_at"], str) or not value["observed_at"].strip():
            raise sqlite3.DatabaseError("evidence_refs 数据损坏")
        if not isinstance(value["context"], str):
            raise sqlite3.DatabaseError("evidence_refs 数据损坏")
        if len(value["context"].strip()) > MAX_EVIDENCE_CONTEXT_LENGTH:
            raise sqlite3.DatabaseError("evidence_refs 数据损坏")
        decoded.append(
            EvidenceRef(
                source_slug=value["source_slug"],
                observed_at=value["observed_at"],
                context=value["context"],
            )
        )
    return tuple(decoded)


def _bounded_text(
    value: object,
    field: str,
    limit: int,
    *,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    normalized = value.strip()
    if not allow_empty and not normalized:
        raise ValueError(f"{field} is required")
    if len(normalized) > limit:
        raise ValueError(f"{field} exceeds the maximum length")
    return normalized


class MemoryRepository:
    """CRUD repository backed by a local SQLite database."""

    def __init__(self, db_path: str | Path = Path("data/derived_memory.sqlite3")) -> None:
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        self.connection = sqlite3.connect(self.db_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._transaction_depth = 0
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        self._migrate_schema()

    def _migrate_schema(self) -> None:
        """Migrate older derived-memory databases to the V2.3.1 contract."""

        additions = {
            "memory_fragments": {
                "temporal_scope": "TEXT NOT NULL DEFAULT 'current'",
                "observed_at": "TEXT NOT NULL DEFAULT ''",
            },
            "memory_candidates": {
                "temporal_scope": "TEXT NOT NULL DEFAULT 'current'",
                "observed_at": "TEXT NOT NULL DEFAULT ''",
                "last_supported_at": "TEXT NOT NULL DEFAULT ''",
                "evidence_refs": "TEXT NOT NULL DEFAULT '[]'",
                "dedupe_key": "TEXT NOT NULL DEFAULT ''",
                "protocol_name": "TEXT NOT NULL DEFAULT 'flomo-memory-extraction'",
                "protocol_version": "TEXT NOT NULL DEFAULT '2.1'",
                "revision_of_candidate_id": (
                    "INTEGER REFERENCES memory_candidates(id) ON DELETE RESTRICT"
                ),
                "revision_reason": (
                    f"TEXT CHECK (revision_reason IS NULL OR length(revision_reason) <= "
                    f"{MAX_REVISION_REASON_LENGTH})"
                ),
                "trusted_staging_proof": (
                    "TEXT CHECK (trusted_staging_proof IS NULL OR "
                    f"length(trim(trusted_staging_proof)) BETWEEN 1 AND "
                    f"{MAX_TRUSTED_STAGING_PROOF_LENGTH})"
                ),
            },
        }
        for table, columns in additions.items():
            existing = {
                str(row[1])
                for row in self.connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for column, definition in columns.items():
                if column not in existing:
                    self.connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                    )
        # V2.2 used a version-independent key.  Rebuild every key so an
        # upgraded protocol gets an explicit revision candidate instead of
        # colliding with the old row.  Dropping the index first also makes this
        # safe when an existing database contains legacy duplicate identities.
        self.connection.execute("DROP INDEX IF EXISTS idx_memory_candidates_dedupe_key")
        seen_keys: set[str] = set()
        rows = self.connection.execute(
            "SELECT id, statement, source_slugs, protocol_name, protocol_version "
            "FROM memory_candidates ORDER BY id"
        ).fetchall()
        for row in rows:
            protocol_name = str(row["protocol_name"] or "flomo-memory-extraction")
            protocol_version = str(row["protocol_version"] or "2.1")
            key = candidate_dedupe_key(
                _decode_source_slugs(str(row["source_slugs"])),
                str(row["statement"]),
                protocol_name=protocol_name,
                protocol_version=protocol_version,
            )
            if key in seen_keys:
                key = f"{key}:legacy-{int(row['id'])}"
            seen_keys.add(key)
            self.connection.execute(
                "UPDATE memory_candidates SET dedupe_key = ?, protocol_name = ?, "
                "protocol_version = ? WHERE id = ?",
                (key, protocol_name, protocol_version, int(row["id"])),
            )
        self.connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_candidates_dedupe_key "
            "ON memory_candidates(dedupe_key)"
        )
        self.connection.execute("PRAGMA user_version = 6")
        self.connection.commit()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run several repository writes as one all-or-nothing transaction."""

        if self._transaction_depth:
            yield
            return
        if self.connection.in_transaction:
            raise sqlite3.OperationalError("a repository transaction is already active")
        self._transaction_depth += 1
        try:
            self.connection.execute("BEGIN")
            yield
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        finally:
            self._transaction_depth -= 1

    @contextmanager
    def _write_context(self) -> Iterator[None]:
        """Avoid inner repository methods committing an outer transaction."""

        if self._transaction_depth:
            yield
        else:
            with self.connection:
                yield

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "MemoryRepository":
        return self

    def __exit__(self, _exc_type: Any, _exc_value: Any, _traceback: Any) -> None:
        self.close()

    @staticmethod
    def _ensure_id(record_id: Optional[int]) -> int:
        if record_id is None:
            raise ValueError("record id is required")
        return int(record_id)

    @staticmethod
    def _row_or_none(cursor: sqlite3.Cursor) -> Optional[sqlite3.Row]:
        return cursor.fetchone()

    @staticmethod
    def _fragment_from_row(row: sqlite3.Row) -> MemoryFragment:
        return MemoryFragment(
            id=int(row["id"]),
            type=str(row["type"]),
            statement=str(row["statement"]),
            topic=str(row["topic"]),
            confidence=float(row["confidence"]),
            status=str(row["status"]),
            valid_from=str(row["valid_from"]),
            last_supported_at=str(row["last_supported_at"]),
            source_slugs=_decode_source_slugs(str(row["source_slugs"])),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            temporal_scope=str(row["temporal_scope"]),
            observed_at=str(row["observed_at"]),
        )

    @staticmethod
    def _profile_from_row(row: sqlite3.Row) -> UserProfileEntry:
        return UserProfileEntry(
            id=int(row["id"]),
            attribute=str(row["attribute"]),
            value=str(row["value"]),
            topic=str(row["topic"]),
            confidence=float(row["confidence"]),
            status=str(row["status"]),
            source_slugs=_decode_source_slugs(str(row["source_slugs"])),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _current_memory_from_row(row: sqlite3.Row) -> CurrentMemoryItem:
        return CurrentMemoryItem(
            id=int(row["id"]),
            type=str(row["type"]),
            statement=str(row["statement"]),
            topic=str(row["topic"]),
            status=str(row["status"]),
            priority=int(row["priority"]),
            source_slugs=_decode_source_slugs(str(row["source_slugs"])),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _open_loop_from_row(row: sqlite3.Row) -> OpenLoop:
        return OpenLoop(
            id=int(row["id"]),
            statement=str(row["statement"]),
            topic=str(row["topic"]),
            status=str(row["status"]),
            source_slugs=_decode_source_slugs(str(row["source_slugs"])),
            last_reviewed_at=(
                str(row["last_reviewed_at"]) if row["last_reviewed_at"] is not None else None
            ),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _candidate_from_row(row: sqlite3.Row) -> MemoryCandidate:
        return MemoryCandidate(
            id=int(row["id"]),
            type=str(row["type"]),
            statement=str(row["statement"]),
            topic=str(row["topic"]),
            confidence=float(row["confidence"]),
            source_slugs=_decode_source_slugs(str(row["source_slugs"])),
            evidence_kind=str(row["evidence_kind"]),
            review_status=str(row["review_status"]),
            proposed_action=str(row["proposed_action"]),
            review_note=(str(row["review_note"]) if row["review_note"] is not None else None),
            reviewed_at=(str(row["reviewed_at"]) if row["reviewed_at"] is not None else None),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            temporal_scope=str(row["temporal_scope"]),
            observed_at=str(row["observed_at"]),
            last_supported_at=str(row["last_supported_at"]),
            evidence_refs=_decode_evidence_refs(str(row["evidence_refs"])),
            dedupe_key=str(row["dedupe_key"]),
            protocol_name=str(row["protocol_name"]),
            protocol_version=str(row["protocol_version"]),
            revision_of_candidate_id=(
                int(row["revision_of_candidate_id"])
                if row["revision_of_candidate_id"] is not None
                else None
            ),
            revision_reason=(
                str(row["revision_reason"]) if row["revision_reason"] is not None else None
            ),
            trusted_staging_proof=(
                str(row["trusted_staging_proof"])
                if row["trusted_staging_proof"] is not None
                else None
            ),
        )

    @staticmethod
    def _candidate_provenance_from_row(row: sqlite3.Row) -> CandidateProvenance:
        return CandidateProvenance(
            id=int(row["id"]),
            candidate_id=int(row["candidate_id"]),
            proof_marker=str(row["proof_marker"]),
            origin=str(row["origin"]),
            source_slugs=_decode_source_slugs(str(row["source_slugs"])),
            observed_at=str(row["observed_at"]),
            last_supported_at=str(row["last_supported_at"]),
            evidence_refs=_decode_evidence_refs(str(row["evidence_refs"])),
            protocol_name=str(row["protocol_name"]),
            protocol_version=str(row["protocol_version"]),
            created_at=str(row["created_at"]),
        )

    def create_fragment(
        self, fragment: MemoryFragment, *, _promotion_token: object | None = None
    ) -> MemoryFragment:
        if _promotion_token is not _FORMAL_WRITE_TOKEN:
            raise DirectFragmentWriteError(
                "formal fragments can only be written through candidate promotion"
            )
        statement = _bounded_text(fragment.statement, "statement", MAX_STATEMENT_LENGTH)
        topic = _bounded_text(fragment.topic, "topic", MAX_TOPIC_LENGTH, allow_empty=True)
        source_slugs = _encode_source_slugs(fragment.source_slugs)
        with self._write_context():
            cursor = self.connection.execute(
                """
                INSERT INTO memory_fragments
                    (type, statement, topic, confidence, status, valid_from,
                     last_supported_at, source_slugs, created_at, updated_at,
                     temporal_scope, observed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fragment.type,
                    statement,
                    topic,
                    fragment.confidence,
                    fragment.status,
                    fragment.valid_from,
                    fragment.last_supported_at,
                    source_slugs,
                    fragment.created_at,
                    fragment.updated_at,
                    fragment.temporal_scope,
                    fragment.observed_at,
                ),
            )
        return self.get_fragment(int(cursor.lastrowid))  # type: ignore[return-value]

    def get_fragment(self, fragment_id: int) -> Optional[MemoryFragment]:
        cursor = self.connection.execute(
            "SELECT * FROM memory_fragments WHERE id = ?", (self._ensure_id(fragment_id),)
        )
        row = self._row_or_none(cursor)
        return self._fragment_from_row(row) if row is not None else None

    def list_fragments(
        self,
        *,
        fragment_type: Optional[str] = None,
        status: Optional[str] = None,
        topic: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[MemoryFragment]:
        clauses: List[str] = []
        params: List[Any] = []
        if fragment_type is not None:
            clauses.append("type = ?")
            params.append(fragment_type)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if topic is not None:
            clauses.append("topic = ?")
            params.append(topic)
        query = "SELECT * FROM memory_fragments"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC, id DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        rows = self.connection.execute(query, params).fetchall()
        return [self._fragment_from_row(row) for row in rows]

    def find_fragment_by_identity(
        self, source_slugs: SourceSlugs, statement: str
    ) -> Optional[MemoryFragment]:
        """Find one active/unresolved formal memory for a source-backed proposition."""

        identity = candidate_semantic_key(source_slugs, statement)
        rows = self.connection.execute(
            "SELECT * FROM memory_fragments "
            "WHERE status IN ('active', 'unresolved') "
            "ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, updated_at DESC, id DESC"
        ).fetchall()
        for row in rows:
            fragment = self._fragment_from_row(row)
            if candidate_semantic_key(fragment.source_slugs, fragment.statement) == identity:
                return fragment
        return None

    def update_fragment(
        self, fragment: MemoryFragment, *, _promotion_token: object | None = None
    ) -> Optional[MemoryFragment]:
        if _promotion_token is not _FORMAL_WRITE_TOKEN:
            raise DirectFragmentWriteError(
                "formal fragments can only be updated through candidate promotion"
            )
        fragment_id = self._ensure_id(fragment.id)
        statement = _bounded_text(fragment.statement, "statement", MAX_STATEMENT_LENGTH)
        topic = _bounded_text(fragment.topic, "topic", MAX_TOPIC_LENGTH, allow_empty=True)
        source_slugs = _encode_source_slugs(fragment.source_slugs)
        with self._write_context():
            self.connection.execute(
                """
                UPDATE memory_fragments
                SET type = ?, statement = ?, topic = ?, confidence = ?, status = ?,
                    valid_from = ?, last_supported_at = ?, source_slugs = ?,
                    updated_at = ?, temporal_scope = ?, observed_at = ?
                WHERE id = ?
                """,
                (
                    fragment.type,
                    statement,
                    topic,
                    fragment.confidence,
                    fragment.status,
                    fragment.valid_from,
                    fragment.last_supported_at,
                    source_slugs,
                    fragment.updated_at,
                    fragment.temporal_scope,
                    fragment.observed_at,
                    fragment_id,
                ),
            )
        return self.get_fragment(fragment_id)

    def list_active_fragments_without_lineage(self) -> List[MemoryFragment]:
        """Return active rows that fail the formal promotion invariant."""

        rows = self.connection.execute(
            "SELECT f.* FROM memory_fragments AS f "
            "LEFT JOIN promotion_lineage AS p ON p.promoted_fragment_id = f.id "
            "WHERE f.status = 'active' AND p.id IS NULL "
            "ORDER BY f.id"
        ).fetchall()
        return [self._fragment_from_row(row) for row in rows]

    def delete_fragment(self, fragment_id: int) -> bool:
        with self._write_context():
            cursor = self.connection.execute(
                "DELETE FROM memory_fragments WHERE id = ?", (self._ensure_id(fragment_id),)
            )
        return cursor.rowcount > 0

    def upsert_profile(self, profile: UserProfileEntry) -> UserProfileEntry:
        value = _bounded_text(profile.value, "value", MAX_STATEMENT_LENGTH)
        topic = _bounded_text(profile.topic, "topic", MAX_TOPIC_LENGTH, allow_empty=True)
        source_slugs = _encode_source_slugs(profile.source_slugs)
        with self._write_context():
            self.connection.execute(
                """
                INSERT INTO user_profile
                    (attribute, value, topic, confidence, status, source_slugs,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(attribute) DO UPDATE SET
                    value = excluded.value,
                    topic = excluded.topic,
                    confidence = excluded.confidence,
                    status = excluded.status,
                    source_slugs = excluded.source_slugs,
                    updated_at = excluded.updated_at
                """,
                (
                    profile.attribute,
                    value,
                    topic,
                    profile.confidence,
                    profile.status,
                    source_slugs,
                    profile.created_at,
                    profile.updated_at,
                ),
            )
        return self.get_profile(profile.attribute)  # type: ignore[return-value]

    def get_profile(self, attribute: str) -> Optional[UserProfileEntry]:
        cursor = self.connection.execute(
            "SELECT * FROM user_profile WHERE attribute = ?", (attribute,)
        )
        row = self._row_or_none(cursor)
        return self._profile_from_row(row) if row is not None else None

    def list_profiles(
        self, *, status: Optional[str] = None, limit: Optional[int] = None
    ) -> List[UserProfileEntry]:
        query = "SELECT * FROM user_profile"
        params: List[Any] = []
        if status is not None:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY updated_at DESC, id DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        rows = self.connection.execute(query, params).fetchall()
        return [self._profile_from_row(row) for row in rows]

    def delete_profile(self, attribute: str) -> bool:
        with self._write_context():
            cursor = self.connection.execute(
                "DELETE FROM user_profile WHERE attribute = ?", (attribute,)
            )
        return cursor.rowcount > 0

    def create_current_memory(self, item: CurrentMemoryItem) -> CurrentMemoryItem:
        statement = _bounded_text(item.statement, "statement", MAX_STATEMENT_LENGTH)
        topic = _bounded_text(item.topic, "topic", MAX_TOPIC_LENGTH, allow_empty=True)
        source_slugs = _encode_source_slugs(item.source_slugs)
        with self._write_context():
            cursor = self.connection.execute(
                """
                INSERT INTO current_memory
                    (type, statement, topic, status, priority, source_slugs,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.type,
                    statement,
                    topic,
                    item.status,
                    item.priority,
                    source_slugs,
                    item.created_at,
                    item.updated_at,
                ),
            )
        return self.get_current_memory(int(cursor.lastrowid))  # type: ignore[return-value]

    def get_current_memory(self, item_id: int) -> Optional[CurrentMemoryItem]:
        cursor = self.connection.execute(
            "SELECT * FROM current_memory WHERE id = ?", (self._ensure_id(item_id),)
        )
        row = self._row_or_none(cursor)
        return self._current_memory_from_row(row) if row is not None else None

    def list_current_memory(
        self,
        *,
        item_type: Optional[str] = None,
        status: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[CurrentMemoryItem]:
        clauses: List[str] = []
        params: List[Any] = []
        if item_type is not None:
            clauses.append("type = ?")
            params.append(item_type)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        query = "SELECT * FROM current_memory"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY priority DESC, updated_at DESC, id DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        rows = self.connection.execute(query, params).fetchall()
        return [self._current_memory_from_row(row) for row in rows]

    def update_current_memory(self, item: CurrentMemoryItem) -> Optional[CurrentMemoryItem]:
        item_id = self._ensure_id(item.id)
        statement = _bounded_text(item.statement, "statement", MAX_STATEMENT_LENGTH)
        topic = _bounded_text(item.topic, "topic", MAX_TOPIC_LENGTH, allow_empty=True)
        source_slugs = _encode_source_slugs(item.source_slugs)
        with self._write_context():
            self.connection.execute(
                """
                UPDATE current_memory
                SET type = ?, statement = ?, topic = ?, status = ?, priority = ?,
                    source_slugs = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    item.type,
                    statement,
                    topic,
                    item.status,
                    item.priority,
                    source_slugs,
                    item.updated_at,
                    item_id,
                ),
            )
        return self.get_current_memory(item_id)

    def delete_current_memory(self, item_id: int) -> bool:
        with self._write_context():
            cursor = self.connection.execute(
                "DELETE FROM current_memory WHERE id = ?", (self._ensure_id(item_id),)
            )
        return cursor.rowcount > 0

    def create_open_loop(self, loop: OpenLoop) -> OpenLoop:
        statement = _bounded_text(loop.statement, "statement", MAX_STATEMENT_LENGTH)
        topic = _bounded_text(loop.topic, "topic", MAX_TOPIC_LENGTH, allow_empty=True)
        source_slugs = _encode_source_slugs(loop.source_slugs)
        with self._write_context():
            cursor = self.connection.execute(
                """
                INSERT INTO open_loops
                    (statement, topic, status, source_slugs, last_reviewed_at,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    statement,
                    topic,
                    loop.status,
                    source_slugs,
                    loop.last_reviewed_at,
                    loop.created_at,
                    loop.updated_at,
                ),
            )
        return self.get_open_loop(int(cursor.lastrowid))  # type: ignore[return-value]

    def get_open_loop(self, loop_id: int) -> Optional[OpenLoop]:
        cursor = self.connection.execute(
            "SELECT * FROM open_loops WHERE id = ?", (self._ensure_id(loop_id),)
        )
        row = self._row_or_none(cursor)
        return self._open_loop_from_row(row) if row is not None else None

    def list_open_loops(
        self,
        *,
        status: Optional[str] = None,
        topic: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[OpenLoop]:
        clauses: List[str] = []
        params: List[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if topic is not None:
            clauses.append("topic = ?")
            params.append(topic)
        query = "SELECT * FROM open_loops"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC, id DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        rows = self.connection.execute(query, params).fetchall()
        return [self._open_loop_from_row(row) for row in rows]

    def update_open_loop(self, loop: OpenLoop) -> Optional[OpenLoop]:
        loop_id = self._ensure_id(loop.id)
        statement = _bounded_text(loop.statement, "statement", MAX_STATEMENT_LENGTH)
        topic = _bounded_text(loop.topic, "topic", MAX_TOPIC_LENGTH, allow_empty=True)
        source_slugs = _encode_source_slugs(loop.source_slugs)
        with self._write_context():
            self.connection.execute(
                """
                UPDATE open_loops
                SET statement = ?, topic = ?, status = ?, source_slugs = ?,
                    last_reviewed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    statement,
                    topic,
                    loop.status,
                    source_slugs,
                    loop.last_reviewed_at,
                    loop.updated_at,
                    loop_id,
                ),
            )
        return self.get_open_loop(loop_id)

    def delete_open_loop(self, loop_id: int) -> bool:
        with self._write_context():
            cursor = self.connection.execute(
                "DELETE FROM open_loops WHERE id = ?", (self._ensure_id(loop_id),)
            )
        return cursor.rowcount > 0

    def create_candidate(self, candidate: MemoryCandidate) -> MemoryCandidate:
        """Create a staging row without issuing trusted provenance."""

        if candidate.trusted_staging_proof is not None:
            raise ValueError("trusted staging proof can only be issued by trusted staging")
        return self._create_candidate_row(candidate, provenance=None)

    def _create_candidate_from_trusted_staging(
        self,
        candidate: MemoryCandidate,
        *,
        source_slugs: SourceSlugs,
        observed_at: str,
        last_supported_at: str,
        evidence_refs: EvidenceRefs,
        _staging_token: object | None = None,
    ) -> MemoryCandidate:
        """Insert a candidate and its body-free source proof atomically."""

        if _staging_token is not _STAGING_WRITE_TOKEN:
            raise PermissionError("only trusted staging may issue candidate provenance")
        if candidate.trusted_staging_proof is not None:
            raise ValueError("staging proof is repository-issued")
        if set(source_slugs) != set(candidate.source_slugs):
            raise ValueError("trusted source slugs do not match candidate source slugs")
        if observed_at != candidate.observed_at or last_supported_at != candidate.last_supported_at:
            raise ValueError("trusted source timestamps do not match candidate timestamps")
        if tuple(evidence_refs) != tuple(candidate.evidence_refs):
            raise ValueError("trusted evidence refs do not match candidate evidence refs")

        proof_marker = uuid.uuid4().hex
        return self._create_candidate_row(
            replace(candidate, trusted_staging_proof=proof_marker),
            provenance=(
                source_slugs,
                observed_at,
                last_supported_at,
                evidence_refs,
                proof_marker,
            ),
        )

    def _create_candidate_row(
        self,
        candidate: MemoryCandidate,
        *,
        provenance: tuple[SourceSlugs, str, str, EvidenceRefs, str] | None,
    ) -> MemoryCandidate:
        statement = _bounded_text(candidate.statement, "statement", MAX_STATEMENT_LENGTH)
        topic = _bounded_text(candidate.topic, "topic", MAX_TOPIC_LENGTH, allow_empty=True)
        review_note = (
            _bounded_text(candidate.review_note, "review_note", MAX_REVIEW_NOTE_LENGTH)
            if candidate.review_note is not None
            else None
        )
        source_slugs = _encode_source_slugs(candidate.source_slugs)
        evidence_refs = _encode_evidence_refs(candidate.evidence_refs)
        protocol_name = _bounded_text(candidate.protocol_name, "protocol_name", 100)
        protocol_version = _bounded_text(candidate.protocol_version, "protocol_version", 50)
        trusted_staging_proof = (
            _bounded_text(
                candidate.trusted_staging_proof,
                "trusted_staging_proof",
                MAX_TRUSTED_STAGING_PROOF_LENGTH,
            )
            if candidate.trusted_staging_proof is not None
            else None
        )
        if provenance is not None and trusted_staging_proof is None:
            raise ValueError("trusted staging provenance requires a proof marker")
        revision_reason = (
            _bounded_text(
                candidate.revision_reason,
                "revision_reason",
                MAX_REVISION_REASON_LENGTH,
            )
            if candidate.revision_reason is not None
            else None
        )
        revision_of_candidate_id = (
            self._ensure_id(candidate.revision_of_candidate_id)
            if candidate.revision_of_candidate_id is not None
            else None
        )
        if candidate.id is not None and revision_of_candidate_id == candidate.id:
            raise ValueError("candidate cannot revise itself")
        dedupe_key = candidate_dedupe_key(
            candidate.source_slugs,
            statement,
            protocol_name=protocol_name,
            protocol_version=protocol_version,
        )
        if candidate.dedupe_key and candidate.dedupe_key != dedupe_key:
            raise ValueError("dedupe_key does not match candidate content")
        try:
            with self._write_context():
                cursor = self.connection.execute(
                    """
                    INSERT INTO memory_candidates
                        (type, statement, topic, confidence, source_slugs, evidence_kind,
                         review_status, proposed_action, temporal_scope, observed_at,
                         last_supported_at, evidence_refs, dedupe_key, protocol_name,
                         protocol_version, revision_of_candidate_id, revision_reason,
                         trusted_staging_proof, review_note, reviewed_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate.type,
                        statement,
                        topic,
                        candidate.confidence,
                        source_slugs,
                        candidate.evidence_kind,
                        candidate.review_status,
                        candidate.proposed_action,
                        candidate.temporal_scope,
                        candidate.observed_at,
                        candidate.last_supported_at,
                        evidence_refs,
                        dedupe_key,
                        protocol_name,
                        protocol_version,
                        revision_of_candidate_id,
                        revision_reason,
                        trusted_staging_proof,
                        review_note,
                        candidate.reviewed_at,
                        candidate.created_at,
                        candidate.updated_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            if "dedupe" not in str(exc).lower():
                raise
            existing = self.get_candidate_by_dedupe_key(dedupe_key)
            if existing is None:
                raise
            if self._candidate_classification(existing) != self._candidate_classification(
                candidate
            ):
                raise CandidateIdentityConflictError(
                    "candidate identity already exists with a different classification; "
                    "use an explicit review edit or protocol revision"
                )
            return existing

        candidate_id = int(cursor.lastrowid)
        if provenance is not None:
            (
                trusted_sources,
                trusted_observed_at,
                trusted_last_supported_at,
                trusted_refs,
                proof_marker,
            ) = provenance
            with self._write_context():
                self.connection.execute(
                    """
                    INSERT INTO candidate_provenance
                        (candidate_id, proof_marker, origin, source_slugs, observed_at,
                         last_supported_at, evidence_refs, protocol_name, protocol_version,
                         created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate_id,
                        proof_marker,
                        _STAGING_ORIGIN,
                        _encode_source_slugs(trusted_sources),
                        trusted_observed_at,
                        trusted_last_supported_at,
                        _encode_evidence_refs(trusted_refs),
                        protocol_name,
                        protocol_version,
                        candidate.created_at,
                    ),
                )
        return self.get_candidate(candidate_id)  # type: ignore[return-value]

    def get_candidate(self, candidate_id: int) -> Optional[MemoryCandidate]:
        cursor = self.connection.execute(
            "SELECT * FROM memory_candidates WHERE id = ?", (self._ensure_id(candidate_id),)
        )
        row = self._row_or_none(cursor)
        return self._candidate_from_row(row) if row is not None else None

    def get_candidate_provenance(self, candidate_id: int) -> Optional[CandidateProvenance]:
        cursor = self.connection.execute(
            "SELECT * FROM candidate_provenance WHERE candidate_id = ?",
            (self._ensure_id(candidate_id),),
        )
        row = self._row_or_none(cursor)
        return self._candidate_provenance_from_row(row) if row is not None else None

    @staticmethod
    def _provenance_bound_fields(
        candidate: MemoryCandidate,
    ) -> tuple[frozenset[str], str, str, EvidenceRefs, str, str]:
        return (
            frozenset(candidate.source_slugs),
            candidate.observed_at,
            candidate.last_supported_at,
            candidate.evidence_refs,
            candidate.protocol_name,
            candidate.protocol_version,
        )

    def has_valid_candidate_provenance(self, candidate: MemoryCandidate) -> bool:
        """Validate the persisted staging proof against the current candidate row."""

        if candidate.id is None or not candidate.trusted_staging_proof:
            return False
        provenance = self.get_candidate_provenance(candidate.id)
        if provenance is None or provenance.origin != _STAGING_ORIGIN:
            return False
        if provenance.proof_marker != candidate.trusted_staging_proof:
            return False
        if frozenset(provenance.source_slugs) != frozenset(candidate.source_slugs):
            return False
        if provenance.observed_at != candidate.observed_at:
            return False
        if provenance.last_supported_at != candidate.last_supported_at:
            return False
        if provenance.evidence_refs != candidate.evidence_refs:
            return False
        if provenance.protocol_name != candidate.protocol_name:
            return False
        if provenance.protocol_version != candidate.protocol_version:
            return False
        return True

    def get_candidate_by_dedupe_key(self, dedupe_key: str) -> Optional[MemoryCandidate]:
        key = _bounded_text(dedupe_key, "dedupe_key", 100)
        cursor = self.connection.execute(
            "SELECT * FROM memory_candidates WHERE dedupe_key = ?", (key,)
        )
        row = self._row_or_none(cursor)
        return self._candidate_from_row(row) if row is not None else None

    @staticmethod
    def _candidate_classification(candidate: MemoryCandidate) -> tuple[str, str, str, str]:
        return (
            candidate.type,
            candidate.topic,
            candidate.evidence_kind,
            candidate.temporal_scope,
        )

    def list_candidates_by_semantic_identity(
        self, source_slugs: SourceSlugs, statement: str
    ) -> List[MemoryCandidate]:
        """List all protocol revisions for one source-backed proposition."""

        identity = candidate_semantic_key(source_slugs, statement)
        rows = self.connection.execute(
            "SELECT * FROM memory_candidates ORDER BY id DESC"
        ).fetchall()
        matches = []
        for row in rows:
            candidate = self._candidate_from_row(row)
            if candidate_semantic_key(candidate.source_slugs, candidate.statement) == identity:
                matches.append(candidate)
        return matches

    def list_candidates(
        self,
        *,
        review_status: Optional[str] = None,
        candidate_type: Optional[str] = None,
        proposed_action: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[MemoryCandidate]:
        clauses: List[str] = []
        params: List[Any] = []
        if review_status is not None:
            clauses.append("review_status = ?")
            params.append(review_status)
        if candidate_type is not None:
            clauses.append("type = ?")
            params.append(candidate_type)
        if proposed_action is not None:
            clauses.append("proposed_action = ?")
            params.append(proposed_action)
        query = "SELECT * FROM memory_candidates"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC, id DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        rows = self.connection.execute(query, params).fetchall()
        return [self._candidate_from_row(row) for row in rows]

    def update_candidate(
        self,
        candidate: MemoryCandidate,
        *,
        _provenance_token: object | None = None,
    ) -> Optional[MemoryCandidate]:
        candidate_id = self._ensure_id(candidate.id)
        existing = self.get_candidate(candidate_id)
        if existing is None:
            return None
        if candidate.trusted_staging_proof != existing.trusted_staging_proof:
            raise ValueError("trusted staging proof is immutable")
        provenance_changed = (
            existing.trusted_staging_proof is not None
            and self._provenance_bound_fields(candidate)
            != self._provenance_bound_fields(existing)
        )
        if provenance_changed and _provenance_token is not _STAGING_WRITE_TOKEN:
            raise PermissionError(
                "trusted source provenance can only be refreshed by the review path"
            )
        statement = _bounded_text(candidate.statement, "statement", MAX_STATEMENT_LENGTH)
        topic = _bounded_text(candidate.topic, "topic", MAX_TOPIC_LENGTH, allow_empty=True)
        review_note = (
            _bounded_text(candidate.review_note, "review_note", MAX_REVIEW_NOTE_LENGTH)
            if candidate.review_note is not None
            else None
        )
        source_slugs = _encode_source_slugs(candidate.source_slugs)
        evidence_refs = _encode_evidence_refs(candidate.evidence_refs)
        protocol_name = _bounded_text(candidate.protocol_name, "protocol_name", 100)
        protocol_version = _bounded_text(candidate.protocol_version, "protocol_version", 50)
        revision_reason = (
            _bounded_text(
                candidate.revision_reason,
                "revision_reason",
                MAX_REVISION_REASON_LENGTH,
            )
            if candidate.revision_reason is not None
            else None
        )
        revision_of_candidate_id = (
            self._ensure_id(candidate.revision_of_candidate_id)
            if candidate.revision_of_candidate_id is not None
            else None
        )
        if revision_of_candidate_id == candidate_id:
            raise ValueError("candidate cannot revise itself")
        dedupe_key = candidate_dedupe_key(
            candidate.source_slugs,
            statement,
            protocol_name=protocol_name,
            protocol_version=protocol_version,
        )
        if candidate.dedupe_key and candidate.dedupe_key != dedupe_key:
            raise ValueError("dedupe_key does not match candidate content")
        with self._write_context():
            self.connection.execute(
                """
                UPDATE memory_candidates
                    SET type = ?, statement = ?, topic = ?, confidence = ?, source_slugs = ?,
                    evidence_kind = ?, review_status = ?, proposed_action = ?,
                    temporal_scope = ?, observed_at = ?, last_supported_at = ?,
                    evidence_refs = ?, dedupe_key = ?, protocol_name = ?,
                    protocol_version = ?, revision_of_candidate_id = ?, revision_reason = ?,
                    trusted_staging_proof = ?, review_note = ?, reviewed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    candidate.type,
                    statement,
                    topic,
                    candidate.confidence,
                    source_slugs,
                    candidate.evidence_kind,
                    candidate.review_status,
                    candidate.proposed_action,
                    candidate.temporal_scope,
                    candidate.observed_at,
                    candidate.last_supported_at,
                    evidence_refs,
                    dedupe_key,
                    protocol_name,
                    protocol_version,
                    (
                        revision_of_candidate_id
                    ),
                    revision_reason,
                    candidate.trusted_staging_proof,
                    review_note,
                    candidate.reviewed_at,
                    candidate.updated_at,
                    candidate_id,
                ),
            )
            if provenance_changed:
                provenance = self.get_candidate_provenance(candidate_id)
                if provenance is None:
                    raise PermissionError("candidate proof record is missing")
                self.connection.execute(
                    """
                    UPDATE candidate_provenance
                    SET source_slugs = ?, observed_at = ?, last_supported_at = ?,
                        evidence_refs = ?, protocol_name = ?, protocol_version = ?
                    WHERE candidate_id = ?
                    """,
                    (
                        source_slugs,
                        candidate.observed_at,
                        candidate.last_supported_at,
                        evidence_refs,
                        protocol_name,
                        protocol_version,
                        candidate_id,
                    ),
                )
        return self.get_candidate(candidate_id)

    def delete_candidate(self, candidate_id: int) -> bool:
        with self._write_context():
            cursor = self.connection.execute(
                "DELETE FROM memory_candidates WHERE id = ?", (self._ensure_id(candidate_id),)
            )
        return cursor.rowcount > 0

    def create_promotion_lineage(
        self,
        lineage: PromotionLineage,
        *,
        _review_token: object | None = None,
    ) -> PromotionLineage:
        if _review_token is not _FORMAL_WRITE_TOKEN:
            raise PermissionError("promotion lineage can only be written by the review path")
        if lineage.action not in {"accept", "supersede"}:
            raise ValueError("invalid promotion lineage action")
        if lineage.action == "accept" and lineage.superseded_fragment_id is not None:
            raise ValueError("accept lineage cannot reference a superseded fragment")
        if lineage.action == "supersede" and lineage.superseded_fragment_id is None:
            raise ValueError("supersede lineage requires a superseded fragment")
        candidate = self.get_candidate(lineage.candidate_id)
        expected_status = {
            "accept": "accepted",
            "supersede": "superseded",
        }[lineage.action]
        if (
            candidate is None
            or candidate.review_status != expected_status
            or not self.has_valid_candidate_provenance(candidate)
        ):
            raise PermissionError(
                "promotion lineage requires a reviewed candidate with trusted provenance"
            )
        protocol_name = _bounded_text(lineage.protocol_name, "protocol_name", 100)
        protocol_version = _bounded_text(lineage.protocol_version, "protocol_version", 50)
        with self._write_context():
            cursor = self.connection.execute(
                """
                INSERT INTO promotion_lineage
                    (candidate_id, promoted_fragment_id, action, superseded_fragment_id,
                     protocol_name, protocol_version, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._ensure_id(lineage.candidate_id),
                    self._ensure_id(lineage.promoted_fragment_id),
                    lineage.action,
                    lineage.superseded_fragment_id,
                    protocol_name,
                    protocol_version,
                    lineage.created_at,
                ),
            )
        return self.get_promotion_lineage(int(cursor.lastrowid))  # type: ignore[return-value]

    @staticmethod
    def _promotion_lineage_from_row(row: sqlite3.Row) -> PromotionLineage:
        return PromotionLineage(
            id=int(row["id"]),
            candidate_id=int(row["candidate_id"]),
            promoted_fragment_id=int(row["promoted_fragment_id"]),
            action=str(row["action"]),
            superseded_fragment_id=(
                int(row["superseded_fragment_id"])
                if row["superseded_fragment_id"] is not None
                else None
            ),
            protocol_name=str(row["protocol_name"]),
            protocol_version=str(row["protocol_version"]),
            created_at=str(row["created_at"]),
        )

    def get_promotion_lineage(self, lineage_id: int) -> Optional[PromotionLineage]:
        cursor = self.connection.execute(
            "SELECT * FROM promotion_lineage WHERE id = ?", (self._ensure_id(lineage_id),)
        )
        row = self._row_or_none(cursor)
        return self._promotion_lineage_from_row(row) if row is not None else None

    def get_promotion_for_candidate(self, candidate_id: int) -> Optional[PromotionLineage]:
        cursor = self.connection.execute(
            "SELECT * FROM promotion_lineage WHERE candidate_id = ?",
            (self._ensure_id(candidate_id),),
        )
        row = self._row_or_none(cursor)
        return self._promotion_lineage_from_row(row) if row is not None else None

    def list_promotion_lineage(self, *, limit: Optional[int] = None) -> List[PromotionLineage]:
        query = "SELECT * FROM promotion_lineage ORDER BY created_at DESC, id DESC"
        params: List[Any] = []
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        rows = self.connection.execute(query, params).fetchall()
        return [self._promotion_lineage_from_row(row) for row in rows]
