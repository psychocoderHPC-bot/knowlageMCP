# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Deterministic quality evaluation over ``benchmarks/corpus_v1.json``.

The corpus is loaded through the real :class:`KnowledgeService`; published
fixtures use an operator grant plus ``record`` (which preserves scope, tags,
lifecycle, and provenance) and one fixture goes through the candidate review
path. No test-only back door writes to SQLite. Labels are assertions about user
intent; a miss is reported and must be documented in the corpus ``known_gaps``
list instead of being deleted or relabelled.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from knowlage_mcp.config import Config, StorageConfig, resolve_paths
from knowlage_mcp.db import Database
from knowlage_mcp.evidence import EvidenceStore
from knowlage_mcp.models import (
    EntryPayload,
    ErrorCode,
    EvidenceClass,
    Kind,
    Lifecycle,
    ProvenanceRef,
    RecordOutcomeRequest,
    RecordRequest,
    ReviewResult,
    Scope,
    SearchFilters,
    SearchRequest,
    ServiceError,
    SessionInfo,
    SubmitReviewRequest,
)
from knowlage_mcp.service import KnowledgeService

pytestmark = pytest.mark.quality

CORPUS_PATH = Path(__file__).resolve().parents[2] / "benchmarks" / "corpus_v1.json"
SEARCH_RESULTS = 5
SEARCH_BYTES = 4000
CONTEXT_BYTES = 2000


