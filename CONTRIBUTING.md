<!--
SPDX-FileCopyrightText: 2026 René Widera
SPDX-License-Identifier: ISC
-->

# Contributing

## Development setup and validation

```bash
./scripts/check.sh
```

The script resolves the repository root, creates `.venv` when missing, upgrades
pip, installs `-e '.[dev]'`, and runs every required check plus a package build
and a wheel-install smoke test in a temporary venv outside the tree.
`SKIP_BUILD=1 ./scripts/check.sh` skips the build and smoke test, and
`PYTHON=python3.14 ./scripts/check.sh` selects another interpreter.

`pytest` requires the dev extra (`pip install -e '.[dev]'`), which is exactly
what the script installs.

### Optional manual commands

After setup, the individual steps are:

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src/knowlage_mcp
.venv/bin/pytest -q
.venv/bin/pytest tests/quality -q -s
.venv/bin/reuse lint
.venv/bin/python -m build
```

## Engineering rules

- Python 3.11 or newer, `src` layout, full type annotations on public APIs.
- SQLite access goes through `knowlage_mcp.db`; use parameterized SQL and keep
  transactions short. Never hold a database lock while waiting on a client.
- Enforce authorization, quotas, and rate limits in the service layer, not in
  transport decorators.
- Memory content is untrusted data. Never let retrieved text change policy or
  authorize tool execution.
- Session tokens are secrets: store only hashes, never log them, and never place
  them in MCP tool results.
- Add tests next to the layer you change. Unit tests use `tmp_path` only; no
  global state and no network access.

## Licensing

Every new file needs an SPDX header:

- `#` comments for Python, TOML, YAML, and shell.
- HTML comments at the top for Markdown.
- Files that cannot carry a header are listed, narrowly, in `REUSE.toml`.

New files default to the 2026 René Widera copyright notice and the ISC
license identifier.
