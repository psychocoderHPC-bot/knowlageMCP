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

## Secrets

Session credentials are opaque 32-byte random values. Only their SHA-256 hash is
stored. They are held in bridge memory and are never returned by MCP tools or
written to logs. Uploaded evidence is scanned for common credential patterns and
redacted before storage. Redaction is best effort, never a guarantee: do not
upload secrets in the first place.

## Supported versions

The project is pre-1.0. Security fixes target the latest release only.
