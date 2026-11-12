<!--
SPDX-FileCopyrightText: 2026 René Widera
SPDX-License-Identifier: ISC
-->

# Integrations

The optional semantic/vector layer adds hybrid retrieval (lexical FTS5 plus a
vector index), a bounded reranker, and extraction adapters. It is off by
default and entirely optional: the core gateway runs without it, and every
adapter is individually disable-able.

## Architecture

- **SQLite remains canonical.** Entries, revisions, ACLs, scopes, lifecycle,
  review state, and the FTS5 index live in the gateway-controlled database.
- **Qdrant is a derived, rebuildable index.** It owns no authority. A point
  payload only helps deduplicate and label a hit; it never decides visibility,
  lifecycle, or review state. The index is rebuilt from SQLite at any time.
- **ACL, scope, and lifecycle are re-applied from SQLite after every vector
  hit.** A vector hit is not trusted for authorization: the canonical
  namespace ACL, scope applicability, lifecycle, and validity checks run
  again against SQLite before a hit is displayed. A payload namespace that
  grants access the session does not have is ignored.
- **The reranker may only reorder an already-authorized set.** Unknown entry
  IDs and duplicates in a reranker response are dropped; it can never add or
  remove an entry.
- **Extractors return proposals, never published knowledge.** Graphiti and
  Mem0 output is stored in `extraction_proposals` with state `proposed`.
  Accepting a proposal goes through the normal candidate and review path
  (`import-memories` creates candidate/unverified entries, which still require
  review to be published).

## Feature flags

- `integrations.enabled = false` is the master switch. While it is false, the
  registry constructs only offline null adapters: no client with network
  capability exists, no network I/O happens, and search stays lexical FTS5.
- Every adapter is `none` or disabled by default (`embedding.provider =
  "none"`, `vector.backend = "none"`, `reranker.provider = "none"`,
  `graphiti.enabled = false`, `mem0.enabled = false`).
- No network connection is opened at import time. Adapters are constructed
  lazily and only when the layer is enabled.

## Embeddings

`integrations.embedding.provider` selects the provider:

- `none` (default): performs no work. Calling it raises
  `BACKEND_UNAVAILABLE`; search falls back to lexical.
- `hash`: a deterministic offline development and test provider. It hashes
  lowercased word tokens and character trigrams with `blake2b` into an
  L2-normalized fixed-size vector, so identical text always yields the
  identical vector across processes and hosts. It is **not** a semantic
  model: similar surface strings get similar vectors, synonyms and
  paraphrases do not. Use it to exercise the pipeline offline, never as a
  quality benchmark for semantic retrieval.
- `openai`: speaks an OpenAI-compatible `POST {base_url}/embeddings` JSON
  endpoint. Requests are split into batches of `batch_size`; each batch is one
  POST and is not retried. `model`, `dimensions`, and `timeout_s` are
  configured; the API key is read from the environment variable named by
  `api_key_env` on every request and is never stored or logged.

`dimensions` request field: it is sent only for models whose name starts
with `text-embedding-3`, because some providers reject the field for other
models. Any response whose vector length differs from the configured
`dimensions` (or whose input count is wrong) is a hard error:
`embedding dimension mismatch: expected N, received M`, mapped to
`BACKEND_UNAVAILABLE`.

## Vector index

`integrations.vector.backend` is `none` (default) or `qdrant`. The Qdrant
adapter uses plain REST calls (no client library):

- `PUT /collections/{name}` (idempotent; a 409 is treated as success if a
  `GET /collections/{name}` proves the collection exists)
- `PUT /collections/{name}/points?wait=true` (upsert in chunks of 256)
- `POST /collections/{name}/points/delete?wait=true` (filter on `entry_id` or
  `revision_id`)
- `POST /collections/{name}/points/search` (namespace and lifecycle filter,
  `limit`, `with_payload`)
- `POST /collections/{name}/points/payload?wait=true` (lifecycle updates)

Details:

- **Collection naming.** `collection_prefix + "_" + generation_id`, for
  example `knowlagemcp_gen_a1b2c3d4e5f60718`. Each embedding model and
  dimension set has its own generation and its own collection, so vectors
  from different models never mix.
- **Generation ids.** `gen_` plus the first 16 hex characters of
  SHA-256 over `backend|model|dimensions|collection_prefix`. Deterministic
  across processes.
- **Point ids.** Deterministic UUID5 over `uuid.NAMESPACE_URL` and the
  revision id, so a delete never needs to search first and a rebuild is
  idempotent.
- **Payload fields.** `entry_id`, `revision_id`, `namespace`, `lifecycle`,
  `evidence_class`, `content_hash`, `scope`, `author_root_id`. Anything else
  is dropped.
- **Candidate pool and filters.** A query embeds the text, then searches with
  `limit = min(max(top_k, 4 * max_results), 100)`, a `namespace` match over
  the session's readable namespaces, and a `must_not` on `archived`/
  `superseded` (plus `candidate` unless candidates are included). The query
  vector length is checked against `integrations.vector.vector_size` before
  the call. Lexical and vector ranks are fused with reciprocal rank fusion
  (k=60).

