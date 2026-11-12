<!--
SPDX-FileCopyrightText: 2026 René Widera
SPDX-License-Identifier: ISC
-->

# Security policy

## Reporting

Report suspected vulnerabilities privately to the maintainer at
`r.widera@hzdr.de`. Do not open a public issue for an unpatched problem.
Include the affected version, configuration, a minimal reproduction, and the
expected impact. We aim to acknowledge reports within five working days.

## Trust model

- The gateway host is canonical. The SQLite database, evidence objects, and
  backups are owned by the operator account.
- The agent TCP listener exposes health, registration, mentor enrollment,
  bounded MCP tools, and evidence transfer only. It never exposes administration,
  SQL, backup destinations, or host filesystem paths.
- Self-registration is a convenience for trusted local or container networks.
  Anyone who can reach the endpoint can obtain the default grants, which cover
  only shared published knowledge and shared candidate creation.
- Personal namespaces, operator roles, and mentor rights require explicit
  operator approval over the local Unix-domain admin socket.
- Retrieved memory is data, not instructions. It cannot grant permissions,
  override the current user request, or authorize tool execution.

Each guarantee above is enforced by production code and pinned by a regression
test; the mapping is in [docs/security-checklist.md](docs/security-checklist.md).

## Administrative surface

The Unix-domain admin socket is created with mode 0600 and rejects peers whose
uid differs from the service owner. Access is by design equivalent to operator
shell access: through it the operator can export and import to operator-chosen
paths and create, verify, or restore backups. The agent-facing TCP/MCP surface
never exposes filesystem paths, SQL, backup destinations, or administrative
commands.

## Secrets

Session credentials are opaque 32-byte random values. Only their SHA-256 hash is
stored. They are held in bridge memory and are never returned by MCP tools or
written to logs. Uploaded evidence is scanned for common credential patterns and
redacted before storage. Redaction is best effort, never a guarantee: do not
upload secrets in the first place.

## Optional integration egress

Every integration adapter is off by default; with `integrations.enabled =
false` the service makes no integration network I/O. When an operator enables
the layer and configures an adapter, egress is limited to the configured
endpoint:

- Query text is sent to the configured embedding endpoint on every semantic
  search.
- Entry titles and claims are sent to that endpoint during indexing.
- Entry claims are sent to the configured Graphiti endpoint by explicit
  extraction commands.
- Conversation text is sent to the configured Mem0 endpoint by explicit
  extraction commands.

Personal and private namespace content is indexed only when
`integrations.embedding.allow_private_text = true`. API keys are read from the
named environment variables at request time and are never stored in config
files or written to logs. Admin integration commands are available only over
the operator Unix socket. Extractors emit proposals only; they cannot publish
or overwrite canonical entries.

## Dependency vulnerability policy

CI runs `pip-audit` against the installed dependency set in an advisory job.
Every reported finding is triaged before merge for reachability from
knowlageMCP code, severity, exploitability in the supported deployment, and
availability of a fixed or pinned version. The decision and its evidence are
recorded in the pull request or `CHANGELOG.md`; findings are never silently
ignored.

High or critical findings with a reachable path are fixed, upgraded, or pinned
within 30 days. Findings that are unreachable, dev-only, or mitigated by the
host-only deployment are documented as accepted with a re-check date and the
reason. A finding without a fix stays tracked until a fix exists or the
affected dependency is removed.

`reuse lint` checks SPDX and license metadata for files in this repository. It
does not establish redistribution compatibility for third-party dependencies;
dependency license review is separate from the vulnerability triage above.

## Supported versions

The project is pre-1.0. Security fixes target the latest release only.
