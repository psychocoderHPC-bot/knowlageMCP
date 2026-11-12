<!--
SPDX-FileCopyrightText: 2026 René Widera
SPDX-License-Identifier: ISC
-->

# knowlageMCP

knowlageMCP is a host-owned knowledge service for AI coding agents that run in
terok containers. Agents register themselves through MCP and store scoped,
provenance-tracked entries (preferences, instructions, decisions, observations,
hypotheses, procedures, failures, task summaries). At task start they retrieve
a small, budgeted bundle of relevant entries instead of replaying history. An
operator on the host administers everything through a CLI, and an optional
externally started AI mentor reviews candidate entries after manual approval.
All canonical data (SQLite database, evidence files, backups) stays on the
host; the container sees only a small HTTP/MCP surface. The core runs without
API keys, embeddings, a mentor, or any external service.

## Components

| Executable | Runs on | Responsibility |
|---|---|---|
| `knowlageMCP-gateway` | Host | Service lifecycle, SQLite database (single writer), identity registry, policy, FTS5 search, review queue, HTTP/MCP endpoints, Unix admin socket |
| `knowlageMCP-gatewayctl` | Host | Bootstrap assistance, status, approvals, namespaces/grants, inspect/correct/forget, backup/restore, diagnostics |
| `knowlageMCP-mcp` | Agent environment | Stdio MCP bridge; only the gateway host/port needs configuration |

## Requirements

- Linux host, Python 3.11 or newer (tested on 3.11, 3.12, 3.13 and 3.14),
  `git`, and `pipx` (or a virtualenv).
- SQLite with FTS5 (standard in current system SQLite builds).
- For container access: terok network access to the host service, or a
  manually reachable host port. See the note under "Agent setup".

Publication to PyPI is not part of this release. Install from the git URL or
a local clone. The repository may carry an unreleased 0.1.0 version.

## Host setup

**Host terminal 1** (the machine that will keep the database):

```bash
git clone https://github.com/psychocoderHPC-bot/knowlageMCP.git
cd knowlageMCP
pipx install .
knowlageMCP-gateway --bootstrap
```

The wizard asks a few questions, all with usable defaults, then writes the
config, creates private data directories, and initializes the database. In CI
or scripts use:

```bash
knowlageMCP-gateway --bootstrap --non-interactive
```

Optional overrides: `--host`, `--port`, `--database`, `--evidence-dir`,
`--backup-dir`, `--no-systemd`, `--start` / `--no-start`, `--force`.

**Host terminal 2** (same machine; the gateway must be running):

```bash
knowlageMCP-gatewayctl status
knowlageMCP-gatewayctl smoke-test
```

`status` prints version, schema version, listener, session and agent counts.
`smoke-test` exercises health, registration, a record, and a search against a
live service and prints the steps it ran.

### Run as a systemd user service

The repository ships `systemd/knowlage-mcp-gateway.service`. Install and start
it with:

```bash
cp systemd/knowlage-mcp-gateway.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user start knowlage-mcp-gateway
systemctl --user status knowlage-mcp-gateway
```

Stop with `systemctl --user stop knowlage-mcp-gateway`. The service starts
without a mentor and without API keys. Optionally, keep the service alive
after logout with `loginctl enable-linger $USER`; the unit does not enable
linger automatically.

## Agent setup

**Inside the agent container**, install the bridge (only endpoint/port
configuration is needed; registration itself happens through MCP):

```bash
pipx install "git+https://github.com/psychocoderHPC-bot/knowlageMCP.git"
```

or clone the repository and run `pipx install .`. Then register the MCP server
in your client. OpenCode-style configuration (the bridge defaults the host to
`host.containers.internal`, which matches the terok host-service arrangement):

```json
{
  "mcp": {
    "knowledge": {
      "type": "local",
      "command": ["knowlageMCP-mcp", "--port", "2230"],
      "enabled": true
    }
  }
}
```

Overrides, in precedence order: `--url` wins over `--host`/`--port`, which win
over the environment variables `KNOWLAGEMCP_URL`, `KNOWLAGEMCP_HOST`,
`KNOWLAGEMCP_PORT`. The defaults are `host.containers.internal` and port 2230.
This is the only manually configured connection information; after the client
connects, the agent calls the `register_agent` tool itself and no token is
ever copied by hand.

Validate the client's actual MCP schema against its own documentation; the
block above is the intended shape, not a guarantee for every client.

### Terok shield and container reachability

The container needs network permission to reach the gateway. In the terok
project configuration:

```yaml
shield:
  allow:
    - "localhost:2230"
```

Two caveats:

- Existing running tasks were started with the old shield rules and may need
  recreation before the new allow rule takes effect.
- Binding a loopback port on the host does not automatically make it
  reachable from every container. If the bridge cannot connect, check
  `knowlageMCP-gatewayctl doctor` on the host (it prints host/container
  connection hints) and verify the actual terok host-service forwarding for
  your container runtime.

## Optional mentor approval

The mentor is any external AI session the operator starts manually with the
same bridge and the mentor skill (`skills/knowlageMCP/mentor.md`). It requests
enrollment through the `request_mentor` tool; the operator approves on the
host:

**Host terminal:**

```bash
knowlageMCP-gatewayctl enrollments
knowlageMCP-gatewayctl approve REQUEST --role mentor --ttl 2h --namespace shared
```

Replace `REQUEST` with the request ID shown by `enrollments` (or by `watch`);
the comparison code printed next to it is for operator verification only and
is not an approval credential. Deny or revoke at any time:

```bash
knowlageMCP-gatewayctl deny REQUEST
knowlageMCP-gatewayctl revoke SESSION
```

Approval is bound to the exact pending session and requested scopes, expires
after the TTL, and is revocable. Without an approved mentor, all normal
read/write work continues and reviews simply stay queued.

## Optional semantic search

Hybrid search (lexical FTS5 plus a Qdrant vector index), a bounded reranker,
and extraction adapters are optional and off by default. To enable a minimal
setup in the config file:

```toml
[integrations]
enabled = true

[integrations.embedding]
provider = "openai"
base_url = "http://127.0.0.1:8080"
dimensions = 1536

[integrations.vector]
backend = "qdrant"
url = "http://127.0.0.1:6333"
vector_size = 1536

[integrations.reranker]
provider = "lexical"
```

then rebuild the index once:

```bash
knowlageMCP-gatewayctl integrations reindex
```

The core runs fully without any of it: with `integrations.enabled = false`
search stays lexical, no network I/O happens, and no secrets are required.
Search remains fail-open when a backend is down. Details, operations, and
failure semantics are in [docs/integrations.md](docs/integrations.md).

## Smoke test

With the gateway running and at least one agent container connected (the
smoke test registers its own temporary agent), run on the host:

```bash
knowlageMCP-gatewayctl smoke-test
```

Expected output (abridged; IDs are random):

```
health            ok (protocol 1.0, schema 1)
register          ok (agent_..., actor_...)
record            ok (entry_...)
search            ok (1 result)
close             ok
smoke test passed
```

## Measured baseline

See [docs/testing.md](docs/testing.md) for the 2026-11-11 retrieval and
latency baseline on the reference hardware.

## Documentation and license

- [docs/architecture.md](docs/architecture.md) - trust boundaries, flows, restart policy
- [docs/configuration.md](docs/configuration.md) - every config key and limit
- [docs/operations.md](docs/operations.md) - bootstrap, CLI, backup, forgetting
- [docs/protocol.md](docs/protocol.md) - MCP tools, HTTP bridge API, errors
- [docs/data-model.md](docs/data-model.md) - tables, lifecycle, provenance
- [docs/testing.md](docs/testing.md) - test layout, quality corpus, measured baseline, CI
- [docs/troubleshooting.md](docs/troubleshooting.md) - symptom to fix
- [docs/mentor.md](docs/mentor.md) - mentor operating procedure
- [docs/integrations.md](docs/integrations.md) - adapter boundaries, JSONL format
- [docs/security-checklist.md](docs/security-checklist.md) - trust-boundary guarantees mapped to enforcement and regression tests
- [LICENSE](LICENSE) (ISC), [SECURITY.md](SECURITY.md), [CONTRIBUTING.md](CONTRIBUTING.md)

### Limitations

- Self-registration does not prove an independent person, model, or process;
  it is a convenience for a single-operator trusted environment.
- A process can still register multiple identities; the system does not detect
  Sybil behaviour in general.
- Backups live on the same disk and do not protect against disk loss.
- There is no high availability, no multi-host SQLite, and no web dashboard.
- Search is lexical (FTS5) by default. Semantic/hybrid search is an optional
  layer, off by default, and never mandatory (see "Optional semantic search").
