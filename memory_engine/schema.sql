PRAGMA user_version = 6;

CREATE TABLE IF NOT EXISTS memory_fragments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL CHECK (type IN (
        'fact', 'preference', 'value', 'goal', 'project',
        'belief', 'decision', 'experience', 'pattern'
    )),
    statement TEXT NOT NULL CHECK (length(trim(statement)) > 0 AND length(statement) <= 1000),
    topic TEXT NOT NULL DEFAULT '' CHECK (length(topic) <= 200),
    confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    status TEXT NOT NULL DEFAULT 'active' CHECK (
        status IN ('active', 'superseded', 'unresolved')
    ),
    valid_from TEXT NOT NULL,
    last_supported_at TEXT NOT NULL,
    source_slugs TEXT NOT NULL CHECK (length(trim(source_slugs)) > 2),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    temporal_scope TEXT NOT NULL DEFAULT 'current' CHECK (
        temporal_scope IN ('stable', 'current', 'episodic')
    ),
    observed_at TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_memory_fragments_status
    ON memory_fragments(status);
CREATE INDEX IF NOT EXISTS idx_memory_fragments_type_topic
    ON memory_fragments(type, topic);

CREATE TABLE IF NOT EXISTS user_profile (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attribute TEXT NOT NULL UNIQUE,
    value TEXT NOT NULL CHECK (length(trim(value)) > 0 AND length(value) <= 1000),
    topic TEXT NOT NULL DEFAULT '' CHECK (length(topic) <= 200),
    confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    status TEXT NOT NULL DEFAULT 'active' CHECK (
        status IN ('active', 'superseded', 'unresolved')
    ),
    source_slugs TEXT NOT NULL CHECK (length(trim(source_slugs)) > 2),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_user_profile_status
    ON user_profile(status);

CREATE TABLE IF NOT EXISTS current_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL CHECK (type IN ('project', 'goal', 'focus', 'decision')),
    statement TEXT NOT NULL CHECK (length(trim(statement)) > 0 AND length(statement) <= 1000),
    topic TEXT NOT NULL DEFAULT '' CHECK (length(topic) <= 200),
    status TEXT NOT NULL DEFAULT 'active' CHECK (
        status IN ('active', 'superseded', 'unresolved')
    ),
    priority INTEGER NOT NULL DEFAULT 0 CHECK (priority >= 0),
    source_slugs TEXT NOT NULL CHECK (length(trim(source_slugs)) > 2),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_current_memory_type_status
    ON current_memory(type, status);

CREATE TABLE IF NOT EXISTS open_loops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    statement TEXT NOT NULL CHECK (length(trim(statement)) > 0 AND length(statement) <= 1000),
    topic TEXT NOT NULL DEFAULT '' CHECK (length(topic) <= 200),
    status TEXT NOT NULL DEFAULT 'open' CHECK (
        status IN ('open', 'resolved', 'dismissed')
    ),
    source_slugs TEXT NOT NULL CHECK (length(trim(source_slugs)) > 2),
    last_reviewed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_open_loops_status_topic
    ON open_loops(status, topic);

CREATE TABLE IF NOT EXISTS memory_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL CHECK (type IN (
        'fact', 'preference', 'value', 'goal', 'project',
        'belief', 'decision', 'experience', 'pattern'
    )),
    statement TEXT NOT NULL CHECK (length(trim(statement)) > 0 AND length(statement) <= 1000),
    topic TEXT NOT NULL DEFAULT '' CHECK (length(topic) <= 200),
    confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    source_slugs TEXT NOT NULL CHECK (length(trim(source_slugs)) > 2),
    evidence_kind TEXT NOT NULL CHECK (evidence_kind IN (
        'explicit', 'repeated', 'corroborated', 'experience',
        'transient_emotion', 'contradictory', 'insufficient', 'low_value',
        'method_endorsement'
    )),
    review_status TEXT NOT NULL DEFAULT 'pending' CHECK (
        review_status IN ('pending', 'accepted', 'rejected', 'edited', 'superseded', 'unresolved')
    ),
    proposed_action TEXT NOT NULL CHECK (
        proposed_action IN ('accept', 'reject', 'edit', 'supersede', 'unresolved')
    ),
    temporal_scope TEXT NOT NULL DEFAULT 'current' CHECK (
        temporal_scope IN ('stable', 'current', 'episodic')
    ),
    observed_at TEXT NOT NULL DEFAULT '',
    last_supported_at TEXT NOT NULL DEFAULT '',
    evidence_refs TEXT NOT NULL DEFAULT '[]',
    dedupe_key TEXT NOT NULL,
    protocol_name TEXT NOT NULL DEFAULT 'flomo-memory-extraction',
    protocol_version TEXT NOT NULL DEFAULT '2.3',
    revision_of_candidate_id INTEGER REFERENCES memory_candidates(id) ON DELETE RESTRICT,
    revision_reason TEXT CHECK (revision_reason IS NULL OR length(revision_reason) <= 100),
    trusted_staging_proof TEXT CHECK (
        trusted_staging_proof IS NULL OR length(trim(trusted_staging_proof)) BETWEEN 1 AND 128
    ),
    review_note TEXT CHECK (review_note IS NULL OR length(review_note) <= 500),
    reviewed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memory_candidates_review_status
    ON memory_candidates(review_status);
CREATE INDEX IF NOT EXISTS idx_memory_candidates_type_topic
    ON memory_candidates(type, topic);

-- This row is a durable, body-free proof issued by the production flomo
-- staging boundary. A candidate without it may remain in staging but cannot
-- pass the promotion gate.
CREATE TABLE IF NOT EXISTS candidate_provenance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL UNIQUE REFERENCES memory_candidates(id) ON DELETE CASCADE,
    proof_marker TEXT NOT NULL UNIQUE CHECK (
        length(trim(proof_marker)) BETWEEN 1 AND 128
    ),
    origin TEXT NOT NULL CHECK (origin = 'flomo_trusted_staging'),
    source_slugs TEXT NOT NULL CHECK (length(trim(source_slugs)) > 2),
    observed_at TEXT NOT NULL,
    last_supported_at TEXT NOT NULL,
    evidence_refs TEXT NOT NULL DEFAULT '[]',
    protocol_name TEXT NOT NULL,
    protocol_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_candidate_provenance_origin
    ON candidate_provenance(origin);

CREATE TABLE IF NOT EXISTS promotion_lineage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL UNIQUE REFERENCES memory_candidates(id) ON DELETE RESTRICT,
    promoted_fragment_id INTEGER NOT NULL REFERENCES memory_fragments(id) ON DELETE RESTRICT,
    action TEXT NOT NULL CHECK (action IN ('accept', 'supersede')),
    superseded_fragment_id INTEGER REFERENCES memory_fragments(id) ON DELETE RESTRICT,
    protocol_name TEXT NOT NULL,
    protocol_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK (
        (action = 'accept' AND superseded_fragment_id IS NULL)
        OR (action = 'supersede' AND superseded_fragment_id IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_promotion_lineage_promoted_fragment
    ON promotion_lineage(promoted_fragment_id);
CREATE INDEX IF NOT EXISTS idx_promotion_lineage_superseded_fragment
    ON promotion_lineage(superseded_fragment_id);

-- Promotion inserts a fragment as unresolved, creates lineage, then activates
-- it in the same transaction. These triggers prevent active rows from being
-- manufactured directly through SQLite while preserving that controlled path.
CREATE TRIGGER IF NOT EXISTS enforce_active_fragment_lineage_insert
BEFORE INSERT ON memory_fragments
WHEN NEW.status = 'active'
BEGIN
    SELECT RAISE(ABORT, 'active fragment requires promotion lineage');
END;

CREATE TRIGGER IF NOT EXISTS enforce_active_fragment_lineage_update
BEFORE UPDATE OF status ON memory_fragments
WHEN NEW.status = 'active'
 AND NOT EXISTS (
     SELECT 1 FROM promotion_lineage
     WHERE promoted_fragment_id = NEW.id
 )
BEGIN
    SELECT RAISE(ABORT, 'active fragment requires promotion lineage');
END;
