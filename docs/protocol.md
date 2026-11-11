<!--
SPDX-FileCopyrightText: 2026 René Widera
SPDX-License-Identifier: ISC
-->

# Protocol

## Versions and negotiation

- Application version: `0.1.0` (this repository may be an unreleased 0.1.0).
- Bridge/MCP wire protocol version: `1.0`.
- Schema version: `1` (database migration level).

`get_protocol_info` returns the protocol version, application version, schema
version, tool names, the skill resource URI, and feature flags, with no
private state. Compatibility rule: the client major must equal the server
major, and the client minor must not exceed the server minor. Unknown or
malformed client versions are rejected, not guessed.

## MCP tools (the full set)

16 tool names are served over the MCP transports: the 15 canonical names in
`TOOL_NAMES` (`get_protocol_info` through `session_close`) plus the bounded
`upload_evidence` helper. `get_protocol_info` reports the canonical 15 names in
its `tool_names` field. Every authenticated tool takes an optional
`actor_handle` argument (see multiplexing below). Tool results are a single
JSON text block.

| Tool | Arguments | Response |
|---|---|---|
| `get_protocol_info` | none | `ProtocolInfo` |
| `register_agent` | `mode` (`root`\|`child`), `ticket?`, `label?`, `model?`, `provider?`, `task?` | `Registration` (actor handle, agent IDs, session ID, expiry, grants). The session token is held by the bridge and never returned. |
| `delegate_child` | `ttl?` (duration, default from `[identity] child_ticket_lifetime`) | `Delegation` (one-time ticket, parent and root IDs, expiry) |
| `request_mentor` | `namespace`, `scopes[]` | `MentorRequest` (request ID, comparison code, `pending` state, expiry) |
| `enrollment_status` | `request_id` | `MentorRequest` (current state, `approved_at` when approved). Only the caller's own request is inspectable. |
| `get_context` | `task?`, `scope?` | a small session-start bundle: relevant entries with snippets, scope, evidence labels, and warnings, within the search byte budget |
| `search` | `query`, `filters?` (namespace, kinds, lifecycle, evidence_class, tags, scope, include_candidates, author_agent_id), `max_results?`, `max_bytes?`, `cursor?` | `SearchResponse` (results with entry/revision IDs, snippets, scope, evidence label, score, time; total; truncated flag; opaque `next_cursor`; approx bytes) |
| `get_entry` | `entry_id`, `revision_id?`, `page?`, `cursor?` | bounded `EntryDetail` page: full revision fields, sources, evidence references, attached reviews; history pages on request |
| `record` | `namespace`, `entry` (see payload below), `idempotency_key?` | the stored entry (dedupe may return an existing entry ID) |
| `propose_revision` | `entry_id`, `expected_revision`, `entry`, `idempotency_key?` | the new candidate revision; `REVISION_CONFLICT` when stale |
| `record_outcome` | `outcome`, `task_label?`, `revision_ids[]`, `notes?`, `idempotency_key?` | the stored outcome |
| `review_next` | `filters?` | a leased `ReviewJob` (job ID, entry, revision, priority, lease expiry) or an empty-queue report |
| `submit_review` | `entry_id`, `revision_id`, `result` (`supports`\|`contradicts`\|`inconclusive`\|`duplicate`\|`out_of_scope`), `method`, `job_id?`, `lease_id?`, `evidence?`, `conditions?`, `limitations?`, `evidence_class?`, `idempotency_key?` | `SubmitReviewResult`: `review_id`, `entry_id`, `revision_id`, `lifecycle` (the revision lifecycle after the decision), `evidence_class`, `published` (true when the decision moved the entry to `published`), `job_state` (default `done`), `entry_current_revision_id` (the entry's current revision after the write) |
| `session_close` | none | `{closed: true}`; releases leases and revokes the session |
| `upload_evidence` | `text`, `media_type?` (default `text/plain`), `name?`, `actor_handle?` | `{evidence_id, sha256, size_bytes, media_type, redacted, name, agent_id}`. Bounded text-only helper: the payload is UTF-8 text, never a filesystem path; the object is capped at `limits.evidence_object_bytes` (256 KiB default) and counted against the evidence quota. Exceeding the per-object cap returns `BUDGET_EXCEEDED` with the size in `details`. |

Also:

- `get_usage_skill` returns the packaged skill text, bounded. The skill is
  also exposed as the MCP resource `knowlageMCP://skill`. Neither is emitted
  automatically on every tool result.

The `entry` payload for `record` and `propose_revision` is `EntryPayload`:
`kind` (one of `preference`, `instruction`, `decision`, `observation`,
`hypothesis`, `procedure`, `failure`, `task_summary`), `title`, `claim`,
`conditions?`, `expected_result?`, `limitations?`, `tags[]`, `scope`
(`repository`, `project`, `version`, `commit`, `compiler`, `backend`, `host`),
`observed_at`, `valid_from?`, `valid_until?`, `last_verified_at?`,
`source_role`, `evidence_class` (`unverified`, `source_checked`,
`reproduced`, `operator_confirmed`), `sensitivity`, `sources[]`, `evidence[]`,
`contributors[]`. Unknown keys are rejected.

## Error envelope and codes

Failed operations return, over HTTP:

```json
{"ok": false, "error": {"code": "...", "message": "...", "retry_after_s": 5, "details": {}}}
```

and over MCP as a single JSON text block with the same shape. Messages are
safe to show; they never contain tokens or host paths, and no stack traces are
returned.

| Code | Meaning | Retry hint |
|---|---|---|
| `AUTH_REQUIRED` | No valid session, or actor handle missing/ambiguous on a multiplexed connection | Re-run `register_agent`; supply `actor_handle` when more than one actor is active |
| `SESSION_EXPIRED` | The session passed its idle or absolute expiry | Re-register |
| `APPROVAL_PENDING` | Mentor enrollment requested but not yet approved | Poll `enrollment_status` with backoff |
| `FORBIDDEN` | Authorized, but not allowed by namespace role | Do not retry; check grants |
| `INVALID_ARGUMENT` | Payload failed validation (unknown key, bad field) | Fix the payload; do not retry unchanged |
| `REVISION_CONFLICT` | `expected_revision` is stale | Re-read the entry, then retry with the current revision |
| `RATE_LIMITED` | Registration or request rate limit | Honor `retry_after_s` |
| `BUDGET_EXCEEDED` | Per-hour write budget, mentor item budget, or byte budget exhausted | Stop or wait for the budget window |
| `STORAGE_FULL` | Evidence quota exceeded | Trim evidence or raise the quota; do not retry unchanged |
| `REVIEW_INELIGIBLE` | Reviewer family, lease, grant, or revision check failed at submission | Do not retry; another eligible reviewer or the operator must act |
| `BACKEND_UNAVAILABLE` | An optional backend is down; the core continues | Core reads/writes still work |
| `NOT_FOUND` | ID does not exist or is not visible to this session | Verify the ID and scope |
| `CONFLICT` | Generic state conflict (for example an already-decided enrollment) | Re-read state, do not retry unchanged |
| `INTERNAL` | Unexpected server fault | Report; do not retry in a loop |

### Argument validation and the envelope

The MCP SDK validates tool arguments against each tool's JSON schema before the
handler runs. Malformed arguments, including missing required fields, wrong
types, and unknown properties, are rejected there and surface as an MCP tool
error with `isError` set. They do not produce a `{"ok": ...}` envelope. The
envelope applies to calls that pass schema validation and reach the service
layer, where domain validation can still return `INVALID_ARGUMENT`.

### Bounded search and context totals

`search` and `get_context` gather at most 100 candidates before ranking and
filtering. Their `total` value and `next_cursor` reflect that bounded candidate
pool, not every database match. This is a bounded-work guard: narrow the query
or add filters when you need coverage beyond the pool.

## HTTP bridge API

The agent-facing HTTP surface. JSON bodies are capped at
`server.max_request_bytes`; the raw evidence upload is bounded separately by
`limits.evidence_object_bytes` (see below), so a 256 KiB evidence object fits
even when the generic JSON cap is smaller. Security middleware enforces the
body cap, the `Host` allowlist, and `Origin` rejection before routing; there
is no directory serving.

| Endpoint | Auth | Body | Response |
|---|---|---|---|
| `GET /v1/health` | none | - | `{status, version, protocol_version, schema_version, uptime_s}` |
| `POST /v1/bridge/register` | none | `{mode: "root"\|"child", ticket?, label?, model?, provider?, task?}` | `{session_token, actor_handle, agent_id, root_agent_id, session_id, expires_at, grants: [{namespace, role}]}` |
| `POST /v1/bridge/mentor` | Bearer | `{namespace, scopes: []}` | `{request_id, code, state, namespace, scopes, expires_at, approved_at?}`. `request_id` is the credential for approval; `code` is a short human comparison string for operator verification only and is never an approval credential. |
| `GET /v1/bridge/enrollment/{request_id}` | Bearer | - | `{request_id, state, approved_at?}` |
| `POST /v1/tools/{tool}` | Bearer | the tool arguments | `{"ok": true, "data": ...}` or the error envelope |
| `POST /v1/bridge/evidence` | Bearer, raw bytes, `Content-Type`, `X-Evidence-Name` | raw bytes, capped at `limits.evidence_object_bytes` (256 KiB default), not at `server.max_request_bytes` | `{evidence_id, sha256, size_bytes, redacted}` |
| `GET /v1/bridge/evidence/{evidence_id}` | Bearer | - | raw bytes; 404 when missing or not authorized |
| `POST /v1/bridge/close` | Bearer | - | `{closed: true}` |

### Bearer token flow

1. `POST /v1/bridge/register` returns a session token (an opaque 32-byte
   secret). The gateway stores only its SHA-256 hash and never logs the token.
2. Every subsequent agent request sends `Authorization: Bearer <token>`. The
   gateway compares the hash in constant time.
3. `POST /v1/bridge/close` (or expiry, revocation, or a gateway restart) ends
   the session. A revoked token replayed later fails with
   `SESSION_EXPIRED`/`AUTH_REQUIRED`.
4. The bridge holds the token in memory only. It is never written to config
   files, never logged, and never returned in MCP tool results.

### Direct MCP endpoint and a session-id caveat

The gateway also serves the MCP Streamable HTTP protocol at `/mcp`. A client
can connect directly with its own HTTP client, sending the bearer header on
every request. Two caveats apply:

- A protocol session ID (the MCP transport session identifier) is not an
  authentication credential. It only routes messages to a transport session;
  authorization always comes from the bearer token and the service-layer ACLs.
- Direct-MCP clients usually cannot update request headers mid-session: the
  `Authorization` header is fixed when the client session is created, so a
  token obtained by registering later in the same session cannot be attached
  to it. This is why the stdio bridge is the recommended path: it performs the
  registration handshake internally and forwards every call with the current
  credentials, while keeping the secret out of model-visible results. Native
  HTTP clients that need mid-session credential changes require a documented,
  client-side handshake adapter. Application registration is not an
  implementation of the full MCP OAuth specification.

### Registration over `/mcp` and the direct-token flag

By default (`[server] direct_mcp_registration_token = false`), calling
`register_agent` over `/mcp` registers the agent and returns identity metadata
(`agent_id`, `root_agent_id`, `session_id`, `actor_handle`, `expires_at`,
`grants`) plus `credential` with `mode: "bridge_required"` and a
`handshake_url`; the session token is never returned. The agent must use the
stdio bridge (or a client-side handshake adapter that calls the HTTP register
endpoint) to obtain credentials. When the operator sets
`direct_mcp_registration_token = true`, the response instead carries
`session_token` and `credential.mode: "direct"`; the value is returned once,
never logged, and exists solely for a client-side adapter that can install the
bearer header on its own MCP connection.

## Actor handle multiplexing

- Each registered actor on a bridge connection receives an opaque,
  connection-local `actor_handle`.
- When exactly one actor is active on a connection, `actor_handle` may be
  omitted from tool calls.
- When more than one actor is active, `actor_handle` is required; a missing or
  ambiguous handle fails closed with `AUTH_REQUIRED`.
- `register_agent` on an already-registered connection adds another actor and
  never overwrites an existing one.
- There is no mutable global "current agent". Actor handles are routing
  capabilities, not a protection against two logical agents sharing one
  process or context.
- Over HTTP the actor handle travels in the `X-Actor-Handle` header; over MCP
  it is an argument.

## What is and is not on the wire

The agent TCP surface exposes only health, the MCP endpoint, bridge
registration, bounded tools, and bounded evidence upload/download. It does not
expose administration, approval, backup destination selection, raw filesystem
access, or SQL execution. All of that lives behind the Unix admin socket
(see `docs/operations.md`), which is authenticated by peer uid, not by a token
copied into containers.
