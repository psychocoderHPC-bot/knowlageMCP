# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Client-supplied identity fields and actor handles cannot forge identity."""

from __future__ import annotations

import json

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from secutil import RunningGateway, auth, register

from knowlage_mcp.bridge import BridgeBackend

pytestmark = pytest.mark.security


def test_http_registration_ignores_or_rejects_forged_ids(gateway_server: RunningGateway) -> None:
    import httpx2

    forged = {
        "root_agent_id": "root_forged_9999",
        "agent_id": "agt_forged_9999",
        "session_id": "ses_forged_9999",
    }
    with httpx2.Client(base_url=gateway_server.base_url) as client:
        response = client.post("/v1/bridge/register", json={"mode": "root", **forged})
        if response.status_code == 200:
            data = response.json()
            assert data["root_agent_id"] != forged["root_agent_id"]
            assert data["agent_id"] != forged["agent_id"]
            assert data["session_id"] != forged["session_id"]
        else:
            assert response.status_code == 400, response.text
            assert response.json()["error"]["code"] == "INVALID_ARGUMENT"
        for value in forged.values():
            assert value not in response.text


def test_child_registration_cannot_choose_root(gateway_server: RunningGateway) -> None:
    import httpx2

    with httpx2.Client(base_url=gateway_server.base_url) as client:
        parent = register(client)
        ticket = client.post("/v1/tools/delegate_child", headers=auth(parent["session_token"]), json={})
        assert ticket.status_code == 200, ticket.text
        ticket_id = ticket.json()["data"]["ticket"]
        forged_root = "root_forged_child_1234"
        response = client.post(
            "/v1/bridge/register",
            json={"mode": "child", "ticket": ticket_id, "root_agent_id": forged_root},
        )
        if response.status_code == 200:
            assert response.json()["root_agent_id"] == parent["root_agent_id"]
            assert response.json()["root_agent_id"] != forged_root
        else:
            assert response.status_code == 400
            assert response.json()["error"]["code"] == "INVALID_ARGUMENT"
        assert forged_root not in response.text


def test_tool_schema_rejects_forged_identity_fields(gateway_server: RunningGateway) -> None:
    import httpx2

    with httpx2.Client(base_url=gateway_server.base_url) as client:
        token = register(client)["session_token"]
        for field in ("root_agent_id", "agent_id", "session_id"):
            response = client.post(
                "/v1/tools/register_agent",
                headers=auth(token),
                json={"mode": "root", field: "forged_value"},
            )
            assert response.status_code == 400, response.text
            assert response.json()["error"]["code"] == "INVALID_ARGUMENT"
            assert "forged_value" not in response.text


def test_actor_handle_of_another_session_is_rejected(gateway_server: RunningGateway) -> None:
    import httpx2

    with httpx2.Client(base_url=gateway_server.base_url) as client:
        first = register(client, label="first")
        second = register(client, label="second")
        forged = client.post(
            "/v1/tools/search",
            headers={**auth(second["session_token"]), "X-Actor-Handle": first["actor_handle"]},
            json={"query": "x"},
        )
        assert forged.status_code == 401, forged.text
        assert forged.json()["error"]["code"] == "AUTH_REQUIRED"
        foreign = client.post(
            "/v1/tools/search",
            headers={**auth(first["session_token"]), "X-Actor-Handle": "act_foreign"},
            json={"query": "x"},
        )
        assert foreign.status_code == 401
        assert foreign.json()["error"]["code"] == "AUTH_REQUIRED"


async def test_multiplexed_bridge_without_actor_fails_closed(gateway_server: RunningGateway) -> None:
    backend = BridgeBackend(gateway_server.base_url)
    try:
        one = await backend.register({"label": "bridge-one"})
        two = await backend.register({"label": "bridge-two"})
        assert one["ok"] is True and two["ok"] is True
        missing = await backend.tool("search", {"query": "x"})
        assert missing["ok"] is False
        assert missing["error"]["code"] == "AUTH_REQUIRED"
        foreign = await backend.tool("search", {"query": "x"}, "act_foreign")
        assert foreign["ok"] is False
        assert foreign["error"]["code"] == "AUTH_REQUIRED"
    finally:
        await backend.aclose()


async def test_mcp_register_agent_does_not_accept_forged_ids(gateway_server: RunningGateway) -> None:
    url = f"{gateway_server.base_url}/mcp"
    forged = {
        "root_agent_id": "root_forged_mcp",
        "agent_id": "agt_forged_mcp",
        "session_id": "ses_forged_mcp",
    }
    async with (
        httpx2.AsyncClient() as http_client,
        streamable_http_client(url, http_client=http_client) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        try:
            result = await session.call_tool("register_agent", {"mode": "root", **forged})
        except Exception:
            # The MCP boundary rejected the unknown arguments outright.
            return
        text = result.content[0].text
        envelope = json.loads(text)
        if envelope.get("ok"):
            data = envelope["data"]
            assert data["root_agent_id"] != forged["root_agent_id"]
            assert data["agent_id"] != forged["agent_id"]
            assert data["session_id"] != forged["session_id"]
        else:
            assert envelope["error"]["code"] == "INVALID_ARGUMENT"
        for value in forged.values():
            assert value not in text
