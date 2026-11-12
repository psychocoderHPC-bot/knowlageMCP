<!--
SPDX-FileCopyrightText: 2026 René Widera
SPDX-License-Identifier: ISC
-->

# Architecture

## Components and trust boundaries

```
+---------------------------------------------------------------+
| Host (one user, one gateway process)                          |
|                                                               |
|  knowlageMCP-gateway                                           |
|  +---------------------------------------------------------+  |
|  | Transports                                              |  |
|  |  - HTTP listener 127.0.0.1:2230 (agent-facing)         |  |
|  |      /v1/health, /v1/bridge/*, /v1/tools/*, /mcp        |  |
|  |  - Unix admin socket, mode 0600, peer-uid checked      |  |
|  |                                                         |  |
|  | KnowledgeService (single entry for all transports)      |  |
|  |  identity / policy / knowledge / retrieval / review /   |  |
|  |  evidence  (all limits and ACLs live here)              |  |
|  |                                                         |  |
|  | SQLite (WAL, FK, busy timeout 5 s, single writer +      |  |
|  | process lock file) + evidence dir + backup dir          |  |
|  +---------------------------------------------------------+  |
|  ^                       ^                                    |
|  | UDS (admin JSON/line) | TCP, Bearer session token          |
+--|-----------------------|------------------------------------+
   |                       |
   |                       | terok shield "localhost:2230"
   |                       | (host.containers.internal)
   |                       v
+--+---------------------------------------------------------+
| knowlageMCP-gatewayctl   Agent container(s)                 |
| (host CLI, socket only) knowlageMCP-mcp bridge (stdio MCP)  |
|                            agent process(es) with MCP client|
+-------------------------------------------------------------+
```

Three executables, two machines:

- `knowlageMCP-gateway` runs on the host and owns the database. One process is
  the sole writer; a lock file prevents a second writer on the same database.
- `knowlageMCP-gatewayctl` runs on the host and talks to the gateway only
  through the Unix admin socket. It is the only path for administration.
- `knowlageMCP-mcp` runs in the agent environment as a stdio MCP server and
  forwards to the gateway over HTTP. It keeps the session credential in
  memory and out of model-visible tool results.

### Optional integration boundary

The optional semantic layer sits beside the canonical store, not inside it.
SQLite remains canonical: entries, revisions, ACLs, scopes, lifecycle, review
state, and FTS5 all live there. Qdrant, embeddings, and rerankers are derived
views that fail open, so an unavailable backend degrades to lexical search.
Extractors produce proposals only and never publish. After any vector hit the
service re-applies ACL, scope, and lifecycle from SQLite before showing it.
The layer is off by default, adds no mandatory dependency, and opens no
network connection at import or while disabled.

## What is authenticated, what is not

- **Admin socket**: authenticated by the OS. The socket is mode 0600 in a
  mode-0700 directory, and the gateway verifies the peer uid equals its own
  effective uid via `SO_PEERCRED` where the platform provides it. No token is
  involved.
- **Agent HTTP/MCP surface**: authenticated by a bearer session token issued at
  registration. The gateway stores only the SHA-256 hash of the token, compares
  it in constant time, and never logs or returns it.
- **Registration**: not authenticated. Self-registration is open by default
  (`identity.allow_self_registration = true`) and grants only the default
  grants: read `shared` published entries, create `shared` candidate entries,
  and review read-authorized entries. The `personal` namespace is never
  auto-granted. Anyone who can reach the listener can obtain exactly those
  grants, which is why the listener binds to loopback by default and why
  reachability from untrusted networks must be deliberately enabled.
- **Host/Origin**: the gateway checks the `Host` header against
  `server.allowed_hosts` (default `127.0.0.1`, `localhost`,
  `host.containers.internal`) and rejects `Origin` headers unless allowlisted
  (empty by default, so browser origins are refused).
