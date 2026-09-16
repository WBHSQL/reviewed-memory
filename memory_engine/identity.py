"""Deterministic identities for derived candidates."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Iterable


def canonical_statement(statement: str) -> str:
    """Normalize only formatting, not meaning, before deduplication."""

    normalized = unicodedata.normalize("NFKC", statement).strip().casefold()
    return re.sub(r"\s+", " ", normalized)


def candidate_semantic_key(source_slugs: Iterable[str], statement: str) -> str:
    """Return the version-independent identity of one source-backed proposition."""

    sources = sorted({str(slug).strip() for slug in source_slugs})
    payload = json.dumps(
        {"source_slugs": sources, "statement": canonical_statement(statement)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def candidate_dedupe_key(
    source_slugs: Iterable[str],
    statement: str,
    *,
    protocol_name: str = "flomo-memory-extraction",
    protocol_version: str = "2.3",
) -> str:
    """Return a stable key for one protocol-versioned candidate identity."""

    payload = json.dumps(
        {
            "protocol_name": str(protocol_name).strip(),
            "protocol_version": str(protocol_version).strip(),
            "semantic_key": candidate_semantic_key(source_slugs, statement),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
