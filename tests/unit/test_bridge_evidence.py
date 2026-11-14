# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Bounded evidence previews exposed by the stdio bridge."""

from __future__ import annotations

import httpx2
import pytest

from knowlage_mcp.bridge import Actor, BridgeBackend


@pytest.mark.asyncio
async def test_evidence_preview_is_bounded_at_a_utf8_boundary() -> None:
    payload = ("a" * 11_999 + "é" * 3).encode()

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.headers["Authorization"] == "Bearer secret"
        assert request.url.path == "/v1/bridge/evidence/ev_test"
        return httpx2.Response(200, content=payload, headers={"Content-Type": "text/plain"})

    bridge = BridgeBackend("http://gateway.invalid")
    await bridge.aclose()
    bridge._client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    bridge.actors.append(Actor("secret", "act_test", "agent", "root", "later"))
    try:
        result = await bridge.tool("get_evidence_text", {"evidence_id": "ev_test"})
    finally:
        await bridge.aclose()
    assert result["ok"] is True
    assert result["data"]["text"] == "a" * 11_999
    assert result["data"]["preview_bytes"] == 11_999
    assert result["data"]["truncated"] is True


@pytest.mark.asyncio
async def test_evidence_preview_rejects_binary_content() -> None:
    bridge = BridgeBackend("http://gateway.invalid")
    await bridge.aclose()
    bridge._client = httpx2.AsyncClient(
        transport=httpx2.MockTransport(
            lambda request: httpx2.Response(
                200, content=b"\x00\xff", headers={"Content-Type": "application/octet-stream"}
            )
        )
    )
    bridge.actors.append(Actor("secret", "act_test", "agent", "root", "later"))
    try:
        result = await bridge.tool("get_evidence_text", {"evidence_id": "ev_test"})
    finally:
        await bridge.aclose()
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_ARGUMENT"


@pytest.mark.asyncio
async def test_evidence_preview_rejects_binary_media_type_even_with_ascii_bytes() -> None:
    bridge = BridgeBackend("http://gateway.invalid")
    await bridge.aclose()
    bridge._client = httpx2.AsyncClient(
        transport=httpx2.MockTransport(
            lambda request: httpx2.Response(
                200, content=b"binary-looking-ascii", headers={"Content-Type": "application/pdf"}
            )
        )
    )
    bridge.actors.append(Actor("secret", "act_test", "agent", "root", "later"))
    try:
        result = await bridge.tool("get_evidence_text", {"evidence_id": "ev_test"})
    finally:
        await bridge.aclose()
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_ARGUMENT"


@pytest.mark.asyncio
async def test_evidence_preview_allows_legacy_octet_stream_text() -> None:
    bridge = BridgeBackend("http://gateway.invalid")
    await bridge.aclose()
    bridge._client = httpx2.AsyncClient(
        transport=httpx2.MockTransport(
            lambda request: httpx2.Response(
                200, content=b"old build log\n", headers={"Content-Type": "application/octet-stream"}
            )
        )
    )
    bridge.actors.append(Actor("secret", "act_test", "agent", "root", "later"))
    try:
        result = await bridge.tool("get_evidence_text", {"evidence_id": "ev_test"})
    finally:
        await bridge.aclose()
    assert result["ok"] is True
    assert result["data"]["text"] == "old build log\n"
