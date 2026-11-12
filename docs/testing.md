<!--
SPDX-FileCopyrightText: 2026 René Widera
SPDX-License-Identifier: ISC
-->

# Testing

## Layout

```
tests/
  conftest.py            shared fixtures; everything isolated under tmp_path
  unit/                  no transports, no long sleeps, injectable clock
  integration/           real temporary SQLite, real HTTP transports
  security/              security-boundary regression tests
  fixtures/              synthetic, versioned inputs (no real operational data)
    export_v1.jsonl      golden version 1 JSONL export-format fixture
benchmarks/
  generate_corpus.py     builds the labelled retrieval corpus
  run_bench.py           runs retrieval and fault benchmarks
  corpus_v1.json         the versioned deterministic corpus
```

Markers: `integration` (real transports or processes), `security`
(boundary regressions), `slow` (more than a few seconds). All tests run with
`tmp_path` only, no network, no shared state, and no paid API access.

## Inventory

The current suite has 401 tests:

| Layer | Tests |
|---|---|
| `tests/unit` | 314 |
| `tests/integration` | 43 |
| `tests/security` | 40 |
| `tests/quality` | 4 |

Fixtures such as `tests/fixtures/export_v1.jsonl` are inputs, not tests. The
counts are the observed result of `pytest --collect-only -q`; update them
when the suite changes. The new integration suites are
`tests/unit/test_integrations_base.py`, `test_embeddings.py`,
`test_qdrant.py`, `test_rerank.py`, `test_graphiti.py`, `test_mem0.py`, and
`test_semantic_retrieval.py` (adapter units over fake transports, hybrid merge
and ACL re-check, offline fallback), plus
`tests/integration/test_integrations_admin.py` (gatewayctl admin surface,
proposals), `tests/integration/test_integrations_http.py` (real-HTTP
end-to-end hybrid retrieval with a mock Qdrant), and
`tests/integration/test_integrations_e2e_http.py` (real-HTTP end-to-end
contract tests over loopback fake Qdrant, embeddings, Graphiti and Mem0
servers built in `tests/integration/fake_backends.py`).

## Running

```bash
./scripts/check.sh
```

The script resolves the repository root, creates `.venv` when missing, upgrades
pip, installs `-e '.[dev]'`, and runs every prescribed check in order: `ruff
check`, `ruff format --check`, `mypy`, the full `pytest` suite, the quality
corpus, `reuse lint`, `python -m build`, and a wheel-install smoke test in a
temporary venv outside the source tree. Set `SKIP_BUILD=1` to skip the build
and smoke test, or `PYTHON=python3.14` to select another interpreter.

