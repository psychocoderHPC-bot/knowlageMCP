# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Review queue leases, mentor publication, peer evidence, and disputes."""

from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import ServiceEnv, make_mentor
from knowlage_mcp.db import iso
from knowlage_mcp.models import (
    EntryPayload,
    ErrorCode,
    EvidenceClass,
    Kind,
    Lifecycle,
    RecordRequest,
    ReviewResult,
    SearchFilters,
    SearchRequest,
    ServiceError,
    SessionInfo,
    SubmitReviewRequest,
)


def _author_entry(env: ServiceEnv, claim: str = "review probe claim") -> tuple[SessionInfo, str, str]:
    session = env.session(env.register(source="author"))
    write = env.service.record(
        session,
        RecordRequest(
            namespace="shared",
            entry=EntryPayload(kind=Kind.OBSERVATION, title="review probe", claim=claim),
        ),
    )
    return session, write.entry.entry_id, write.entry.revision_id


def _submit(
    env: ServiceEnv,
    session: SessionInfo,
    entry_id: str,
    revision_id: str,
    job_id: str | None,
    lease_id: str | None,
    *,
    result: ReviewResult = ReviewResult.SUPPORTS,
    idempotency_key: str | None = None,
):
    return env.service.submit_review(
        session,
        SubmitReviewRequest(
            entry_id=entry_id,
            revision_id=revision_id,
            result=result,
            method="manual verification",
            evidence="ran the documented command",
            job_id=job_id,
            lease_id=lease_id,
            idempotency_key=idempotency_key,
        ),
    )


def test_mentor_supports_publishes_exact_revision(env: ServiceEnv) -> None:
    author, entry_id, revision_id = _author_entry(env)
    mentor, _ = make_mentor(env)
    job = env.service.review_next(mentor)
    assert job is not None
    assert job.entry_id == entry_id
    assert job.revision_id == revision_id
    assert job.title == "review probe"
    assert job.claim == "review probe claim"
    assert job.lease_id is not None

    result = _submit(env, mentor, entry_id, revision_id, job.job_id, job.lease_id)
    assert result.published is True
    assert result.lifecycle is Lifecycle.PUBLISHED
    assert result.evidence_class is EvidenceClass.SOURCE_CHECKED

    detail = env.service.get_entry(author, entry_id)
    assert detail.lifecycle is Lifecycle.PUBLISHED
    assert detail.evidence_class is EvidenceClass.SOURCE_CHECKED
    assert detail.current_revision_id == revision_id
    assert detail.last_verified_at is not None


def test_submit_review_is_idempotent(env: ServiceEnv) -> None:
    _, entry_id, revision_id = _author_entry(env)
    mentor, _ = make_mentor(env)
    job = env.service.review_next(mentor)
    assert job is not None
    first = _submit(env, mentor, entry_id, revision_id, job.job_id, job.lease_id, idempotency_key="review-1")
    second = _submit(env, mentor, entry_id, revision_id, job.job_id, job.lease_id, idempotency_key="review-1")
    assert first.review_id == second.review_id
    count = env.db.connect().execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
    assert count == 1


def test_peer_review_adds_evidence_but_cannot_publish(env: ServiceEnv) -> None:
    author, entry_id, revision_id = _author_entry(env)
    peer = env.session(env.register(source="peer"))
    job = env.service.review_next(peer)
    assert job is not None
    result = _submit(env, peer, entry_id, revision_id, job.job_id, job.lease_id)
    assert result.published is False
    assert result.lifecycle is Lifecycle.CANDIDATE
    assert result.job_state == "queued"

    detail = env.service.get_entry(author, entry_id)
    assert detail.lifecycle is Lifecycle.CANDIDATE
    assert env.db.connect().execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 1

    mentor, _ = make_mentor(env)
    mentor_job = env.service.review_next(mentor)
    assert mentor_job is not None
    published = _submit(env, mentor, entry_id, revision_id, mentor_job.job_id, mentor_job.lease_id)
    assert published.published is True
    assert env.service.get_entry(author, entry_id).lifecycle is Lifecycle.PUBLISHED


