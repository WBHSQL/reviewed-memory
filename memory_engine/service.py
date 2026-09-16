"""Validation and use-case service for derived memory CRUD."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Iterable, Optional

from .constants import (
    CURRENT_STATE_MARKERS,
    CURRENT_MEMORY_STATUSES,
    CURRENT_MEMORY_TYPES,
    FRAGMENT_STATUSES,
    FRAGMENT_TYPES,
    MAX_SOURCE_SLUG_LENGTH,
    MAX_STATEMENT_LENGTH,
    MAX_TOPIC_LENGTH,
    OPEN_LOOP_STATUSES,
    TEMPORAL_SCOPES,
)
from .models import CurrentMemoryItem, MemoryFragment, OpenLoop, SourceSlugs, UserProfileEntry
from .repository import MemoryRepository, _FORMAL_WRITE_TOKEN


class MemoryValidationError(ValueError):
    """Raised when a derived-memory record is incomplete or invalid."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(
    value: str,
    field: str,
    *,
    allow_empty: bool = False,
    limit: Optional[int] = None,
) -> str:
    if not isinstance(value, str):
        raise MemoryValidationError(f"{field} must be text")
    normalized = value.strip()
    if not allow_empty and not normalized:
        raise MemoryValidationError(f"{field} is required")
    if limit is not None and len(normalized) > limit:
        raise MemoryValidationError(f"{field} exceeds the maximum length")
    return normalized


def _confidence(value: float) -> float:
    if isinstance(value, bool):
        raise MemoryValidationError("confidence must be between 0 and 1")
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise MemoryValidationError("confidence must be between 0 and 1") from exc
    if not math.isfinite(normalized) or not 0 <= normalized <= 1:
        raise MemoryValidationError("confidence must be between 0 and 1")
    return normalized


def _choice(value: str, field: str, choices: frozenset[str]) -> str:
    normalized = _text(value, field)
    if normalized not in choices:
        raise MemoryValidationError(f"invalid {field}")
    return normalized


def _source_slugs(values: Iterable[str]) -> SourceSlugs:
    if isinstance(values, str):
        raise MemoryValidationError("source_slugs must be a sequence of slugs")
    normalized = []
    for value in values:
        slug = _text(value, "source slug")
        if len(slug) > MAX_SOURCE_SLUG_LENGTH:
            raise MemoryValidationError("source slug exceeds the maximum length")
        if slug not in normalized:
            normalized.append(slug)
    if not normalized:
        raise MemoryValidationError("at least one source slug is required")
    return tuple(normalized)


def _optional_timestamp(value: Optional[str], field: str) -> Optional[str]:
    if value is None:
        return None
    return _text(value, field)


def _temporal_scope(value: Optional[str], fragment_type: str, statement: str) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        normalized = {
            "project": "current",
            "goal": "current",
            "experience": "episodic",
            "fact": "episodic",
            "decision": "episodic",
            "belief": "current",
            "preference": "current",
            "value": "current",
            "pattern": "current",
        }.get(fragment_type, "episodic")
    else:
        normalized = _choice(value, "temporal_scope", TEMPORAL_SCOPES)
    if (
        fragment_type == "fact"
        and normalized == "current"
        and not any(marker.casefold() in statement.casefold() for marker in CURRENT_STATE_MARKERS)
    ):
        raise MemoryValidationError(
            "a fact may be current only when its statement explicitly describes an ongoing state"
        )
    return normalized


