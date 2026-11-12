<!--
SPDX-FileCopyrightText: 2026 René Widera
SPDX-License-Identifier: ISC
-->

# Configuration

The configuration file is TOML at `~/.config/knowlageMCP/config.toml`
(`$XDG_CONFIG_HOME/knowlageMCP/config.toml` when set). The bootstrap wizard
writes it; every key below has a default, so an empty or missing file is a
valid configuration. A reference file with comments is
[config.example.toml](../config.example.toml). Unknown tables and unknown keys
are rejected, so a file that does not parse cleanly is a configuration error,
not silently ignored input.

## Path resolution

Paths are resolved to absolute paths at load time via platformdirs/XDG:

| Setting | Default |
|---|---|
| Config | `~/.config/knowlageMCP/config.toml` |
| Data dir | `~/.local/share/knowlageMCP/` |
| `storage.database` | `<data>/knowledge.sqlite3` |
| `storage.evidence_dir` | `<data>/evidence/` |
| `storage.backup_dir` | `<data>/backups/` |
| `storage.runtime_socket` | `$XDG_RUNTIME_DIR/knowlageMCP/admin.sock`, otherwise a private user-specific temporary directory |

Relative paths in the file are resolved against the current working directory
at load time; `~` is expanded. The wizard creates data directories with mode
0700 and the config file with mode 0600.

## Duration syntax

Duration-valued keys accept an integer (seconds) or a string with a unit
suffix: `30s`, `10m`, `8h`, `24h`, `1d`, `2w` (case-insensitive). Booleans and
negative values are rejected.

## [server]

| Key | Default | Effect |
|---|---|---|
| `host` | `127.0.0.1` | Bind address of the agent-facing listener. Keep loopback unless the deployment explicitly requires another interface. |
| `port` | `2230` | TCP port. Must be 1..65535. The bootstrap validates availability and reports conflicts without silently choosing another port. |
| `allowed_hosts` | `["127.0.0.1", "localhost", "host.containers.internal"]` | `Host` header allowlist. Must not be empty and must not contain `*`. Requests with another `Host` are rejected. |
| `allowed_origins` | `[]` | `Origin` allowlist. Empty means all origins are rejected, which blocks browser callers. `*` is not allowed. |
| `max_request_bytes` | `131072` | Hard cap on HTTP JSON request bodies and the `/mcp` transport (1..16 MiB). Larger bodies are rejected before routing. Evidence upload (`POST /v1/bridge/evidence`) is not limited by this cap; it is bounded by `limits.evidence_object_bytes` instead, and `upload_evidence` over MCP is likewise bounded only by `evidence_object_bytes`. |
| `direct_mcp_registration_token` | `false` | Return a session token from a direct `/mcp` `register_agent` call. Off by default: the safe failure mode returns identity metadata plus `credential.mode: "bridge_required"` and no token. Enable only for a client-side handshake adapter that installs the bearer header on its own MCP connection. |

## [storage]

| Key | Default | Effect |
|---|---|---|
| `database` | `<data>/knowledge.sqlite3` | SQLite file (WAL mode, foreign keys, busy timeout 5 s). Must not point at an existing directory. |
| `evidence_dir` | `<data>/evidence/` | Bounded evidence objects. Must differ from `backup_dir`. |
| `backup_dir` | `<data>/backups/` | Online SQLite backup snapshots. Backups do not protect against disk loss. |
| `runtime_socket` | `$XDG_RUNTIME_DIR/knowlageMCP/admin.sock` | Private Unix socket for `gatewayctl`. Never bound to TCP; parent directory 0700, socket file 0600. `config.example.toml` shows the portable `~/.local/state/knowlageMCP/admin.sock` path because it works without `$XDG_RUNTIME_DIR`; the live default uses the runtime directory. |
| `evidence_quota_bytes` | `1073741824` (1 GiB) | Total evidence storage budget; writes beyond it fail with `STORAGE_FULL`. |
| `database_soft_limit_bytes` | `1073741824` (1 GiB) | Soft warning threshold for the database file size, reported by `doctor` and `status`. |

