# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Identity, registration, delegation, session, mentor, and revocation tests."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from conftest import ServiceEnv, build_env
from knowlage_mcp.identity import hash_token
from knowlage_mcp.models import ErrorCode, NamespaceRole, ServiceError
from knowlage_mcp.policy import effective_role


def _code(exc_info: pytest.ExceptionInfo[ServiceError]) -> ErrorCode:
    return exc_info.value.code


def test_root_registration_defaults_and_token_storage(env: ServiceEnv) -> None:
    registration = env.register(label="root-a")
    roles = {grant.role for grant in registration.grants}
    assert roles == {NamespaceRole.READER, NamespaceRole.CONTRIBUTOR, NamespaceRole.REVIEWER}
    assert {grant.namespace for grant in registration.grants} == {"shared"}
    assert registration.actor_handle.startswith("act_")
    assert registration.session_token not in registration.actor_handle
    assert len(registration.session_token) >= 32

    row = (
        env.db.connect()
        .execute("SELECT token_hash FROM sessions WHERE session_id = ?", (registration.session_id,))
        .fetchone()
    )
    assert row is not None
    assert row["token_hash"] == hash_token(registration.session_token)
    assert row["token_hash"] != registration.session_token

    session = env.session(registration)
    assert session.agent_id == registration.agent_id
    assert session.root_agent_id == registration.agent_id


def test_registration_source_rate_limit(tmp_path: Path) -> None:
    env = build_env(
        tmp_path,
        limits={"registration_per_minute_per_source": 2, "registration_per_minute_global": 100},
    )
    try:
        env.register(source="same")
        env.register(source="same")
        with pytest.raises(ServiceError) as excinfo:
            env.register(source="same")
        assert _code(excinfo) is ErrorCode.RATE_LIMITED
        assert excinfo.value.retry_after_s is not None
    finally:
        env.db.close()


def test_registration_global_rate_limit(tmp_path: Path) -> None:
    env = build_env(
        tmp_path,
        limits={"registration_per_minute_per_source": 100, "registration_per_minute_global": 2},
    )
    try:
        env.register(source="a")
        env.register(source="b")
        with pytest.raises(ServiceError) as excinfo:
            env.register(source="c")
        assert _code(excinfo) is ErrorCode.RATE_LIMITED
    finally:
        env.db.close()


def test_max_sessions_fails_without_eviction(tmp_path: Path) -> None:
    env = build_env(tmp_path, identity={"max_sessions": 2})
    try:
        first = env.register(source="a")
        second = env.register(source="b")
        with pytest.raises(ServiceError) as excinfo:
            env.register(source="c")
        assert _code(excinfo) is ErrorCode.RATE_LIMITED
        assert excinfo.value.details == {"max_sessions": 2}
        # Existing sessions stay usable.
        assert env.service.authenticate(first.session_token).session_id == first.session_id
        assert env.service.authenticate(second.session_token).session_id == second.session_id
    finally:
        env.db.close()


def test_child_ticket_single_use_and_root_inheritance(env: ServiceEnv) -> None:
    parent = env.register(label="parent")
    parent_session = env.session(parent)
    delegation = env.service.delegate_child(parent_session, ttl_s=60)
    child = env.register(mode="child", ticket=delegation.ticket, label="child")
    assert child.root_agent_id == parent.agent_id
    row = (
        env.db.connect()
        .execute("SELECT parent_agent_id FROM agents WHERE agent_id = ?", (child.agent_id,))
        .fetchone()
    )
    assert row["parent_agent_id"] == parent.agent_id

    with pytest.raises(ServiceError) as excinfo:
        env.register(mode="child", ticket=delegation.ticket, label="replay")
    assert _code(excinfo) in {ErrorCode.INVALID_ARGUMENT, ErrorCode.FORBIDDEN}


def test_child_grants_capped_to_parent(env: ServiceEnv) -> None:
    parent = env.register(label="parent")
    parent_session = env.session(parent)
    delegation = env.service.delegate_child(parent_session, ttl_s=60)
    # Remove the stronger parent grants after issuing the ticket.
    with env.db.transaction() as conn:
        conn.execute(
            "UPDATE grants SET revoked_at = '2025-01-01T00:00:00.000000Z' "
            "WHERE agent_id = ? AND role != 'reader'",
            (parent.agent_id,),
        )
    child = env.register(mode="child", ticket=delegation.ticket)
    child_roles = {grant.role for grant in child.grants}
    assert child_roles == {NamespaceRole.READER}


def test_revoking_parent_cascades_to_descendants(env: ServiceEnv) -> None:
    parent = env.register(label="parent")
    parent_session = env.session(parent)
    child_reg = env.register(mode="child", ticket=env.service.delegate_child(parent_session, ttl_s=60).ticket)
    child_session = env.session(child_reg)
    grandchild_reg = env.register(
        mode="child", ticket=env.service.delegate_child(child_session, ttl_s=60).ticket
    )
    dangling = env.service.delegate_child(child_session, ttl_s=600)

    result = env.service.revoke(parent.agent_id)
    assert result["agents"] == 3
    for registration in (parent, child_reg, grandchild_reg):
        with pytest.raises(ServiceError) as excinfo:
            env.service.authenticate(registration.session_token)
        assert _code(excinfo) is ErrorCode.SESSION_EXPIRED
    with pytest.raises(ServiceError) as excinfo:
        env.register(mode="child", ticket=dangling.ticket)
    assert _code(excinfo) is ErrorCode.INVALID_ARGUMENT


