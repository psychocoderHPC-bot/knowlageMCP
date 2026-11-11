# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Entry/revision writes, dedupe, idempotency, outcomes, retract, and forget."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import ServiceEnv, build_env
from knowlage_mcp.models import (
    EntryPayload,
    ErrorCode,
    EvidenceClass,
    EvidenceRef,
    Kind,
    Lifecycle,
    ProposeRevisionRequest,
    RecordOutcomeRequest,
    RecordRequest,
    SearchRequest,
    ServiceError,
    SessionInfo,
    SourceRef,
)


def _record(
    env: ServiceEnv,
    session: SessionInfo,
    claim: str,
    *,
    title: str = "note",
    kind: Kind = Kind.OBSERVATION,
    idempotency_key: str | None = None,
    evidence: tuple[EvidenceRef, ...] = (),
    sources: tuple[SourceRef, ...] = (),
):
    return env.service.record(
        session,
        RecordRequest(
            namespace="shared",
            entry=EntryPayload(
                kind=kind,
                title=title,
                claim=claim,
                evidence=evidence,
                sources=sources,
            ),
            idempotency_key=idempotency_key,
        ),
    )


def test_record_creates_candidate_and_dedupes(env: ServiceEnv) -> None:
    session = env.session(env.register())
    first = _record(env, session, "the build needs clang 18")
    assert first.created is True
    assert first.duplicate is False
    assert first.entry.lifecycle is Lifecycle.CANDIDATE
    assert first.entry.evidence_class is EvidenceClass.UNVERIFIED
    assert first.entry.namespace == "shared"

    repeat = _record(env, session, "  The   BUILD needs CLANG 18  ", title="NOTE")
    assert repeat.duplicate is True
    assert repeat.entry.entry_id == first.entry.entry_id
    revision_count = env.db.connect().execute("SELECT COUNT(*) FROM entry_revisions").fetchone()[0]
    assert revision_count == 1


def test_record_idempotency_key_returns_stored_response(env: ServiceEnv) -> None:
    session = env.session(env.register())
    first = _record(env, session, "idempotent claim", idempotency_key="key-1")
    second = _record(env, session, "different claim", idempotency_key="key-1")
    assert second.idempotent is True
    assert second.entry.entry_id == first.entry.entry_id
    assert second.entry.claim == first.entry.claim

    with pytest.raises(ServiceError) as excinfo:
        env.service.propose_revision(
            session,
            ProposeRevisionRequest(
                entry_id=first.entry.entry_id,
                expected_revision=first.entry.revision_id,
                entry=EntryPayload(kind=Kind.OBSERVATION, title="x", claim="y"),
                idempotency_key="key-1",
            ),
        )
    assert excinfo.value.code is ErrorCode.CONFLICT


def test_claim_size_limit(env: ServiceEnv) -> None:
    session = env.session(env.register())
    oversized = "x" * (env.config.limits.claim_body_bytes + 1)
    with pytest.raises(ServiceError) as excinfo:
        _record(env, session, oversized)
    assert excinfo.value.code is ErrorCode.BUDGET_EXCEEDED


def test_control_characters_and_tags_are_rejected(env: ServiceEnv) -> None:
    session = env.session(env.register())
    with pytest.raises(ServiceError) as excinfo:
        env.service.record(
            session,
            RecordRequest(
                namespace="shared",
                entry=EntryPayload(kind=Kind.OBSERVATION, title="ok", claim="bad\x00claim"),
            ),
        )
    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENT
    with pytest.raises(ServiceError):
        env.service.record(
            session,
            RecordRequest(
                namespace="shared",
                entry=EntryPayload(
                    kind=Kind.OBSERVATION,
                    title="ok",
                    claim="fine",
                    tags=("x" * 80,),
                ),
            ),
        )


def test_candidate_quota_per_family(tmp_path: Path) -> None:
    env = build_env(
        tmp_path,
        limits={"candidate_writes_per_hour": 2, "candidate_writes_per_hour_global": 100},
    )
    try:
        session = env.session(env.register())
        _record(env, session, "one")
        _record(env, session, "two")
        with pytest.raises(ServiceError) as excinfo:
            _record(env, session, "three")
        assert excinfo.value.code is ErrorCode.RATE_LIMITED
    finally:
        env.db.close()


def test_global_candidate_quota(tmp_path: Path) -> None:
    env = build_env(
        tmp_path,
        limits={"candidate_writes_per_hour": 100, "candidate_writes_per_hour_global": 2},
    )
    try:
        first = env.session(env.register(source="a"))
        second = env.session(env.register(source="b"))
        _record(env, first, "one")
        _record(env, second, "two")
        with pytest.raises(ServiceError) as excinfo:
            _record(env, second, "three")
        assert excinfo.value.code is ErrorCode.RATE_LIMITED
    finally:
        env.db.close()


def test_propose_revision_conflict_and_optimistic_concurrency(env: ServiceEnv) -> None:
    session = env.session(env.register())
    write = _record(env, session, "original claim")
    entry_id = write.entry.entry_id
    current = write.entry.revision_id

    with pytest.raises(ServiceError) as excinfo:
        env.service.propose_revision(
            session,
            ProposeRevisionRequest(
                entry_id=entry_id,
                expected_revision="stale-revision",
                entry=EntryPayload(kind=Kind.OBSERVATION, title="note", claim="updated claim"),
            ),
        )
    assert excinfo.value.code is ErrorCode.REVISION_CONFLICT
    assert excinfo.value.details == {"current_revision_id": current}

    proposed = env.service.propose_revision(
        session,
        ProposeRevisionRequest(
            entry_id=entry_id,
            expected_revision=current,
            entry=EntryPayload(kind=Kind.OBSERVATION, title="note", claim="updated claim"),
        ),
    )
    assert proposed.created is True
    assert proposed.entry.lifecycle is Lifecycle.CANDIDATE
    row = (
        env.db.connect()
        .execute("SELECT current_revision_id FROM entries WHERE entry_id = ?", (entry_id,))
        .fetchone()
    )
    assert row["current_revision_id"] == current
    # The candidate revision is still retrievable by id.
    detail = env.service.get_entry(session, entry_id, proposed.entry.revision_id)
    assert detail.claim == "updated claim"

    duplicate = env.service.propose_revision(
        session,
        ProposeRevisionRequest(
            entry_id=entry_id,
            expected_revision=current,
            entry=EntryPayload(kind=Kind.OBSERVATION, title="note", claim="updated claim"),
        ),
    )
    assert duplicate.duplicate is True


