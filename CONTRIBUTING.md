<!--
SPDX-FileCopyrightText: 2026 René Widera
SPDX-License-Identifier: ISC
-->

# Contributing

## Development setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

## Required checks

Run all of them before handing off a change:

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src/knowlage_mcp
.venv/bin/pytest tests/unit -q
.venv/bin/reuse lint
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
