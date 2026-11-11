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
```

- `create` uses SQLite's online backup API and writes a snapshot plus a
  manifest (schema version, checksums, evidence inventory, application
  version) to the backup directory.
- `verify SNAP` checks the manifest checksums and integrity.
- `restore SNAP --destination DIR` restores into a separate directory. It
  refuses to overwrite a non-empty destination, and it re-applies the deletion
  ledger so forgotten content does not resurrect.

Retention caveat: scheduled backups keep the configured number of daily and
weekly snapshots, and backups can retain content that was forgotten later.
Prune old snapshots explicitly if you need bounded retention. Backups are on
the same disk and are not protection against disk loss. No forensic erasure
from SSDs or external backups is claimed.

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
reports what would change without writing.

## Index maintenance

```bash
knowlageMCP-gatewayctl index rebuild
```

Rebuilds the FTS5 index from the canonical entries. Search indices are
rebuildable; SQLite remains canonical.

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
