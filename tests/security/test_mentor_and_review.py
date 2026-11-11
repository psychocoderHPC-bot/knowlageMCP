# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Mentor rights cannot be forged, borrowed, or used on one's own work."""

from __future__ import annotations

from typing import Any

import httpx2
import pytest

from conftest import RunningGateway, auth, record, register, sqlite_exec, sqlite_one

pytestmark = pytest.mark.security


def _request_mentor(client: httpx2.Client, token: str) -> dict[str, Any]:
    response = client.post(
        "/v1/bridge/mentor",
        headers=auth(token),
        json={"namespace": "shared", "scopes": ["review", "publish"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _approve(admin: Any, request_id: str) -> None:
    approved = admin.call("approve", {"request_id": request_id, "role": "mentor", "ttl": "2h"})
    assert approved["state"] == "approved"


def test_role_fields_in_payloads_cannot_self_grant(gateway_server: RunningGateway) -> None:
    with httpx2.Client(base_url=gateway_server.base_url) as client:
        token = register(client)["session_token"]
        forged_request = client.post(
            "/v1/bridge/mentor",
            headers=auth(token),
            json={"namespace": "shared", "scopes": [], "role": "mentor"},
        )
        assert forged_request.status_code == 400, forged_request.text
        assert forged_request.json()["error"]["code"] == "INVALID_ARGUMENT"

        forged_review = client.post(
            "/v1/tools/submit_review",
            headers=auth(token),
            json={
                "entry_id": "ent_x",
                "revision_id": "rev_x",
                "result": "supports",
                "method": "forged",
                "mentor": True,
            },
        )
        assert forged_review.status_code == 400
        assert forged_review.json()["error"]["code"] == "INVALID_ARGUMENT"

        forged_entry = client.post(
            "/v1/tools/record",
            headers=auth(token),
            json={
                "namespace": "shared",
                "entry": {
                    "kind": "observation",
                    "title": "forged",
                    "claim": "forged",
                    "role": "operator",
                },
            },
        )
        assert forged_entry.status_code == 400
        assert forged_entry.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_reviewer_cannot_publish_and_admin_commands_unreachable(gateway_server: RunningGateway) -> None:
    with httpx2.Client(base_url=gateway_server.base_url) as client:
        author_token = register(client, label="author")["session_token"]
        data = record(client, author_token, "review probe candidate")
        entry_id = data["entry"]["entry_id"]
        revision_id = data["entry"]["revision_id"]
        token = register(client, label="reviewer")["session_token"]

        leased = client.post("/v1/tools/review_next", headers=auth(token), json={})
        assert leased.status_code == 200, leased.text
        job = leased.json()["data"]["job"]
        assert job is not None
        submitted = client.post(
            "/v1/tools/submit_review",
            headers=auth(token),
            json={
                "entry_id": entry_id,
                "revision_id": revision_id,
                "job_id": job["job_id"],
                "lease_id": job["lease_id"],
                "result": "supports",
                "method": "manual",
                "evidence": "inspected the claim",
            },
        )
        assert submitted.status_code == 200, submitted.text
        result = submitted.json()["data"]
        assert result["published"] is False
        assert result["job_state"] == "queued"

        for tool in ("approve", "deny", "revoke", "reload", "config_show", "export", "memory_add"):
            response = client.post(f"/v1/tools/{tool}", headers=auth(token), json={})
            assert response.status_code == 404, (tool, response.text)
            assert response.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_foreign_session_cannot_poll_or_approve_request(gateway_server: RunningGateway, admin: Any) -> None:
    with httpx2.Client(base_url=gateway_server.base_url) as client:
        first = register(client, label="requester")
        second = register(client, label="other")
        request_id = _request_mentor(client, first["session_token"])["request_id"]
        poll = client.get(f"/v1/bridge/enrollment/{request_id}", headers=auth(second["session_token"]))
        assert poll.status_code == 404, poll.text
        assert poll.json()["error"]["code"] == "NOT_FOUND"
        # There is no agent-facing approval route.
        approve = client.post(
            "/v1/tools/approve",
            headers=auth(second["session_token"]),
            json={"request_id": request_id, "role": "mentor"},
        )
        assert approve.status_code == 404, approve.text
        assert approve.json()["error"]["code"] == "INVALID_ARGUMENT"
    enrollment = next(item for item in admin.call("enrollments") if item["request_id"] == request_id)
    assert enrollment["state"] == "pending"


def test_expired_mentor_request_cannot_be_approved(gateway_server: RunningGateway, admin: Any) -> None:
    from knowlage_mcp.admin_client import AdminError

    with httpx2.Client(base_url=gateway_server.base_url) as client:
        token = register(client)["session_token"]
        request_id = _request_mentor(client, token)["request_id"]
    sqlite_exec(
        gateway_server.config.storage.database,
        "UPDATE mentor_requests SET expires_at = ? WHERE request_id = ?",
        ("2000-01-01T00:00:00.000000Z", request_id),
    )
    with pytest.raises(AdminError) as error:
        admin.call("approve", {"request_id": request_id, "role": "mentor"})
    assert error.value.code == "CONFLICT"


def test_mentor_cannot_review_own_contributed_revision(gateway_server: RunningGateway, admin: Any) -> None:
    database = gateway_server.config.storage.database
    with httpx2.Client(base_url=gateway_server.base_url) as client:
        mentor = register(client, label="mentor")
        token = mentor["session_token"]
        _approve(admin, _request_mentor(client, token)["request_id"])
        data = record(
            client,
            token,
            "mentor contributed claim",
            contributors=[mentor["agent_id"]],
        )
        entry_id = data["entry"]["entry_id"]
        revision_id = data["entry"]["revision_id"]
        job = _job_id(database, revision_id)
        sqlite_exec(
            database,
            "UPDATE review_jobs SET state = 'leased', lease_owner = ?, lease_id = ?, "
            "lease_expires_at = ? WHERE job_id = ?",
            (mentor["session_id"], "lse_forced", "2999-01-01T00:00:00.000000Z", job),
        )
        submitted = client.post(
            "/v1/tools/submit_review",
            headers=auth(token),
            json={
                "entry_id": entry_id,
                "revision_id": revision_id,
                "job_id": job,
                "lease_id": "lse_forced",
                "result": "supports",
                "method": "manual",
                "evidence": "self review",
            },
        )
        assert submitted.status_code == 403, submitted.text
        assert submitted.json()["error"]["code"] == "REVIEW_INELIGIBLE"
        detail = client.post("/v1/tools/get_entry", headers=auth(token), json={"entry_id": entry_id})
    assert detail.status_code == 200
    assert detail.json()["data"]["lifecycle"] == "candidate"


def _job_id(database: Any, revision_id: str) -> str:
    row = sqlite_one(database, "SELECT job_id FROM review_jobs WHERE revision_id = ?", (revision_id,))
    assert row is not None
    return str(row["job_id"])
