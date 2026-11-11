<!--
SPDX-FileCopyrightText: 2026 René Widera
SPDX-License-Identifier: ISC
-->

# Changelog

All notable changes to this project are documented here. The format follows
Keep a Changelog and the project uses Semantic Versioning.

## [0.1.0] - 2026-11-11

### Added

- Foundation package layout for `knowlage_mcp` with version, protocol version,
  and the canonical tool-name registry.
- Typed request/response models, enums, error codes, and the service error
  envelope used by every transport.
- Configuration schema, default resolution, duration parsing, validation,
  atomic TOML writes, redaction, and config diffing.
- SQLite database helpers with WAL pragmas, threaded connections, immediate
  transactions, an ordered migration runner, a writer lock, backup, and
  integrity checks.
- Version 1 database schema including provenance tables and an FTS5 index.
- Bounded evidence store with hash deduplication, quota enforcement, atomic
  writes, and secret redaction.
- Protocol boundary interfaces plus offline null integrations.
- Packaged agent skill resource, example configuration, licensing, and unit
  tests for the foundation.