## [identity]

| Key | Default | Effect |
|---|---|---|
| `initial_namespace` | `shared` | Namespace created at bootstrap. Agents get read on its published entries and candidate-write by default. |
| `personal_namespace` | `personal` | Operator-only namespace until explicitly granted. Never auto-granted. Must differ from `initial_namespace`. |
| `allow_self_registration` | `true` | Allow open registration. Set `false` to require operator enrollment approval (for untrusted networks). |
| `session_idle_timeout` | `8h` | A session expires after this much inactivity. Must not exceed `session_absolute_lifetime`. |
| `session_absolute_lifetime` | `24h` | Hard expiry of a session regardless of activity. |
| `mentor_request_lifetime` | `10m` | A pending mentor request expires after this long; expired requests cannot be approved. |
| `mentor_approved_lifetime` | `2h` | Default TTL for an approved mentor enrollment (the operator can pass `--ttl` to override). |
| `child_ticket_lifetime` | `5m` | One-time delegation tickets expire after this long. |
| `max_sessions` | `256` | Cap on simultaneously live sessions. |

## [limits]

All limits are enforced in the service layer, not in transport decorators, and
they use an injectable clock so tests can advance time without sleeping.

| Key | Default | Effect |
|---|---|---|
| `registration_per_minute_per_source` | `10` | Registration rate limit per source (container). Exceeding returns `RATE_LIMITED` with `retry_after_s`. |
| `registration_per_minute_global` | `60` | Global registration rate limit per minute. |
| `requests_per_minute` | `60` | Per-session tool request rate limit. |
| `request_burst` | `20` | Allowed burst above the sustained per-session request rate. |
| `requests_per_minute_global` | `6000` | Process-wide tool request rate limit across all sessions. Admin socket calls are not counted. |
| `request_burst_global` | `1000` | Allowed burst above the sustained global request rate. |
| `candidate_writes_per_hour` | `20` | Candidate writes per agent per hour. Exceeding returns `BUDGET_EXCEEDED`. |
| `candidate_writes_per_hour_global` | `200` | Candidate writes per hour across all agents. |
| `search_default_results` | `5` | Results per search when the caller does not ask for more. |
| `search_max_results` | `10` | Hard maximum results per search (documented ceiling: 10). |
| `search_default_bytes` | `4000` | Default response byte budget for search. |
| `search_max_bytes` | `8000` | Hard maximum byte budget for search (documented ceiling: 8000). |
| `detail_page_bytes` | `12000` | Byte budget per `get_entry` detail page. |
| `claim_body_bytes` | `8192` | Maximum bytes for the `claim` field of an entry. |
| `evidence_object_bytes` | `262144` (256 KiB) | Maximum size of one evidence object, enforced separately from `server.max_request_bytes` (hard ceiling 16 MiB). Applies to `POST /v1/bridge/evidence` and to `upload_evidence` over MCP. The total evidence budget is `storage.evidence_quota_bytes`. |

## [review]

| Key | Default | Effect |
|---|---|---|
| `lease_seconds` | `600` | A review job lease expires after this many seconds; the item becomes available again. |
| `max_attempts` | `3` | A job with this many failed attempts is parked, preventing retry loops on broken items. |
| `mentor_items_per_session` | `20` | Review items one mentor session may handle; beyond it `review_next` reports the queue empty for that session (`BUDGET_EXCEEDED`). |

## [retrieval]

| Key | Default | Effect |
|---|---|---|
| `include_candidates` | `true` | Let candidate entries appear as clearly labeled low-trust fallback results. Callers can override per request. |
| `backend` | `fts5` | Search backend. This release only accepts `fts5`. |

## [backup]

