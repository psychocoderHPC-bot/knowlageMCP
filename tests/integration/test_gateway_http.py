# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Real-HTTP tests for the bridge API and tool dispatcher."""

from __future__ import annotations

from typing import Any

import httpx2
import pytest

from conftest import RunningGateway

pytestmark = pytest.mark.integration


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _register(client: httpx2.Client) -> dict[str, Any]:
    response = client.post(
        "/v1/bridge/register",
        json={"mode": "root", "label": "http-test", "model": "pytest", "provider": "local"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _record(client: httpx2.Client, token: str, claim: str) -> dict[str, Any]:
    response = client.post(
        "/v1/tools/record",
        headers=_auth(token),
        json={
            "namespace": "shared",
            "entry": {
                "kind": "observation",
                "title": "integration probe",
                "claim": claim,
                "tags": ["integration"],
            },
        },
    )
    assert response.status_code == 200, response.text
    envelope = response.json()
    assert envelope["ok"] is True, envelope
    return envelope["data"]


def test_health_reveals_no_secrets(gateway_server: RunningGateway) -> None:
    with httpx2.Client(base_url=gateway_server.base_url) as client:
        response = client.get("/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"status", "version", "protocol_version", "schema_version", "uptime_s"}
    assert body["status"] == "ok"
    assert "token" not in response.text.lower()
    assert "database" not in response.text.lower()


def test_register_record_search_get_entry(gateway_server: RunningGateway) -> None:
    claim = "distinct integration phrase 91f3"
    with httpx2.Client(base_url=gateway_server.base_url) as client:
        registration = _register(client)
        token = registration["session_token"]
        assert registration["actor_handle"].startswith("act_")
        assert registration["grants"]
        data = _record(client, token, claim)
        entry = data["entry"]
        assert entry["claim"] == claim

        response = client.post(
            "/v1/tools/search",
            headers=_auth(token),
            json={"query": "distinct integration phrase", "filters": {"namespace": "shared"}},
        )
        assert response.status_code == 200
        envelope = response.json()
        assert envelope["ok"] is True
        entry_ids = [item["entry_id"] for item in envelope["data"]["results"]]
        assert entry["entry_id"] in entry_ids

        detail = client.post(
            "/v1/tools/get_entry",
            headers=_auth(token),
            json={"entry_id": entry["entry_id"]},
        )
        assert detail.status_code == 200
        detail_body = detail.json()
        assert detail_body["ok"] is True
        assert detail_body["data"]["entry_id"] == entry["entry_id"]


def test_bearer_required_and_wrong_token(gateway_server: RunningGateway) -> None:
    with httpx2.Client(base_url=gateway_server.base_url) as client:
        missing = client.post("/v1/tools/search", json={"query": "x"})
        assert missing.status_code == 401
        assert missing.json()["error"]["code"] == "AUTH_REQUIRED"
        wrong = client.post(
            "/v1/tools/search",
            headers=_auth("not-a-real-token"),
            json={"query": "x"},
        )
    assert wrong.status_code == 401
    assert wrong.json()["ok"] is False
    assert set(wrong.json()["error"]) >= {"code", "message"}


def test_revoked_token_is_rejected(gateway_server: RunningGateway) -> None:
    with httpx2.Client(base_url=gateway_server.base_url) as client:
        token = _register(client)["session_token"]
        closed = client.post("/v1/bridge/close", headers=_auth(token))
        assert closed.status_code == 200
        assert closed.json()["closed"] is True
        replay = client.post("/v1/tools/search", headers=_auth(token), json={"query": "x"})
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "SESSION_EXPIRED"


def test_host_and_origin_rejection(gateway_server: RunningGateway) -> None:
    with httpx2.Client(base_url=gateway_server.base_url) as client:
        host = client.get("/v1/health", headers={"Host": "evil.example"})
        origin = client.get("/v1/health", headers={"Origin": "http://evil.example"})
    assert host.status_code == 400
    assert origin.status_code == 403
    assert host.json()["ok"] is False
    assert origin.json()["ok"] is False


def test_oversized_body_is_rejected(gateway_server: RunningGateway) -> None:
    with httpx2.Client(base_url=gateway_server.base_url) as client:
        response = client.post(
            "/v1/bridge/register",
            content=b"{" + b"a" * 9000,
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "BUDGET_EXCEEDED"


def test_unknown_tool_is_not_found(gateway_server: RunningGateway) -> None:
    with httpx2.Client(base_url=gateway_server.base_url) as client:
        token = _register(client)["session_token"]
        response = client.post("/v1/tools/not_a_tool", headers=_auth(token), json={})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_invalid_tool_arguments_return_envelope(gateway_server: RunningGateway) -> None:
    with httpx2.Client(base_url=gateway_server.base_url) as client:
        token = _register(client)["session_token"]
        response = client.post("/v1/tools/search", headers=_auth(token), json={"unknown": 1})
    assert response.status_code == 400
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "INVALID_ARGUMENT"
    assert "unknown" in body["error"]["message"]

    with httpx2.Client(base_url=gateway_server.base_url) as client:
        token = _register(client)["session_token"]
        invalid_enum = client.post(
            "/v1/tools/submit_review",
            headers=_auth(token),
            json={
                "entry_id": "ent_x",
                "revision_id": "rev_x",
                "result": "not-a-result",
                "method": "manual",
            },
        )
    assert invalid_enum.status_code == 400
    assert invalid_enum.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_evidence_upload_download_and_cap(gateway_server: RunningGateway) -> None:
    payload = b"evidence body with a synthetic sha\n"
    with httpx2.Client(base_url=gateway_server.base_url) as client:
        token = _register(client)["session_token"]
        headers = {
            **_auth(token),
            "Content-Type": "text/plain",
            "X-Evidence-Name": "note.txt",
        }
        uploaded = client.post("/v1/bridge/evidence", content=payload, headers=headers)
        assert uploaded.status_code == 200, uploaded.text
        record = uploaded.json()
        assert record["size_bytes"] == len(payload)
        assert record["evidence_id"].startswith("ev_")

        downloaded = client.get(f"/v1/bridge/evidence/{record['evidence_id']}", headers=_auth(token))
        assert downloaded.status_code == 200
        assert downloaded.content == payload

        too_big = client.post("/v1/bridge/evidence", content=b"x" * 2048, headers=headers)
        assert too_big.status_code == 413
        assert too_big.json()["error"]["code"] == "BUDGET_EXCEEDED"

        unauthenticated = client.get(f"/v1/bridge/evidence/{record['evidence_id']}")
        assert unauthenticated.status_code == 401
