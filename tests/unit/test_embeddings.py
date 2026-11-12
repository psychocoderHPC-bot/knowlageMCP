# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Unit tests for the OpenAI-compatible embedding adapter."""

from __future__ import annotations

import json
from typing import Any

import httpx2
import pytest

from knowlage_mcp.config import EmbeddingIntegrationConfig
from knowlage_mcp.integrations.embeddings import OpenAICompatibleEmbeddingProvider
from knowlage_mcp.models import ErrorCode, ServiceError

_API_KEY_ENV = "KNOWLAGEMCP_TEST_EMBED_KEY"


def _config(**overrides: Any) -> EmbeddingIntegrationConfig:
    values: dict[str, Any] = {
        "provider": "openai",
        "model": "text-embedding-3-small",
        "base_url": "https://api.example.invalid/v1",
        "api_key_env": _API_KEY_ENV,
        "dimensions": 3,
        "batch_size": 2,
        "timeout_s": 5.0,
    }
    values.update(overrides)
    return EmbeddingIntegrationConfig(**values)


def _provider(handler: Any, **overrides: Any) -> OpenAICompatibleEmbeddingProvider:
    return OpenAICompatibleEmbeddingProvider(_config(**overrides), transport=httpx2.MockTransport(handler))


def _vectors(count: int) -> list[dict[str, Any]]:
    return [{"index": index, "embedding": [float(index), float(index) + 10.0, 0.5]} for index in range(count)]


def test_embed_requests_one_batch_and_json_shape() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(
            {
                "method": request.method,
                "path": request.url.path,
                "body": json.loads(request.content),
            }
        )
        return httpx2.Response(200, json={"data": _vectors(1)})

    provider = _provider(handler, batch_size=8)
    assert provider.embed(["hello"]) == [[0.0, 10.0, 0.5]]
    assert seen == [
        {
            "method": "POST",
            "path": "/v1/embeddings",
            "body": {"model": "text-embedding-3-small", "input": ["hello"], "dimensions": 3},
        }
    ]


def test_embed_splits_into_batches_and_sorts_by_index() -> None:
    batches: list[list[str]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        batch = json.loads(request.content)["input"]
        batches.append(batch)
        # Return the vectors out of order to prove the adapter sorts by index.
        return httpx2.Response(200, json={"data": list(reversed(_vectors(len(batch))))})

    provider = _provider(handler, batch_size=2)
    result = provider.embed(["a", "b", "c", "d", "e"])
    assert batches == [["a", "b"], ["c", "d"], ["e"]]
    assert result == [
        [0.0, 10.0, 0.5],
        [1.0, 11.0, 0.5],
        [0.0, 10.0, 0.5],
        [1.0, 11.0, 0.5],
        [0.0, 10.0, 0.5],
    ]


def test_dimensions_only_sent_for_text_embedding_3_models() -> None:
    payloads: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        payloads.append(json.loads(request.content))
        return httpx2.Response(200, json={"data": _vectors(1)})

    with_dimensions = _provider(handler, model="text-embedding-3-large")
    assert with_dimensions.embed(["x"]) == [[0.0, 10.0, 0.5]]
    without_dimensions = _provider(handler, model="bge-small-en")
    assert without_dimensions.embed(["x"]) == [[0.0, 10.0, 0.5]]
    assert "dimensions" in payloads[0]
    assert "dimensions" not in payloads[1]


def test_dimension_mismatch_reports_only_lengths() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={"data": [{"index": 0, "embedding": [1.0, 2.0]}]},
        )

    provider = _provider(handler, dimensions=3)
    with pytest.raises(ServiceError) as excinfo:
        provider.embed(["private text"])
    assert excinfo.value.code is ErrorCode.BACKEND_UNAVAILABLE
    assert "3" in excinfo.value.message
    assert "2" in excinfo.value.message
    assert "private text" not in excinfo.value.message


def test_count_mismatch_is_rejected() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"data": _vectors(1)})

    provider = _provider(handler)
    with pytest.raises(ServiceError) as excinfo:
        provider.embed(["a", "b"])
    assert excinfo.value.code is ErrorCode.BACKEND_UNAVAILABLE
    assert "expected 2" in excinfo.value.message
    assert "received 1" in excinfo.value.message


def test_api_key_header_is_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str | None] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers.get("authorization"))
        return httpx2.Response(200, json={"data": _vectors(1)})

    monkeypatch.setenv(_API_KEY_ENV, "sk-embed-secret")
    provider = _provider(handler)
    provider.embed(["x"])
    monkeypatch.delenv(_API_KEY_ENV)
    provider.embed(["x"])
    assert seen == ["Bearer sk-embed-secret", None]


def test_api_key_never_appears_in_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401, json={"error": "denied"})

    monkeypatch.setenv(_API_KEY_ENV, "sk-embed-secret")
    provider = _provider(handler)
    with pytest.raises(ServiceError) as excinfo:
        provider.embed(["x"])
    assert excinfo.value.code is ErrorCode.FORBIDDEN
    assert "sk-embed-secret" not in str(excinfo.value)


def test_unauthorized_maps_to_forbidden() -> None:
    provider = _provider(lambda request: httpx2.Response(401, json={}))
    with pytest.raises(ServiceError) as excinfo:
        provider.embed(["x"])
    assert excinfo.value.code is ErrorCode.FORBIDDEN


def test_server_error_maps_to_backend_unavailable_without_retry() -> None:
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(500, json={})

    provider = _provider(handler)
    with pytest.raises(ServiceError) as excinfo:
        provider.embed(["x"])
    assert excinfo.value.code is ErrorCode.BACKEND_UNAVAILABLE
    assert calls == 1


def test_empty_input_makes_no_request() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("no request expected")

    provider = _provider(handler)
    assert provider.embed([]) == []


def test_empty_strings_are_replaced_but_count_is_kept() -> None:
    seen: list[list[str]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        batch = json.loads(request.content)["input"]
        seen.append(batch)
        return httpx2.Response(200, json={"data": _vectors(len(batch))})

    provider = _provider(handler, batch_size=8)
    assert len(provider.embed(["", "   ", "text"])) == 3
    assert seen == [[" ", " ", "text"]]


def test_health_is_configuration_only() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("health must not perform a request")

    provider = _provider(handler)
    assert provider.health() == {
        "available": True,
        "provider": "openai-compatible",
        "model": "text-embedding-3-small",
        "dimensions": 3,
    }


def test_missing_base_url_is_unavailable() -> None:
    provider = OpenAICompatibleEmbeddingProvider(
        _config(base_url=""), transport=httpx2.MockTransport(lambda r: None)
    )
    assert provider.available is False
    assert provider.health()["available"] is False
    with pytest.raises(ServiceError) as excinfo:
        provider.embed(["x"])
    assert excinfo.value.code is ErrorCode.BACKEND_UNAVAILABLE


def test_non_sequence_input_is_rejected() -> None:
    provider = _provider(lambda request: httpx2.Response(200, json={"data": []}))
    with pytest.raises(ServiceError) as excinfo:
        provider.embed("not-a-sequence-of-texts")  # type: ignore[arg-type]
    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENT
