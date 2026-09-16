"""Versioned contract for external/LLM-produced memory candidates.

The protocol validates derived fields only. It never accepts or stores the
original memo body and has no path to the formal memory tables.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Mapping, Optional

from .constants import (
    CURRENT_STATE_MARKERS,
    EVIDENCE_KINDS,
    FRAGMENT_TYPES,
    MAX_EVIDENCE_CONTEXT_LENGTH,
    MAX_SOURCE_SLUG_LENGTH,
    MAX_STATEMENT_LENGTH,
    MAX_TOPIC_LENGTH,
    REVIEW_ACTIONS,
    SUPPRESSION_REASONS,
    TEMPORAL_SCOPES,
)
from .models import EvidenceRef, EvidenceRefs, SourceSlugs, SuppressionRecord


PROTOCOL_NAME = "flomo-memory-extraction"
PROTOCOL_VERSION = "2.3"
FORBIDDEN_RAW_FIELDS = frozenset({"body", "content", "memo", "memo_body", "raw_text", "text"})
ALLOWED_PAYLOAD_FIELDS = frozenset(
    {
        "type",
        "statement",
        "topic",
        "confidence",
        "source_slugs",
        "evidence_kind",
        "review_status",
        "proposed_action",
        "temporal_scope",
        "observed_at",
        "last_supported_at",
        "evidence_refs",
    }
)
SUPPRESSION_PAYLOAD_FIELDS = frozenset({"source_slugs", "suppression_reason"})
EXTRACTION_RULES = (
    "extract only explicit user statements or information supported by sufficient evidence",
    "never present an AI inference as a user fact or a personality/profile conclusion",
    "one candidate contains one independently true-or-false atomic proposition",
    "do not combine an event with a psychological explanation or a fact with a pattern",
    "pattern is aggregation-only; if produced by an aggregation layer it requires at least three independent sources, two dates, and two contexts/topics",
    "conservative temporal defaults: project/goal current; experience/fact/decision episodic; belief/preference/value current observation; pattern is aggregation-only",
    "a generic fact may be current only when the source-faithful statement explicitly describes an ongoing state",
    "a single belief or value expression must not be promoted or labelled stable automatically",
    "recognition of a method is not a preference unless the source explicitly expresses a personal preference",
    "use unresolved for uncertainty, contradiction, or an unfinished conclusion",
    "make source_slugs mandatory for every candidate and evidence_refs mandatory for patterns",
    "source observed_at and context/tags come from the trusted flomo read catalog; extractor metadata is not authoritative",
    "when no candidate is justified, emit a suppression reason instead of treating it as a missed extraction",
    "never persist the original memo body",
    "generate candidates only; formal memory changes require the review gate",
)


class ExtractionProtocolError(ValueError):
    """Raised when an extractor output violates the V2.3 candidate contract."""


@dataclass(frozen=True)
class CandidateDraft:
    type: str
    statement: str
    topic: str
    confidence: float
    source_slugs: SourceSlugs
    evidence_kind: str
    proposed_action: str
    temporal_scope: str = "episodic"
    observed_at: str = ""
    last_supported_at: str = ""
    evidence_refs: EvidenceRefs = ()


class ExtractionProtocol:
    """Parser and validator for the external extraction output format."""

    name = PROTOCOL_NAME
    version = PROTOCOL_VERSION

    @classmethod
    def specification(cls) -> Mapping[str, object]:
        """Return a JSON-serializable summary for an extractor implementer."""

        return {
            "protocol": cls.name,
            "version": cls.version,
            "required": [
                "type",
                "statement",
                "confidence",
                "source_slugs",
                "evidence_kind",
                "proposed_action",
            ],
            "optional": [
                "topic",
                "review_status",
                "temporal_scope",
                "observed_at",
                "last_supported_at",
                "evidence_refs",
            ],
            "pattern_evidence_refs": {
                "required": True,
                "minimum_independent_sources": 3,
                "minimum_distinct_dates": 2,
                "minimum_distinct_contexts": 2,
            },
            "pattern_generation": {
                "ordinary_extraction": "forbidden",
                "aggregation_compatibility": True,
            },
            "trusted_evidence_catalog": {
                "required_for_staging": True,
                "builder": "FlomoEvidenceCatalogBuilder.from_api_response",
                "fields": ["source_slug", "observed_at", "context", "tags"],
                "memo_body": "forbidden",
                "extractor_dates_and_context": "assertions_only",
            },
            "temporal_provenance": {
                "observed_at": "earliest trusted source observed_at",
                "last_supported_at": "latest trusted source observed_at",
                "extraction_timestamp": "never used as source time",
            },
            "idempotency": "deterministic protocol-name/version plus source-slug-set plus normalized atomic statement key",
            "suppression_reasons": sorted(SUPPRESSION_REASONS),
            "review_status": "staging always sets this to pending",
            "raw_memo_fields": "forbidden",
            "rules": list(EXTRACTION_RULES),
        }

    @classmethod
    def parse_candidate(
        cls,
        payload: Mapping[str, object],
        *,
        require_pattern_evidence: bool = True,
        allow_pattern: bool = False,
    ) -> Optional[CandidateDraft]:
        if not isinstance(payload, Mapping):
            raise ExtractionProtocolError("candidate payload must be an object")

        keys = {str(key) for key in payload}
        cls._reject_raw_fields(keys)
        unknown = keys - ALLOWED_PAYLOAD_FIELDS
        if unknown:
            raise ExtractionProtocolError("candidate payload contains unknown fields")

        review_status = payload.get("review_status", "pending")
        if review_status != "pending":
            raise ExtractionProtocolError("extractor cannot set a reviewed candidate status")

        draft = cls.validate_fields(
            candidate_type=payload.get("type"),
            statement=payload.get("statement"),
            topic=payload.get("topic", ""),
            confidence=payload.get("confidence"),
            source_slugs=payload.get("source_slugs"),
            evidence_kind=payload.get("evidence_kind"),
            proposed_action=payload.get("proposed_action"),
            temporal_scope=payload.get("temporal_scope"),
            observed_at=payload.get("observed_at"),
            last_supported_at=payload.get("last_supported_at"),
            evidence_refs=payload.get("evidence_refs"),
            require_pattern_evidence=require_pattern_evidence,
            allow_pattern=allow_pattern,
        )
        if draft is not None and draft.evidence_kind == "low_value":
            if draft.proposed_action != "reject":
                raise ExtractionProtocolError("low-value output must propose reject")
            return None
        return draft

    @classmethod
    def parse_suppression(cls, payload: Mapping[str, object]) -> SuppressionRecord:
        """Validate a source-scoped no-candidate outcome.

        Suppression outcomes are deliberately not persisted as formal memory
        and contain no memo text. A caller may retain them as run metadata.
        """

        if not isinstance(payload, Mapping):
            raise ExtractionProtocolError("suppression payload must be an object")
        keys = {str(key) for key in payload}
        cls._reject_raw_fields(keys)
        unknown = keys - SUPPRESSION_PAYLOAD_FIELDS
        if unknown:
            raise ExtractionProtocolError("suppression payload contains unknown fields")
        return SuppressionRecord(
            source_slugs=cls._source_slugs(payload.get("source_slugs")),
            suppression_reason=cls._choice(
                payload.get("suppression_reason"),
                "suppression_reason",
                SUPPRESSION_REASONS,
            ),
        )

    @classmethod
    def normalize_source_slugs(cls, value: object) -> SourceSlugs:
        """Normalize source identifiers for trusted-catalog lookup."""

        return cls._source_slugs(value)

    @classmethod
    def validate_fields(
        cls,
        *,
        candidate_type: object,
        statement: object,
        topic: object,
        confidence: object,
        source_slugs: object,
        evidence_kind: object,
        proposed_action: object,
        temporal_scope: object = None,
        observed_at: object = None,
        last_supported_at: object = None,
        evidence_refs: object = None,
        require_pattern_evidence: bool = True,
        allow_pattern: bool = False,
    ) -> Optional[CandidateDraft]:
        normalized_type = cls._choice(candidate_type, "type", FRAGMENT_TYPES)
        normalized_statement = cls._text(statement, "statement")
        normalized_topic = cls._text(topic, "topic", allow_empty=True)
        normalized_confidence = cls._confidence(confidence)
        normalized_sources = cls._source_slugs(source_slugs)
        normalized_evidence = cls._choice(evidence_kind, "evidence_kind", EVIDENCE_KINDS)
        normalized_action = cls._choice(proposed_action, "proposed_action", REVIEW_ACTIONS)
        normalized_scope = cls._temporal_scope(
            temporal_scope, normalized_type, normalized_statement
        )
        normalized_observed = cls._timestamp(observed_at, "observed_at", allow_empty=True)
        normalized_last_supported = cls._timestamp(
            last_supported_at, "last_supported_at", allow_empty=True
        )

        cls._validate_atomicity(normalized_statement, normalized_type)

        if normalized_type == "pattern" and not allow_pattern:
            raise ExtractionProtocolError(
                "pattern candidates are aggregation-only and cannot come from ordinary extraction"
            )

        normalized_refs = cls._evidence_refs(
            evidence_refs,
            source_slugs=normalized_sources,
            topic=normalized_topic,
            observed_at=normalized_observed,
            required=normalized_type == "pattern" and require_pattern_evidence,
        )
        if normalized_refs:
            if not normalized_observed:
                normalized_observed = min(
                    (ref.observed_at for ref in normalized_refs), key=cls._date_key
                )
            if not normalized_last_supported:
                normalized_last_supported = max(
                    (ref.observed_at for ref in normalized_refs), key=cls._date_key
                )

        if normalized_type == "pattern" and require_pattern_evidence:
            cls._validate_pattern(
                normalized_sources,
                normalized_evidence,
                normalized_refs,
            )

        if normalized_type == "preference" and normalized_evidence in {
            "transient_emotion",
            "method_endorsement",
        }:
            raise ExtractionProtocolError(
                "transient emotion or method endorsement cannot directly become a preference"
            )
        if normalized_type == "preference" and cls._looks_like_method_endorsement(
            normalized_statement
        ):
            raise ExtractionProtocolError(
                "recognition of a method or saying is not a preference by default"
            )

        if normalized_type in {"belief", "value"} and normalized_scope == "stable":
            if len(normalized_sources) < 2:
                raise ExtractionProtocolError(
                    "a single belief or value source cannot be marked stable"
                )
            if normalized_evidence == "transient_emotion":
                raise ExtractionProtocolError(
                    "a transient self-motivation cannot become a stable belief or value"
                )

        if normalized_type == "experience" and normalized_scope == "stable":
            raise ExtractionProtocolError("experience candidates cannot be marked stable")

        if normalized_evidence in {"contradictory", "insufficient"} and normalized_action not in {
            "unresolved",
            "reject",
        }:
            raise ExtractionProtocolError(
                "contradictory or insufficient evidence cannot propose acceptance"
            )

        draft = CandidateDraft(
            type=normalized_type,
            statement=normalized_statement,
            topic=normalized_topic,
            confidence=normalized_confidence,
            source_slugs=normalized_sources,
            evidence_kind=normalized_evidence,
            proposed_action=normalized_action,
            temporal_scope=normalized_scope,
            observed_at=normalized_observed,
            last_supported_at=normalized_last_supported,
            evidence_refs=normalized_refs,
        )
        if draft.evidence_kind == "low_value":
            if draft.proposed_action != "reject":
                raise ExtractionProtocolError("low-value output must propose reject")
            return None
        return draft

    @classmethod
    def _validate_pattern(
        cls,
        source_slugs: SourceSlugs,
        evidence_kind: str,
        evidence_refs: EvidenceRefs,
    ) -> None:
        if evidence_kind not in {"repeated", "corroborated"}:
            raise ExtractionProtocolError(
                "pattern candidate requires repeated or corroborated evidence"
            )
        if len(source_slugs) < 3:
            raise ExtractionProtocolError(
                "pattern candidate requires at least three independent source slugs"
            )
        if len({ref.source_slug for ref in evidence_refs}) < 3:
            raise ExtractionProtocolError(
                "pattern evidence_refs must cover at least three independent sources"
            )
        if len({cls._date_key(ref.observed_at) for ref in evidence_refs}) < 2:
            raise ExtractionProtocolError(
                "pattern candidate requires evidence from at least two different dates"
            )
        contexts = {ref.context.strip() for ref in evidence_refs if ref.context.strip()}
        if len(contexts) < 2:
            raise ExtractionProtocolError(
                "pattern candidate requires at least two different contexts or topics"
            )
        if {ref.source_slug for ref in evidence_refs} != set(source_slugs):
            raise ExtractionProtocolError(
                "pattern evidence_refs must cover exactly the declared source slugs"
            )

    @classmethod
    def _evidence_refs(
        cls,
        value: object,
        *,
        source_slugs: SourceSlugs,
        topic: str,
        observed_at: str,
        required: bool,
    ) -> EvidenceRefs:
        if value is None:
            if required:
                raise ExtractionProtocolError("pattern candidate requires evidence_refs")
            if not observed_at:
                return ()
            return tuple(EvidenceRef(slug, observed_at, topic) for slug in source_slugs)
        if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
            raise ExtractionProtocolError("evidence_refs must be an array of objects")

        refs: list[EvidenceRef] = []
        for raw_ref in value:
            if not isinstance(raw_ref, Mapping):
                raise ExtractionProtocolError("each evidence_ref must be an object")
            keys = {str(key) for key in raw_ref}
            cls._reject_raw_fields(keys)
            if keys != {"source_slug", "observed_at", "context"}:
                raise ExtractionProtocolError("evidence_ref fields are invalid")
            source_slug = cls._text(raw_ref.get("source_slug"), "evidence_ref.source_slug")
            if source_slug not in source_slugs:
                raise ExtractionProtocolError(
                    "evidence_ref source_slug must be listed in source_slugs"
                )
            ref_observed_at = cls._timestamp(
                raw_ref.get("observed_at"), "evidence_ref.observed_at"
            )
            context = cls._text(raw_ref.get("context"), "evidence_ref.context", allow_empty=True)
            refs.append(EvidenceRef(source_slug, ref_observed_at, context))
        if required and not refs:
            raise ExtractionProtocolError("pattern candidate requires evidence_refs")
        return tuple(refs)

    @classmethod
    def _temporal_scope(
        cls, value: object, candidate_type: str, statement: str
    ) -> str:
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
                # Kept for aggregation-layer compatibility. Ordinary
                # extraction rejects this type before a candidate is built.
                "pattern": "current",
            }.get(candidate_type, "episodic")
        else:
            normalized = cls._choice(value, "temporal_scope", TEMPORAL_SCOPES)

        if (
            candidate_type == "fact"
            and normalized == "current"
            and not cls._expresses_current_state(statement)
        ):
            raise ExtractionProtocolError(
                "a fact may be current only when its statement explicitly describes an ongoing state"
            )
        return normalized

    @staticmethod
    def _expresses_current_state(statement: str) -> bool:
        normalized = statement.casefold()
        return any(marker.casefold() in normalized for marker in CURRENT_STATE_MARKERS)

    @classmethod
    def _validate_atomicity(cls, statement: str, candidate_type: str) -> None:
        if "\n" in statement or "\r" in statement or "；" in statement or ";" in statement:
            raise ExtractionProtocolError(
                "statement must contain one atomic proposition, not multiple clauses"
            )
        if "。" in statement[:-1] or statement.count("。") > 1:
            raise ExtractionProtocolError(
                "statement must contain one atomic proposition, not multiple sentences"
            )
        if re.search(
            r"[，,]\s*(?:因为|由于|因此|所以|说明|表明|意味着|导致|反思|意识到|提醒|证明|体现|从而|包含|涉及|重点|主张|表示|认为|一切|所有|最终|行业|目前|已经|已|未|尚未|用户|自己|我|他|她|需要|应该|必须|要|会|使用|练习|进行|完成|达到)",
            statement,
        ):
            raise ExtractionProtocolError(
                "do not combine an event or fact with its explanation or interpretation"
            )
        if re.search(r"[，,]\s*(?:并且|而且|同时)", statement):
            raise ExtractionProtocolError("statement must contain one atomic proposition")
        if re.search(r"[，,]\s*(?:但|但是|而|不过|然而)", statement):
            raise ExtractionProtocolError(
                "statement must contain one atomic proposition, not coordinated propositions"
            )
        if re.search(r"[，,]\s*并", statement):
            raise ExtractionProtocolError("statement must contain one atomic proposition")
        if re.search(r"(?:且|以及|不仅.*(?:还|而且)|既.*(?:又|也|还)|(?:不能|不只是).*(?:也|还))", statement):
            raise ExtractionProtocolError("statement must contain one atomic proposition")
        if re.search(r"、[^。]*[，,]\s*(?:需要|应该|必须|要)", statement):
            raise ExtractionProtocolError(
                "do not combine a self-assessment with a recommendation or requirement"
            )
        if re.search(r"[，,]\s*(?:总是|经常|通常|往往|习惯(?:于)?|倾向(?:于)?)", statement):
            raise ExtractionProtocolError(
                "do not combine a specific event or fact with a generalized pattern"
            )
        if candidate_type == "pattern" and re.search(
            r"(?:一次|曾经|那次|当时).*(?:总是|经常|通常|往往|习惯|倾向)", statement
        ):
            raise ExtractionProtocolError("pattern statement cannot include a one-off event")

    @staticmethod
    def _looks_like_method_endorsement(statement: str) -> bool:
        if re.search(r"(?:认同|赞同|认可|主张|这句话|这类说法|该方法|这种方法)", statement):
            return not re.search(
                r"(?:我|用户)\s*(?:偏好|喜欢使用|更喜欢用|倾向使用|习惯使用|选择用)",
                statement,
            )
        if re.search(r"(?:喜欢|欣赏).*(?:方法|策略|做法|原则|理念|说法)", statement):
            return not re.search(
                r"(?:我|用户)\s*(?:偏好|喜欢使用|更喜欢用|倾向使用|习惯使用|选择用)",
                statement,
            )
        return False

    @staticmethod
    def _reject_raw_fields(keys: set[str]) -> None:
        if keys & FORBIDDEN_RAW_FIELDS:
            raise ExtractionProtocolError("raw memo fields are not allowed")

    @staticmethod
    def _text(value: object, field: str, *, allow_empty: bool = False) -> str:
        if not isinstance(value, str):
            raise ExtractionProtocolError(f"{field} must be text")
        normalized = value.strip()
        if not allow_empty and not normalized:
            raise ExtractionProtocolError(f"{field} is required")
        limits = {
            "statement": MAX_STATEMENT_LENGTH,
            "topic": MAX_TOPIC_LENGTH,
            "evidence_ref.context": MAX_EVIDENCE_CONTEXT_LENGTH,
            "evidence_ref.source_slug": MAX_SOURCE_SLUG_LENGTH,
        }
        limit = limits.get(field)
        if limit is not None and len(normalized) > limit:
            raise ExtractionProtocolError(f"{field} exceeds the maximum length")
        return normalized

    @staticmethod
    def _choice(value: object, field: str, choices: frozenset[str]) -> str:
        normalized = ExtractionProtocol._text(value, field)
        if normalized not in choices:
            raise ExtractionProtocolError(f"invalid {field}")
        return normalized

    @staticmethod
    def _confidence(value: object) -> float:
        if isinstance(value, bool):
            raise ExtractionProtocolError("confidence must be between 0 and 1")
        try:
            normalized = float(value)
        except (TypeError, ValueError) as exc:
            raise ExtractionProtocolError("confidence must be between 0 and 1") from exc
        if not math.isfinite(normalized) or not 0 <= normalized <= 1:
            raise ExtractionProtocolError("confidence must be between 0 and 1")
        return normalized

    @classmethod
    def _timestamp(cls, value: object, field: str, *, allow_empty: bool = False) -> str:
        if value is None:
            if allow_empty:
                return ""
            raise ExtractionProtocolError(f"{field} is required")
        normalized = cls._text(value, field, allow_empty=allow_empty)
        if not normalized:
            return normalized
        try:
            cls._date_key(normalized)
        except ValueError as exc:
            raise ExtractionProtocolError(
                f"{field} must contain an ISO date or timestamp"
            ) from exc
        return normalized

    @staticmethod
    def _date_key(value: str) -> date:
        normalized = value.strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized).date()
        except ValueError:
            try:
                return date.fromisoformat(normalized[:10])
            except ValueError as exc:
                raise ValueError("invalid ISO date") from exc

    @staticmethod
    def _source_slugs(value: object) -> SourceSlugs:
        if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
            raise ExtractionProtocolError("source_slugs must be a non-empty array")
        normalized = []
        for slug in value:
            if not isinstance(slug, str) or not slug.strip():
                raise ExtractionProtocolError("source_slugs must contain non-empty strings")
            clean_slug = slug.strip()
            if len(clean_slug) > MAX_SOURCE_SLUG_LENGTH:
                raise ExtractionProtocolError("source slug exceeds the maximum length")
            if clean_slug not in normalized:
                normalized.append(clean_slug)
        if not normalized:
            raise ExtractionProtocolError("at least one source slug is required")
        return tuple(normalized)