def test_idle_and_absolute_session_expiry(env: ServiceEnv) -> None:
    registration = env.register()
    env.clock.advance(env.config.identity.session_idle_timeout + 1)
    with pytest.raises(ServiceError) as excinfo:
        env.service.authenticate(registration.session_token)
    assert _code(excinfo) is ErrorCode.SESSION_EXPIRED

    absolute = env.register()
    env.clock.advance(env.config.identity.session_absolute_lifetime + 1)
    with pytest.raises(ServiceError) as excinfo:
        env.service.authenticate(absolute.session_token)
    assert _code(excinfo) is ErrorCode.SESSION_EXPIRED


def test_session_close_and_revocation(env: ServiceEnv) -> None:
    registration = env.register()
    session = env.session(registration)
    env.service.session_close(session)
    with pytest.raises(ServiceError) as excinfo:
        env.service.authenticate(registration.session_token)
    assert _code(excinfo) is ErrorCode.SESSION_EXPIRED

    other = env.register()
    env.service.revoke(other.session_id)
    with pytest.raises(ServiceError) as excinfo:
        env.service.authenticate(other.session_token)
    assert _code(excinfo) is ErrorCode.SESSION_EXPIRED


def test_actor_handle_ambiguity(env: ServiceEnv) -> None:
    first = env.register(source="a")
    second = env.register(source="b")
    handles = (first.actor_handle, second.actor_handle)

    with pytest.raises(ServiceError) as excinfo:
        env.service.resolve_actor(first.session_token, connection_actors=handles)
    assert _code(excinfo) is ErrorCode.AUTH_REQUIRED

    with pytest.raises(ServiceError) as excinfo:
        env.service.resolve_actor(first.session_token, second.actor_handle, connection_actors=handles)
    assert _code(excinfo) is ErrorCode.AUTH_REQUIRED

    resolved = env.service.resolve_actor(first.session_token, first.actor_handle, connection_actors=handles)
    assert resolved.session_id == first.session_id
    # A single actor may omit the handle.
    assert env.service.resolve_actor(first.session_token).session_id == first.session_id


def test_mentor_request_approval_and_status_isolation(env: ServiceEnv) -> None:
    requester = env.register(source="requester")
    session = env.session(requester)
    request = env.service.request_mentor(session, "shared", ["review", "publish"])
    assert request.state == "pending"
    assert re.fullmatch(r"[A-Z]{4}-\d{4}", request.code)
    assert request.scopes == ("review", "publish")

    status = env.service.enrollment_status(session, request.request_id)
    assert status.state == "pending"

    other = env.register(source="other")
    with pytest.raises(ServiceError) as excinfo:
        env.service.enrollment_status(env.session(other), request.request_id)
    assert _code(excinfo) is ErrorCode.NOT_FOUND

    approved = env.service.approve(request.request_id, role="mentor", ttl=7200, namespace="shared")
    assert approved.state == "approved"
    refreshed = env.service.authenticate(requester.session_token)
    role = effective_role(env.db.connect(), requester.agent_id, "shared", env.service._now())
    assert role is NamespaceRole.MENTOR
    assert any(grant.role is NamespaceRole.MENTOR for grant in refreshed.grants)


def test_expired_request_cannot_be_approved(env: ServiceEnv) -> None:
    registration = env.register()
    session = env.session(registration)
    request = env.service.request_mentor(session, "shared", [])
    env.clock.advance(env.config.identity.mentor_request_lifetime + 1)
    with pytest.raises(ServiceError) as excinfo:
        env.service.approve(request.request_id)
    assert _code(excinfo) is ErrorCode.CONFLICT
    status = env.service.enrollment_status(session, request.request_id)
    assert status.state == "expired"


def test_deny_mentor_request_rejects_later_approval(env: ServiceEnv) -> None:
    registration = env.register()
    session = env.session(registration)
    request = env.service.request_mentor(session, "shared", [])
    denied = env.service.deny(request.request_id)
    assert denied.state == "denied"
    with pytest.raises(ServiceError) as excinfo:
        env.service.approve(request.request_id)
    assert _code(excinfo) is ErrorCode.CONFLICT


def test_bootstrap_is_idempotent_and_revokes_live_sessions(env: ServiceEnv) -> None:
    registration = env.register()
    first = env.service.bootstrap()
    assert first["schema_version"] >= 1
    namespaces = {item["name"] for item in env.service.namespaces_list()}
    assert namespaces == {"shared", "personal"}
    second = env.service.bootstrap()
    assert second["schema_version"] == first["schema_version"]
    assert {item["name"] for item in env.service.namespaces_list()} == namespaces
    with pytest.raises(ServiceError) as excinfo:
        env.service.authenticate(registration.session_token)
    assert _code(excinfo) is ErrorCode.SESSION_EXPIRED
