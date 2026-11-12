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

Run the one-command clean-environment check before handing off:

```bash
./scripts/check.sh
```

It creates `.venv` when missing, installs `-e '.[dev]'`, and runs lint,
formatting, typing, the full test suite, the quality corpus, REUSE lint, a
package build and a wheel smoke test. `SKIP_BUILD=1 ./scripts/check.sh` skips
the build and smoke test. `pytest` needs the dev extra
(`pip install -e '.[dev]'`), which the script installs.

Optional manual steps:

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src/knowlage_mcp
.venv/bin/pytest -q
.venv/bin/pytest tests/quality -q -s
.venv/bin/reuse lint
```
