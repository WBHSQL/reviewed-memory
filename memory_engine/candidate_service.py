"""Candidate staging and batch review gate for Memory Engine V2.3.1."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Iterable, Mapping, Optional

from .constants import (
    CANDIDATE_REVIEW_STATUSES,
    MAX_REVIEW_NOTE_LENGTH,
    REVIEW_ACTIONS,
)
from .evidence import TrustedEvidenceCatalog, TrustedEvidenceProvenance
from .identity import candidate_dedupe_key
from .models import (
    BatchReviewResult,
    CandidateReview,
    CandidateReviewResult,
    CandidateStagingResult,
    MemoryCandidate,
    MemoryFragment,
    PromotionLineage,
)
from .protocol import (
    PROTOCOL_NAME,
    PROTOCOL_VERSION,
    CandidateDraft,
    ExtractionProtocol,
    ExtractionProtocolError,
)
from .repository import MemoryRepository, _FORMAL_WRITE_TOKEN, _STAGING_WRITE_TOKEN
from .service import MemoryService


class CandidateReviewError(ValueError):
    """Raised when a batch review cannot pass the review gate."""


class EvidenceCatalogRequiredError(CandidateReviewError):
    """Raised when staging is attempted without trusted source metadata."""


class CandidateRevisionRequiredError(CandidateReviewError):
    """Raised when an existing protocol identity changes classification."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _note(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CandidateReviewError("review_note must be text")
    normalized = value.strip()
    if len(normalized) > MAX_REVIEW_NOTE_LENGTH:
        raise CandidateReviewError("review_note exceeds the maximum length")
    return normalized or None


class CandidateStagingService:
    """Accept extractor output into staging without touching formal memory."""

    def __init__(
        self,
        repository: MemoryRepository,
        evidence_catalog: TrustedEvidenceCatalog | None = None,
    ) -> None:
        self.repository = repository
        self.evidence_catalog = evidence_catalog

    def stage_batch(
        self,
        payloads: Iterable[Mapping[str, object]],
        *,
        evidence_catalog: TrustedEvidenceCatalog | None = None,
    ) -> tuple[MemoryCandidate, ...]:
        return self.stage_batch_with_audit(
            payloads, evidence_catalog=evidence_catalog
        ).candidates

    def stage_batch_with_audit(
        self,
        payloads: Iterable[Mapping[str, object]],
        suppression_payloads: Iterable[Mapping[str, object]] = (),
        *,
        evidence_catalog: TrustedEvidenceCatalog | None = None,
    ) -> CandidateStagingResult:
        """Validate and stage a batch of protocol payloads.

        Validation happens for the complete batch before any row is inserted.
        A ``low_value`` payload is intentionally suppressed and produces no
        candidate; explicit suppression outcomes are returned as run metadata,
        not written to formal memory tables.
        """

        candidate_payloads = tuple(payloads)
        explicit_suppression_payloads = tuple(suppression_payloads)
        if not candidate_payloads and not explicit_suppression_payloads:
            return CandidateStagingResult(candidates=(), suppressions=())

        catalog = (
            evidence_catalog
            if evidence_catalog is not None
            else self.evidence_catalog
        )
        if catalog is None:
            raise EvidenceCatalogRequiredError(
                "staging requires a trusted evidence catalog"
            )
        if not catalog.is_flomo_api_catalog:
            raise EvidenceCatalogRequiredError(
                "staging requires a catalog built from a flomo API response"
            )

        drafts: list[tuple[CandidateDraft, TrustedEvidenceProvenance]] = []
        suppressions = []
        for payload in candidate_payloads:
            trusted_draft = self._trusted_draft(payload, catalog)
            if trusted_draft is not None:
                drafts.append(trusted_draft)
            else:
                source_slugs = ExtractionProtocol.normalize_source_slugs(
                    payload.get("source_slugs")
                )
                catalog.require(source_slugs)
                suppressions.append(
                    ExtractionProtocol.parse_suppression(
                        {
                            "source_slugs": source_slugs,
                            "suppression_reason": "low_value",
                        }
                    )
                )
        for payload in explicit_suppression_payloads:
            suppression = ExtractionProtocol.parse_suppression(payload)
            catalog.require(suppression.source_slugs)
            suppressions.append(suppression)

        staged: list[MemoryCandidate] = []
        seen_drafts: dict[str, CandidateDraft] = {}
        with self.repository.transaction():
            for draft, provenance in drafts:
                dedupe_key = candidate_dedupe_key(
                    draft.source_slugs,
                    draft.statement,
                    protocol_name=PROTOCOL_NAME,
                    protocol_version=PROTOCOL_VERSION,
                )
                previous_draft = seen_drafts.get(dedupe_key)
                if previous_draft is not None:
                    if not self._same_classification_draft(previous_draft, draft):
                        raise CandidateRevisionRequiredError(
                            "batch contains one candidate identity with multiple classifications; "
                            "use an explicit review edit"
                        )
                    continue
                seen_drafts[dedupe_key] = draft

                existing = self.repository.get_candidate_by_dedupe_key(dedupe_key)
                if existing is None:
                    # This fallback also handles a legacy row whose key was
                    # migrated with a compatibility suffix.  It is still
                    # idempotent for the same protocol version, while a
                    # protocol upgrade gets a new explicit revision row.
                    same_semantic = self.repository.list_candidates_by_semantic_identity(
                        draft.source_slugs, draft.statement
                    )
                    existing = next(
                        (
                            candidate
                            for candidate in same_semantic
                            if candidate.protocol_name == PROTOCOL_NAME
                            and candidate.protocol_version == PROTOCOL_VERSION
                        ),
                        None,
                    )
                if existing is not None:
                    if not self._same_classification(existing, draft):
                        raise CandidateRevisionRequiredError(
                            "candidate identity already exists with a different classification; "
                            "use an explicit review edit"
                        )
                    if not self.repository.has_valid_candidate_provenance(existing):
                        raise CandidateReviewError(
                            "existing candidate lacks valid trusted staging provenance; "
                            "restage it through the flomo trust boundary"
                        )
                    staged.append(existing)
                    continue

                prior_candidates = self.repository.list_candidates_by_semantic_identity(
                    draft.source_slugs, draft.statement
                )
                prior_revision = next(
                    (
                        candidate
                        for candidate in prior_candidates
                        if candidate.protocol_name != PROTOCOL_NAME
                        or candidate.protocol_version != PROTOCOL_VERSION
                    ),
                    None,
                )
                now = _now()
                staged.append(
                    self.repository._create_candidate_from_trusted_staging(
                        MemoryCandidate(
                            id=None,
                            type=draft.type,
                            statement=draft.statement,
                            topic=draft.topic,
                            confidence=draft.confidence,
                            source_slugs=draft.source_slugs,
                            evidence_kind=draft.evidence_kind,
                            review_status="pending",
                            proposed_action=draft.proposed_action,
                            created_at=now,
                            updated_at=now,
                            temporal_scope=draft.temporal_scope,
                            observed_at=draft.observed_at,
                            last_supported_at=draft.last_supported_at,
                            evidence_refs=draft.evidence_refs,
                            dedupe_key=dedupe_key,
                            protocol_name=PROTOCOL_NAME,
                            protocol_version=PROTOCOL_VERSION,
                            revision_of_candidate_id=(
                                int(prior_revision.id)
                                if prior_revision is not None and prior_revision.id is not None
                                else None
                            ),
                            revision_reason=(
                                "protocol_upgrade" if prior_revision is not None else None
                            ),
                        ),
                        source_slugs=draft.source_slugs,
                        observed_at=provenance.observed_at,
                        last_supported_at=provenance.last_supported_at,
                        evidence_refs=provenance.evidence_refs,
                        _staging_token=_STAGING_WRITE_TOKEN,
                    )
                )
        return CandidateStagingResult(
            candidates=tuple(staged), suppressions=tuple(suppressions)
        )

    @staticmethod
    def _trusted_draft(
        payload: Mapping[str, object], catalog: TrustedEvidenceCatalog
    ) -> Optional[tuple[CandidateDraft, TrustedEvidenceProvenance]]:
        source_slugs = ExtractionProtocol.normalize_source_slugs(
            payload.get("source_slugs")
        )
        catalog.require(source_slugs)
        draft = ExtractionProtocol.parse_candidate(
            payload, require_pattern_evidence=False, allow_pattern=False
        )
        if draft is None:
            return None

        provenance = catalog.provenance(draft.source_slugs)
        catalog.assert_submitted_metadata(
            payload,
            draft.observed_at,
            draft.last_supported_at,
            draft.evidence_refs,
            provenance,
        )
        trusted_refs = [
            {
                "source_slug": evidence_ref.source_slug,
                "observed_at": evidence_ref.observed_at,
                "context": evidence_ref.context,
            }
            for evidence_ref in provenance.evidence_refs
        ]
        hydrated = ExtractionProtocol.validate_fields(
            candidate_type=draft.type,
            statement=draft.statement,
            topic=draft.topic,
            confidence=draft.confidence,
            source_slugs=draft.source_slugs,
            evidence_kind=draft.evidence_kind,
            proposed_action=draft.proposed_action,
            temporal_scope=draft.temporal_scope,
            observed_at=provenance.observed_at,
            last_supported_at=provenance.last_supported_at,
            evidence_refs=trusted_refs,
            require_pattern_evidence=True,
            allow_pattern=False,
        )
        if hydrated is None:
            raise CandidateReviewError("trusted candidate cannot be low-value")
        return hydrated, provenance

    @staticmethod
    def _same_classification(candidate: MemoryCandidate, draft: CandidateDraft) -> bool:
        return (
            candidate.type,
            candidate.topic,
            candidate.evidence_kind,
            candidate.temporal_scope,
        ) == (
            draft.type,
            draft.topic,
            draft.evidence_kind,
            draft.temporal_scope,
        )

    @staticmethod
    def _same_classification_draft(first: CandidateDraft, second: CandidateDraft) -> bool:
        return (
            first.type,
            first.topic,
            first.evidence_kind,
            first.temporal_scope,
        ) == (
            second.type,
            second.topic,
            second.evidence_kind,
            second.temporal_scope,
        )

    def get(self, candidate_id: int) -> Optional[MemoryCandidate]:
        return self.repository.get_candidate(candidate_id)

    def list(
        self,
        *,
        review_status: Optional[str] = None,
        candidate_type: Optional[str] = None,
        proposed_action: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[MemoryCandidate]:
        if review_status is not None and review_status not in CANDIDATE_REVIEW_STATUSES:
            raise CandidateReviewError("invalid review_status")
        if proposed_action is not None and proposed_action not in REVIEW_ACTIONS:
            raise CandidateReviewError("invalid proposed_action")
        return self.repository.list_candidates(
            review_status=review_status,
            candidate_type=candidate_type,
            proposed_action=proposed_action,
            limit=limit,
        )

    def delete(self, candidate_id: int) -> bool:
        return self.repository.delete_candidate(candidate_id)


class CandidateReviewService:
    """Apply an explicitly supplied batch of review actions.

    Only this service can promote a candidate into ``memory_fragments``.
    ``user_profile`` and ``current_memory`` are deliberately not touched by
    V2 review actions.
    """

    _FINAL_STATUSES = frozenset({"accepted", "rejected", "superseded"})
    _EDIT_FIELDS = (
        "edited_type",
        "edited_statement",
        "edited_topic",
        "edited_confidence",
        "edited_source_slugs",
        "edited_evidence_kind",
        "edited_proposed_action",
        "edited_temporal_scope",
        "edited_observed_at",
        "edited_last_supported_at",
        "edited_evidence_refs",
    )

    def __init__(
        self,
        repository: MemoryRepository,
        evidence_catalog: TrustedEvidenceCatalog | None = None,
    ) -> None:
        self.repository = repository
        self.memory_service = MemoryService(repository)
        self.evidence_catalog = evidence_catalog

    def review_batch(self, reviews: Iterable[CandidateReview]) -> BatchReviewResult:
        """Review many candidates in one request; no per-item prompt is needed."""

        review_list = tuple(reviews)
        if not review_list:
            return BatchReviewResult(results=())

        plans: list[
            tuple[CandidateReview, MemoryCandidate, Optional[MemoryCandidate], Optional[MemoryFragment]]
        ] = []
        seen_ids: set[int] = set()
        for review in review_list:
            self._validate_review_shape(review, seen_ids)
            candidate = self.repository.get_candidate(review.candidate_id)
            if candidate is None:
                raise CandidateReviewError(f"candidate not found: {review.candidate_id}")
            if candidate.review_status in self._FINAL_STATUSES:
                raise CandidateReviewError("candidate has already reached a final review status")

            edited_candidate: Optional[MemoryCandidate] = None
            superseded_fragment = None
            if review.action == "edit":
                edited_candidate = self._edited_candidate(candidate, review)
            elif review.action == "supersede":
                if review.supersedes_fragment_id is None:
                    raise CandidateReviewError("supersede requires supersedes_fragment_id")
                superseded_fragment = self.repository.get_fragment(review.supersedes_fragment_id)
                if superseded_fragment is None:
                    raise CandidateReviewError(
                        f"fragment not found: {review.supersedes_fragment_id}"
                    )
                if superseded_fragment.status not in {"active", "unresolved"}:
                    raise CandidateReviewError("only active or unresolved fragments can be superseded")
            plans.append((review, candidate, edited_candidate, superseded_fragment))

        results: list[CandidateReviewResult] = []
        with self.repository.transaction():
            for review, candidate, edited_candidate, superseded_fragment in plans:
                now = _now()
                if review.action == "edit":
                    assert edited_candidate is not None
                    saved = self.repository.update_candidate(
                        replace(
                            edited_candidate,
                            review_status="edited",
                            review_note=_note(review.review_note) or candidate.review_note,
                            reviewed_at=now,
                            updated_at=now,
                        ),
                        _provenance_token=_STAGING_WRITE_TOKEN,
                    )
                    if saved is None:
                        raise CandidateReviewError(f"candidate disappeared: {candidate.id}")
                    results.append(
                        CandidateReviewResult(
                            candidate_id=int(candidate.id), action="edit", review_status="edited"
                        )
                    )
                    continue

                if review.action in {"reject", "unresolved"}:
                    review_status = {"reject": "rejected", "unresolved": "unresolved"}[review.action]
                    saved = self.repository.update_candidate(
                        replace(
                            candidate,
                            review_status=review_status,
                            proposed_action=review.action,
                            review_note=_note(review.review_note) or candidate.review_note,
                            reviewed_at=now,
                            updated_at=now,
                        )
                    )
                    if saved is None:
                        raise CandidateReviewError(f"candidate disappeared: {candidate.id}")
                    results.append(
                        CandidateReviewResult(
                            candidate_id=int(candidate.id),
                            action=review.action,
                            review_status=saved.review_status,
                        )
                    )
                    continue

                self._assert_promotable_candidate(candidate)

                fragment = self.repository.find_fragment_by_identity(
                    candidate.source_slugs, candidate.statement
                )
                activate_fragment = False
                if fragment is None:
                    fragment = self.memory_service.create_fragment(
                        fragment_type=candidate.type,
                        statement=candidate.statement,
                        topic=candidate.topic,
                        confidence=candidate.confidence,
                        source_slugs=candidate.source_slugs,
                        valid_from=candidate.observed_at,
                        last_supported_at=candidate.last_supported_at,
                        temporal_scope=candidate.temporal_scope,
                        observed_at=candidate.observed_at,
                        status="unresolved",
                        _promotion_token=_FORMAL_WRITE_TOKEN,
                    )
                    activate_fragment = True
                elif (
                    review.action == "supersede"
                    and superseded_fragment is not None
                    and fragment.id == superseded_fragment.id
                ):
                    raise CandidateReviewError(
                        "a supersede candidate cannot reuse the fragment it supersedes"
                    )
                elif fragment.status == "unresolved":
                    activate_fragment = True

                if review.action == "supersede":
                    assert superseded_fragment is not None
                    updated_superseded = self.memory_service.update_fragment(
                        replace(superseded_fragment, status="superseded"),
                        _promotion_token=_FORMAL_WRITE_TOKEN,
                    )
                    if updated_superseded is None:
                        raise CandidateReviewError("fragment to supersede disappeared")

                final_status = "accepted" if review.action == "accept" else "superseded"
                saved = self.repository.update_candidate(
                    replace(
                        candidate,
                        review_status=final_status,
                        proposed_action=review.action,
                        review_note=_note(review.review_note) or candidate.review_note,
                        reviewed_at=now,
                        updated_at=now,
                    )
                )
                if saved is None:
                    raise CandidateReviewError(f"candidate disappeared: {candidate.id}")
                self.repository.create_promotion_lineage(
                    PromotionLineage(
                        id=None,
                        candidate_id=int(candidate.id),
                        promoted_fragment_id=int(fragment.id),
                        action=review.action,
                        superseded_fragment_id=(
                            int(superseded_fragment.id)
                            if superseded_fragment is not None
                            else None
                        ),
                        protocol_name=candidate.protocol_name,
                        protocol_version=candidate.protocol_version,
                        created_at=now,
                    ),
                    _review_token=_FORMAL_WRITE_TOKEN,
                )
                if activate_fragment:
                    activated = self.memory_service.update_fragment(
                        replace(fragment, status="active"),
                        _promotion_token=_FORMAL_WRITE_TOKEN,
                    )
                    if activated is None:
                        raise CandidateReviewError("promoted fragment disappeared")
                    fragment = activated
                results.append(
                    CandidateReviewResult(
                        candidate_id=int(candidate.id),
                        action=review.action,
                        review_status=final_status,
                        promoted_fragment_id=fragment.id,
                    )
                )

        return BatchReviewResult(results=tuple(results))

    def _assert_promotable_candidate(self, candidate: MemoryCandidate) -> None:
        """Revalidate durable trust and promotion-specific constraints."""

        if not self.repository.has_valid_candidate_provenance(candidate):
            raise CandidateReviewError(
                "candidate lacks valid flomo trusted staging provenance"
            )
        if not candidate.observed_at or not candidate.last_supported_at:
            raise CandidateReviewError(
                "candidate lacks trusted source timestamps; restage it"
            )
        if not candidate.evidence_refs:
            raise CandidateReviewError(
                "candidate lacks trusted evidence_refs; restage it"
            )
        try:
            ExtractionProtocol.validate_fields(
                candidate_type=candidate.type,
                statement=candidate.statement,
                topic=candidate.topic,
                confidence=candidate.confidence,
                source_slugs=candidate.source_slugs,
                evidence_kind=candidate.evidence_kind,
                proposed_action=candidate.proposed_action,
                temporal_scope=candidate.temporal_scope,
                observed_at=candidate.observed_at,
                last_supported_at=candidate.last_supported_at,
                evidence_refs=[
                    {
                        "source_slug": ref.source_slug,
                        "observed_at": ref.observed_at,
                        "context": ref.context,
                    }
                    for ref in candidate.evidence_refs
                ],
                require_pattern_evidence=True,
                allow_pattern=True,
            )
        except ExtractionProtocolError as exc:
            raise CandidateReviewError(
                "candidate does not satisfy promotion constraints"
            ) from exc

    def _validate_review_shape(self, review: CandidateReview, seen_ids: set[int]) -> None:
        if not isinstance(review.candidate_id, int) or isinstance(review.candidate_id, bool):
            raise CandidateReviewError("candidate_id must be an integer")
        if review.candidate_id < 1 or review.candidate_id in seen_ids:
            raise CandidateReviewError("candidate_id must be positive and unique in a batch")
        seen_ids.add(review.candidate_id)
        if review.action not in REVIEW_ACTIONS:
            raise CandidateReviewError("invalid review action")
        _note(review.review_note)

        has_edit_fields = any(getattr(review, field) is not None for field in self._EDIT_FIELDS)
        if review.action == "edit" and not has_edit_fields:
            raise CandidateReviewError("edit requires at least one edited field")
        if review.action != "edit" and has_edit_fields:
            raise CandidateReviewError("edited fields are only valid for the edit action")
        if review.action != "supersede" and review.supersedes_fragment_id is not None:
            raise CandidateReviewError("supersedes_fragment_id is only valid for supersede")

    def _edited_candidate(
        self, candidate: MemoryCandidate, review: CandidateReview
    ) -> MemoryCandidate:
        source_slugs = (
            review.edited_source_slugs
            if review.edited_source_slugs is not None
            else candidate.source_slugs
        )
        evidence_refs = (
            review.edited_evidence_refs
            if review.edited_evidence_refs is not None
            else candidate.evidence_refs
        )
        observed_at = (
            review.edited_observed_at
            if review.edited_observed_at is not None
            else candidate.observed_at
        )
        last_supported_at = (
            review.edited_last_supported_at
            if review.edited_last_supported_at is not None
            else candidate.last_supported_at
        )
        metadata_changed = any(
            value is not None
            for value in (
                review.edited_source_slugs,
                review.edited_observed_at,
                review.edited_last_supported_at,
                review.edited_evidence_refs,
            )
        )
        if metadata_changed:
            if self.evidence_catalog is None or not self.evidence_catalog.is_flomo_api_catalog:
                raise CandidateReviewError(
                    "edited source metadata requires a catalog built from a flomo API response"
                )
            normalized_sources = ExtractionProtocol.normalize_source_slugs(source_slugs)
            provenance = self.evidence_catalog.provenance(normalized_sources)
            submitted_payload = {
                "observed_at": review.edited_observed_at or "",
                "last_supported_at": review.edited_last_supported_at or "",
                "evidence_refs": (
                    [
                        {
                            "source_slug": ref.source_slug,
                            "observed_at": ref.observed_at,
                            "context": ref.context,
                        }
                        for ref in (review.edited_evidence_refs or ())
                    ]
                    if review.edited_evidence_refs is not None
                    else []
                ),
            }
            self.evidence_catalog.assert_submitted_metadata(
                submitted_payload,
                observed_at,
                last_supported_at,
                tuple(evidence_refs),
                provenance,
            )
            source_slugs = normalized_sources
            observed_at = provenance.observed_at
            last_supported_at = provenance.last_supported_at
            evidence_refs = provenance.evidence_refs

        draft = ExtractionProtocol.validate_fields(
            candidate_type=review.edited_type if review.edited_type is not None else candidate.type,
            statement=(
                review.edited_statement
                if review.edited_statement is not None
                else candidate.statement
            ),
            topic=review.edited_topic if review.edited_topic is not None else candidate.topic,
            confidence=(
                review.edited_confidence
                if review.edited_confidence is not None
                else candidate.confidence
            ),
            source_slugs=source_slugs,
            evidence_kind=(
                review.edited_evidence_kind
                if review.edited_evidence_kind is not None
                else candidate.evidence_kind
            ),
            proposed_action=(
                review.edited_proposed_action
                if review.edited_proposed_action is not None
                else candidate.proposed_action
            ),
            temporal_scope=(
                review.edited_temporal_scope
                if review.edited_temporal_scope is not None
                else candidate.temporal_scope
            ),
            observed_at=observed_at,
            last_supported_at=last_supported_at,
            evidence_refs=(
                [
                    {
                        "source_slug": ref.source_slug,
                        "observed_at": ref.observed_at,
                        "context": ref.context,
                    }
                    for ref in evidence_refs
                ]
            ),
            allow_pattern=candidate.type == "pattern",
        )
        if draft is None:
            raise CandidateReviewError("edited candidate cannot be low-value")
        return replace(
            candidate,
            type=draft.type,
            statement=draft.statement,
            topic=draft.topic,
            confidence=draft.confidence,
            source_slugs=draft.source_slugs,
            evidence_kind=draft.evidence_kind,
            proposed_action=draft.proposed_action,
            temporal_scope=draft.temporal_scope,
            observed_at=draft.observed_at,
            last_supported_at=draft.last_supported_at,
            evidence_refs=draft.evidence_refs,
            dedupe_key=candidate_dedupe_key(
                draft.source_slugs,
                draft.statement,
                protocol_name=candidate.protocol_name,
                protocol_version=candidate.protocol_version,
            ),
            revision_reason=(
                "manual_reclassification"
                if draft.type != candidate.type
                else candidate.revision_reason
            ),
        )