class ManualClock:
    """Deterministic clock so seeded revisions and review leases are stable."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def now_dt(self) -> datetime:
        return datetime.fromtimestamp(self.now, UTC)


@dataclass
class Seeded:
    service: KnowledgeService
    db: Database
    clock: ManualClock
    corpus: dict[str, Any]
    reader: SessionInfo
    readable_namespaces: set[str]
    entries: dict[str, dict[str, str]]

    def symbol(self, entry_id: str) -> str:
        return self._by_entry_id.get(entry_id, entry_id)

    def entry_id(self, symbol: str) -> str:
        return self.entries[symbol]["entry_id"]

    def revision_id(self, symbol: str) -> str:
        return self.entries[symbol]["revision_id"]

    @property
    def _by_entry_id(self) -> dict[str, str]:
        return {value["entry_id"]: key for key, value in self.entries.items()}


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(UTC)


def _make_filters(raw: dict[str, Any] | None) -> SearchFilters:
    return SearchFilters.model_validate(raw or {})


def _seed(corpus: dict[str, Any], tmp_path: Path) -> Seeded:
    data = tmp_path / "quality-data"
    config = resolve_paths(
        Config(
            storage=StorageConfig(
                database=data / "knowledge.sqlite3",
                evidence_dir=data / "evidence",
                backup_dir=data / "backups",
            )
        )
    )
    database = Database(config.storage.database)
    database.migrate()
    evidence = EvidenceStore(
        config.storage.evidence_dir, quota_bytes=8 * 1024 * 1024, max_object_bytes=1024 * 1024
    )
    clock = ManualClock()
    service = KnowledgeService(config, database, evidence, clock=clock, monotonic=ManualClock(1000.0))
    service.bootstrap()

    existing = {namespace["name"] for namespace in service.namespaces_list()}
    for namespace in corpus["namespaces"]:
        if namespace["name"] not in existing:
            service.namespace_create(
                namespace["name"], kind=namespace["kind"], description=namespace["description"]
            )

    writer = service.register(label="corpus-writer", source="quality-corpus")
    for namespace in corpus["namespaces"]:
        service.grants_add(writer.agent_id, namespace["name"], "operator")
    writer_session = service.authenticate(writer.session_token)

    reader = service.register(label="corpus-reader", source="quality-corpus")
    reader_session = service.authenticate(reader.session_token)

    mentor = service.register(label="corpus-mentor", source="quality-corpus")
    mentor_session = service.authenticate(mentor.session_token)
    request = service.request_mentor(mentor_session, "shared", ["review", "publish"])
    service.approve(request.request_id, role="mentor", ttl=7200, namespace="shared")
    mentor_session = service.authenticate(mentor.session_token)

    entries: dict[str, dict[str, str]] = {}
    pending = list(corpus["entries"])
    guard = 0
    while pending and guard < 16:
        guard += 1
        deferred = []
        for entry in pending:
            refs = entry.get("supersedes", []) + entry.get("contradicts", [])
            if any(ref not in entries for ref in refs):
                deferred.append(entry)
                continue
            provenance = [
                ProvenanceRef(to_revision_id=entries[ref]["revision_id"], relation=relation)
                for relation, refs in (
                    ("supersedes", entry.get("supersedes", [])),
                    ("contradicts", entry.get("contradicts", [])),
                )
                for ref in refs
            ]
            payload = EntryPayload(
                kind=Kind(entry["kind"]),
                title=entry["title"],
                claim=entry["claim"],
                conditions=entry.get("conditions"),
                tags=tuple(entry.get("tags", [])),
                scope=Scope.model_validate(entry.get("scope") or {}),
                observed_at=_parse(entry.get("observed_at")) or clock.now_dt(),
                valid_from=_parse(entry.get("valid_from")),
                valid_until=_parse(entry.get("valid_until")),
                evidence_class=EvidenceClass(entry.get("evidence_class", "operator_confirmed")),
                sensitivity=entry.get("sensitivity", "normal"),
                lifecycle=(
                    None
                    if entry.get("seed_via") == "review"
                    else Lifecycle(entry.get("lifecycle", "published"))
                ),
                provenance=tuple(provenance),
            )
            if entry.get("seed_via") == "review":
                write = service.record(
                    reader_session, RecordRequest(namespace=entry["namespace"], entry=payload)
                )
                job = service.review_next(mentor_session)
                assert job is not None
                result = service.submit_review(
                    mentor_session,
                    SubmitReviewRequest(
                        entry_id=job.entry_id,
                        revision_id=job.revision_id,
                        result=ReviewResult.SUPPORTS,
                        method="synthetic fixture review",
                        evidence="checked against the synthetic fixture",
                        job_id=job.job_id,
                        lease_id=job.lease_id,
                        evidence_class=EvidenceClass.SOURCE_CHECKED,
                    ),
                )
                assert result.published, "review fixture must publish"
            else:
                write = service.record(
                    writer_session, RecordRequest(namespace=entry["namespace"], entry=payload)
                )
            entries[entry["id"]] = {
                "entry_id": write.entry.entry_id,
                "revision_id": write.entry.revision_id,
            }
            for _ in range(int(entry.get("seed_outcomes", 0))):
                service.record_outcome(
                    reader_session,
                    RecordOutcomeRequest(
                        outcome="success",
                        task_label="synthetic popularity fixture",
                        revision_ids=(write.entry.revision_id,),
                        notes="used only to prove popularity does not widen scope",
                    ),
                )
                clock.advance(1)
            clock.advance(60)
        pending = deferred
    assert not pending, f"unresolved corpus entry references: {[entry['id'] for entry in pending]}"

    readable = {
        grant.namespace for grant in reader.grants if grant.revoked_at is None and grant.expires_at is None
    }
    return Seeded(
        service=service,
        db=database,
        clock=clock,
        corpus=corpus,
        reader=reader_session,
        readable_namespaces=readable,
        entries=entries,
    )


@pytest.fixture(scope="module")
def corpus() -> dict[str, Any]:
    return json.loads(CORPUS_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def seeded(corpus: dict[str, Any], tmp_path_factory: pytest.TempPathFactory) -> Seeded:
    env = _seed(corpus, tmp_path_factory.mktemp("quality-corpus"))
    try:
        yield env
    finally:
        env.db.close()


def _search(env: Seeded, query: dict[str, Any]):
    return env.service.search(
        env.reader,
        SearchRequest(
            query=query["query"],
            filters=_make_filters(query.get("filters")),
            max_results=SEARCH_RESULTS,
            max_bytes=SEARCH_BYTES,
        ),
    )


def _answerable(corpus: dict[str, Any]) -> list[dict[str, Any]]:
    return [query for query in corpus["queries"] if query["answerable"]]


def _known_gaps(corpus: dict[str, Any], scope: str) -> set[str]:
    return {gap["id"] for gap in corpus["known_gaps"] if gap["scope"] == scope}


def test_corpus_contract(corpus: dict[str, Any]) -> None:
    assert corpus["_license"] == "ISC"
    assert corpus["_copyright"] == "2026 René Widera"
    assert corpus["synthetic"] is True
    assert corpus["schema_version"] == 1
    assert corpus["corpus_version"]

    entry_ids = {entry["id"] for entry in corpus["entries"]}
    required = {
        "id",
        "namespace",
        "kind",
        "title",
        "claim",
        "conditions",
        "scope",
        "tags",
        "lifecycle",
        "evidence_class",
        "observed_at",
    }
    for entry in corpus["entries"]:
        assert required <= set(entry), entry["id"]
        assert entry["namespace"] in {namespace["name"] for namespace in corpus["namespaces"]}
        assert Kind(entry["kind"])
        assert Lifecycle(entry["lifecycle"])
        assert EvidenceClass(entry["evidence_class"])
        for ref in entry.get("supersedes", []) + entry.get("contradicts", []):
            assert ref in entry_ids, f"{entry['id']} links to unknown entry {ref}"
        assert isinstance(entry["scope"], dict)
        assert isinstance(entry["tags"], list)

    namespace_names = {namespace["name"] for namespace in corpus["namespaces"]}
    assert {"shared", "personal"} <= namespace_names
    assert len(namespace_names) >= 3

    assert len([query for query in corpus["queries"] if query["answerable"]]) >= 60
    assert len([query for query in corpus["queries"] if query["category"] == "no_answer"]) >= 10
    assert len(corpus["task_scenarios"]) >= 12

    categories = {query["category"] for query in corpus["queries"]}
    assert {
        "exact_compiler_error",
        "identifier",
        "synonym",
        "conflicting_version",
        "stale_fact",
        "question_vs_preference",
        "repeated_lineage",
        "misleading_popular",
        "narrow_host",
        "adversarial",
        "no_answer",
        "contradiction",
    } <= categories

    for query in corpus["queries"]:
        for ref in query["expected_entry_ids"] + query.get("forbidden_entry_ids", []):
            assert ref in entry_ids, f"{query['id']} references unknown entry {ref}"
        assert query["expected_entry_ids"] or not query["answerable"] or query["category"] != "no_answer"

    for scenario in corpus["task_scenarios"]:
        for ref in scenario["expected_entry_ids"] + scenario["forbidden_entry_ids"]:
            assert ref in entry_ids, f"{scenario['id']} references unknown entry {ref}"

    superseded = [entry for entry in corpus["entries"] if entry["lifecycle"] == "superseded"]
    assert superseded
    popular = [entry for entry in corpus["entries"] if entry.get("seed_outcomes", 0) >= 20]
    assert popular, "the misleading-popular fixture needs many recorded outcomes"
    conflicts = [
        entry
        for entry in corpus["entries"]
        if entry["valid_from"] and entry["scope"].get("version") and entry["lifecycle"] == "published"
    ]
    assert len({entry["scope"]["version"] for entry in conflicts}) >= 2
    assert all(gap["reason"] for gap in corpus["known_gaps"])


def test_retrieval_recall_and_precision(seeded: Seeded) -> None:
    env = seeded
    rows: list[tuple[str, str, str, str, list[str], list[str]]] = []
    recalls: list[float] = []
    precisions: list[float] = []
    forbidden_violations: list[str] = []
    no_answer_failures: list[str] = []
    lineage_failures: list[str] = []

    for query in _answerable(env.corpus):
        response = _search(env, query)
        assert response.approx_bytes <= SEARCH_BYTES
        got_ids = [result.entry_id for result in response.results]
        got_symbols = [env.symbol(entry_id) for entry_id in got_ids]
        expected = {env.entry_id(symbol) for symbol in query["expected_entry_ids"]}
        forbidden = {env.entry_id(symbol) for symbol in query.get("forbidden_entry_ids", [])}
        hits = expected & set(got_ids)
        recalls.append(1.0 if hits else 0.0)
        precisions.append(len(hits) / len(got_ids) if got_ids else 0.0)
        if forbidden & set(got_ids):
            forbidden_violations.append(query["id"])
        if not hits:
            rows.append(
                (
                    query["id"],
                    query["category"],
                    query["query"],
                    ", ".join(query["expected_entry_ids"]),
                    got_symbols,
                    sorted(query.get("forbidden_entry_ids", [])),
                )
            )
        group = set(query.get("lineage_group", []))
        if group:
            members = [symbol for symbol in got_symbols if symbol in group]
            if len(members) > 1:
                lineage_failures.append(f"{query['id']}: {members}")
            if members:
                member_result = response.results[got_symbols.index(members[0])]
                if not any("duplicate lineage" in warning for warning in member_result.warnings):
                    lineage_failures.append(f"{query['id']}: missing duplicate-lineage warning")

    for query in env.corpus["queries"]:
        if query["category"] != "no_answer":
            continue
        response = _search(env, query)
        assert response.approx_bytes <= SEARCH_BYTES
        if response.results:
            no_answer_failures.append(query["id"])

    recall = sum(recalls) / len(recalls)
    precision = sum(precisions) / len(precisions)
    print(f"\nretrieval: queries={len(recalls)} recall@5={recall:.4f} precision@5={precision:.4f}")
    print(f"{'query':<6} {'category':<22} {'expected':<34} {'retrieved'}")
    for query_id, category, text, expected, got, forbidden in rows:
        print(f"{query_id:<6} {category:<22} {expected:<34} {got}  query={text!r} forbidden={forbidden}")
    print(f"no-answer failures: {no_answer_failures}")
    print(f"lineage failures: {lineage_failures}")
    print(f"forbidden violations: {forbidden_violations}")

    assert recall >= 0.80, f"recall@5 {recall:.4f} is below the 0.80 target"
    assert not no_answer_failures, f"no-answer queries returned results: {no_answer_failures}"
    assert not forbidden_violations, f"forbidden entries retrieved: {forbidden_violations}"
    assert not lineage_failures, f"lineage isolation/warning failures: {lineage_failures}"
    documented = _known_gaps(env.corpus, "query")
    undocumented = [row[0] for row in rows if row[0] not in documented]
    assert not undocumented, f"undocumented retrieval misses: {undocumented}"


def test_context_scenarios(seeded: Seeded) -> None:
    env = seeded
    passed = 0
    failures: list[tuple[str, list[str], list[str], list[str]]] = []
    forbidden_violations: list[str] = []
    for scenario in env.corpus["task_scenarios"]:
        bundle = env.service.get_context(
            env.reader, scenario["task"], Scope.model_validate(scenario["scope"])
        )
        assert bundle.approx_bytes <= CONTEXT_BYTES
        assert "personal" not in bundle.namespaces
        got_symbols = [env.symbol(result.entry_id) for result in bundle.results]
        expected = set(scenario["expected_entry_ids"])
        forbidden = set(scenario["forbidden_entry_ids"])
        leaked = forbidden & set(got_symbols)
        if leaked:
            forbidden_violations.append(f"{scenario['id']}: {sorted(leaked)}")
        ok = expected <= set(got_symbols) and not leaked
        passed += int(ok)
        if not ok:
            failures.append((scenario["id"], sorted(expected), got_symbols, sorted(forbidden)))

    rate = passed / len(env.corpus["task_scenarios"])
    print(f"\ncontext scenarios: passed={passed}/{len(env.corpus['task_scenarios'])} rate={rate:.4f}")
    for scenario_id, expected, got, forbidden in failures:
        print(f"{scenario_id}: expected={expected} got={got} forbidden={forbidden}")
    print(f"context forbidden violations: {forbidden_violations}")

    assert not forbidden_violations, f"forbidden context entries retrieved: {forbidden_violations}"
    assert rate >= 0.5, f"context pass rate {rate:.4f} regressed sharply"
    documented = _known_gaps(env.corpus, "scenario")
    undocumented = [failure[0] for failure in failures if failure[0] not in documented]
    assert not undocumented, f"undocumented context misses: {undocumented}"


def test_privacy_budgets_contradictions_and_adversarial_inertness(seeded: Seeded) -> None:
    env = seeded
    # Ordinary readers must not read a private namespace at all.
    with pytest.raises(ServiceError) as excinfo:
        env.service.search(
            env.reader,
            SearchRequest(query="PERSONAL-ONLY", filters=SearchFilters(namespace="personal")),
        )
    assert excinfo.value.code is ErrorCode.FORBIDDEN

    private_namespaces = {
        namespace["name"]
        for namespace in env.corpus["namespaces"]
        if namespace["name"] not in env.readable_namespaces
    }
    assert {"personal", "project-atlas", "project-borealis"} <= private_namespaces

    counts_before = _table_counts(env)
    config_before = env.service.config.model_dump(mode="json")
    for query in env.corpus["queries"]:
        response = _search(env, query)
        for result in response.results:
            assert result.namespace in env.readable_namespaces, f"{query['id']} leaked {result.namespace}"
    # Adversarial fixtures may be returned as inert data or excluded, never executed.
    by_symbol = {entry["id"]: entry for entry in env.corpus["entries"]}
    for query in env.corpus["queries"]:
        if query["category"] != "adversarial":
            continue
        response = _search(env, query)
        for result in response.results:
            symbol = env.symbol(result.entry_id)
            if symbol.startswith("e_adv_"):
                assert result.claim == by_symbol[symbol]["claim"]
                assert result.lifecycle in {Lifecycle.PUBLISHED, Lifecycle.DISPUTED}
    counts_after = _table_counts(env)
    assert counts_after == counts_before, f"read paths mutated state: {counts_before} -> {counts_after}"
    assert env.service.config.model_dump(mode="json") == config_before
    with pytest.raises(ServiceError) as excinfo:
        env.service.search(
            env.reader,
            SearchRequest(query="ZXQ-PERSONAL-771", filters=SearchFilters(namespace="personal")),
        )
    assert excinfo.value.code is ErrorCode.FORBIDDEN

    # Contradiction labels are visible as warnings on both directions of the edge.
    for entry in env.corpus["entries"]:
        for target in entry.get("contradicts", []):
            for symbol in (entry["id"], target):
                other = target if symbol == entry["id"] else entry["id"]
                detail = env.service.get_entry(env.reader, env.entry_id(symbol))
                assert env.revision_id(other) in detail.contradictions
                assert any("contradicts revision" in warning for warning in detail.warnings)


def _table_counts(env: Seeded) -> dict[str, int]:
    conn = env.db.connect()
    tables = (
        "entries",
        "entry_revisions",
        "task_outcomes",
        "deletion_ledger",
        "audit_events",
        "grants",
    )
    return {table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in tables}
