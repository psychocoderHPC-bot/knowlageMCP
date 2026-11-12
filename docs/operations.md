<!--
SPDX-FileCopyrightText: 2026 René Widera
SPDX-License-Identifier: ISC
-->

# Operations

## Bootstrap

Interactive, on the host:

```bash
knowlageMCP-gateway --bootstrap
```

The wizard asks:

1. Listen address and port (default `127.0.0.1`, `2230`).
2. Database file path (default `~/.local/share/knowlageMCP/knowledge.sqlite3`).
3. Evidence directory (default `~/.local/share/knowlageMCP/evidence/`).
4. Backup directory (default `~/.local/share/knowlageMCP/backups/`; the wizard
   explains that backups are not protection against disk loss).
5. Allow standard agent self-registration (default yes).
6. Install a systemd user unit (interactive default yes), asked separately from
   whether to start it now.

It then shows a summary, validates, creates private directories (0700),
writes the config (0600, atomic rename), initializes the schema, and prints
the next commands. Re-running preserves existing data and config unless
explicit replace options are supplied; it never truncates an existing database
and validates port availability without silently picking a different port.

Non-interactive (CI):

```bash
knowlageMCP-gateway --bootstrap --non-interactive
```

with optional overrides `--host`, `--port`, `--database`, `--evidence-dir`,
`--backup-dir`, `--no-systemd`, `--start` / `--no-start`, `--force`, and
`--config PATH`. Non-interactive bootstrap never starts a service unless
`--start` is given explicitly.

## Starting and stopping the gateway

```bash
knowlageMCP-gateway                      # foreground, config from default path
knowlageMCP-gateway --config PATH        # foreground, explicit config
```

systemd user service (see `systemd/knowlage-mcp-gateway.service`):

```bash
cp systemd/knowlage-mcp-gateway.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user start knowlage-mcp-gateway
systemctl --user stop knowlage-mcp-gateway
systemctl --user status knowlage-mcp-gateway
```

The service starts without a mentor and without API keys. `Restart=on-failure`
retries after a crash; the restart policy invalidates live sessions and leases
but preserves agents, grants, knowledge, and the audit log. `loginctl
enable-linger $USER` keeps the service running after logout; it is optional and
not enabled automatically.

## Status and diagnostics

```bash
knowlageMCP-gatewayctl status [--json]
knowlageMCP-gatewayctl doctor
knowlageMCP-gatewayctl smoke-test [--json]
knowlageMCP-gatewayctl watch
```

- `status`: version, protocol version, schema version, listener, session and
  agent counts, database size against the soft limit.
- `doctor`: checks config validity, permissions, SQLite/FTS5 availability,
  migration version, free space, the listener, the admin socket mode and path,
  optional backend readiness, and host/container connection hints. It never
  leaks secrets. Each check reports its name, result, and a short detail; a
  failing check lowers the overall `ok` flag instead of being silently dropped.
- `smoke-test`: runs health, registration, record, search, and close against
  the live service and prints each step.
- `watch`: streams audit events live until you interrupt it.

The admin socket lives at `storage.runtime_socket` (default
`$XDG_RUNTIME_DIR/knowlageMCP/admin.sock`; the parent directory is 0700 and the
socket file is 0600, so only the gateway user can reach it).
`config.example.toml` shows a portable `~/.local/state/knowlageMCP/admin.sock`
path so the example also works where `$XDG_RUNTIME_DIR` is unset; a gateway run
with the default configuration uses the runtime directory.

## Agents, enrollments, and approvals

```bash
knowlageMCP-gatewayctl agents [--json]
knowlageMCP-gatewayctl enrollments [--json]
knowlageMCP-gatewayctl approve REQUEST --role mentor --ttl 2h --namespace shared
knowlageMCP-gatewayctl deny REQUEST
knowlageMCP-gatewayctl revoke SESSION
```

- `agents` lists registered agents with enrollment mode, family, and grants.
- `enrollments` lists pending and recently decided mentor requests, showing
  the `request_id` and the short human comparison code for each.