| Key | Default | Effect |
|---|---|---|
| `daily` | `true` | Take one online SQLite backup per day. |
| `keep_daily` | `7` | Daily snapshots kept. |
| `keep_weekly` | `4` | Weekly snapshots kept. |

Backups use SQLite's online backup mechanism, never a raw copy of a live WAL
database, and a snapshot manifest records schema version, checksums, the
evidence inventory, and the application version.

## [logging]

| Key | Default | Effect |
|---|---|---|
| `level` | `INFO` | One of `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. Logs go to stderr/journal and never contain session tokens or evidence content. |

## [integrations]

The optional semantic/vector layer. Every adapter is off by default; with
`enabled = false` the service constructs only offline null adapters and makes
no network I/O. Nested adapter settings may be present in that state; the
master switch disables them at runtime without a validation error. Secrets are referenced by environment variable name only; no
config key holds a key value. See [integrations.md](integrations.md) for the
architecture, operations, and failure semantics.

| Key | Default | Effect |
|---|---|---|
| `enabled` | `false` | Master switch for the layer. `false` disables all adapters and keeps search lexical FTS5. |
| `inference` | `false` | Reserved. No inference backend ships in this release. |
| `telemetry` | `false` | Telemetry is disabled and stays off. |

### [integrations.embedding]

| Key | Default | Effect |
|---|---|---|
| `provider` | `none` | `none`, `hash` (deterministic offline development/test provider, not a semantic model), or `openai` (OpenAI-compatible `POST {base_url}/embeddings`). |
| `model` | `text-embedding-3-small` | Model name sent to the provider. Must not be empty; required for `openai`. |
| `base_url` | `""` | Base URL of the embedding endpoint. Must be an http(s) URL when `provider = "openai"`; empty means the openai provider is unavailable. |
| `api_key_env` | `KNOWLAGEMCP_EMBEDDING_API_KEY` | Name of the environment variable holding the API key. The key value is read per request, never stored or logged. Must be a valid environment variable name. |
| `dimensions` | `1536` | Expected vector length. Must be positive. A response with another length is a hard `BACKEND_UNAVAILABLE` error. Must equal `integrations.vector.vector_size` when a qdrant backend is configured with a real provider. |
| `batch_size` | `16` | Inputs per `POST /embeddings` batch. Must be positive. |
| `timeout_s` | `20.0` | Per-request timeout. Must be positive. |
| `allow_private_text` | `false` | Must be `true` before personal/private namespace text is embedded and indexed by a remote provider. Local providers (`none`, `hash`) never leave the host. This gates indexing, not search: query text still goes to the configured endpoint on every semantic search. |

### [integrations.vector]

| Key | Default | Effect |
|---|---|---|
| `backend` | `none` | `none` or `qdrant`. `qdrant` requires `integrations.embedding.provider` to be `hash` or `openai`; `none` produces no vectors and is rejected. The index is derived and rebuildable; it is never authoritative for ACL or review. |
| `url` | `http://127.0.0.1:6333` | Qdrant REST endpoint. Must be an http(s) URL when `backend = "qdrant"`. |
| `api_key_env` | `KNOWLAGEMCP_QDRANT_API_KEY` | Environment variable name for the Qdrant API key, sent as a Bearer token when set. |
| `collection_prefix` | `knowlagemcp` | Prefix of the per-generation collection names (`prefix_gen_<16 hex>`). Must not be empty. |
| `vector_size` | `1536` | Vector dimension of the collection. Must be positive and equal `integrations.embedding.dimensions` for `hash` or `openai` providers. |
| `distance` | `Cosine` | `Cosine`, `Euclid`, or `Dot` (aliases like `euclidean` are normalized). |
| `timeout_s` | `10.0` | Per-request timeout. Must be positive. |
| `top_k` | `40` | Base bound for the vector candidate pool. A query fetches `min(max(top_k, 4 * max_results), 100)`. Must be positive. |
| `hybrid` | `true` | When false, vector candidates are not merged into results even if the index is healthy. |

### [integrations.reranker]

