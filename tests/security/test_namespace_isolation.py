# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""A shared-only agent sees no personal or foreign-project content anywhere."""

from __future__ import annotations

from typing import Any

import httpx2
import pytest

from conftest import RunningGateway, auth, record, register

pytestmark = pytest.mark.security

PERSONAL = "PERSONALLEAK zeta private"
PROJECT = "PROJECTLEAK omega internal"


def _seed(gateway_server: RunningGateway, admin: Any, client: httpx2.Client) -> dict[str, Any]:
    personal = admin.call(
        "memory.add",
        {
            "kind": "observation",
            "namespace": "personal",
            "text": PERSONAL,
            "title": "personal secret",
        },
    )
    admin.call("namespaces.create", {"name": "projectx", "kind": "shared"})
    writer = register(client, label="project-writer")
    admin.call(
        "grants.add",
        {"agent_id": writer["agent_id"], "namespace": "projectx", "role": "contributor"},
    )
    evidence = client.post(
        "/v1/bridge/evidence",
        headers={**auth(writer["session_token"]), "Content-Type": "text/plain"},
        content=b"project evidence body",
    )
    assert evidence.status_code == 200, evidence.text
    evidence_id = evidence.json()["evidence_id"]
    recorded = client.post(
        "/v1/tools/record",
        headers=auth(writer["session_token"]),
        json={
            "namespace": "projectx",
            "entry": {
                "kind": "observation",
                "title": "project secret",
                "claim": PROJECT,
                "evidence": [{"evidence_id": evidence_id}],
            },
        },
    )
    assert recorded.status_code == 200, recorded.text
    return {
        "personal_id": personal["entry"]["entry_id"],
        "project_id": recorded.json()["data"]["entry"]["entry_id"],
        "evidence_id": evidence_id,
    }


def test_shared_only_agent_sees_no_foreign_content(gateway_server: RunningGateway, admin: Any) -> None:
    with httpx2.Client(base_url=gateway_server.base_url) as client:
        seeded = _seed(gateway_server, admin, client)
        token = register(client, label="ordinary")["session_token"]
        headers = auth(token)

        for query, forbidden in ((PERSONAL, PERSONAL), (PROJECT, PROJECT)):
            response = client.post("/v1/tools/search", headers=headers, json={"query": query})
            assert response.status_code == 200, response.text
            envelope = response.json()
            assert envelope["ok"] is True
            assert envelope["data"]["total"] == 0
            assert envelope["data"]["results"] == []
            assert forbidden not in response.text

        for entry_id in (seeded["personal_id"], seeded["project_id"]):
            detail = client.post("/v1/tools/get_entry", headers=headers, json={"entry_id": entry_id})
            assert detail.status_code == 403, detail.text
            assert detail.json()["error"]["code"] == "FORBIDDEN"
            assert PERSONAL not in detail.text
            assert PROJECT not in detail.text

        context = client.post(
            "/v1/tools/get_context", headers=headers, json={"task": "PERSONALLEAK PROJECTLEAK"}
        )
        assert context.status_code == 200, context.text
        assert context.json()["data"]["results"] == []
        assert context.json()["data"]["namespaces"] == ["shared"]
        assert PERSONAL not in context.text and PROJECT not in context.text

        queue = client.post(
            "/v1/tools/review_next", headers=headers, json={"filters": {"namespace": "projectx"}}
        )
        assert queue.status_code == 200, queue.text
        assert queue.json()["data"]["available"] is False
        assert PROJECT not in queue.text

        evidence = client.get(f"/v1/bridge/evidence/{seeded['evidence_id']}", headers=headers)
        assert evidence.status_code == 404, evidence.text
        assert b"project evidence body" not in evidence.content

        for tool in ("export", "admin_search", "inspect", "memory_forget"):
            response = client.post(f"/v1/tools/{tool}", headers=headers, json={})
            assert response.status_code == 404, (tool, response.text)

        shared = record(client, token, "SHAREDOK visible claim")
        found = client.post("/v1/tools/search", headers=headers, json={"query": "SHAREDOK"})
        assert found.status_code == 200
        assert shared["entry"]["entry_id"] in [item["entry_id"] for item in found.json()["data"]["results"]]