- `approve REQUEST` takes the `request_id`; the comparison code shown next to
  it is for operator verification only (confirming that the row on the host
  matches the request the agent reported) and is not an approval credential.
  Approval binds the mentor role to the exact pending session and requested
  scopes, with the given TTL (default from `[identity]
  mentor_approved_lifetime`). Expired requests cannot be approved.
- `deny` records a negative decision for the request.
- `revoke` revokes a live session; revocation cascades to child families and
  invalidates outstanding delegation tickets.

## Namespaces and grants

```bash
knowlageMCP-gatewayctl namespaces list
knowlageMCP-gatewayctl namespaces create NAME
knowlageMCP-gatewayctl grants add AGENT --namespace NS --role ROLE
```

Namespace creation is operator-only. Roles are `reader`, `contributor`,
`reviewer`, `mentor`, and `operator`. The `personal` namespace is operator-only
until explicitly granted.

## Knowledge inspection and maintenance

```bash
knowlageMCP-gatewayctl search QUERY
knowlageMCP-gatewayctl inspect ENTRY
knowlageMCP-gatewayctl reviews
```

`inspect` prints the full entry: revisions, sources, evidence references,
reviews, and the provenance chain.

### Operator memory operations

```bash
knowlageMCP-gatewayctl memory add --kind K --namespace NS --file F [--title T]
knowlageMCP-gatewayctl memory correct ENTRY --expected-revision REV --file F
knowlageMCP-gatewayctl memory retract ENTRY --reason R
knowlageMCP-gatewayctl memory forget ENTRY --dry-run
knowlageMCP-gatewayctl memory forget ENTRY --yes
```

These are the trusted operator path for confirmed preferences and
corrections. Operator-confirmed preferences can be published directly with
`operator_confirmed` evidence; agent-reported content cannot.

- `add` ingests a file as a new entry revision with the given kind
  (`preference`, `instruction`, `decision`, `observation`, `hypothesis`,
  `procedure`, `failure`, `task_summary`).
- `correct` appends a new revision under optimistic concurrency; it fails
  with exit 4 if `--expected-revision` does not match.
- `retract` immediately excludes the entry from retrieval with the stated
  reason.
- `forget --dry-run` reports what would be purged; `forget --yes` executes.
  Forgetting excludes the data from retrieval immediately and purges active
  revisions, unreferenced sources, unreferenced evidence, FTS entries, caches,
  and derived summaries. It preserves minimal non-content audit tombstones and
  records the deletion in the deletion ledger.

## Backup

```bash
knowlageMCP-gatewayctl backup create
knowlageMCP-gatewayctl backup verify SNAP
knowlageMCP-gatewayctl backup restore SNAP --destination DIR
knowlageMCP-gatewayctl backup prune [--keep-daily N] [--keep-weekly N] [--dry-run] [--json]
```

- `create` uses SQLite's online backup API and writes a snapshot plus a
  manifest (schema version, checksums, evidence inventory, application
  version) to the backup directory.
- `verify SNAP` checks the manifest checksums and integrity.
- `restore SNAP --destination DIR` restores into a separate directory. It
  refuses to overwrite a non-empty destination, and it re-applies the deletion
  ledger so forgotten content does not resurrect.

  The Qdrant vector index is derived and is not part of the backup snapshot, so
  after a restore the operator should run
  `knowlageMCP-gatewayctl integrations reindex` to rebuild it for the restored
  database. Stale points are never trusted for ACL or scope: hits are
  re-checked against SQLite, so the only cost is incomplete semantic recall
  until the rebuild.
- `prune` applies the configured retention policy explicitly. It keeps the
  newest snapshot for each of the most recent `--keep-daily` days (default
  `backup.keep_daily`, 7) and the newest snapshot for each of the most recent
  `--keep-weekly` ISO weeks (default `backup.keep_weekly`, 4). The newest
  snapshot is always kept even when both limits are zero. Snapshot directories
  are recognised by the UTC timestamp in `snapshot-<timestamp>-<uuid>`; a
  symlinked snapshot directory is ignored and never removed. `--dry-run`
  reports the would-be removals without deleting anything, and `--json` emits
  the removed and kept paths. Negative limits fail with `INVALID_ARGUMENT`
  (exit 4).