def test_record_outcome_usage_and_validation(env: ServiceEnv) -> None:
    session = env.session(env.register())
    write = _record(env, session, "usage tracking claim")
    revision_id = write.entry.revision_id
    outcome = env.service.record_outcome(
        session,
        RecordOutcomeRequest(
            outcome="success",
            task_label="build",
            revision_ids=(revision_id,),
            notes="worked",
            idempotency_key="outcome-1",
        ),
    )
    assert outcome.outcome == "success"
    assert env.service.review._usage_count(env.db.connect(), revision_id) == 1

    repeat = env.service.record_outcome(
        session,
        RecordOutcomeRequest(
            outcome="failure",
            revision_ids=(revision_id,),
            idempotency_key="outcome-1",
        ),
    )
    assert repeat.outcome_id == outcome.outcome_id

    with pytest.raises(ServiceError) as excinfo:
        env.service.record_outcome(
            session,
            RecordOutcomeRequest(outcome="success", revision_ids=("missing",)),
        )
    assert excinfo.value.code is ErrorCode.NOT_FOUND
    with pytest.raises(ServiceError) as excinfo:
        env.service.record_outcome(session, RecordOutcomeRequest(outcome="maybe"))
    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENT


def test_operator_memory_add_is_published(env: ServiceEnv) -> None:
    write = env.service.memory_add(
        kind="preference", namespace="shared", text="prefer clang for local builds", title="compiler"
    )
    assert write.entry.lifecycle is Lifecycle.PUBLISHED
    assert write.entry.evidence_class is EvidenceClass.OPERATOR_CONFIRMED
    session = env.session(env.register())
    response = env.service.search(session, SearchRequest(query="prefer clang"))
    assert any(result.entry_id == write.entry.entry_id for result in response.results)


def test_memory_correct_supersedes_previous_revision(env: ServiceEnv) -> None:
    write = env.service.memory_add(namespace="shared", kind="preference", text="use tabs")
    old_revision = write.entry.revision_id
    corrected = env.service.memory_correct(write.entry.entry_id, old_revision, "use four spaces")
    assert corrected.entry.lifecycle is Lifecycle.PUBLISHED
    assert corrected.entry.revision_id != old_revision
    session = env.session(env.register())
    response = env.service.search(session, SearchRequest(query="four spaces"))
    assert any(result.entry_id == write.entry.entry_id for result in response.results)
    old = env.service.get_entry(session, write.entry.entry_id, old_revision)
    assert old.lifecycle is Lifecycle.SUPERSEDED


def test_retract_excludes_entry_immediately(env: ServiceEnv) -> None:
    write = env.service.memory_add(namespace="shared", kind="decision", text="retract me")
    env.service.memory_retract(write.entry.entry_id, "obsolete")
    session = env.session(env.register())
    response = env.service.search(session, SearchRequest(query="retract me"))
    assert response.results == ()
    with pytest.raises(ServiceError) as excinfo:
        env.service.get_entry(session, write.entry.entry_id)
    assert excinfo.value.code is ErrorCode.NOT_FOUND


def test_forget_dry_run_purges_and_writes_ledger(env: ServiceEnv) -> None:
    session = env.session(env.register())
    evidence_record = env.evidence.put(b"forget evidence\n", "text/plain")
    write = _record(
        env,
        session,
        "forget this claim",
        evidence=(
            EvidenceRef(
                evidence_id=evidence_record.evidence_id,
                sha256=evidence_record.sha256,
                size_bytes=evidence_record.size_bytes,
                media_type="text/plain",
            ),
        ),
        sources=(SourceRef(kind="log"),),
    )
    entry_id = write.entry.entry_id

    preview = env.service.memory_forget(entry_id, dry_run=True)
    assert preview["dry_run"] is True
    assert preview["revisions"] == 1
    assert preview["evidence"] == 1
    assert (
        env.db.connect().execute("SELECT COUNT(*) FROM entries WHERE entry_id = ?", (entry_id,)).fetchone()[0]
        == 1
    )

    result = env.service.memory_forget(entry_id, dry_run=False)
    assert result["forgotten"] is True
    assert (
        env.db.connect().execute("SELECT COUNT(*) FROM entries WHERE entry_id = ?", (entry_id,)).fetchone()[0]
        == 0
    )
    assert (
        env.db.connect()
        .execute("SELECT COUNT(*) FROM entry_fts WHERE entry_id = ?", (entry_id,))
        .fetchone()[0]
        == 0
    )
    assert env.evidence.exists(evidence_record.evidence_id) is False
    ledger = env.db.connect().execute("SELECT object_type, object_id FROM deletion_ledger").fetchall()
    ledger_pairs = {(row["object_type"], row["object_id"]) for row in ledger}
    assert ("entry", entry_id) in ledger_pairs
    assert ("revision", write.entry.revision_id) in ledger_pairs
    assert ("evidence", evidence_record.evidence_id) in ledger_pairs

    assert env.service.search(session, SearchRequest(query="forget this claim")).results == ()
