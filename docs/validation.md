<!--
SPDX-FileCopyrightText: 2026 René Widera
SPDX-License-Identifier: ISC
-->

# Release validation: 0.1.0

Scope: reproducible release validation for knowlageMCP 0.1.0 at base commit
`eb6fe768e958667333b17a0d6406eb7e61d375fd` (`eb6fe76`) plus the review-response
change that adds `scripts/check.sh`, the JSON benchmark report and this record.
Date: 2026-11-12.

## Environment

| Item | Value |
|---|---|
| Python | CPython 3.12.3 and CPython 3.14.7 in this container; CI matrix covers 3.11, 3.12, 3.13 and 3.14 |
| SQLite | 3.45.1 (Python 3.12), 3.53.1 (Python 3.14) |
| OS and kernel | Linux 6.17.0-1033-oem, x86_64, glibc 2.39 |
| CPU | Intel Core Ultra 7 255U, 14 hardware threads |
| Prior baseline CI | GitHub Actions run [43187281569](https://github.com/psychocoderHPC-bot/knowlageMCP/actions/runs/43187281569), success, 2026-11-12T08:57:55Z |

## Commands

```bash
./scripts/check.sh
/home/dev/.local/bin/python3.14 -m pytest -q
.venv/bin/python benchmarks/run_bench.py --entries 10000 --json-out benchmarks/baseline_v1.json
```

The 3.14 run used a separate venv with `pip install -e '.[dev]'`. The wheel
clean install is the last step of `scripts/check.sh`: a temporary venv outside
the source tree installs `dist/*.whl`, imports the package and integration
modules, asserts the packaged `SKILL.md`, and runs `--version` for the gateway,
gatewayctl and MCP entry points.

## Results

| Check | Result |
|---|---|
| `ruff check .` | pass |
| `ruff format --check .` | pass, 105 files formatted |
| `mypy src/knowlage_mcp` | pass, 34 source files |
| `pytest -q` on CPython 3.12.3 | 401 passed in ~64.02 s |
| `pytest -q` on CPython 3.14.7 | 401 passed in ~60.47 s |
| `pytest tests/quality -q -s` | 4 passed; recall@5 1.0000; precision@5 0.3874; context scenarios 17/17; 0 no-answer, lineage or forbidden violations |
| `reuse lint` | pass, 116/116 files (includes the concurrent documentation update) |
| `python -m build` | sdist and wheel built |
| Wheel smoke test | pass; all three console scripts report 0.1.0 (protocol 1.0) |

Benchmark reference from `benchmarks/baseline_v1.json`: 10,000 synthetic
entries, seed 7, 5 results and a 4,000-byte response budget per search.

| Mode | count | p50 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|
| 1 client | 50 | 15.756 ms | 38.900 ms | 40.866 ms | 40.866 ms |
| 10 clients | 500 | 37.800 ms | 339.021 ms | 430.272 ms | 455.077 ms |

`db_bytes` is 20,021,248, `avg_response_bytes` is 1904.7, `max_response_bytes`
is 4388, and `correctness_ok` is true.

## Review follow-up (2026-11-12)

The external review found no confirmed functional bug; the earlier pytest
failure was an environment issue (dev extras not installed), now covered by the
one-command script.

- Required: reproducible clean-environment validation of ruff, mypy, tests and
  REUSE: `scripts/check.sh` (fresh `.venv`, dev extras, full suite, quality
  gate, REUSE, build, wheel smoke), this record, and CI as the authoritative gate.
- Nice-to-have: versioned threat-model checklist mapping trust boundaries to
  enforcement points and tests: `docs/security-checklist.md`.
- Nice-to-have: one command to install all development/test tools from a fresh
  checkout: `scripts/check.sh`; `CONTRIBUTING.md` and `AGENTS.md` point to it.
- Nice-to-have: machine-readable benchmark baseline with environment and corpus
  metadata: `benchmarks/run_bench.py --json-out` and `benchmarks/baseline_v1.json`.
- Nice-to-have: operator checklist for exposing the gateway beyond loopback:
  "Exposing the gateway beyond loopback" in `docs/operations.md`.

## Limitations

- No live cloud backend. Qdrant, embeddings, rerank, Graphiti and Mem0 paths are
  faked over loopback in the integration tests.
- No non-loopback deployment and no multi-host validation.
- No live-model task evaluation and no paid API access.
- Timings are informational and never a CI gate. This shared host measured a
  10-client p95 above the 250 ms target; rerun on an idle host before comparing
  numbers.

CI is the authoritative reproducible check:
[`.github/workflows/ci.yml`](https://github.com/psychocoderHPC-bot/knowlageMCP/actions/workflows/ci.yml).
