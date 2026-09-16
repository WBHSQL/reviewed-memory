"""Conservative V0 profile synthesis from reviewed memory fragments only."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from .models import MemoryFragment, SourceSlugs
from .repository import MemoryRepository


class ProfileBuilderError(ValueError):
    """Raised when a proposed profile synthesis is not evidence-grounded."""


_PROFILE_STATES = frozenset({"tentative", "supported", "conflicted"})
_SPEC_FIELDS = frozenset(
    {"attribute", "value", "topic", "state", "supporting_fragment_ids"}
)


@dataclass(frozen=True)
class ProfileCandidateV0:
    """Review-only profile proposal; never writes ``user_profile``."""

    attribute: str
    value: str
    topic: str
    state: str
    supporting_fragment_ids: tuple[int, ...]
    source_slugs: SourceSlugs
    source_count: int
    first_observed_at: str
    last_supported_at: str
    supporting_types: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "attribute": self.attribute,
            "value": self.value,
            "topic": self.topic,
            "state": self.state,
            "supporting_fragment_ids": list(self.supporting_fragment_ids),
            "source_slugs": list(self.source_slugs),
            "source_count": self.source_count,
            "first_observed_at": self.first_observed_at,
            "last_supported_at": self.last_supported_at,
            "supporting_types": list(self.supporting_types),
        }


def _required_text(value: object, field: str, *, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProfileBuilderError(f"{field} is required")
    normalized = value.strip()
    if len(normalized) > limit:
        raise ProfileBuilderError(f"{field} exceeds the maximum length")
    return normalized


class ProfileBuilderV0:
    """Build review-only profile candidates from active reviewed fragments."""

    def __init__(self, repository: MemoryRepository) -> None:
        self.repository = repository

    def build_from_specs(
        self, specs: Iterable[Mapping[str, object]]
    ) -> tuple[ProfileCandidateV0, ...]:
        fragments = {
            int(fragment.id): fragment
            for fragment in self.repository.list_fragments(status="active")
            if fragment.id is not None
        }
        candidates: list[ProfileCandidateV0] = []
        seen_attributes: set[str] = set()

        for spec in specs:
            unknown = set(spec) - _SPEC_FIELDS
            if unknown:
                raise ProfileBuilderError(
                    f"profile spec contains unsupported fields: {sorted(unknown)}"
                )
            attribute = _required_text(spec.get("attribute"), "attribute", limit=200)
            value = _required_text(spec.get("value"), "value", limit=1000)
            topic = _required_text(spec.get("topic"), "topic", limit=200)
            state = _required_text(spec.get("state"), "state", limit=20)
            if state not in _PROFILE_STATES:
                raise ProfileBuilderError("invalid profile candidate state")
            if attribute in seen_attributes:
                raise ProfileBuilderError("duplicate profile attribute in one build")
            seen_attributes.add(attribute)

            raw_ids = spec.get("supporting_fragment_ids")
            if isinstance(raw_ids, (str, bytes)) or not isinstance(raw_ids, (list, tuple)):
                raise ProfileBuilderError("supporting_fragment_ids must be a list")
            if not raw_ids:
                raise ProfileBuilderError("profile candidate requires supporting fragments")
            if any(not isinstance(item, int) or isinstance(item, bool) for item in raw_ids):
                raise ProfileBuilderError("supporting fragment ids must be integers")
            ids = tuple(dict.fromkeys(int(item) for item in raw_ids))
            if len(ids) != len(raw_ids):
                raise ProfileBuilderError("supporting fragment ids must be unique")

            missing = [fragment_id for fragment_id in ids if fragment_id not in fragments]
            if missing:
                raise ProfileBuilderError(
                    f"supporting fragments must exist and be active: {missing}"
                )
            supporting = tuple(fragments[fragment_id] for fragment_id in ids)
            if len(supporting) == 1 and supporting[0].type in {"experience", "decision"}:
                raise ProfileBuilderError(
                    "one experience/decision fragment cannot become a profile-level claim"
                )

            source_slugs = tuple(
                dict.fromkeys(
                    slug
                    for fragment in supporting
                    for slug in fragment.source_slugs
                )
            )
            if state == "supported" and len(source_slugs) < 2:
                raise ProfileBuilderError(
                    "supported profile candidate requires two independent sources"
                )

            observed_values = [fragment.observed_at for fragment in supporting]
            supported_values = [fragment.last_supported_at for fragment in supporting]
            supporting_types = tuple(sorted({fragment.type for fragment in supporting}))
            candidates.append(
                ProfileCandidateV0(
                    attribute=attribute,
                    value=value,
                    topic=topic,
                    state=state,
                    supporting_fragment_ids=ids,
                    source_slugs=source_slugs,
                    source_count=len(source_slugs),
                    first_observed_at=min(observed_values),
                    last_supported_at=max(supported_values),
                    supporting_types=supporting_types,
                )
            )

        return tuple(candidates)
