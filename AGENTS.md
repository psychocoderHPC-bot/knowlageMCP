<!--
SPDX-FileCopyrightText: 2026 René Widera
SPDX-License-Identifier: ISC
-->

# Agent instructions

This repository builds `knowlageMCP`, a host-canonical knowledge memory service
for AI coding agents with a stdio MCP bridge.

## Layout

- `src/knowlage_mcp/` is the only package. Keep the `src` layout.
- `models.py` owns enums, typed models, and `ServiceError`. Do not redefine error
  codes or lifecycle values elsewhere.
- `config.py` owns defaults and path resolution. Never create directories at
  import time.
- `db.py` and `migrations.py` own all SQLite access and ordered schema changes.
  Add a new migration; never edit an applied one.
- `evidence.py` owns bounded, hashed, redacted blob storage.
- `integrations/` holds optional protocol boundaries; the core must work with
  the null implementations.
- `resources/SKILL.md` is packaged. Any copy under `skills/` must stay
  byte-identical.

## Rules for changes

- Use `ruff` and `mypy` cleanly; do not add blanket `# type: ignore`.
- Enforce policy, quotas, and limits in the service layer, not in route
  decorators.
- Never log or return session tokens, and never store plaintext credentials.
- Treat verified facts as facts and clearly label hypotheses in both code and
  prose.
- Tests use only `tmp_path` and local resources; no network and no shared state.
- Every file needs an SPDX header or a narrow `REUSE.toml` annotation.

## Validation

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src/knowlage_mcp
.venv/bin/pytest tests/unit -q
.venv/bin/reuse lint
```