class MemoryService:
    """Application service that creates only structured derived memories."""

    def __init__(self, repository: MemoryRepository) -> None:
        self.repository = repository

    def create_fragment(
        self,
        *,
        fragment_type: str,
        statement: str,
        topic: str = "",
        confidence: float = 0.5,
        status: str = "active",
        source_slugs: Iterable[str],
        valid_from: Optional[str] = None,
        last_supported_at: Optional[str] = None,
        temporal_scope: Optional[str] = None,
        observed_at: Optional[str] = None,
        _promotion_token: object | None = None,
    ) -> MemoryFragment:
        if _promotion_token is not _FORMAL_WRITE_TOKEN:
            raise MemoryValidationError(
                "formal fragments can only be created through candidate review promotion"
            )
        created_at = _now()
        normalized_type = _choice(fragment_type, "fragment_type", FRAGMENT_TYPES)
        normalized_sources = _source_slugs(source_slugs)
        normalized_statement = _text(statement, "statement", limit=MAX_STATEMENT_LENGTH)
        normalized_scope = _temporal_scope(
            temporal_scope, normalized_type, normalized_statement
        )
        if normalized_type == "pattern" and len(normalized_sources) < 3:
            raise MemoryValidationError(
                "a formal pattern requires at least three independent sources"
            )
        if normalized_type in {"belief", "value"} and normalized_scope == "stable":
            if len(normalized_sources) < 2:
                raise MemoryValidationError(
                    "a single belief or value source cannot be marked stable"
                )
        if normalized_type == "experience" and normalized_scope == "stable":
            raise MemoryValidationError("experience fragments cannot be marked stable")
        normalized_valid_from = _optional_timestamp(valid_from, "valid_from") or created_at
        normalized_observed_at = (
            _optional_timestamp(observed_at, "observed_at") or normalized_valid_from
        )
        normalized_last_supported_at = (
            _optional_timestamp(last_supported_at, "last_supported_at") or normalized_observed_at
        )
        return self.repository.create_fragment(
            MemoryFragment(
                id=None,
                type=normalized_type,
                statement=normalized_statement,
                topic=_text(topic, "topic", allow_empty=True, limit=MAX_TOPIC_LENGTH),
                confidence=_confidence(confidence),
                status=_choice(status, "status", FRAGMENT_STATUSES),
                valid_from=normalized_valid_from,
                last_supported_at=normalized_last_supported_at,
                source_slugs=normalized_sources,
                created_at=created_at,
                updated_at=created_at,
                temporal_scope=normalized_scope,
                observed_at=normalized_observed_at,
            ),
            _promotion_token=_promotion_token,
        )

    def get_fragment(self, fragment_id: int) -> Optional[MemoryFragment]:
        return self.repository.get_fragment(fragment_id)

    def list_fragments(
        self,
        *,
        fragment_type: Optional[str] = None,
        status: Optional[str] = None,
        topic: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[MemoryFragment]:
        if fragment_type is not None:
            fragment_type = _choice(fragment_type, "fragment_type", FRAGMENT_TYPES)
        if status is not None:
            status = _choice(status, "status", FRAGMENT_STATUSES)
        if topic is not None:
            topic = _text(topic, "topic", allow_empty=True)
        return self.repository.list_fragments(
            fragment_type=fragment_type, status=status, topic=topic, limit=_limit(limit)
        )

    def update_fragment(
        self,
        fragment: MemoryFragment,
        *,
        _promotion_token: object | None = None,
    ) -> Optional[MemoryFragment]:
        if _promotion_token is not _FORMAL_WRITE_TOKEN:
            raise MemoryValidationError(
                "formal fragments can only be updated through candidate review promotion"
            )
        normalized_type = _choice(fragment.type, "fragment_type", FRAGMENT_TYPES)
        normalized_sources = _source_slugs(fragment.source_slugs)
        normalized_statement = _text(
            fragment.statement, "statement", limit=MAX_STATEMENT_LENGTH
        )
        normalized_scope = _temporal_scope(
            fragment.temporal_scope, normalized_type, normalized_statement
        )
        if normalized_type == "pattern" and len(normalized_sources) < 3:
            raise MemoryValidationError(
                "a formal pattern requires at least three independent sources"
            )
        if normalized_type in {"belief", "value"} and normalized_scope == "stable":
            if len(normalized_sources) < 2:
                raise MemoryValidationError(
                    "a single belief or value source cannot be marked stable"
                )
        if normalized_type == "experience" and normalized_scope == "stable":
            raise MemoryValidationError("experience fragments cannot be marked stable")
        normalized_observed_at = _text(
            fragment.observed_at or fragment.valid_from, "observed_at"
        )
        updated = MemoryFragment(
            id=fragment.id,
            type=normalized_type,
            statement=normalized_statement,
            topic=_text(fragment.topic, "topic", allow_empty=True, limit=MAX_TOPIC_LENGTH),
            confidence=_confidence(fragment.confidence),
            status=_choice(fragment.status, "status", FRAGMENT_STATUSES),
            valid_from=_text(fragment.valid_from, "valid_from"),
            last_supported_at=_text(fragment.last_supported_at, "last_supported_at"),
            source_slugs=normalized_sources,
            created_at=_text(fragment.created_at, "created_at"),
            updated_at=_now(),
            temporal_scope=normalized_scope,
            observed_at=normalized_observed_at,
        )
        return self.repository.update_fragment(
            updated, _promotion_token=_promotion_token
        )

    def delete_fragment(self, fragment_id: int) -> bool:
        return self.repository.delete_fragment(fragment_id)

    def upsert_profile(
        self,
        *,
        attribute: str,
        value: str,
        topic: str = "",
        confidence: float = 0.5,
        status: str = "active",
        source_slugs: Iterable[str],
    ) -> UserProfileEntry:
        now = _now()
        existing = self.repository.get_profile(_text(attribute, "attribute"))
        return self.repository.upsert_profile(
            UserProfileEntry(
                id=existing.id if existing else None,
                attribute=_text(attribute, "attribute"),
                value=_text(value, "value", limit=MAX_STATEMENT_LENGTH),
                topic=_text(topic, "topic", allow_empty=True, limit=MAX_TOPIC_LENGTH),
                confidence=_confidence(confidence),
                status=_choice(status, "status", FRAGMENT_STATUSES),
                source_slugs=_source_slugs(source_slugs),
                created_at=existing.created_at if existing else now,
                updated_at=now,
            )
        )

    def get_profile(self, attribute: str) -> Optional[UserProfileEntry]:
        return self.repository.get_profile(_text(attribute, "attribute"))

    def list_profiles(
        self, *, status: Optional[str] = None, limit: Optional[int] = None
    ) -> list[UserProfileEntry]:
        if status is not None:
            status = _choice(status, "status", FRAGMENT_STATUSES)
        return self.repository.list_profiles(status=status, limit=_limit(limit))

    def delete_profile(self, attribute: str) -> bool:
        return self.repository.delete_profile(_text(attribute, "attribute"))

    def create_current_memory(
        self,
        *,
        item_type: str,
        statement: str,
        topic: str = "",
        status: str = "active",
        priority: int = 0,
        source_slugs: Iterable[str],
    ) -> CurrentMemoryItem:
        now = _now()
        return self.repository.create_current_memory(
            CurrentMemoryItem(
                id=None,
                type=_choice(item_type, "item_type", CURRENT_MEMORY_TYPES),
                statement=_text(statement, "statement", limit=MAX_STATEMENT_LENGTH),
                topic=_text(topic, "topic", allow_empty=True, limit=MAX_TOPIC_LENGTH),
                status=_choice(status, "status", CURRENT_MEMORY_STATUSES),
                priority=_priority(priority),
                source_slugs=_source_slugs(source_slugs),
                created_at=now,
                updated_at=now,
            )
        )

    def get_current_memory(self, item_id: int) -> Optional[CurrentMemoryItem]:
        return self.repository.get_current_memory(item_id)

    def list_current_memory(
        self,
        *,
        item_type: Optional[str] = None,
        status: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[CurrentMemoryItem]:
        if item_type is not None:
            item_type = _choice(item_type, "item_type", CURRENT_MEMORY_TYPES)
        if status is not None:
            status = _choice(status, "status", CURRENT_MEMORY_STATUSES)
        return self.repository.list_current_memory(
            item_type=item_type, status=status, limit=_limit(limit)
        )

    def update_current_memory(self, item: CurrentMemoryItem) -> Optional[CurrentMemoryItem]:
        updated = CurrentMemoryItem(
            id=item.id,
            type=_choice(item.type, "item_type", CURRENT_MEMORY_TYPES),
            statement=_text(item.statement, "statement", limit=MAX_STATEMENT_LENGTH),
            topic=_text(item.topic, "topic", allow_empty=True, limit=MAX_TOPIC_LENGTH),
            status=_choice(item.status, "status", CURRENT_MEMORY_STATUSES),
            priority=_priority(item.priority),
            source_slugs=_source_slugs(item.source_slugs),
            created_at=_text(item.created_at, "created_at"),
            updated_at=_now(),
        )
        return self.repository.update_current_memory(updated)

    def delete_current_memory(self, item_id: int) -> bool:
        return self.repository.delete_current_memory(item_id)

    def create_open_loop(
        self,
        *,
        statement: str,
        topic: str = "",
        status: str = "open",
        source_slugs: Iterable[str],
        last_reviewed_at: Optional[str] = None,
    ) -> OpenLoop:
        now = _now()
        return self.repository.create_open_loop(
            OpenLoop(
                id=None,
                statement=_text(statement, "statement", limit=MAX_STATEMENT_LENGTH),
                topic=_text(topic, "topic", allow_empty=True, limit=MAX_TOPIC_LENGTH),
                status=_choice(status, "status", OPEN_LOOP_STATUSES),
                source_slugs=_source_slugs(source_slugs),
                last_reviewed_at=_optional_timestamp(last_reviewed_at, "last_reviewed_at"),
                created_at=now,
                updated_at=now,
            )
        )

    def get_open_loop(self, loop_id: int) -> Optional[OpenLoop]:
        return self.repository.get_open_loop(loop_id)

    def list_open_loops(
        self,
        *,
        status: Optional[str] = None,
        topic: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[OpenLoop]:
        if status is not None:
            status = _choice(status, "status", OPEN_LOOP_STATUSES)
        if topic is not None:
            topic = _text(topic, "topic", allow_empty=True)
        return self.repository.list_open_loops(
            status=status, topic=topic, limit=_limit(limit)
        )

    def update_open_loop(self, loop: OpenLoop) -> Optional[OpenLoop]:
        updated = OpenLoop(
            id=loop.id,
            statement=_text(loop.statement, "statement", limit=MAX_STATEMENT_LENGTH),
            topic=_text(loop.topic, "topic", allow_empty=True, limit=MAX_TOPIC_LENGTH),
            status=_choice(loop.status, "status", OPEN_LOOP_STATUSES),
            source_slugs=_source_slugs(loop.source_slugs),
            last_reviewed_at=_optional_timestamp(loop.last_reviewed_at, "last_reviewed_at"),
            created_at=_text(loop.created_at, "created_at"),
            updated_at=_now(),
        )
        return self.repository.update_open_loop(updated)

    def delete_open_loop(self, loop_id: int) -> bool:
        return self.repository.delete_open_loop(loop_id)


def _limit(value: Optional[int]) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise MemoryValidationError("limit must be a positive integer")
    return value


def _priority(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MemoryValidationError("priority must be a non-negative integer")
    return value
