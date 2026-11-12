# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Response-size bound for the shared JSON-over-HTTP helper."""

from __future__ import annotations

from collections.abc import Callable

import httpx2
import pytest

from knowlage_mcp.integrations.http import MAX_RESPONSE_BYTES, JsonHttpClient
from knowlage_mcp.models import ErrorCode, ServiceError


def _client(handler: Callable[[httpx2.Request], httpx2.Response]) -> JsonHttpClient:
    return JsonHttpClient(
        "https://backend.example.invalid",
        timeout_s=1.0,
        transport=httpx2.MockTransport(handler),
    )


def test_get_rejects_response_above_the_size_bound() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))

    client = _client(handler)
    try:
        with pytest.raises(ServiceError) as excinfo:
            client.request("GET", "/v1/items")
    finally:
        client.close()

    assert excinfo.value.code is ErrorCode.BACKEND_UNAVAILABLE
    assert "4 MiB" in excinfo.value.message
    assert "x" * 64 not in excinfo.value.message


def test_post_rejects_response_above_the_size_bound_without_retry() -> None:
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        del request
        calls += 1
        return httpx2.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))

    client = _client(handler)
    try:
        with pytest.raises(ServiceError) as excinfo:
            client.request("POST", "/v1/items", json={"claim": "bounded"})
    finally:
        client.close()

    assert excinfo.value.code is ErrorCode.BACKEND_UNAVAILABLE
    assert calls == 1