def test_self_and_family_reviews_are_ineligible(env: ServiceEnv) -> None:
    mentor, mentor_registration = make_mentor(env)
    write = env.service.record(
        mentor,
        RecordRequest(
            namespace="shared",
            entry=EntryPayload(kind=Kind.OBSERVATION, title="own work", claim="self review probe"),
        ),
    )
    entry_id = write.entry.entry_id
    revision_id = write.entry.revision_id

    assert env.service.review_next(mentor) is None

    child_registration = env.register(
        mode="child",
        ticket=env.service.delegate_child(mentor, ttl_s=600).ticket,
        label="child",
        source="child",
    )
    child = env.session(child_registration)
    assert env.service.review_next(child) is None

    future = iso(env.service._now() + timedelta(seconds=600))
    with env.db.transaction() as conn:
        conn.execute(
            "UPDATE review_jobs SET state = 'leased', lease_owner = ?, lease_id = 'lease-family', "
            "lease_expires_at = ? WHERE revision_id = ?",
            (child.session_id, future, revision_id),
        )
    with pytest.raises(ServiceError) as excinfo:
        _submit(env, child, entry_id, revision_id, None, "lease-family")
    assert excinfo.value.code is ErrorCode.REVIEW_INELIGIBLE
    attempts = (
        env.db.connect()
        .execute("SELECT attempts FROM review_jobs WHERE revision_id = ?", (revision_id,))
        .fetchone()[0]
    )
    assert attempts == 1
    assert mentor_registration.agent_id == mentor.agent_id


def test_contradiction_marks_revision_disputed(env: ServiceEnv) -> None:
    author, entry_id, revision_id = _author_entry(env, claim="contradiction probe claim")
    mentor, _ = make_mentor(env)
    job = env.service.review_next(mentor)
    assert job is not None
    result = _submit(
        env,
        mentor,
        entry_id,
        revision_id,
        job.job_id,
        job.lease_id,
        result=ReviewResult.CONTRADICTS,
    )
    assert result.published is False
    assert result.lifecycle is Lifecycle.DISPUTED

    detail = env.service.get_entry(author, entry_id)
    assert detail.lifecycle is Lifecycle.DISPUTED
    assert any("disputed" in warning for warning in detail.warnings)

    response = env.service.search(
        author,
        SearchRequest(query="contradiction probe", filters=SearchFilters(namespace="shared")),
    )
    assert response.results
    assert any("disputed" in warning for warning in response.results[0].warnings)


def test_lease_renewal_expiry_and_attempts(env: ServiceEnv) -> None:
    _, entry_id, revision_id = _author_entry(env, claim="lease probe claim")
    mentor, _ = make_mentor(env)
    first = env.service.review_next(mentor)
    assert first is not None
    env.clock.advance(100)
    renewed = env.service.review_next(mentor)
    assert renewed is not None
    assert renewed.job_id == first.job_id
    assert renewed.lease_expires_at is not None
    assert renewed.lease_expires_at > first.lease_expires_at  # type: ignore[operator]

    for _ in range(env.config.review.max_attempts):
        env.clock.advance(env.config.review.lease_seconds + 1)
        with pytest.raises(ServiceError) as excinfo:
            _submit(env, mentor, entry_id, revision_id, first.job_id, first.lease_id)
        assert excinfo.value.code is ErrorCode.REVIEW_INELIGIBLE

    row = (
        env.db.connect()
        .execute("SELECT attempts, state FROM review_jobs WHERE job_id = ?", (first.job_id,))
        .fetchone()
    )
    assert row["attempts"] == env.config.review.max_attempts
    assert row["state"] == "exhausted"
    assert env.service.review_next(mentor) is None


def test_expired_mentor_grant_cannot_review(env: ServiceEnv) -> None:
    author, entry_id, revision_id = _author_entry(env, claim="expiry probe claim")
    registration = env.register(source="expiring-mentor")
    session = env.session(registration)
    request = env.service.request_mentor(session, "shared", [])
    env.service.approve(request.request_id, role="mentor", ttl=60, namespace="shared")
    mentor = env.service.authenticate(registration.session_token)
    env.clock.advance(61)
    job = env.service.review_next(mentor)
    assert job is not None
    result = _submit(env, mentor, entry_id, revision_id, job.job_id, job.lease_id)
    # The mentor grant expired, so the remaining reviewer grant only adds
    # evidence and can never publish.
    assert result.published is False
    assert result.lifecycle is Lifecycle.CANDIDATE
    assert env.service.get_entry(author, entry_id).lifecycle is Lifecycle.CANDIDATE
