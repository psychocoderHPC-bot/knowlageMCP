# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Reproducible retrieval benchmark over the real service layer.

Loads a deterministic synthetic JSONL corpus into a temporary SQLite database
through :class:`KnowledgeService`, then measures bounded lexical searches at 1
and 10 concurrent clients. Timing targets are reported, never enforced: a
non-zero exit means a correctness assertion failed. ``--quick`` keeps CI work
small (1000 entries); the default is 10000.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import platform
import random
import sqlite3
import sys
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from generate_corpus import generate_entries  # noqa: E402

from knowlage_mcp import __version__  # noqa: E402
from knowlage_mcp.config import Config, StorageConfig, resolve_paths  # noqa: E402
from knowlage_mcp.db import Database  # noqa: E402
from knowlage_mcp.evidence import EvidenceStore  # noqa: E402
from knowlage_mcp.models import (  # noqa: E402
    EntryPayload,
    EvidenceClass,
    Kind,
    Lifecycle,
    RecordRequest,
    Scope,
    SearchRequest,
    SessionInfo,
)
from knowlage_mcp.service import KnowledgeService  # noqa: E402

RESULTS = 5
BYTES = 4000
P95_TARGET_MS = 250.0
READ_WRITE_NAMESPACES = ("shared", "project-atlas", "project-borealis")
_COMMON_WORDS = {
    "reproduced",
    "interrupted",
    "observation",
    "conditions",
    "benchmark",
    "synthetic",
    "documents",
    "regenerate",
    "migration",
    "dedicated",
}


@dataclass(frozen=True)
class Stats:
    count: int
    p50: float
    p95: float
    p99: float
    mean: float
    maximum: float
    mean_bytes: float
    max_bytes: int
    failures: tuple[str, ...]


def _percentile(ordered: list[float], percent: float) -> float:
    if not ordered:
        return 0.0
    rank = max(0, math.ceil(percent / 100.0 * len(ordered)) - 1)
    return ordered[min(rank, len(ordered) - 1)]


def _stats(latencies: list[float], sizes: list[int], failures: list[str]) -> Stats:
    ordered = sorted(latencies)
    return Stats(
        count=len(latencies),
        p50=_percentile(ordered, 50),
        p95=_percentile(ordered, 95),
        p99=_percentile(ordered, 99),
        mean=sum(latencies) / len(latencies) if latencies else 0.0,
        maximum=max(latencies) if latencies else 0.0,
        mean_bytes=sum(sizes) / len(sizes) if sizes else 0.0,
        max_bytes=max(sizes) if sizes else 0,
        failures=tuple(failures),
    )


def _cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def _mode_report(stats: Stats) -> dict[str, float | int]:
    return {
        "count": stats.count,
        "p50": round(stats.p50, 3),
        "p95": round(stats.p95, 3),
        "p99": round(stats.p99, 3),
        "mean": round(stats.mean, 3),
        "max": round(stats.maximum, 3),
    }