The scheduler calls the same pruning helper after each scheduled snapshot.
Retention cannot erase content that was forgotten between snapshots and then
captured by an older snapshot, and backups on the same disk are not protection
against disk loss. No forensic erasure from SSDs or external backups is
claimed.

## Export and import

```bash
knowlageMCP-gatewayctl export --namespace NS --output FILE
knowlageMCP-gatewayctl import FILE --dry-run
```

The format is JSONL with a schema version, sources, immutable revision IDs,
and namespaces. Exports preserve source attribution and any stated content
license; operational knowledge is not automatically relicensed. Imports are
operator-initiated, idempotent, and untrusted by default: imported role names
or `verified` labels never grant authority. `--dry-run` validates the file and
reports what would change without writing. Imports are bounded to 32 MiB and
50,000 records; split larger files before importing.

## Index maintenance

```bash
knowlageMCP-gatewayctl index rebuild
```

Rebuilds the FTS5 index from the canonical entries. Search indices are
rebuildable; SQLite remains canonical.

## Optional integrations and hybrid retrieval

```bash
knowlageMCP-gatewayctl integrations status [--json]
knowlageMCP-gatewayctl integrations probe [--json]
knowlageMCP-gatewayctl integrations reindex [--json]
knowlageMCP-gatewayctl integrations extract ENTRY [--json]
knowlageMCP-gatewayctl integrations proposals [--entry ENTRY] [--state STATE] [--json]
knowlageMCP-gatewayctl integrations import-memories FILE [--dry-run] [--json]
```

Every adapter is disabled by default under `[integrations]`. The core continues
to serve lexical FTS5 search when an adapter is absent or down.

- `status` reports adapter names, availability, and the recorded index
  generations. It contains no secrets.
- `probe` performs bounded health checks against the enabled adapters. A
  disabled integration layer returns an empty map without network work.
- `reindex` rebuilds FTS5 and, when `integrations.vector.backend` is not
  `none`, embeds and upserts the current published, disputed, and candidate
  revisions into a new generation-specific Qdrant collection. The previous
  ready generation for a different model or dimension set is marked `stale`;
  the new one is marked `ready`. Embedding model or dimension changes therefore
  require an explicit reindex and never mix vectors. Personal namespaces are
  skipped unless the configured embedding provider may send private text
  (`integrations.embedding.allow_private_text` for a remote provider). After a
  restart the service adopts the most recent ready generation when its model
  and dimensions still match the configured embedding adapter, so semantic
  search resumes without a second reindex; a mismatch keeps the vector index
  unavailable until an operator reindexes.
- `extract ENTRY [ENTRY ...]` stores extractor proposals in
  `extraction_proposals` with state `proposed`, making one extractor call per
  unique entry. A batch larger than `integrations.graphiti.max_calls_per_task`
  is rejected with `INVALID_ARGUMENT` naming the cap. It never creates,
  publishes, or modifies entries.
- `proposals` lists advisory extraction proposals, newest first, capped at 50
  rows. Filter with `--entry` and `--state` (default `proposed`). It accepts or
  rejects nothing: acceptance goes through the normal candidate and review
  path.
- `import-memories FILE` reads JSONL records of
  `{"role","text","source_role","task","entry_id","revision_id"}` and writes new
  candidate entries to the initial shared namespace
  (`identity.initial_namespace`) through the normal knowledge insert path. It
  preserves the original `role`/`source_role`, `unverified` evidence, a `mem0`
  tag, and a `derived_from` provenance edge when the optional `entry_id` or
  `revision_id` identifies a known revision. The extractor receives the
  conversation role as `messages[0].role`. It deduplicates by content hash,
  never publishes, never overwrites, and budgets the run to
  `integrations.mem0.max_items` extractor calls; every call counts, even one
  that returns no proposals, and the result reports `calls`. `--dry-run` still
  calls the extractor to count proposals, then reports counts without writing.

Search stays lexical unless the embedding and vector adapters are enabled and
healthy. When semantic use succeeds, search and context responses report
`retrieval_backend="hybrid"`, `semantic_used`, `reranked`, and
`index_generation`. Failures are fail-open: the lexical result is returned with
a response warning and no 5xx. A vector hit is re-checked against the canonical
ACL, scope, lifecycle, and validity rules before display; a reranker can only
reorder already authorized candidates.

