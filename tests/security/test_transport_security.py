# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Host/Origin allowlists, bounded bodies, and admin socket bind safety."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from secutil import RunningGateway, auth, register

from knowlage_mcp.admin import AdminServer

pytestmark = pytest.mark.security

HOST_MARKER = "Host header is not allowed"
ORIGIN_MARKER = "Origin header is not allowed"


def test_host_and_origin_rejected_on_v1_and_mcp(gateway_server: RunningGateway) -> None:
    with httpx2.Client(base_url=gateway_server.base_url) as client:
        bad_host = client.get("/v1/health", headers={"Host": "evil.example"})
        assert bad_host.status_code == 400, bad_host.text
        assert HOST_MARKER in bad_host.text
        bad_origin = client.get("/v1/health", headers={"Origin": "http://evil.example"})
        assert bad_origin.status_code == 403, bad_origin.text
        assert ORIGIN_MARKER in bad_origin.text
        allowed = client.get("/v1/health", headers={"Host": "localhost"})
        assert allowed.status_code == 200
        assert HOST_MARKER not in allowed.text

        mcp_headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        mcp_bad_host = client.post("/mcp", headers={"Host": "evil.example", **mcp_headers}, content=b"{}")
        assert mcp_bad_host.status_code == 400, mcp_bad_host.text
        assert HOST_MARKER in mcp_bad_host.text
        mcp_bad_origin = client.post(
            "/mcp", headers={"Origin": "http://evil.example", **mcp_headers}, content=b"{}"
        )
        assert mcp_bad_origin.status_code == 403, mcp_bad_origin.text
        assert ORIGIN_MARKER in mcp_bad_origin.text
        mcp_allowed = client.get("/mcp", headers={"Host": "localhost", "Accept": "text/event-stream"})
        assert HOST_MARKER not in mcp_allowed.text
        assert mcp_allowed.status_code != 403


def test_json_body_over_generic_limit_is_rejected(gateway_server: RunningGateway) -> None:
    limit = gateway_server.config.server.max_request_bytes
    with httpx2.Client(base_url=gateway_server.base_url) as client:
        response = client.post(
            "/v1/bridge/register",
            content=b'{"label":"' + b"x" * (limit + 16) + b'"}',
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 413, response.text
    assert response.json()["error"]["code"] == "BUDGET_EXCEEDED"
    assert str(limit) in response.json()["error"]["message"]


def test_evidence_uses_its_own_byte_budget(gateway_server: RunningGateway) -> None:
    json_limit = gateway_server.config.server.max_request_bytes
    evidence_limit = gateway_server.config.limits.evidence_object_bytes
    assert evidence_limit > json_limit
    headers = {"Content-Type": "application/octet-stream"}
    with httpx2.Client(base_url=gateway_server.base_url) as client:
        token = register(client)["session_token"]
        upload_headers = {**auth(token), **headers}

        within_json_cap = client.post(
            "/v1/bridge/evidence", content=b"a" * (json_limit + 1024), headers=upload_headers
        )
        assert within_json_cap.status_code == 200, within_json_cap.text
        assert within_json_cap.json()["size_bytes"] == json_limit + 1024

        at_object_cap = client.post(
            "/v1/bridge/evidence", content=b"b" * evidence_limit, headers=upload_headers
        )
        assert at_object_cap.status_code == 200, at_object_cap.text
        assert at_object_cap.json()["size_bytes"] == evidence_limit

        above_object_cap = client.post(
            "/v1/bridge/evidence", content=b"c" * (evidence_limit + 1), headers=upload_headers
        )
        assert above_object_cap.status_code == 413, above_object_cap.text
        assert above_object_cap.json()["error"]["code"] == "BUDGET_EXCEEDED"
        assert str(evidence_limit) in above_object_cap.json()["error"]["message"]


def test_admin_socket_refuses_symlink_live_socket_and_regular_file(
    gateway_server: RunningGateway, tmp_path: Path
) -> None:
    async def attempt() -> None:
        target = tmp_path / "target.sock"
        symlink = tmp_path / "symlink.sock"
        symlink.symlink_to(target)
        with pytest.raises(RuntimeError):
            await AdminServer(gateway_server.service, symlink).start()

        live_path = tmp_path / "live.sock"
        live = await asyncio.start_unix_server(lambda _r, _w: None, path=str(live_path))
        try:
            with pytest.raises(RuntimeError):
                await AdminServer(gateway_server.service, live_path).start()
        finally:
            live.close()
            await live.wait_closed()

        regular = tmp_path / "regular.sock"
        regular.write_text("not a socket", encoding="utf-8")
        with pytest.raises(RuntimeError):
            await AdminServer(gateway_server.service, regular).start()

    asyncio.run(attempt())


async def test_allowed_host_succeeds_on_mcp(gateway_server: RunningGateway) -> None:
    url = f"{gateway_server.base_url}/mcp"
    async with (
        httpx2.AsyncClient(headers={"Host": "localhost"}) as http_client,
        streamable_http_client(url, http_client=http_client) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
    assert tools.tools, "allowed Host must reach the MCP app"
