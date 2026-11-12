# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Ordered SQL schema migrations.

Never edit an applied migration. Append a new ``(version, description, sql)``
tuple and raise :data:`MAX_VERSION` accordingly. The FTS table is maintained by
the knowledge layer, not by SQLite triggers.
"""

from __future__ import annotations

MIGRATIONS: list[tuple[int, str, str]] = [
    (
        1,
        "initial schema: identity, knowledge, evidence, review, audit, FTS5",
        """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE namespaces (
    name TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    description TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE agents (
    agent_id TEXT PRIMARY KEY,
    root_agent_id TEXT NOT NULL,
    parent_agent_id TEXT,
    label TEXT,
    model TEXT,
    provider TEXT,
    task TEXT,
    enrollment_mode TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    root_agent_id TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    actor_handle TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    idle_expires_at TEXT NOT NULL,
    absolute_expires_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE grants (
    grant_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    namespace TEXT NOT NULL,
    role TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    revoked_at TEXT
);

CREATE TABLE delegation_tickets (
    ticket_id TEXT PRIMARY KEY,
    parent_agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    root_agent_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT
);

CREATE TABLE mentor_requests (
    request_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    namespace TEXT NOT NULL,
    code TEXT NOT NULL,
    scopes TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT
);

CREATE TABLE entries (
    entry_id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL REFERENCES namespaces(name),
    created_at TEXT NOT NULL,
    created_by_agent_id TEXT NOT NULL,
    current_revision_id TEXT
);

CREATE TABLE entry_revisions (
    revision_id TEXT PRIMARY KEY,
    entry_id TEXT NOT NULL REFERENCES entries(entry_id) ON DELETE RESTRICT,
    prev_revision_id TEXT REFERENCES entry_revisions(revision_id),
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    claim TEXT NOT NULL,
    conditions TEXT,
    expected_result TEXT,
    limitations TEXT,
    tags TEXT NOT NULL DEFAULT '[]',
    scope TEXT NOT NULL DEFAULT '{}',
    observed_at TEXT NOT NULL,
    valid_from TEXT,
    valid_until TEXT,
    last_verified_at TEXT,
    author_agent_id TEXT NOT NULL,
    author_root_id TEXT NOT NULL,
    contributors TEXT NOT NULL DEFAULT '[]',
    source_role TEXT NOT NULL,
    evidence_class TEXT NOT NULL,
    sensitivity TEXT NOT NULL DEFAULT 'normal',
    lifecycle TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE sources (
    source_id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL REFERENCES entry_revisions(revision_id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    role TEXT NOT NULL,
    identity TEXT,
    uri TEXT,
    snapshot_hash TEXT,
    trust TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE evidence_objects (
    evidence_id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    size_bytes INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    redacted INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    revision_id TEXT REFERENCES entry_revisions(revision_id) ON DELETE SET NULL
);

CREATE TABLE provenance_edges (
    edge_id TEXT PRIMARY KEY,
    from_revision_id TEXT NOT NULL REFERENCES entry_revisions(revision_id) ON DELETE CASCADE,
    to_revision_id TEXT NOT NULL REFERENCES entry_revisions(revision_id) ON DELETE CASCADE,
    relation TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE reviews (
    review_id TEXT PRIMARY KEY,
    entry_id TEXT NOT NULL REFERENCES entries(entry_id) ON DELETE CASCADE,
    revision_id TEXT NOT NULL REFERENCES entry_revisions(revision_id) ON DELETE CASCADE,
    reviewer_agent_id TEXT NOT NULL,
    reviewer_root_id TEXT NOT NULL,
    result TEXT NOT NULL,
    method TEXT NOT NULL,
    evidence TEXT,
    conditions TEXT,
    limitations TEXT,
    lease_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE review_jobs (
    job_id TEXT PRIMARY KEY,
    entry_id TEXT NOT NULL REFERENCES entries(entry_id) ON DELETE CASCADE,
    revision_id TEXT NOT NULL REFERENCES entry_revisions(revision_id) ON DELETE CASCADE,
    state TEXT NOT NULL,
    priority REAL NOT NULL,
    created_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_id TEXT,
    lease_expires_at TEXT
);

CREATE TABLE task_outcomes (
    outcome_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    session_id TEXT NOT NULL,
    task_label TEXT,
    outcome TEXT NOT NULL,
    revision_ids TEXT NOT NULL DEFAULT '[]',
    notes TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor_agent_id TEXT,
    action TEXT NOT NULL,
    object_type TEXT,
    object_id TEXT,
    details TEXT
);

CREATE TABLE idempotency_records (
    key TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    response TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE deletion_ledger (
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    deleted_at TEXT NOT NULL,
    reason TEXT,
    PRIMARY KEY (object_type, object_id)
);

CREATE INDEX idx_entries_namespace_current ON entries(namespace, current_revision_id);
CREATE INDEX idx_entries_namespace ON entries(namespace);
CREATE INDEX idx_sessions_token_hash ON sessions(token_hash);
CREATE INDEX idx_grants_agent_namespace ON grants(agent_id, namespace);
CREATE UNIQUE INDEX idx_grants_live_unique ON grants(agent_id, namespace, role) WHERE revoked_at IS NULL;
CREATE INDEX idx_mentor_requests_session_state ON mentor_requests(session_id, state);
CREATE INDEX idx_entry_revisions_entry_created ON entry_revisions(entry_id, created_at);
CREATE INDEX idx_reviews_revision ON reviews(revision_id);
CREATE INDEX idx_review_jobs_state_priority ON review_jobs(state, priority DESC, created_at);
CREATE INDEX idx_sources_revision ON sources(revision_id);
CREATE INDEX idx_provenance_from ON provenance_edges(from_revision_id);
CREATE INDEX idx_evidence_revision ON evidence_objects(revision_id);

CREATE VIRTUAL TABLE entry_fts USING fts5(
    entry_id UNINDEXED,
    revision_id UNINDEXED,
    title,
    claim,
    conditions,
    tags,
    tokenize='unicode61 remove_diacritics 2'
);
""",
    ),
    (
        2,
        "optional vector generations, derived index state, extraction proposals",
        """
CREATE TABLE index_generations (
    generation_id TEXT PRIMARY KEY,
    backend TEXT NOT NULL,
    model TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('building', 'ready', 'stale', 'failed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (backend, model, dimensions, state)
);

CREATE TABLE entry_index_state (
    generation_id TEXT NOT NULL REFERENCES index_generations(generation_id) ON DELETE CASCADE,
    revision_id TEXT NOT NULL REFERENCES entry_revisions(revision_id) ON DELETE CASCADE,
    backend TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    indexed_at TEXT NOT NULL,
    PRIMARY KEY (generation_id, revision_id)
);

CREATE TABLE extraction_proposals (
    proposal_id TEXT PRIMARY KEY,
    entry_id TEXT NOT NULL REFERENCES entries(entry_id) ON DELETE CASCADE,
    revision_id TEXT NOT NULL REFERENCES entry_revisions(revision_id) ON DELETE CASCADE,
    extractor TEXT NOT NULL,
    relation TEXT,
    subject TEXT,
    object TEXT,
    detail TEXT,
    state TEXT NOT NULL DEFAULT 'proposed',
    created_at TEXT NOT NULL
);

CREATE INDEX idx_index_generations_backend_state ON index_generations(backend, state);
CREATE INDEX idx_entry_index_state_revision ON entry_index_state(revision_id);
CREATE INDEX idx_extraction_proposals_entry_state ON extraction_proposals(entry_id, state);
""",
    ),
]

MAX_VERSION: int = max(version for version, _, _ in MIGRATIONS)

__all__ = ["MAX_VERSION", "MIGRATIONS"]