def _write_report(path: Path, report: dict[str, Any]) -> None:
    """Write ``report`` atomically as JSON with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(report, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _build_service(root: Path) -> tuple[KnowledgeService, Config]:
    config = resolve_paths(
        Config(
            storage=StorageConfig(
                database=root / "bench" / "knowledge.sqlite3",
                evidence_dir=root / "bench" / "evidence",
                backup_dir=root / "bench" / "backups",
            )
        )
    )
    database = Database(config.storage.database)
    database.migrate()
    evidence = EvidenceStore(
        config.storage.evidence_dir, quota_bytes=64 * 1024 * 1024, max_object_bytes=1024 * 1024
    )
    service = KnowledgeService(config, database, evidence)
    service.bootstrap()
    return service, config


def _ensure_namespaces(service: KnowledgeService, rows: list[dict[str, Any]]) -> list[str]:
    existing = {namespace["name"] for namespace in service.namespaces_list()}
    for namespace in sorted({str(row["namespace"]) for row in rows}):
        if namespace not in existing:
            service.namespace_create(namespace, kind="project", description="Synthetic benchmark namespace")
    return sorted({str(row["namespace"]) for row in rows} | existing)


def _load(
    service: KnowledgeService, rows: list[dict[str, Any]], namespaces: list[str]
) -> tuple[SessionInfo, dict[str, str]]:
    writer = service.register(label="bench-writer", source="benchmark")
    for namespace in namespaces:
        service.grants_add(writer.agent_id, namespace, "operator")
    session = service.authenticate(writer.session_token)
    entry_ids: dict[str, str] = {}
    for row in rows:
        payload = EntryPayload(
            kind=Kind(str(row["kind"])),
            title=str(row["title"]),
            claim=str(row["claim"]),
            conditions=str(row["conditions"]) if row.get("conditions") else None,
            tags=tuple(str(tag) for tag in row.get("tags", [])),
            scope=Scope.model_validate(row.get("scope") or {}),
            observed_at=_parse_time(str(row["observed_at"])) or datetime.now(UTC),
            evidence_class=EvidenceClass(str(row.get("evidence_class", "operator_confirmed"))),
            lifecycle=Lifecycle(str(row.get("lifecycle", "published"))),
            sensitivity=str(row.get("sensitivity", "normal")),
        )
        write = service.record(session, RecordRequest(namespace=str(row["namespace"]), entry=payload))
        entry_ids[str(row["id"])] = write.entry.entry_id
    return session, entry_ids


def _reader(service: KnowledgeService, namespaces: list[str]) -> tuple[SessionInfo, set[str]]:
    registration = service.register(label="bench-reader", source="benchmark")
    granted = {grant.namespace for grant in registration.grants}
    for namespace in READ_WRITE_NAMESPACES:
        if namespace in namespaces and namespace not in granted:
            service.grants_add(registration.agent_id, namespace, "reader")
    session = service.authenticate(registration.session_token)
    return session, {grant.namespace for grant in session.grants}


def _build_queries(
    rows: list[dict[str, Any]], count: int, seed: int, entry_ids: dict[str, str]
) -> list[dict[str, str]]:
    readable = [row for row in rows if row["namespace"] != "personal"]
    rng = random.Random(seed ^ 0x5EED)
    queries: list[dict[str, str]] = []
    components = sorted({str(row["tags"][0]) for row in readable if row.get("tags")})
    for _ in range(count):
        roll = rng.random()
        if roll < 0.10:
            queries.append(
                {
                    "kind": "no_answer",
                    "query": f"zzqx{rng.randrange(10**9):09d} frobnicate{rng.randrange(10**9):09d}",
                }
            )
        elif roll < 0.60:
            row = rng.choice(readable)
            queries.append(
                {"kind": "identifier", "query": str(row["probe"]), "expected": entry_ids[str(row["id"])]}
            )
        elif roll < 0.95:
            row = rng.choice(readable)
            words = [
                word.strip(";,`")
                for word in str(row["claim"]).split()
                if len(word) >= 8 and word.lower() not in _COMMON_WORDS
            ]
            selected = rng.sample(words, min(2, len(words))) if words else []
            selected.append(str(row["probe"]))
            queries.append(
                {"kind": "phrase", "query": " ".join(selected), "expected": entry_ids[str(row["id"])]}
            )
        else:
            queries.append({"kind": "broad", "query": rng.choice(components)})
    return queries


def _check(query: dict[str, str], response: Any) -> str | None:
    ids = {result.entry_id for result in response.results}
    if any(result.namespace == "personal" for result in response.results):
        return f"{query['kind']} query leaked the personal namespace"
    if query["kind"] == "no_answer" and ids:
        return f"no-answer query returned {len(ids)} results"
    if query["kind"] == "identifier" and query["expected"] not in ids:
        return f"identifier query missed {query['expected']}"
    if query["kind"] in {"phrase", "broad"} and not ids:
        return f"{query['kind']} query returned no results"
    return None


def _round(
    service: KnowledgeService,
    session: SessionInfo,
    queries: list[dict[str, str]],
    *,
    clients: int,
    per_client: int,
) -> Stats:
    start = threading.Event()

    def worker(worker_id: int) -> tuple[list[float], list[int], list[str]]:
        start.wait(timeout=10.0)
        latencies: list[float] = []
        sizes: list[int] = []
        failures: list[str] = []
        try:
            for index in range(per_client):
                query = queries[(worker_id * per_client + index) % len(queries)]
                began = time.perf_counter()
                response = service.search(
                    session,
                    SearchRequest(query=query["query"], max_results=RESULTS, max_bytes=BYTES),
                )
                latencies.append((time.perf_counter() - began) * 1000.0)
                sizes.append(len(response.model_dump_json().encode("utf-8")))
                failure = _check(query, response)
                if failure is not None:
                    failures.append(failure)
        finally:
            service.db.close()
        return latencies, sizes, failures

    with ThreadPoolExecutor(max_workers=clients) as pool:
        futures = [pool.submit(worker, worker_id) for worker_id in range(clients)]
        time.sleep(0.05)
        start.set()
        results = [future.result() for future in futures]
    latencies = [value for batch, _, _ in results for value in batch]
    sizes = [value for _, batch, _ in results for value in batch]
    failures = [value for _, _, batch in results for value in batch]
    return _stats(latencies, sizes, failures)


def _print_stats(label: str, stats: Stats) -> None:
    print(
        f"{label}: n={stats.count} p50={stats.p50:.2f}ms p95={stats.p95:.2f}ms "
        f"p99={stats.p99:.2f}ms mean={stats.mean:.2f}ms max={stats.maximum:.2f}ms "
        f"avg_response_bytes={stats.mean_bytes:.0f} max_response_bytes={stats.max_bytes}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entries", type=int, default=None, help="entries to generate (default 10000)")
    parser.add_argument("--seed", type=int, default=7, help="deterministic seed (default 7)")
    parser.add_argument("--quick", action="store_true", help="CI mode: 1000 entries and fewer queries")
    parser.add_argument(
        "--json-out",
        default=None,
        metavar="PATH",
        help="write a machine-readable JSON report atomically (mode 0600); '-' is rejected",
    )
    args = parser.parse_args(argv)
    if args.json_out == "-":
        parser.error("--json-out requires a file path; JSON on stdout would corrupt the report")

    entries = args.entries if args.entries is not None else (1000 if args.quick else 10_000)
    per_client = 30 if args.quick else 50
    if entries < 1:
        parser.error("--entries must be positive")

    with tempfile.TemporaryDirectory(prefix="knowlagemcp-bench-") as tmp:
        corpus_path = Path(tmp) / "corpus.jsonl"
        with corpus_path.open("w", encoding="utf-8", newline="\n") as handle:
            for entry in generate_entries(entries, args.seed):
                handle.write(json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n")
        rows = [
            json.loads(line) for line in corpus_path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        service, config = _build_service(Path(tmp))
        try:
            namespaces = _ensure_namespaces(service, rows)
            load_began = time.perf_counter()
            _, entry_ids = _load(service, rows, namespaces)
            load_seconds = time.perf_counter() - load_began
            session, readable = _reader(service, namespaces)
            service.db.connect().execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db_path = config.storage.database
            db_bytes = db_path.stat().st_size
            wal_path = Path(str(db_path) + "-wal")
            if wal_path.exists():
                db_bytes += wal_path.stat().st_size

            queries = _build_queries(rows, max(per_client, 50), args.seed, entry_ids)
            distribution = Counter(query["kind"] for query in queries)
            print("knowlageMCP retrieval benchmark")
            print(
                f"hardware: platform={platform.platform()} cpu={_cpu_model()!r} "
                f"os.cpu_count={os.cpu_count()} python={platform.python_version()} "
                f"sqlite={sqlite3.sqlite_version}"
            )
            print(
                f"corpus: entries={entries} seed={args.seed} namespaces={len(namespaces)} "
                f"readable={sorted(readable)} db_bytes={db_bytes} load_s={load_seconds:.2f}"
            )
            print("query mix: " + " ".join(f"{kind}={count}" for kind, count in sorted(distribution.items())))

            warmup = _round(service, session, queries, clients=1, per_client=min(20, per_client))
            _print_stats("warmup", warmup)
            one = _round(service, session, queries, clients=1, per_client=per_client)
            _print_stats("1 client", one)
            ten = _round(service, session, queries, clients=10, per_client=per_client)
            _print_stats("10 clients", ten)
            comparison = "within" if ten.p95 <= P95_TARGET_MS else "above"
            print(
                f"p95 target {P95_TARGET_MS:.1f}ms at 10 clients: measured {ten.p95:.2f}ms "
                f"({comparison} target; informational, not a gate)"
            )

            failures = one.failures + ten.failures
            measured = one.count + ten.count
            avg_bytes = (
                (one.mean_bytes * one.count + ten.mean_bytes * ten.count) / measured if measured else 0.0
            )
            report: dict[str, Any] = {
                "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "package_version": __version__,
                "python_version": platform.python_version(),
                "python_implementation": platform.python_implementation(),
                "sqlite_version": sqlite3.sqlite_version,
                "platform": platform.platform(),
                "cpu_model": _cpu_model(),
                "cpu_count": os.cpu_count(),
                "entries": entries,
                "seed": args.seed,
                "corpus_version": "generated",
                "clients": [1, 10],
                "query_mix": dict(sorted(distribution.items())),
                "latency": {
                    "warmup": _mode_report(warmup),
                    "1_client": _mode_report(one),
                    "10_clients": _mode_report(ten),
                },
                "avg_response_bytes": round(avg_bytes, 1),
                "max_response_bytes": max(one.max_bytes, ten.max_bytes),
                "db_bytes": db_bytes,
                "correctness_ok": not failures,
                "p95_target_ms": P95_TARGET_MS,
                "command": list(sys.argv),
            }
            if args.json_out is not None:
                _write_report(Path(args.json_out), report)

            if failures:
                print(f"correctness failures: {len(failures)}")
                for failure in dict.fromkeys(failures):
                    print(f"  - {failure}")
                return 1
            print("correctness: ok")
            return 0
        finally:
            service.close()


if __name__ == "__main__":
    raise SystemExit(main())
