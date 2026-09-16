"""Data models for records derived from flomo memos.

These models intentionally contain no raw memo field. ``source_slugs`` is the
only link back to the original flomo evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple


SourceSlugs = Tuple[str, ...]


@dataclass(frozen=True)
class EvidenceRef:
    """Structured evidence metadata; never contains the memo body."""

    source_slug: str
    observed_at: str
    context: str


EvidenceRefs = Tuple[EvidenceRef, ...]


@dataclass(frozen=True)
class MemoryFragment:
    id: Optional[int]
    type: str
    statement: str
    topic: str
    confidence: float
    status: str
    valid_from: str
    last_supported_at: str
    source_slugs: SourceSlugs
    created_at: str
    updated_at: str
    temporal_scope: str = "current"
    observed_at: str = ""


@dataclass(frozen=True)
class UserProfileEntry:
    id: Optional[int]
    attribute: str
    value: str
    topic: str
    confidence: float
    status: str
    source_slugs: SourceSlugs
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class CurrentMemoryItem:
    id: Optional[int]
    type: str
    statement: str
    topic: str
    status: str
    priority: int
    source_slugs: SourceSlugs
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class OpenLoop:
    id: Optional[int]
    statement: str
    topic: str
    status: str
    source_slugs: SourceSlugs
    last_reviewed_at: Optional[str]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class MemoryCandidate:
    """A staged extraction result that has not yet passed review."""

    id: Optional[int]
    type: str
    statement: str
    topic: str
    confidence: float
    source_slugs: SourceSlugs
    evidence_kind: str
    review_status: str
    proposed_action: str
    created_at: str
    updated_at: str
    review_note: Optional[str] = None
    reviewed_at: Optional[str] = None
    temporal_scope: str = "current"
    observed_at: str = ""
    last_supported_at: str = ""
    evidence_refs: EvidenceRefs = ()
    dedupe_key: str = ""
    protocol_name: str = "flomo-memory-extraction"
    protocol_version: str = "2.3"
    revision_of_candidate_id: Optional[int] = None
    revision_reason: Optional[str] = None
    # Opaque marker issued only by the trusted flomo staging boundary.  It is
    # not a substitute for the provenance row; both must agree at promotion.
    trusted_staging_proof: Optional[str] = None


@dataclass(frozen=True)
class CandidateProvenance:
    """Body-free proof that staging validated one candidate against flomo metadata."""

    id: Optional[int]
    candidate_id: int
    proof_marker: str
    origin: str
    source_slugs: SourceSlugs
    observed_at: str
    last_supported_at: str
    evidence_refs: EvidenceRefs
    protocol_name: str
    protocol_version: str
    created_at: str


@dataclass(frozen=True)
class SuppressionRecord:
    """A source-scoped extraction outcome that intentionally made no candidate."""

    source_slugs: SourceSlugs
    suppression_reason: str


@dataclass(frozen=True)
class CandidateStagingResult:
    """Candidate rows plus non-candidate suppression outcomes for one batch."""

    candidates: Tuple[MemoryCandidate, ...]
    suppressions: Tuple[SuppressionRecord, ...]


@dataclass(frozen=True)
class CandidateReview:
    """One item in a batch review request.

    ``edit`` fields are optional and only apply when ``action`` is ``edit``.
    They never write a formal memory by themselves.
    """

    candidate_id: int
    action: str
    edited_type: Optional[str] = None
    edited_statement: Optional[str] = None
    edited_topic: Optional[str] = None
    edited_confidence: Optional[float] = None
    edited_source_slugs: Optional[SourceSlugs] = None
    edited_evidence_kind: Optional[str] = None
    edited_proposed_action: Optional[str] = None
    review_note: Optional[str] = None
    supersedes_fragment_id: Optional[int] = None
    edited_temporal_scope: Optional[str] = None
    edited_observed_at: Optional[str] = None
    edited_last_supported_at: Optional[str] = None
    edited_evidence_refs: Optional[EvidenceRefs] = None


@dataclass(frozen=True)
class CandidateReviewResult:
    candidate_id: int
    action: str
    review_status: str
    promoted_fragment_id: Optional[int] = None


@dataclass(frozen=True)
class BatchReviewResult:
    results: Tuple[CandidateReviewResult, ...]


@dataclass(frozen=True)
class PromotionLineage:
    """Durable link between a staged candidate and its promoted fragment."""

    id: Optional[int]
    candidate_id: int
    promoted_fragment_id: int
    action: str
    superseded_fragment_id: Optional[int]
    protocol_name: str
    protocol_version: str
    created_at: str
