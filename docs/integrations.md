<!--
SPDX-FileCopyrightText: 2026 René Widera
SPDX-License-Identifier: ISC
-->

# Integrations

## Stable protocols

`knowlage_mcp.integrations.base` defines five small, runtime-checkable
protocols. They are driven by actual use cases, and the core release ships
only the offline null implementations (`integrations/null.py`), which are what
make the service run with no optional dependency installed.

| Protocol | Role | Core implementation |
|---|---|---|
| `KnowledgeStore` | Canonical entry and revision persistence. | SQLite (the gateway-controlled store). |
| `SearchIndex` | Queryable index over entries. Must be rebuildable from canonical data. | SQLite FTS5. |
| `EmbeddingProvider` | Optional text embeddings for semantic candidate generation. | Null (disabled). |
| `MemoryExtractor` | Optional extraction of memory candidates from raw material. | Null (disabled). |
| `EvidenceStore` | Bounded, hashed, redacted blob storage with quota. | Local evidence directory. |

Invariants for every implementation, core or optional:

- Canonical entries and review decisions always remain in the
  gateway-controlled store. An index or external engine is a derived view.
- ACL filtering must be equivalent to the core's: an optional backend must not
  leak entries through linked objects, counts, snippets, or reranking prompts.
- Deletion must propagate (retract and forget must remove from the derived
  view).
- Scope handling must match the core (unspecified scope means unknown, not
  universal).
- Provenance must be preserved, not re-inferred.
- The core must stay functional when the optional backend is disabled or
  unavailable.

## Optional adapter candidates (not in this release)

The following are candidates for later adapters. None of them is included in
the core release, none is a default dependency, and none is wired up in
0.1.0:

- **Qdrant** - filtered vector/hybrid retrieval as a derived index, never the
  authority for ACL or verification.
- **Graphiti** - temporal entity and relationship extraction and retrieval.
  Extracted edges are proposals with provenance; LLM and embedding calls must
  be budgeted.
- **Mem0** - conversation-to-memory extraction and semantic memory. Original
  source roles must be preserved; an external engine may not silently overwrite
  canonical decisions.
- **OpenAI-compatible local/remote providers** - explicit optional
  embeddings/extraction configuration. No default API calls and no required
  secrets.

An adapter is added only when a reproducible test shows a benefit. Its
contract tests must prove equivalent ACL filtering, deletion propagation,
scope handling, provenance, and offline fallback. Exporting private text to a
cloud backend requires explicit operator configuration.

## JSONL import/export

The operator-facing exchange format is JSONL, one JSON object per line:

```jsonl
{"schema_version": 1, "op": "entry", "entry_id": "entry_...", "namespace": "shared", "revision_id": "rev_...", "created_at": "2026-11-11T00:00:00Z", "payload": {"kind": "failure", "title": "...", "claim": "...", "scope": {"compiler": "gcc-13"}, "sources": [{"kind": "log", "trust": "agent_reported", "detail": "..."}], "evidence_class": "unverified"}}
{"schema_version": 1, "op": "edge", "edge_id": "edge_...", "from_revision_id": "rev_...", "to_revision_id": "rev_...", "relation": "contradicts"}
```

Rules:

- Exports carry a schema version, sources, immutable revision IDs, and
  namespaces. They preserve source attribution and any stated content license;
  stored operational knowledge is not automatically relicensed.
- Imports are operator-initiated, idempotent (re-importing the same file
  changes nothing), and untrusted by default.
- Imported role names and `verified` labels never grant authority. An imported
  entry enters with the trust classification it carries, and promotion still
  requires the normal review or operator path.
- Traversal paths and unsafe archives are rejected. Import works on explicit
  files; there is no autonomous crawling. Provider-specific session parsers
  stay outside the core model.

Commands:

```bash
knowlageMCP-gatewayctl export --namespace shared --output knowledge.jsonl
knowlageMCP-gatewayctl import knowledge.jsonl --dry-run
```

## Dependency licenses

Runtime dependencies only: `mcp` (MIT), `fastapi` (MIT), `uvicorn`
(BSD-3-Clause), `httpx` (BSD-3-Clause), `httpx2` (BSD-3-Clause), `pydantic`
(MIT), `platformdirs` (MIT), `tomli-w` (MIT). All are permissive and
redistributable with this project's ISC license; there is no copyleft runtime
dependency.

Development-only tools include `reuse` (GPL-3.0-or-later and other licenses)
and its transitive `python-debian` (GPL-2.0-or-later). They are not imported,
bundled, or redistributed by the built wheel/sdist as runtime code; they run
only in CI/development. GPL dev tools do not change the license of this
project's code but must not be vendored into it.

`reuse lint` proves SPDX/REUSE compliance of this repository's files and does
not by itself establish redistribution compatibility of third-party packages.
Run a license audit when adding a dependency.
