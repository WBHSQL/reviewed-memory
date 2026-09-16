"""Ephemeral trusted source metadata for one extraction run.

The catalog intentionally has no memo-body field. A flomo read stage may create
it from source metadata while holding the memo body elsewhere in memory, then
discard it after extraction/staging.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping, Optional

from .constants import (
    MAX_EVIDENCE_CONTEXT_LENGTH,
    MAX_SOURCE_SLUG_LENGTH,
    MAX_TAG_LENGTH,
)
from .models import EvidenceRef, SourceSlugs


_FORBIDDEN_RAW_FIELDS = frozenset({"body", "content", "memo", "memo_body", "raw_text", "text"})


class TrustedEvidenceError(ValueError):
    """Raised when trusted source metadata is missing, malformed, or forged."""


_FLOMO_CATALOG_TOKEN = object()
_CONVERSATION_CATALOG_TOKEN = object()


def _text(value: object, field: str, *, allow_empty: bool = False, limit: int) -> str:
    if not isinstance(value, str):
        raise TrustedEvidenceError(f"{field} must be text")
    normalized = value.strip()
    if not allow_empty and not normalized:
        raise TrustedEvidenceError(f"{field} is required")
    if len(normalized) > limit:
        raise TrustedEvidenceError(f"{field} exceeds the maximum length")
    return normalized


def _timestamp(value: object, field: str) -> str:
    normalized = _text(value, field, limit=80)
    try:
        _timestamp_key(normalized)
    except ValueError as exc:
        raise TrustedEvidenceError(f"{field} must contain an ISO date or timestamp") from exc
    return normalized


def _timestamp_key(value: str) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = datetime.combine(date.fromisoformat(normalized[:10]), datetime.min.time())
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=None)
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True)
class TrustedEvidence:
    """Source metadata supplied by a read stage, never the memo body."""

    source_slug: str
    observed_at: str
    context: str = ""
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.source_slug, "source_slug", limit=MAX_SOURCE_SLUG_LENGTH)
        _timestamp(self.observed_at, "observed_at")
        _text(
            self.context,
            "context",
            allow_empty=True,
            limit=MAX_EVIDENCE_CONTEXT_LENGTH,
        )
        if isinstance(self.tags, (str, bytes)) or not isinstance(self.tags, tuple):
            raise TrustedEvidenceError("tags must be a tuple of strings")
        for tag in self.tags:
            _text(tag, "tag", limit=MAX_TAG_LENGTH)
        if len(self.metadata_context) > MAX_EVIDENCE_CONTEXT_LENGTH:
            raise TrustedEvidenceError("trusted evidence context exceeds the maximum length")

    @property
    def metadata_context(self) -> str:
        parts = []
        if self.context.strip():
            parts.append(self.context.strip())
        if self.tags:
            parts.append("tags=" + ",".join(self.tags))
        return " | ".join(parts)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "TrustedEvidence":
        if not isinstance(value, Mapping):
            raise TrustedEvidenceError("trusted evidence must be an object")
        keys = {str(key) for key in value}
        if keys & _FORBIDDEN_RAW_FIELDS:
            raise TrustedEvidenceError("trusted evidence cannot contain memo body fields")
        unknown = keys - {"source_slug", "observed_at", "context", "tags"}
        if unknown:
            raise TrustedEvidenceError("trusted evidence contains unknown fields")
        raw_tags = value.get("tags", ())
        if isinstance(raw_tags, (str, bytes)) or not isinstance(raw_tags, (list, tuple)):
            raise TrustedEvidenceError("tags must be an array of strings")
        normalized_tags = []
        for raw_tag in raw_tags:
            tag = _text(raw_tag, "tag", limit=MAX_TAG_LENGTH)
            if tag not in normalized_tags:
                normalized_tags.append(tag)
        return cls(
            source_slug=_text(
                value.get("source_slug"),
                "source_slug",
                limit=MAX_SOURCE_SLUG_LENGTH,
            ),
            observed_at=_timestamp(value.get("observed_at"), "observed_at"),
            context=_text(
                value.get("context", ""),
                "context",
                allow_empty=True,
                limit=MAX_EVIDENCE_CONTEXT_LENGTH,
            ),
            tags=tuple(normalized_tags),
        )


@dataclass(frozen=True)
class TrustedEvidenceProvenance:
    observed_at: str
    last_supported_at: str
    evidence_refs: tuple[EvidenceRef, ...]


class TrustedEvidenceCatalog:
    """In-memory source metadata registry for one extraction/staging run."""

    def __init__(
        self,
        records: Iterable[TrustedEvidence | Mapping[str, object]],
        *,
        _origin_token: object | None = None,
    ) -> None:
        normalized: dict[str, TrustedEvidence] = {}
        for raw_record in records:
            record = (
                TrustedEvidence.from_mapping(raw_record)
                if isinstance(raw_record, Mapping)
                else raw_record
            )
            if not isinstance(record, TrustedEvidence):
                raise TrustedEvidenceError("trusted evidence record has an invalid type")
            slug = record.source_slug.strip()
            existing = normalized.get(slug)
            if existing is not None and existing != record:
                raise TrustedEvidenceError("source_slug has conflicting trusted metadata")
            normalized[slug] = record
        self._records = normalized
        # A normal constructor is intentionally not sufficient for the
        # bootstrap trust boundary.  Only the production builder can mark the
        # catalog as originating from a flomo API read.
        self._is_flomo_api_catalog = _origin_token is _FLOMO_CATALOG_TOKEN

    @classmethod
    def from_records(
        cls, records: Iterable[TrustedEvidence | Mapping[str, object]]
    ) -> "TrustedEvidenceCatalog":
        return cls(records)

    @classmethod
    def _from_flomo_projection(
        cls, records: Iterable[TrustedEvidence | Mapping[str, object]]
    ) -> "TrustedEvidenceCatalog":
        """Create a staging-authorized catalog for the production builder."""

        return cls(records, _origin_token=_FLOMO_CATALOG_TOKEN)

    @property
    def is_flomo_api_catalog(self) -> bool:
        """Whether this catalog was produced by the flomo metadata boundary."""

        return self._is_flomo_api_catalog

    @property
    def source_slugs(self) -> SourceSlugs:
        return tuple(self._records)

    def get(self, source_slug: str) -> Optional[TrustedEvidence]:
        return self._records.get(source_slug.strip()) if isinstance(source_slug, str) else None

    def require(self, source_slugs: SourceSlugs) -> tuple[TrustedEvidence, ...]:
        records = []
        for source_slug in source_slugs:
            record = self.get(source_slug)
            if record is None:
                raise TrustedEvidenceError(
                    f"trusted evidence metadata not found for source_slug: {source_slug}"
                )
            records.append(record)
        return tuple(records)

    def provenance(self, source_slugs: SourceSlugs) -> TrustedEvidenceProvenance:
        records = self.require(source_slugs)
        earliest = min(records, key=lambda record: _timestamp_key(record.observed_at))
        latest = max(records, key=lambda record: _timestamp_key(record.observed_at))
        refs = tuple(
            EvidenceRef(
                source_slug=record.source_slug,
                observed_at=record.observed_at,
                context=record.metadata_context,
            )
            for record in records
        )
        return TrustedEvidenceProvenance(
            observed_at=earliest.observed_at,
            last_supported_at=latest.observed_at,
            evidence_refs=refs,
        )

    def assert_submitted_metadata(
        self,
        payload: Mapping[str, object],
        parsed_observed_at: str,
        parsed_last_supported_at: str,
        parsed_evidence_refs: tuple[EvidenceRef, ...],
        provenance: TrustedEvidenceProvenance,
    ) -> None:
        submitted_observed = payload.get("observed_at")
        if isinstance(submitted_observed, str) and submitted_observed.strip():
            if parsed_observed_at != provenance.observed_at:
                raise TrustedEvidenceError("submitted observed_at does not match trusted metadata")

        submitted_last = payload.get("last_supported_at")
        if isinstance(submitted_last, str) and submitted_last.strip():
            if parsed_last_supported_at != provenance.last_supported_at:
                raise TrustedEvidenceError(
                    "submitted last_supported_at does not match trusted metadata"
                )

        submitted_refs = payload.get("evidence_refs")
        if isinstance(submitted_refs, (list, tuple)) and submitted_refs:
            expected = {
                ref.source_slug: (ref.observed_at, ref.context)
                for ref in provenance.evidence_refs
            }
            actual = {
                ref.source_slug: (ref.observed_at, ref.context)
                for ref in parsed_evidence_refs
            }
            if len(parsed_evidence_refs) != len(expected) or actual != expected:
                raise TrustedEvidenceError(
                    "submitted evidence_refs do not match trusted metadata"
                )


class FlomoEvidenceCatalogBuilder:
    """Project flomo API responses into body-free trusted evidence metadata.

    The input may contain full API memo objects because that is what the
    existing read-only client returns.  This builder reads only ``slug``,
    ``created_at`` (the source memo time), ``tags`` and the explicitly supported
    ``context`` metadata.  It does not retain or forward any other field,
    including memo content.
    """

    _TIME_FIELDS = (
        "created_at",
        "createdAt",
        "created_time",
        "createdTime",
        "memo_created_at",
    )
    _TAG_FIELDS = ("tags", "tag_names", "tag")

    @classmethod
    def from_api_response(cls, response: object) -> TrustedEvidenceCatalog:
        """Build a production catalog from a ``FlomoClient`` API result.

        ``FlomoClient`` returns the response's ``data`` value, while accepting
        an envelope here keeps the adapter safe for callers that pass the raw
        JSON response in tests or integrations.
        """

        records = cls._response_records(response)
        projected = [cls._project_record(record) for record in records]
        return TrustedEvidenceCatalog._from_flomo_projection(projected)

    @classmethod
    def from_memo_records(cls, records: Iterable[Mapping[str, object]]) -> TrustedEvidenceCatalog:
        """Alias for callers that already extracted the API memo list."""

        return cls.from_api_response(records)

    @staticmethod
    def _response_records(response: object) -> tuple[Mapping[str, object], ...]:
        if isinstance(response, Mapping):
            if "data" in response:
                response = response.get("data")
            elif "memos" in response:
                response = response.get("memos")
            else:
                response = (response,)
        if isinstance(response, (str, bytes)) or not isinstance(response, (list, tuple)):
            raise TrustedEvidenceError("flomo API response must contain memo objects")
        records = []
        for record in response:
            if not isinstance(record, Mapping):
                raise TrustedEvidenceError("flomo API response contains a non-object memo")
            records.append(record)
        return tuple(records)

    @classmethod
    def _project_record(cls, record: Mapping[str, object]) -> TrustedEvidence:
        slug = record.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            raise TrustedEvidenceError("flomo memo metadata requires slug")

        raw_time: Any = None
        for field in cls._TIME_FIELDS:
            value = cls._metadata_value(record, field)
            if value is not None:
                raw_time = value
                break
        if raw_time is None:
            # ``updated_at`` is deliberately not a fallback: it is not the
            # memo's source time and could make an old memo look newly observed.
            raise TrustedEvidenceError("flomo memo metadata requires created_at")

        raw_context = cls._metadata_value(record, "context")
        if raw_context is None:
            raw_context = ""
        if not isinstance(raw_context, str):
            raise TrustedEvidenceError("flomo metadata context must be text")

        return TrustedEvidence(
            source_slug=_text(slug, "source_slug", limit=MAX_SOURCE_SLUG_LENGTH),
            observed_at=cls._api_timestamp(raw_time, "created_at"),
            context=_text(
                raw_context,
                "context",
                allow_empty=True,
                limit=MAX_EVIDENCE_CONTEXT_LENGTH,
            ),
            tags=cls._api_tags(record),
        )

    @staticmethod
    def _api_timestamp(value: object, field: str) -> str:
        if isinstance(value, bool):
            raise TrustedEvidenceError(f"{field} must contain a real source timestamp")
        if isinstance(value, (int, float)):
            numeric = float(value)
            if not math.isfinite(numeric):
                raise TrustedEvidenceError(f"{field} must contain a real source timestamp")
            if numeric > 10_000_000_000:
                numeric /= 1000
            try:
                return datetime.fromtimestamp(numeric, tz=timezone.utc).isoformat(
                    timespec="seconds"
                )
            except (OverflowError, OSError, ValueError) as exc:
                raise TrustedEvidenceError(
                    f"{field} must contain a real source timestamp"
                ) from exc
        if isinstance(value, datetime):
            parsed = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
        if isinstance(value, str) and value.strip().isdigit():
            return FlomoEvidenceCatalogBuilder._api_timestamp(
                int(value.strip()), field
            )
        return _timestamp(value, field)

    @classmethod
    def _api_tags(cls, record: Mapping[str, object]) -> tuple[str, ...]:
        raw_tags: object = ()
        for field in cls._TAG_FIELDS:
            value = cls._metadata_value(record, field)
            if value is not None:
                raw_tags = value
                break
        if raw_tags in (None, ""):
            return ()
        if isinstance(raw_tags, str):
            raw_values = (raw_tags,)
        elif isinstance(raw_tags, (list, tuple)):
            raw_values = tuple(raw_tags)
        else:
            raise TrustedEvidenceError("flomo tags must be a string or array")

        tags: list[str] = []
        for raw_tag in raw_values:
            if isinstance(raw_tag, Mapping):
                raw_tag = raw_tag.get("name", raw_tag.get("slug"))
            tag = _text(raw_tag, "tag", limit=MAX_TAG_LENGTH)
            if tag not in tags:
                tags.append(tag)
        return tuple(tags)

    @staticmethod
    def _metadata_value(record: Mapping[str, object], field: str) -> object:
        if field in record:
            return record[field]
        metadata = record.get("metadata")
        if isinstance(metadata, Mapping) and field in metadata:
            return metadata[field]
        return None
