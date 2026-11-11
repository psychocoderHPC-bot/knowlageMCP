# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Authorization, ACL, and rate-limit policy tests."""

from __future__ import annotations

import pytest

from conftest import ManualClock, ServiceEnv
from knowlage_mcp.models import (
    EntryPayload,
    ErrorCode,
    NamespaceRole,
    RecordRequest,
    ServiceError,
)
from knowlage_mcp.policy import (
    ROLE_RANK,
    SlidingWindowLimiter,
    can_read,
    can_write,
    effective_grants,
    family_roots,
    role_rank,
)


def test_role_ranking_is_strictly_ordered() -> None:
    assert (
        role_rank(NamespaceRole.READER)
        < role_rank(NamespaceRole.CONTRIBUTOR)
        < role_rank(NamespaceRole.REVIEWER)
        < role_rank(NamespaceRole.MENTOR)
        < role_rank(NamespaceRole.OPERATOR)
    )
    assert role_rank("unknown-role") == 0
    assert set(ROLE_RANK) == set(NamespaceRole)


def test_effective_grants_keep_strongest_role(env: ServiceEnv) -> None:
    registration = env.register()
    env.service.grants_add(registration.agent_id, "shared", "mentor")
    grants = effective_grants(env.db.connect(), registration.agent_id, env.service._now())
    assert grants["shared"] is NamespaceRole.MENTOR
    # The weaker duplicate grant rows still exist but are not returned.
    conn = env.db.connect()
    count = conn.execute(
        "SELECT COUNT(DISTINCT role) FROM grants WHERE agent_id = ?", (registration.agent_id,)
    ).fetchone()[0]
    assert count == 4  # reader, contributor, reviewer, mentor


def test_personal_namespace_requires_privileged_explicit_grant(env: ServiceEnv) -> None:
    registration = env.register()
    now = env.service._now()
    conn = env.db.connect()
    assert can_read(conn, registration.agent_id, "personal", now) is False
    assert can_write(conn, registration.agent_id, "personal", now) is False

    env.service.grants_add(registration.agent_id, "personal", "reader")
    assert can_read(conn, registration.agent_id, "personal", now) is False

    env.service.grants_add(registration.agent_id, "personal", "mentor")
    assert can_read(conn, registration.agent_id, "personal", now) is True
    assert can_write(conn, registration.agent_id, "personal", now) is False

    env.service.grants_add(registration.agent_id, "personal", "operator")
    assert can_write(conn, registration.agent_id, "personal", now) is True


def test_shared_namespace_write_requires_contributor(env: ServiceEnv) -> None:
    registration = env.register()
    now = env.service._now()
    conn = env.db.connect()
    with env.db.transaction() as tx:
        tx.execute(
            "UPDATE grants SET revoked_at = '2025-01-01T00:00:00.000000Z' WHERE agent_id = ?",
            (registration.agent_id,),
        )
    assert can_read(conn, registration.agent_id, "shared", now) is False
    with pytest.raises(ServiceError):
        env.service.record(
            env.session(registration),
            RecordRequest(
                namespace="shared",
                entry=EntryPayload(kind="observation", title="t", claim="denied"),
            ),
        )


def test_family_roots_include_contributor_families(env: ServiceEnv) -> None:
    author = env.register(source="author")
    contributor = env.register(source="contributor")
    conn = env.db.connect()
    roots = family_roots(
        conn,
        author_root_id=author.agent_id,
        contributors=[contributor.agent_id],
    )
    assert roots == {author.agent_id, contributor.agent_id}


def test_sliding_window_limiter_with_fake_clock() -> None:
    clock = ManualClock(start=100.0)
    limiter = SlidingWindowLimiter(clock)
    assert limiter.allow("k", 2, 60) == (True, 0)
    assert limiter.allow("k", 2, 60) == (True, 0)
    allowed, retry = limiter.allow("k", 2, 60)
    assert allowed is False
    assert retry >= 1
    clock.advance(61)
    assert limiter.allow("k", 2, 60) == (True, 0)
    assert limiter.allow("other", 1, 60) == (True, 0)


def test_unknown_role_is_invalid_argument(env: ServiceEnv) -> None:
    registration = env.register()
    with pytest.raises(ServiceError) as excinfo:
        env.service.grants_add(registration.agent_id, "shared", "superuser")
    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENT
