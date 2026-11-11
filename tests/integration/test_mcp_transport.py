# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Real streamable-HTTP MCP client tests against the mounted gateway app."""

from __future__ import annotations

import json
from typing import Any

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from conftest import RunningGateway, free_port
from knowlage_mcp import TOOL_NAMES
from knowlage_mcp.config import Config, ServerConfig, StorageConfig, resolve_paths

pytestmark = pytest.mark.integration


def _envelope(result: Any) -> dict[str, Any]:
    return json.loads(result.content[0].text)


async def _http_register(base_url: str) -> dict[str, Any]:
    async with httpx2.AsyncClient(base_url=base_url) as client:
        response = await client.post(
            "/v1/bridge/register",
            json={"mode": "root", "label": "mcp-http", "model": "pytest", "provider": "local"},
        )
    assert response.status_code == 200, response.text
    return response.json()


async def test_mcp_lists_tools_and_unauth_protocol(gateway_server: RunningGateway) -> None:
    url = f"{gateway_server.base_url}/mcp"
    async with (
        httpx2.AsyncClient() as http_client,
        streamable_http_client(url, http_client=http_client) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        names = {tool.name for tool in tools.tools}
        assert set(TOOL_NAMES).issubset(names)
        assert "upload_evidence" in names
        protocol = _envelope(await session.call_tool("get_protocol_info", {}))
        assert protocol["ok"] is True
        assert protocol["data"]["protocol_version"]
        registration = _envelope(await session.call_tool("register_agent", {"label": "direct"}))
        assert registration["ok"] is True
        data = registration["data"]
        assert "session_token" not in data
        assert data["credential"]["mode"] == "bridge_required"
        assert data["credential"]["handshake_url"].endswith("/v1/bridge/register")
        assert data["credential"]["note"]["error"]["code"] == "AUTH_REQUIRED"


async def test_mcp_authenticated_calls_respect_actor_handle(
    gateway_server: RunningGateway,
) -> None:
    registration = await _http_register(gateway_server.base_url)
    token = registration["session_token"]
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{gateway_server.base_url}/mcp"
    claim = "mcp transport probe 52ab"

    async with (
        httpx2.AsyncClient(headers=headers) as http_client,
        streamable_http_client(url, http_client=http_client) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        recorded = _envelope(
            await session.call_tool(
                "record",
                {
                    "namespace": "shared",
                    "entry": {
                        "kind": "observation",
                        "title": "mcp transport",
                        "claim": claim,
                    },
                },
            )
        )
        assert recorded["ok"] is True, recorded
        entry_id = recorded["data"]["entry"]["entry_id"]

        found = _envelope(
            await session.call_tool(
                "search",
                {"query": "mcp transport probe", "filters": {"namespace": "shared"}},
            )
        )
        assert found["ok"] is True, found
        assert entry_id in [item["entry_id"] for item in found["data"]["results"]]

        context = _envelope(
            await session.call_tool(
                "get_context",
                {"task": "integration task", "scope": {"project": "integration"}},
            )
        )
        assert context["ok"] is True, context

        foreign = _envelope(await session.call_tool("search", {"query": "x", "actor_handle": "act_not_mine"}))
        assert foreign["ok"] is False
        assert foreign["error"]["code"] == "AUTH_REQUIRED"

        detail = _envelope(await session.call_tool("get_entry", {"entry_id": entry_id}))
        assert detail["ok"] is True
        assert detail["data"]["entry_id"] == entry_id


async def test_direct_mcp_upload_evidence_uses_object_cap(
    gateway_factory: Any, tmp_path: Any, short_runtime_dir: Any
) -> None:
    data = tmp_path / "mcp-evidence-data"
    config = resolve_paths(
        Config(
            server=ServerConfig(
                host="127.0.0.1",
                port=free_port(),
                allowed_hosts=["127.0.0.1", "localhost"],
                max_request_bytes=131072,
            ),
            storage=StorageConfig(
                database=data / "knowledge.sqlite3",
                evidence_dir=data / "evidence",
                backup_dir=data / "backups",
                runtime_socket=short_runtime_dir / "admin.sock",
            ),
        )
    )
    running = gateway_factory(config)
    registration = await _http_register(running.base_url)
    headers = {"Authorization": f"Bearer {registration['session_token']}"}
    text = ("evidence payload line alpha beta\n" * 7000)[: 200 * 1024]
    assert len(text.encode()) > running.config.server.max_request_bytes
    async with (
        httpx2.AsyncClient(headers=headers) as http_client,
        streamable_http_client(f"{running.base_url}/mcp", http_client=http_client) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        uploaded = _envelope(await session.call_tool("upload_evidence", {"text": text}))
        cap = running.config.limits.evidence_object_bytes
        over = _envelope(await session.call_tool("upload_evidence", {"text": "z" * (cap + 1)}))
    assert uploaded["ok"] is True, uploaded
    assert uploaded["data"]["size_bytes"] == len(text.encode())
    assert over["ok"] is False, over
    assert over["error"]["code"] == "BUDGET_EXCEEDED"