Deletion propagates to the derived index. `memory retract` removes the entry
from Qdrant after archiving the canonical revision. `memory forget` deletes the
entry's points and index-state rows after the database purge. Cleanup is
best-effort: a failure never blocks the canonical operation, and the returned
dict carries a `warning` when the derived index could not be updated.

### Privacy gate

A configured remote embedding provider is an explicit operator decision, so
semantic search is allowed even when `allow_private_text = false`. Query text
is sent to the configured embedding endpoint (`integrations.embedding.base_url`)
on every semantic search. `integrations.embedding.allow_private_text` gates
indexing: personal and private namespace content is only embedded and indexed
when it is `true`. With `false`, that content stays in SQLite and is only
reachable through lexical search. Local providers (`hash`) and the
disabled/none provider never leave the host.

## Limits

Rate and budget limits live in `[limits]` (see
[configuration.md](configuration.md)). The service enforces a per-session
request bucket and a process-wide bucket. Admin socket calls do not consume
either request bucket, and registration has its own limiter.

| Scope | Keys | Default |
|---|---|---|
| Per-session requests | `requests_per_minute`, `request_burst` | 60 per minute, burst 20 |
| Global requests | `requests_per_minute_global`, `request_burst_global` | 6000 per minute, burst 1000 |
| Registration | `registration_per_minute_per_source`, `registration_per_minute_global` | 10 per source, 60 globally |
| Candidate writes | `candidate_writes_per_hour`, `candidate_writes_per_hour_global` | 20 per agent, 200 globally |

## Exposing the gateway beyond loopback

The default binding is loopback (`server.host = "127.0.0.1"`). For a single
container runtime the default `server.allowed_hosts` list already includes
`host.containers.internal`, so a terok container can reach a loopback port
without further configuration. Do not widen `allowed_hosts` to reach a
container when the runtime already forwards loopback: it is the wrong fix and
it changes the trust model.

If you must bind the gateway to a non-loopback address, change all four of
these together. The allowlist is enforced per request by the `Host`/`Origin`
middleware; there is no automatic widening, and `doctor` reports a
`listener_exposure` warning when the bind address is not loopback:

1. `server.host`: the listen address (for example `0.0.0.0` or a LAN IP).
2. `server.allowed_hosts`: the exact `Host` header values you will accept.
   Keep this minimal and explicit. A value other than the loopback defaults
   makes the gateway reject every request whose `Host` header does not match.
3. `server.allowed_origins`: browser `Origin` values to accept. Leave empty
   for the non-browser agent surface. Only add an origin if a browser UI talks
   to the gateway directly.
4. `limits` and the listener exposure in the terok shield. The shield grant
   `localhost:2230` covers loopback only; a non-loopback address needs its own
   explicit allow rule and, for already-running tasks, recreation of the task
   so the new grant takes effect.

The agent-facing TCP/MCP surface never exposes administration, SQL, backup
destinations, or host filesystem paths, so exposing it does not widen the admin
surface. The Unix-domain admin socket stays on the host and is not reachable
through the network listener.

Before you bind beyond loopback, confirm:

- `allowed_hosts` and `allowed_origins` contain only the values you intend.
- The terok shield allows the new address for the containers that need it.
- Rate limits in `[limits]` are appropriate for the wider exposure
  (registration and request buckets apply across all sessions).
- You have a backup, because a wider listener increases the chance of
  malformed or hostile traffic reaching the parser.

The trust-boundary claims and the regression tests that pin them are in
[security-checklist.md](security-checklist.md).

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Error (validation, I/O, service failure) |
| 2 | Usage error (bad arguments) |
| 3 | Gateway or admin socket unavailable |
| 4 | Conflict or invalid state (stale revision, validation problems in `config check`, expired request) |

## Troubleshooting

See [troubleshooting.md](troubleshooting.md) for the symptom to fix table and
`knowlageMCP-gatewayctl doctor` for structured diagnostics.
