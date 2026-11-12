<!--
SPDX-FileCopyrightText: 2026 René Widera
SPDX-License-Identifier: ISC
-->

# Changelog

All notable changes to this project are documented here. The format follows
Keep a Changelog and the project uses Semantic Versioning.

## [0.1.0] - 2026-11-11

### Added

- Host-canonical SQLite service with WAL mode, foreign keys, ordered
  migrations, an FTS5 index, and a single-writer lock.
- Configuration and bootstrap wizard with interactive and non-interactive
  modes, XDG path resolution, atomic writes, validation, secret redaction, and
  a systemd user unit.
- Identity layer with self-registration, session expiry and revocation, actor
  multiplexing on one connection, one-time delegation tickets, cascading
  family revocation, and a restart policy that invalidates live sessions and
  leases while preserving durable state.
- Knowledge model with eight entry kinds, immutable revisions, provenance
  edges, deduplication, idempotency keys, and optimistic concurrency.
- Retrieval with ACL-first namespace filtering, scope applicability, BM25
  ranking, exact-phrase matching, byte budgets, opaque cursors, and a bounded
  `get_context` bundle.
- Review workflow with mentor enrollment and host approval, leases, attempt
  limits, priority ordering, publication rules, peer evidence, and disputed
  revisions.
- Evidence store with bounded objects, SHA-256 content hashes, deduplication,
  quota enforcement, atomic writes, and secret redaction.
- Operations surface with a peer-uid Unix admin socket, the `gatewayctl`
  command set, backup create, verify, and restore with a deletion ledger, JSONL
  export and import, forgetting, FTS index rebuild, and diagnostics.
- MCP surface with 15 canonical tools, the packaged skill resource, a bounded
  evidence upload helper, direct Streamable HTTP at `/mcp`, and the stdio
  bridge.
- Security boundaries and limits, including Host and Origin validation,
  request body caps, registration limits, per-session and global request rate
  limits, and write budgets.
- Tests for unit, integration, security, quality, and concurrency behavior,
  including 50-client concurrency and rate-limit coverage.
- Retrieval and fault benchmarks with a versioned synthetic corpus.
- Operator and protocol documentation, REUSE-compliant licensing metadata, and
  CI for linting, formatting, type checking, tests, package build, and a
  wheel-install smoke test.

#### Optional integrations

- Qdrant hybrid retrieval as a derived, rebuildable vector index: generation
  and collection lifecycle, deterministic UUID5 point ids, namespace and
  lifecycle filters, reciprocal rank fusion with the lexical FTS5 rank, and
  ACL/scope/lifecycle re-check against SQLite for every vector hit.
- OpenAI-compatible embeddings (`POST {base_url}/embeddings`) with batching,
  conditional `dimensions` for `text-embedding-3*` models, and hard
  dimension-mismatch errors; plus a deterministic offline `hash` provider for
  development and tests.
- Bounded rerankers over already-authorized candidates: offline `lexical`
  (deterministic token overlap) and `openai` (`POST {base_url}/rerank`),
  fail-open with a response warning.
- Proposal-only Graphiti and Mem0 extraction adapters (`extract`,
  `import-memories`, `proposals`) that never publish; acceptance goes through
  the normal candidate/review path.
- Feature flags off by default: `integrations.enabled = false` is the master
  switch, every adapter defaults to `none`/disabled, and no network I/O
  happens at import or while disabled.
- Contract tests over fake Qdrant/embeddings/reranker/Graphiti/Mem0 servers
  (ACL, scope, deletion, offline fallback) and one real-HTTP end-to-end test,
  with no external network access in CI.
- Real-HTTP contract tests against loopback fake Qdrant, embeddings, Graphiti,
  and Mem0 servers that drive the production configuration, registry, and
  adapter path over real sockets without external network access.