Individual steps, after the script has created the environment:

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src/knowlage_mcp
.venv/bin/pytest -q
.venv/bin/pytest tests/quality -q -s
.venv/bin/reuse lint
.venv/bin/python -m build
```

`pytest` needs the dev extra (`pip install -e '.[dev]'`), which is what the
script installs. CI (`.github/workflows/ci.yml`) runs the same lint, type and
test steps on Python 3.11 through 3.14, plus `reuse lint`, a package build, and
a wheel-install smoke test in a fresh venv outside the tree.

## Unit layer

- Config: defaults, TOML parsing, path resolution, bounds, secret redaction,
  atomic write and reload failure.
- Identity: hierarchy, delegated grants, expiry, revocation, actor ambiguity,
  review eligibility.
- Knowledge: entry state transitions, immutable revisions, version conflicts,
  provenance lineage, duplicate behavior.
- Retrieval: scope filtering, ranking fixtures, empty results, UTF-8-safe
  truncation, warning preservation.
- Limits: rate and budget limiting with a fake clock; no real sleeps. The
  global request bound is covered by a unit test that advances an injectable
  clock to prove refill over time.
- Backup retention: synthetic snapshot directories with controlled UTC
  timestamps and identifiers prove daily and ISO-week retention, per-day and
  per-week selection, newest-snapshot protection, dry-run behavior,
  symlink refusal, and rejection of negative limits.
- Export format: `tests/fixtures/export_v1.jsonl` is imported (dry-run and
  real), asserted to stay candidate/unverified with its source preserved, then
  exported and re-imported to prove header schema version, stable entry and
  revision identifiers, and idempotent skips without duplicates.
- Reload robustness: an invalid TOML or invalid configuration raises
  `ServiceError` without changing the service or any submodule configuration,
  and a simulated failure during the swap restores every holder to the old
  configuration.
- Import robustness: path-traversal references, non-object records,
  non-dict revisions, bad namespaces or entry IDs, bad tags/scope, oversized
  claims, and unknown record types count as `malformed` and skip only the
  affected record; the transaction still commits valid revisions. Revisions
  whose source lifecycle or evidence class is stronger than the import trust
  rule are counted as `downgraded` while still being stored as
  candidate/unverified.
- Doctor: `tests/unit/test_service.py` covers `gatewayctl doctor` checks,
  including `listener_exposure`, which flags a listener bound beyond the
  configured bind address (e.g. `0.0.0.0` instead of loopback) as a warning.
- Protocol negotiation: the compatibility matrix accepts the current major
  version and newer minors, and rejects older majors, empty strings,
  whitespace, and malformed versions.
- Integrations: `tests/unit/test_integrations_base.py` exercises the
  runtime-checkable protocols and registry construction (disabled layer builds
  only null adapters). `test_embeddings.py` covers batching, the conditional
  `dimensions` field for `text-embedding-3*` models, count and dimension
  mismatch errors, API key handling from the named environment variable, and
  status mapping. `test_qdrant.py` covers collection creation (409 as
  success), deterministic generation and UUID5 point ids, payload shape,
  delete and search filters, and health. `test_rerank.py` covers the lexical
  scorer and the openai-compatible response shapes. `test_graphiti.py` and
  `test_mem0.py` cover proposal-only semantics, source-role preservation, and
  the string and item caps. `test_semantic_retrieval.py` proves hybrid merge,
  that a payload namespace is not trusted for ACL, scope and lifecycle
  filtering, reranker ordering without additions, fail-open lexical fallback
  with a response warning, zero HTTP requests while disabled, dimension
  mismatch handling, restart generation adoption, and the privacy gate.
- Integrations budgets: `tests/unit/test_service.py` covers the
  `integrations.graphiti.max_calls_per_task` batch-extract cap (one call per
  unique entry, `INVALID_ARGUMENT` naming the cap when a batch exceeds it) and
  the `integrations.mem0.max_items` extractor call budget for
  `import-memories` (every call counts, even one that returns no proposals,
  and the run stops at the budget).

## Integration layer

- Bootstrap to start, real admin socket, register, record, search, gateway
  restart, durable entry visible afterwards.
- The stdio bridge and the real Streamable HTTP path at `/mcp`, including
  bearer headers and security middleware, tested over HTTP rather than an
  in-memory transport.
- Two independent agents in one environment, and two actors on one shared
  bridge with interleaved calls; no identity cross-talk.
- Parent/child registration, one-time ticket replay rejection, inherited
  scope, cascading revocation.
- Mentor request, host approval, lease, evidence submission, publication,
  expiry, revocation.
- Peer votes never publish by count; self/family reviews and contributor
  self-approval are rejected.
- Concurrent edits conflict correctly; retried writes and lost responses do
  not duplicate state.
- Backup/restore with evidence consistency, migration upgrade, deletion
  ledger, and index rebuild.
- Optional backends disabled or unavailable still permit core reads and
  writes.
- Integrations: `tests/integration/test_integrations_http.py` runs a real
  gateway and admin socket with a mock Qdrant behind an in-memory transport:
  register, record, `integrations reindex`, hybrid search
  (`retrieval_backend = "hybrid"`, `semantic_used`), and `memory forget`
  removing the points. `tests/integration/test_integrations_admin.py` covers
  `integrations status/probe/reindex/extract/proposals/import-memories`
  through the real admin socket, including that extraction never publishes
  and that imports land as candidate/unverified with source role preserved.
  `tests/integration/test_integrations_e2e_http.py` runs the production path
  over loopback sockets against the fake Qdrant, embeddings, Graphiti, and
  Mem0 servers in `tests/integration/fake_backends.py`: configuration,
  registry, real adapters, hybrid retrieval, reranker behavior, extraction,
  and deletion propagation with no external network access.
- Concurrency: `tests/integration/test_concurrency.py` runs 50 concurrent
  read/write clients and 50 concurrent readers, checks rate-limit rejection
  with `Retry-After`, and verifies the second-writer lock.

## Security layer

- Unregistered data access, forged root IDs, token replay after revocation,
  forged mentor role, approval of an expired request.
- Missing or foreign actor handles on multiplexed sessions.
- Namespace leakage attempts through search, IDs, errors, counts, links,
  export, and evidence.
- Host/Origin validation, oversized request bodies, SQL/FTS injection,
  hostile Unicode, traversal and symlink attacks on evidence, oversized
  evidence, log injection.
- Agent attempts to change config, reach the admin API over TCP, execute a
  stored command, or read arbitrary host files.
- Stored prompt-injection strings are inert: they do not alter server policy
  or trigger tools. These tests do not claim to prove all LLM prompt
  injection is solved.
- Disk-full and read-only database behavior, missing evidence, interrupted
  backups, cancellation, client disconnect, stale leases after restart,
  graceful SIGTERM, and a second gateway process on the same database.
- Secret redaction in logs, errors, JSON output, test reports, and MCP
  results.

## Measured baseline (2026-11-12)

Reference: `benchmarks/baseline_v1.json`, generated on Intel Core Ultra 7
255U, 14 threads, Python 3.12.3, SQLite 3.45.1. The JSON carries the full
environment metadata, query mix, response-byte counts, database size and
per-mode latency percentiles. CI does not compare against it and no timing is
a gate.

Synthetic corpus (`benchmarks/corpus_v1.json`): 75 entries, 85 labelled
queries (69 answerable, 12 no-answer, 4 adversarial), and 17 task scenarios,
with 0 recorded known gaps.

| Metric | Value |
|---|---|
| recall@5 (69 answerable queries) | 1.000 |
| precision@5 | 0.387 (reported, not gated) |
| no-answer failures | 0 of 12 |
| forbidden/private namespace leaks | 0 |
| context task scenarios passed | 17 of 17 (0 misses documented in `known_gaps`) |

Latency (`benchmarks/run_bench.py`, real service layer, 10,000 synthetic
entries, 5 results and a 4000-byte budget per search):

| Workload | p50 | p95 |
|---|---|---|
| 10,000 entries, 1 client | 15.8 ms | 38.9 ms |
| 10,000 entries, 10 clients | 37.8 ms | 339.0 ms |

The 10-client p95 was above the 250 ms target on this shared host. The target
and every timing are informational. The JSON baseline is the machine-readable
record; rerun on an idle host before comparing numbers. The full suite passed
401 tests on CPython 3.12.3 and 401 tests on CPython 3.14.7.

Regenerate the baseline exactly:

```bash
.venv/bin/python benchmarks/run_bench.py --entries 10000 --json-out benchmarks/baseline_v1.json
```

Rerun the quality corpus:

```bash
.venv/bin/pytest tests/quality -q -s
```

## Quality corpus and retrieval target

`benchmarks/corpus_v1.json` is a deterministic, versioned corpus with 75
labelled retrieval queries (69 answerable, 12 no-answer, 4 adversarial) and
17 task scenarios, covering exact compiler errors, synonyms, conflicting
versions, stale facts, questions misread as preferences, repeated copied
claims, popular-but-misleading entries, narrow host-specific fixes,
adversarial memories, and no-answer cases.

A deterministic evaluation test asserts:

- recall@5 at least 0.80 on the lexical baseline;
- zero unauthorized disclosures and zero forbidden state transitions;
- all explicit scope-exclusion and no-answer fixtures behave as specified.

Failures are investigated rather than relabelled; a baseline update that
simply makes CI green is not accepted without review.

### Benchmark generator

```bash
.venv/bin/python benchmarks/generate_corpus.py   # regenerate corpus_v1.json
.venv/bin/python benchmarks/run_bench.py --entries 10000 --json-out benchmarks/baseline_v1.json
```

`run_bench` generates a synthetic 10,000-entry corpus (or fewer with
`--quick`) and concurrent clients, reports the hardware and query
distribution, and writes a machine-readable report when `--json-out` is set.
The JSON is written atomically with mode 0600 and records environment
metadata, query mix, per-mode latency percentiles, response bytes, database
size and correctness. The target is p95 bounded FTS search below 250 ms at 10
concurrent clients on a typical idle developer workstation. The current shared
host measured above that target (see "Measured baseline" above). The number
stays informational, never a CI gate. Ordinary CI runs deterministic
query-plan and bounded-work checks, not flaky shared-runner wall-clock gates,
and a 50-client concurrent correctness and busy-timeout test.

Live end-to-end model evaluation (no memory versus fixed read-only memory
snapshots on the same task set) is opt-in and budgeted; it uses held-out
cases, repeats stochastic runs, records model and version, and judges success
with tests or explicit rubrics. Ordinary CI never requires paid API access.

## Deliberately not tested or claimed

- Live paid-model evaluation in ordinary CI. Integration tests never use
  external network access: every remote endpoint (Qdrant, embeddings,
  reranker, Graphiti, Mem0) is faked in-memory.
- High-availability or multi-host behavior (not designed for).
- Complete LLM prompt-injection immunity (content screening is advisory, not
  a security boundary).
- Forensic erasure guarantees on SSDs or external backups.
- Detection of one process registering many identities (Sybil) in general.
