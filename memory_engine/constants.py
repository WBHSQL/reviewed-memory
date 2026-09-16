"""Shared controlled vocabularies for the local Memory Engine."""

FRAGMENT_TYPES = frozenset(
    {
        "fact",
        "preference",
        "value",
        "goal",
        "project",
        "belief",
        "decision",
        "experience",
        "pattern",
    }
)
FRAGMENT_STATUSES = frozenset({"active", "superseded", "unresolved"})
TEMPORAL_SCOPES = frozenset({"stable", "current", "episodic"})

# Bounds apply to all derived text persisted by the engine. They are deliberately
# finite so an extractor cannot accidentally copy an entire memo into a derived
# field.
MAX_STATEMENT_LENGTH = 1000
MAX_TOPIC_LENGTH = 200
MAX_EVIDENCE_CONTEXT_LENGTH = 300
MAX_REVIEW_NOTE_LENGTH = 500
MAX_SOURCE_SLUG_LENGTH = 200
MAX_TAG_LENGTH = 100
MAX_REVISION_REASON_LENGTH = 100
MAX_TRUSTED_STAGING_PROOF_LENGTH = 128
# These markers are intentionally narrow.  A generic fact remains episodic
# unless its source-faithful statement explicitly describes an ongoing state.
CURRENT_STATE_MARKERS = (
    "当前",
    "目前",
    "现在",
    "现阶段",
    "正在",
    "持续",
    "仍然",
    "依然",
    "截至目前",
    "currently",
    "ongoing",
    "still",
    "as of now",
    "at present",
    "right now",
)
CURRENT_MEMORY_TYPES = frozenset({"project", "goal", "focus", "decision"})
CURRENT_MEMORY_STATUSES = FRAGMENT_STATUSES

EVIDENCE_KINDS = frozenset(
    {
        "explicit",
        "repeated",
        "corroborated",
        "experience",
        "transient_emotion",
        "contradictory",
        "insufficient",
        "low_value",
        "method_endorsement",
    }
)
SUPPRESSION_REASONS = frozenset(
    {
        "low_value",
        "transient",
        "pure_external_content",
        "insufficient_personal_signal",
        "duplicate",
        "no_extractable_memory",
    }
)
CANDIDATE_REVIEW_STATUSES = frozenset(
    {"pending", "accepted", "rejected", "edited", "superseded", "unresolved"}
)
REVIEW_ACTIONS = frozenset({"accept", "reject", "edit", "supersede", "unresolved"})
TRUSTED_STAGING_ORIGINS = frozenset(
    {"flomo_trusted_staging", "conversation_trusted_staging"}
)
OPEN_LOOP_STATUSES = frozenset({"open", "resolved", "dismissed"})
