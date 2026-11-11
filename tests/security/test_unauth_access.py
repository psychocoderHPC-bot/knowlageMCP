# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Unregistered agents cannot read data over HTTP or the mounted MCP app."""

from __future__ import annotations

import json
from typing import Any

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from conftest import RunningGateway

pytestmark = pytest.mark.security

DATA_CALLS: list[tuple[str, dict[str, Any]]] = [
    ("search", {"query": "unregistered-canary"}),
    ("get_entry", {"entry_id": "ent_guessed"}),
    ("get_context", {"task": "unregistered-canary"}),
    ("review_next", {}),
    ("record", {"namespace": "shared", "entry": {"kind": "observation", "title": "x", "claim": "y"}}),
]


def _assert_auth_error(response: httpx2.Response) -> None:
    assert response.status_code == 401, response.text
    body = response.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "AUTH_REQUIRED"
    assert "unregistered-canary" not in response.text


def test_http_data_routes_require_bearer(gateway_server: RunningGateway) -> None:
    with httpx2.Client(base_url=gateway_server.base_url) as client:
        for tool, args in DATA_CALLS:
            _assert_auth_error(client.post(f"/v1/tools/{tool}", json=args))
        _assert_auth_error(client.post("/v1/bridge/mentor", json={"namespace": "shared"}))
        _assert_auth_error(client.get("/v1/bridge/enrollment/req_guessed"))
        _assert_auth_error(client.post("/v1/bridge/close"))
        _assert_auth_error(client.post("/v1/bridge/evidence", content=b"body"))
        _assert_auth_error(client.get(f"/v1/bridge/evidence/ev_{'0' * 64}"))


async def test_mcp_data_tools_require_bearer(gateway_server: RunningGateway) -> None:
    url = f"{gateway_server.base_url}/mcp"
    async with (
        httpx2.AsyncClient() as http_client,
        streamable_http_client(url, http_client=http_client) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        for tool, args in DATA_CALLS:
            result = await session.call_tool(tool, args)
            text = result.content[0].text
            envelope = json.loads(text)
            assert envelope["ok"] is False, envelope
            assert envelope["error"]["code"] == "AUTH_REQUIRED", envelope
            assert "unregistered-canary" not in text

        protocol = json.loads((await session.call_tool("get_protocol_info", {})).content[0].text)
        assert protocol["ok"] is True
        assert "session_token" not in json.dumps(protocol)
