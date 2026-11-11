# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Spawn the real stdio bridge and drive it with the MCP stdio client."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from conftest import RunningGateway

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]


def _envelope(result: Any) -> dict[str, Any]:
    return json.loads(result.content[0].text)


def _params(base_url: str) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "knowlage_mcp.bridge", "--url", base_url],
        env={**os.environ},
        cwd=str(REPO_ROOT),
    )


async def test_bridge_round_trip_and_actor_rule(gateway_server: RunningGateway) -> None:
    claim = "bridge stdio probe 7c1e"
    seen_text: list[str] = []
    async with (
        stdio_client(_params(gateway_server.base_url)) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()

        first = _envelope(await session.call_tool("register_agent", {"label": "bridge-one"}))
        seen_text.append(json.dumps(first))
        assert first["ok"] is True, first
        handle_one = first["data"]["actor_handle"]
        assert "session_token" not in first["data"]

        second = _envelope(await session.call_tool("register_agent", {"label": "bridge-two"}))
        seen_text.append(json.dumps(second))
        assert second["ok"] is True, second
        handle_two = second["data"]["actor_handle"]
        assert handle_two != handle_one

        missing = _envelope(await session.call_tool("search", {"query": "probe"}))
        seen_text.append(json.dumps(missing))
        assert missing["ok"] is False
        assert missing["error"]["code"] == "AUTH_REQUIRED"

        recorded = _envelope(
            await session.call_tool(
                "record",
                {
                    "namespace": "shared",
                    "actor_handle": handle_one,
                    "entry": {
                        "kind": "observation",
                        "title": "bridge round trip",
                        "claim": claim,
                    },
                },
            )
        )
        seen_text.append(json.dumps(recorded))
        assert recorded["ok"] is True, recorded

        found = _envelope(
            await session.call_tool(
                "search",
                {"query": "bridge stdio probe", "actor_handle": handle_one},
            )
        )
        seen_text.append(json.dumps(found))
        assert found["ok"] is True, found
        assert recorded["data"]["entry"]["entry_id"] in [
            item["entry_id"] for item in found["data"]["results"]
        ]

        foreign = _envelope(
            await session.call_tool("search", {"query": "probe", "actor_handle": "act_foreign"})
        )
        seen_text.append(json.dumps(foreign))
        assert foreign["ok"] is False
        assert foreign["error"]["code"] == "AUTH_REQUIRED"

        closed = _envelope(await session.call_tool("session_close", {"actor_handle": handle_two}))
        seen_text.append(json.dumps(closed))
        assert closed["ok"] is True

    collected = "\n".join(seen_text).lower()
    assert "session_token" not in collected
    assert "authorization" not in collected
    assert "bearer " not in collected


def test_url_precedence_and_normalization(monkeypatch: pytest.MonkeyPatch) -> None:
    from knowlage_mcp.bridge import normalize_url, resolve_base_url

    monkeypatch.setenv("KNOWLAGEMCP_URL", "http://env-url:9999")
    monkeypatch.setenv("KNOWLAGEMCP_HOST", "env-host")
    monkeypatch.setenv("KNOWLAGEMCP_PORT", "9998")

    assert resolve_base_url(url="http://cli-url:1234/") == "http://cli-url:1234"
    assert resolve_base_url(host="cli-host") == "http://cli-host:9998"
    assert resolve_base_url(port=4321) == "http://env-host:4321"
    monkeypatch.delenv("KNOWLAGEMCP_URL")
    assert resolve_base_url() == "http://env-host:9998"
    assert normalize_url("host:1/") == "http://host:1"
