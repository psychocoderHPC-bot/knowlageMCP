# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Issued credentials never leak through logs, errors, MCP, or admin JSON."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from conftest import CtlEnv, RunningGateway, auth, record, register

pytestmark = pytest.mark.security


async def test_tokens_never_appear_in_observable_outputs(
    gateway_server: RunningGateway,
    admin: Any,
    ctl_env: CtlEnv,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    with httpx2.Client(base_url=gateway_server.base_url) as client:
        registration = register(client, label="secret-probe")
        token = registration["session_token"]
        token_hash_prefix = hashlib.sha256(token.encode()).hexdigest()[:16]
        headers = auth(token)
        record(client, token, "secret probe claim")
        client.post("/v1/tools/search", headers=headers, json={"query": "secret probe"})
        forbidden = client.post(
            "/v1/tools/search",
            headers=headers,
            json={"query": "x", "filters": {"namespace": "personal"}},
        )
        assert forbidden.status_code == 403
        health = client.get("/v1/health")
        assert health.status_code == 200
        observed = [health.text, forbidden.text]

    url = f"{gateway_server.base_url}/mcp"
    async with (
        httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http_client,
        streamable_http_client(url, http_client=http_client) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        for tool, args in (
            ("search", {"query": "secret probe"}),
            ("get_protocol_info", {}),
            ("register_agent", {"label": "nested"}),
        ):
            observed.append((await session.call_tool(tool, args)).content[0].text)

    admin_json = json.dumps(admin.call("status"), default=str) + json.dumps(admin.call("agents"), default=str)
    ctl_status = ctl_env.run(["status", "--json"])
    assert ctl_status.returncode == 0, ctl_status.stderr

    blob = "\n".join([*observed, admin_json, ctl_status.stdout, ctl_status.stderr, caplog.text])
    assert token not in blob
    assert token_hash_prefix not in blob
    assert "authorization" not in caplog.text.lower()
    assert "bearer " not in caplog.text.lower()
