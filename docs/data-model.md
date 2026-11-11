<!--
SPDX-FileCopyrightText: 2026 René Widera
SPDX-License-Identifier: ISC
-->

# Data model

Schema version 1. Snake-case tables, TEXT primary keys, ISO-8601 UTC
timestamps. The FTS table is maintained by the knowledge layer, not by
triggers. A new schema change is a new ordered migration; applied migrations
are never edited.

## Tables

| Table | Purpose |
|---|---|
| `schema_migrations` | Applied migration versions and times. |
| `namespaces` | Named access domains (`name`, `kind`, `description`, `created_at`). |
| `agents` | Registered agents: `agent_id`, `root_agent_id`, `parent_agent_id?`, `label?`, `model?`, `provider?`, `task?`, `enrollment_mode`, `created_at`, `revoked_at?`. Client-declared model/label are unverified metadata. |
| `sessions` | Live sessions: `session_id`, `agent_id`, `root_agent_id`, `token_hash` (SHA-256, unique), `actor_handle`, `created_at`, `last_seen_at`, `idle_expires_at`, `absolute_expires_at`, `revoked_at?`. |
| `grants` | Namespace roles per agent: `grant_id`, `agent_id`, `namespace`, `role`, `created_at`, `expires_at?`, `revoked_at?`. |
| `delegation_tickets` | One-time parent-to-child tickets: `ticket_id`, `parent_agent_id`, `root_agent_id`, `created_at`, `expires_at`, `used_at?`. |
| `mentor_requests` | Enrollment requests: `request_id`, `agent_id`, `session_id`, `code`, `scopes`, `state`, `created_at`, `expires_at`, `decided_at?`, `decided_by?`. |
| `entries` | Stable entry identity: `entry_id`, `namespace`, `created_at`, `created_by_agent_id`, `current_revision_id?`. |
| `entry_revisions` | Immutable revision content (see below). |
| `sources` | Provenance per revision: `source_id`, `revision_id`, `kind`, `role`, `identity?`, `uri?`, `snapshot_hash?`, `trust`, `detail?`, `created_at`. |
| `evidence_objects` | Bounded hashed blobs: `evidence_id`, `sha256` (unique), `size_bytes`, `media_type`, `storage_path`, `redacted`, `created_at`, `revision_id?`. |
| `provenance_edges` | Revision-to-revision relations: `edge_id`, `from_revision_id`, `to_revision_id`, `relation`, `created_at`. |
| `reviews` | Review outcomes: `review_id`, `entry_id`, `revision_id`, `reviewer_agent_id`, `reviewer_root_id`, `result`, `method`, `evidence?`, `conditions?`, `limitations?`, `lease_id?`, `created_at`. |
| `review_jobs` | Queue: `job_id`, `entry_id`, `revision_id`, `state`, `priority`, `created_at`, `attempts`, `lease_owner?`, `lease_expires_at?`. |
| `task_outcomes` | Observed task results: `outcome_id`, `agent_id`, `session_id`, `task_label?`, `outcome`, `revision_ids`, `notes?`, `created_at`. |
| `audit_events` | Append-only audit: `event_id`, `ts`, `actor_agent_id?`, `action`, `object_type?`, `object_id?`, `details?`. |
| `idempotency_records` | Replay protection: `key`, `scope`, `response`, `created_at`. |
| `deletion_ledger` | Forgetting tombstones: `object_type`, `object_id`, `deleted_at`, `reason?`. |
| `entry_fts` | FTS5 virtual table over `title`, `claim`, `conditions`, `tags` (`unicode61 remove_diacritics 2`), with `entry_id` and `revision_id` unindexed. |

## Entry revisions

Each `entry_revisions` row is immutable and contains:

- Identity: `revision_id`, `entry_id`, `prev_revision_id?`, `created_at`.
- Content: `kind`, `title`, `claim`, `conditions?`, `expected_result?`,
  `limitations?`, `tags` (JSON array), `scope` (JSON object), `observed_at`,
  `valid_from?`, `valid_until?`, `last_verified_at?`.
- Attribution: `author_agent_id`, `author_root_id`, `contributors` (JSON
  array), `source_role`.
- Classification: `evidence_class`, `sensitivity` (default `normal`),
  `lifecycle`, `content_hash`.

## Lifecycle states

`candidate` -> `published` is the normal promotion path, taken when an
eligible approved mentor (or the operator) accepts a specific revision with
adequate evidence. The full set:

| State | Meaning |
|---|---|
| `candidate` | Recorded but not independently verified. Default for agent writes. |
| `published` | Accepted with explicit evidence classification and scope. |
| `disputed` | Credible conflicting evidence surfaced; mentor or operator resolves, history preserved. |
| `superseded` | Replaced by an accepted newer revision. Hidden from search by default, retrievable by authorized ID. |
| `archived` | Excluded from retrieval by operator decision. |

## Evidence classes

Distinct from lifecycle: `unverified` (default), `source_checked`,
`reproduced`, `operator_confirmed`. An operator confirming a preference via
the CLI yields `operator_confirmed`. Hashes on evidence detect byte changes;
they do not prove truth.

## Kinds and review results

- `kind`: `preference`, `instruction`, `decision`, `observation`, `hypothesis`,
  `procedure`, `failure`, `task_summary`.
- Review `result`: `supports`, `contradicts`, `inconclusive`, `duplicate`,
  `out_of_scope`. Submissions require `method`, and may carry `evidence`,
  `conditions`, and `limitations`, always for an exact revision.
- Namespace roles: `reader`, `contributor`, `reviewer`, `mentor`, `operator`.

## Provenance

Original user messages, agent interpretations, and externally observed facts
are distinguishable. Each source records `role`, `identity`, `trust`
(default `agent_reported`), and `created_at`. An agent reporting "the user
said" is agent-reported unless it entered through the operator path. Related
revisions are connected by typed edges: `derived_from`, `contradicts`,
`supersedes`, `duplicates`, `tested_by`, `applies_to`. A chain of repetitions
shares one source lineage and is not independent corroboration.

## Revisions and immutability

There is no in-place update. Corrections create a new revision linked by
`prev_revision_id`; the previous published revision stays visible until the
replacement is accepted. `content_hash` detects exact repeats.

## Optimistic concurrency and idempotency

- `propose_revision` carries `expected_revision`. A mismatch returns
  `REVISION_CONFLICT` with a structured error, never silent last-writer-wins.
- Write operations accept an `idempotency_key` (max 256 chars, scoped). A
  retried request with the same key returns the stored original response
  instead of duplicating entries, approvals, reviews, or outcomes.

## Deduplication

Exact repeats are detected by normalized content hash within the appropriate
namespace and scope; the existing entry ID is returned. Similarity-based
merges remain proposals with separate sources and never cross access
boundaries.

## Sensitivity and publication eligibility

`evidence_class` and `sensitivity` are stored per revision. Publication
eligibility requires: a matching namespace role, a current (non-revoked)
grant, a valid lease for review items, the exact revision, and the
author/contributor family restriction (no self- or family-approval). There is
no inferred promotion of personal data: `sensitivity` only ever moves when an
authenticated operator or the author explicitly changes it, and entries in
the `personal` namespace are never reachable by default grants.