- **Not authenticated / not claimed**: the declared `model`, `provider`,
  `label`, and `task` fields are unverified metadata. Registration does not
  prove an independent person, model, or process, and a container IP is not an
  identity factor. The protocol session ID returned by an MCP transport
  handshake is not a credential and must not be treated as one.

## Request flows

**Register (first tool call of a task)**

1. Agent calls `register_agent` (bridge or `/mcp`).
2. The bridge POSTs `/v1/bridge/register` with `mode`, optional `ticket`, and
   declared metadata. The gateway rate-limits registration, creates the agent
   and session, issues default grants, and returns
   `{session_token, actor_handle, agent_id, root_agent_id, session_id,
   expires_at, grants}`.
3. The bridge stores the token in memory and returns the actor handle and
   metadata to the model. No token ever reaches the model.

**Read path (context/search/detail)**

1. `get_context` / `search` / `get_entry` resolve the session (bearer) and, on
   a multiplexed bridge, the actor handle.
2. The service enforces ACLs before ranking, restricts to compatible scope and
   current lifecycle, runs FTS5 (with exact-match handling for error text and
   identifiers), ranks, dedupes shared lineage, and fits the response to the
   byte budget. Archived and superseded revisions are hidden by default.

**Write path (record/propose_revision/record_outcome)**

1. Payloads are validated (unknown keys rejected), deduplicated by normalized
   content hash within namespace/scope (an exact repeat returns the existing
   entry ID instead of creating a new one), and stored as candidate revisions
   with provenance.
2. Optimistic concurrency: `propose_revision` requires `expected_revision`; a
   stale value returns `REVISION_CONFLICT`. Idempotency keys make retried
   writes return the original result instead of duplicating state.

**Review path (mentor)**

1. An agent with a pending request gets one via `request_mentor`; the operator
   approves it with `gatewayctl approve`. Approval binds to the exact session
   and scopes, carries a TTL, and is revocable.
2. The mentor leases an item with `review_next` (lease seconds from
   `[review]`), does the work, and submits a result with `submit_review`.
3. Eligibility is re-checked at submission: current grant, lease ownership,
   exact revision, and author/contributor family restrictions. A mentor can
   never approve its own revision or one it contributed to. A supporting review
   from an eligible reviewer with adequate evidence can publish that specific
   revision. Peer votes alone never publish.

## Single-writer model

One gateway process writes to the SQLite database. Connections use WAL,
foreign keys, and a 5 second busy timeout; transactions are short and
parameterized. A lock file in the data directory marks the active writer; a
second `knowlageMCP-gateway` pointed at the same database refuses to start
rather than corrupting state. The gateway serves many concurrent clients from
this one process; it is not a high-availability design and does not claim
multi-host support.

## Why memory content is untrusted data

Everything stored in memory (claims, instructions, procedures, evidence text)
was produced by an agent or imported by an operator, and agents are not
trusted with authority. A retrieved instruction therefore never grants
permission, never overrides the current user's request, never changes gateway
policy, and never authorizes tool execution. Content-based prompt-injection
screening can be advisory; it is not a security boundary. The security
boundary is the combination of loopback binding, Host/Origin checks, bearer
authentication, service-layer ACLs, bounded bodies, and the admin socket's
peer-uid check.

## Restart policy

A gateway restart invalidates all live sessions and review leases. It
preserves agents, grants, knowledge entries, reviews, and the audit log.
Agents register again after a restart; an old name or actor handle is not a
resumption credential, and reconnecting must not pretend to be the old
identity. Draft reviews whose leases expire across a restart are reassignable
and are not marked successful.

## Non-goals

- Training or fine-tuning models, cryptocurrency, consensus by voting,
  economic rewards, arbitrary host command execution, general agent
  orchestration.
- Guaranteed detection of one process pretending to be multiple agents.
- Autonomous browsing or execution of commands extracted from memory.
- Automatic scanning of home directories, repositories, chats, or credentials.
- An always-running LLM, a compulsory vector database, a mandatory cloud
  service, or a full web dashboard.