## Rerankers

`integrations.reranker.provider` is `none` (default), `lexical`, or `openai`.

- `lexical`: offline and deterministic. Scores token overlap of the query
  against title (weight 2.0), claim (1.0), tags (1.5), and scope values
  (0.5), normalized by the number of distinct query tokens.
- `openai`: `POST {base_url}/rerank` with `model`, `query`, `documents`
  (entry id plus title and claim), and `top_n`. The adapter sends at most
  `integrations.reranker.top_n` documents. Accepted response shape is a JSON
  object with a `results` list whose items carry `relevance_score` (or
  `score`) and either an `index` or an `id` matching a sent entry. Entries
  the provider did not return keep their position, and a reranker that
  returns nothing usable fails open: the search returns the non-reranked
  hybrid result with a `semantic backend unavailable` warning.

The reranker only reorders the merged, already-authorized candidate set; it
cannot introduce an entry.

## Graphiti and Mem0

Both are proposal-only extractors, disabled by default, and at most one can
be active (Graphiti wins if both are enabled). Each call sends exactly one
HTTP request; empty input returns no proposals without touching the network.

- **Graphiti.** `POST {base_url}/extract` with `text` (bounded), optional
  `task`, and `source_roles`. Accepted response shape is `{"edges": [...]}`
  or a bare list of edge objects with `subject`, `relation`, `object`, and
  optional `detail`. Each edge becomes a proposal with kind `observation`,
  relation `extracted`, source role `agent_observation`, and the tag
  `graphiti`. At most 32 edges per call.
- **Mem0.** `POST {base_url}/memories/extract` with one message
  (`{"role", "content"}`, bounded), optional `task`, and `max_items`.
  Accepted response shape is `{"memories": [...]}` or a bare list of memory
  objects with required `title` and `claim` and optional `kind`,
  `conditions`, `expected_result`, `limitations`, `tags`, `scope`, and
  `source_role`. Original source roles are preserved when present. At most
  `integrations.mem0.max_items` proposals per call (deduplicated by kind,
  title, claim).

Caps: 4096 bytes per string (request and response), 200 characters per
generated title, 32 tags, 16 scope keys, 32 edges, 20 items.

Neither adapter writes to SQLite, publishes entries, attaches confidence
scores, or sets lifecycle or evidence state. `integrations extract ENTRY
[ENTRY ...]` accepts one or more entry ids and makes one extractor call per
unique entry. A batch is rejected with `INVALID_ARGUMENT` when the number of
unique entries exceeds `integrations.graphiti.max_calls_per_task`, which names
the cap in the error. Successful proposals go to `extraction_proposals` with
state `proposed`, and each call is audited. Inspect them with:

```bash
knowlageMCP-gatewayctl integrations extract ENTRY [ENTRY ...]
knowlageMCP-gatewayctl integrations proposals [--entry ENTRY] [--state STATE]
```

`proposals` is read-only (newest first, capped at 50 rows) and accepts or
rejects nothing. Accepting a proposal goes through the normal candidate and
review path.

## Operations

Minimal enablement in the config file:

```toml
[integrations]
enabled = true

[integrations.embedding]
provider = "openai"
base_url = "http://127.0.0.1:8080"
api_key_env = "KNOWLAGEMCP_EMBEDDING_API_KEY"
dimensions = 1536

[integrations.vector]
backend = "qdrant"
url = "http://127.0.0.1:6333"
vector_size = 1536

[integrations.reranker]
provider = "lexical"
```

Then reindex once:

```bash
knowlageMCP-gatewayctl integrations reindex
```

Commands (see also [operations.md](operations.md)):

```bash
knowlageMCP-gatewayctl integrations status [--json]
knowlageMCP-gatewayctl integrations probe [--json]
knowlageMCP-gatewayctl integrations reindex [--json]
knowlageMCP-gatewayctl integrations extract ENTRY [ENTRY ...] [--json]
knowlageMCP-gatewayctl integrations proposals [--entry ENTRY] [--state STATE] [--json]
knowlageMCP-gatewayctl integrations import-memories FILE [--dry-run] [--json]
```

- `status` reports adapter names, availability, and the recorded index
  generations. It contains no secrets.
- `probe` performs bounded health checks on the enabled adapters. A disabled
  layer returns an empty map without network work.
- `reindex` rebuilds FTS5 and, when `integrations.vector.backend` is not
  `none`, embeds the current published, disputed, and candidate revisions
  into a new generation. The previous ready generation for a different model
  or dimension set is marked `stale`; the new one is marked `ready`.
- `extract` resolves every entry before calling the extractor. For one entry
  the JSON result keeps the top-level `entry_id`, `revision_id`, `extractor`,
  `available`, and `proposals` fields; every result also lists per-entry rows
  and the `calls` and `cap` values.