| Key | Default | Effect |
|---|---|---|
| `provider` | `none` | `none`, `lexical` (offline, deterministic token overlap), or `openai` (`POST {base_url}/rerank`). |
| `model` | `rerank-1` | Model name. Must not be empty for `openai`. |
| `base_url` | `""` | Base URL of the rerank endpoint. Must be an http(s) URL when `provider = "openai"`. |
| `api_key_env` | `KNOWLAGEMCP_RERANK_API_KEY` | Environment variable name for the reranker API key. |
| `top_n` | `20` | Maximum number of already-authorized candidates offered to the reranker. Must be positive. The reranker reorders this set only; it cannot add or remove entries. |
| `timeout_s` | `10.0` | Per-request timeout. Must be positive. |

### [integrations.graphiti]

| Key | Default | Effect |
|---|---|---|
| `enabled` | `false` | Enable the proposal-only Graphiti extraction adapter. |
| `base_url` | `http://127.0.0.1:8000` | Graphiti-compatible endpoint. Must be an http(s) URL when enabled. |
| `api_key_env` | `KNOWLAGEMCP_GRAPHITI_API_KEY` | Environment variable name for the API key. |
| `timeout_s` | `20.0` | Per-request timeout. Must be positive. |
| `max_calls_per_task` | `4` | Maximum extractor calls per `integrations extract` command, which is also the maximum number of entries in one batch. Must be positive. |

### [integrations.mem0]

| Key | Default | Effect |
|---|---|---|
| `enabled` | `false` | Enable the proposal-only Mem0 extraction adapter. Mutually exclusive with `integrations.graphiti.enabled`; enabling both is a validation error. |
| `base_url` | `http://127.0.0.1:8001` | Mem0-compatible endpoint. Must be an http(s) URL when enabled. |
| `api_key_env` | `KNOWLAGEMCP_MEM0_API_KEY` | Environment variable name for the API key. |
| `timeout_s` | `20.0` | Per-request timeout. Must be positive. |
| `max_items` | `20` | Maximum proposals per extraction call, and maximum extractor calls per `import-memories` run, which also bounds proposals. Must be positive. |

### Legacy keys

The old flat `integrations.embeddings` boolean is no longer a valid key for
new configuration. Loading a file that still contains it migrates it:
`false` becomes `embedding.provider = "none"`, `true` becomes
`embedding.provider = "hash"`. An explicit `integrations.embedding.provider`
always wins, so a partially migrated file keeps its intent. A non-boolean
value for the legacy key is a configuration error.

### Privacy rules

- A configured remote embedding provider is an explicit operator decision.
  Query text is sent to `integrations.embedding.base_url` on every semantic
  search, even when `allow_private_text = false`.
- `allow_private_text` gates indexing: personal and private namespace content
  is embedded and indexed only when it is `true`; otherwise that content
  stays in SQLite and is reachable only through lexical search.
- Local providers (`none`, `hash`) never send text off the host.
- Extractor adapters (Graphiti, Mem0) send bounded text to their configured
  endpoints only when explicitly enabled and only when an operator invokes
  `integrations extract` or `integrations import-memories`.
- API keys are referenced by environment variable name in the config and
  read at request time; key values never appear in config files, logs, or
  error messages.

## Checking, displaying, and reloading

- `knowlageMCP-gatewayctl config check` loads and validates the active
  configuration and prints every problem; exit 0 when clean, exit 4 when
  problems are found.
- `knowlageMCP-gatewayctl config show --redacted` renders the effective
  configuration as TOML. Any key whose name contains `token`, `secret`,
  `password`, `api_key`, `apikey`, or `private_key` is replaced with
  `<redacted>` before printing.
- `knowlageMCP-gatewayctl reload` re-reads the config file and applies the
  change atomically: the gateway validates the new document before switching,
  reports the changed keys, and keeps the old configuration running if the new
  one fails validation. A failed reload is reported, not applied.