- `import-memories FILE` reads JSONL records of `{"role","text","source_role",
  "task","entry_id","revision_id"}` and writes candidate entries to the initial
  shared namespace (`identity.initial_namespace`) through the normal insert
  path. It preserves the original `role`/`source_role`, `unverified` evidence,
  and a `mem0` tag, and adds a `derived_from` provenance edge when an optional
  `entry_id` or `revision_id` identifies a known revision. The extractor
  receives the conversation role as `messages[0].role`. It deduplicates by
  content hash, never publishes or overwrites, and limits the run to
  `integrations.mem0.max_items` extractor calls, which also bounds proposals.
  Every extractor call counts against that budget, even when it returns no
  proposals, and the result reports `calls`. `--dry-run` still calls the
  extractor to count proposals, then reports without writing. Imports are
  bounded to 32 MiB and 50,000 records; split larger files before importing.

**Model and dimension changes.** Changing `embedding.model` or
`embedding.dimensions` starts a new generation: the old one is marked `stale`,
and semantic search stays off until an operator runs `reindex`. Vectors are
never mixed across generations.

**Restart rehydration.** On startup the service adopts the most recent ready
generation when its model and dimensions still match the configured embedding
adapter, so semantic search resumes without a second reindex. A mismatch
keeps the vector index unavailable until an operator reindexes.

**Deletion propagation.** `memory retract` removes the entry from Qdrant
after archiving the canonical revision; `memory forget` deletes the entry's
points and index-state rows after the database purge. Cleanup is best-effort:
a failure never blocks the canonical operation, and the result carries a
`warning` when the derived index could not be updated.

A rebuild is always possible from SQLite; losing the Qdrant collection costs
one `reindex`, nothing else.

## Failure semantics

- **Search is fail-open.** When the embedding or vector backend is
  unavailable, misconfigured, or returns a dimension mismatch, the response
  still succeeds with `retrieval_backend = "fts5"` and a warning such as
  `semantic backend unavailable: BACKEND_UNAVAILABLE`. No 5xx, no retry
  loop; the lexical result is the answer.
- **Explicit administrative operations are fail-closed.** `reindex` fails
  with `BACKEND_UNAVAILABLE` when it cannot build the index (the generation
  is marked `failed`). `extract` and `import-memories` fail with
  `BACKEND_UNAVAILABLE` when an extractor is configured but unavailable. With
  the `null` extractor (nothing configured), both return an empty soft result
  with `available: false` so an operator can inspect the configuration.
- **Indexing after writes is best-effort.** A failed upsert never fails or
  rolls back the canonical write; the revision is simply missing from the
  derived index until the next `reindex`.

**Private and cloud policy.** A configured remote embedding provider is an
explicit operator decision, so query text goes to `integrations.embedding.base_url`
on every semantic search, even when `allow_private_text` is false.
`integrations.embedding.allow_private_text = true` gates indexing: personal
and private namespace content is embedded and indexed only when it is true.
With false, that content stays in SQLite and is reachable only through
lexical search. Local providers (`hash`) and `none` never leave the host.

## Contract tests

The adapter suite uses fake servers over `httpx2.MockTransport`: fake Qdrant
collections, fake embeddings and reranker endpoints, and fake Graphiti/Mem0
extractors. These prove the ACL re-check (a payload namespace is not trusted
for access), scope and lifecycle filtering, deterministic UUIDs and payload
shape, deletion propagation, dimension-mismatch handling, fail-open lexical
fallback, zero HTTP requests while disabled, and restart generation
adoption. `tests/integration/test_integrations_http.py` runs one real-HTTP
end-to-end test: a live gateway and admin socket, a registered agent, record,
`integrations reindex`, hybrid search, and `memory forget` deleting the
points. `tests/integration/test_integrations_e2e_http.py` exercises the full
production path over loopback sockets against the fake Qdrant, embeddings,
Graphiti, and Mem0 servers in `tests/integration/fake_backends.py`. No test
uses external network access; CI runs without it.

## Still not included

- No automatic background extraction: extraction runs only when an operator
  calls `integrations extract` or `integrations import-memories`.
- No autonomous crawling or provider-specific session parsers.
- No mandatory cloud service. A remote embedding endpoint is an explicit
  operator choice; `hash` and `none` are fully offline.
- No Qdrant, Graphiti, or Mem0 client libraries. The adapters speak plain
  REST over a small internal JSON HTTP helper. Graphiti is used as an
  extraction adapter only; graph-native retrieval is not exposed, and
  canonical retrieval stays in SQLite so ACL and review remain authoritative.
- Mem0 provides conversation extraction only. It emits proposals and never
  overwrites canonical entries.
- No claim that semantic search improves task outcomes. The measured baseline
  in [testing.md](testing.md) covers the lexical pipeline; semantic retrieval
  is measured only as retrieval behavior.

## Exchange format

The JSONL import/export exchange format, its rules, and the export/import
commands are documented in [operations.md](operations.md).
`import-memories FILE` above is a distinct, extraction-based import that
creates candidate entries; it is not the generic JSONL import.
