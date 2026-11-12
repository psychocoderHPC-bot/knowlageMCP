# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Unit tests for the lexical and OpenAI-compatible rerankers."""

from __future__ import annotations

import json
from typing import Any

import httpx2
import pytest

from knowlage_mcp.config import RerankerIntegrationConfig
from knowlage_mcp.integrations.base import RerankCandidate
from knowlage_mcp.integrations.rerank import LexicalReranker, OpenAICompatibleReranker
from knowlage_mcp.models import ErrorCode, ServiceError

_API_KEY_ENV = "KNOWLAGEMCP_TEST_RERANK_KEY"
_BASE_URL = "https://api.example.invalid/v1"


def _config(**overrides: Any) -> RerankerIntegrationConfig:
    values: dict[str, Any] = {
        "provider": "openai",
        "model": "rerank-1",
        "base_url": _BASE_URL,
        "api_key_env": _API_KEY_ENV,
        "top_n": 20,
        "timeout_s": 5.0,
    }
    values.update(overrides)
    return RerankerIntegrationConfig(**values)


def _candidate(
    entry_id: str,
    *,
    title: str = "",
    claim: str = "",
    scope: dict[str, str] | None = None,
    tags: tuple[str, ...] = (),
) -> RerankCandidate:
    return RerankCandidate(
        entry_id=entry_id,
        revision_id=f"rev-{entry_id}",
        title=title,
        claim=claim,
        scope=scope or {},
        evidence_class="unverified",
        tags=tags,
    )


def _openai(handler: Any, **overrides: Any) -> OpenAICompatibleReranker:
    return OpenAICompatibleReranker(_config(**overrides), transport=httpx2.MockTransport(handler))


def test_lexical_orders_by_title_then_claim_and_is_deterministic() -> None:
    reranker = LexicalReranker(_config(provider="lexical"))
    candidates = [
        _candidate("strong", title="gcc compiler optimization"),
        _candidate("claim-only", claim="gcc compiler"),
        _candidate("none", title="unrelated"),
    ]
    expected = ["strong", "claim-only", "none"]
    assert reranker.rerank("gcc compiler optimization", candidates, 3) == expected
    assert reranker.rerank("gcc compiler optimization", candidates, 3) == expected


def test_lexical_ties_keep_original_order() -> None:
    reranker = LexicalReranker(_config(provider="lexical"))
    candidates = [
        _candidate("first", claim="gcc"),
        _candidate("second", claim="gcc"),
    ]
    assert reranker.rerank("gcc", candidates, 2) == ["first", "second"]


def test_lexical_tag_and_scope_weights() -> None:
    reranker = LexicalReranker(_config(provider="lexical"))
    candidates = [
        _candidate("title", title="cuda"),
        _candidate("tag", tags=("cuda",)),
        _candidate("scope", scope={"backend": "cuda"}),
    ]
    assert reranker.rerank("cuda", candidates, 3) == ["title", "tag", "scope"]


def test_lexical_tag_match_changes_order() -> None:
    reranker = LexicalReranker(_config(provider="lexical"))
    candidates = [
        _candidate("plain", claim="compiler"),
        _candidate("tagged", claim="compiler", tags=("gcc",)),
    ]
    assert reranker.rerank("gcc compiler", candidates, 2) == ["tagged", "plain"]


def test_lexical_top_n_and_empty_query() -> None:
    reranker = LexicalReranker(_config(provider="lexical"))
    candidates = [
        _candidate("a", title="alpha"),
        _candidate("b", title="alpha"),
        _candidate("c", title="alpha"),
    ]
    assert reranker.rerank("alpha", candidates, 2) == ["a", "b"]
    assert reranker.rerank("   ", candidates, 3) == ["a", "b", "c"]


def test_openai_reranker_request_shape_and_variant_a() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx2.Response(
            200,
            json={
                "results": [
                    {"index": 2, "relevance_score": 0.1},
                    {"index": 0, "relevance_score": 0.9},
                    {"index": 1, "relevance_score": 0.5},
                ]
            },
        )

    reranker = _openai(handler)
    candidates = [
        _candidate("c1", title="one", claim="first"),
        _candidate("c2", title="two", claim="second"),
        _candidate("c3", title="three", claim="third"),
    ]
    assert reranker.rerank("query", candidates, 2) == ["c1", "c2"]
    assert seen["path"] == "/v1/rerank"
    assert seen["body"] == {
        "model": "rerank-1",
        "query": "query",
        "documents": [
            {"id": "c1", "text": "one\nfirst"},
            {"id": "c2", "text": "two\nsecond"},
            {"id": "c3", "text": "three\nthird"},
        ],
        "top_n": 2,
    }


def test_openai_reranker_accepts_id_and_score_variant() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "results": [
                    {"id": "c1", "score": 0.2},
                    {"id": "c2", "score": 0.7},
                ]
            },
        )

    reranker = _openai(handler)
    candidates = [_candidate("c1"), _candidate("c2")]
    assert reranker.rerank("query", candidates, 2) == ["c2", "c1"]


def test_openai_reranker_drops_unknown_and_duplicate_ids() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "results": [
                    {"index": 99, "relevance_score": 1.0},
                    {"id": "ghost", "score": 0.99},
                    {"id": "c1", "score": 0.2},
                    {"id": "c1", "score": 0.95},
                    {"id": "c2", "score": 0.5},
                ]
            },
        )

    reranker = _openai(handler)
    candidates = [_candidate("c1"), _candidate("c2")]
    assert reranker.rerank("query", candidates, 5) == ["c2", "c1"]


def test_openai_reranker_bounds_documents_and_top_n() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["body"] = json.loads(request.content)
        return httpx2.Response(
            200,
            json={
                "results": [
                    {"index": 0, "relevance_score": 0.9},
                    {"index": 1, "relevance_score": 0.1},
                ]
            },
        )

    reranker = _openai(handler, top_n=2)
    candidates = [_candidate("c1"), _candidate("c2"), _candidate("c3"), _candidate("c4")]
    assert reranker.rerank("query", candidates, 3) == ["c1", "c2"]
    assert len(seen["body"]["documents"]) == 2
    assert seen["body"]["top_n"] == 2


def test_openai_reranker_top_n_never_exceeds_sent_documents() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["body"] = json.loads(request.content)
        return httpx2.Response(
            200,
            json={
                "results": [
                    {"index": 0, "relevance_score": 0.9},
                    {"index": 1, "relevance_score": 0.1},
                ]
            },
        )

    reranker = _openai(handler, top_n=20)
    candidates = [_candidate("c1"), _candidate("c2")]
    assert reranker.rerank("query", candidates, 5) == ["c1", "c2"]
    assert seen["body"]["top_n"] == min(5, len(candidates))


def test_openai_reranker_reports_no_usable_results() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"results": []})

    reranker = _openai(handler)
    with pytest.raises(ServiceError) as excinfo:
        reranker.rerank("query", [_candidate("c1")], 1)
    assert excinfo.value.code is ErrorCode.BACKEND_UNAVAILABLE
    assert excinfo.value.message == "reranker returned no usable results"


def test_openai_reranker_key_header_and_no_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str | None] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers.get("authorization"))
        return httpx2.Response(401, json={})

    monkeypatch.setenv(_API_KEY_ENV, "sk-rerank-secret")
    reranker = _openai(handler)
    with pytest.raises(ServiceError) as excinfo:
        reranker.rerank("query", [_candidate("c1")], 1)
    assert seen == ["Bearer sk-rerank-secret"]
    assert excinfo.value.code is ErrorCode.FORBIDDEN
    assert "sk-rerank-secret" not in str(excinfo.value)


def test_openai_reranker_server_error_maps_to_backend_unavailable() -> None:
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(500, json={})

    reranker = _openai(handler)
    with pytest.raises(ServiceError) as excinfo:
        reranker.rerank("query", [_candidate("c1")], 1)
    assert excinfo.value.code is ErrorCode.BACKEND_UNAVAILABLE
    assert calls == 1


def test_openai_reranker_empty_candidates_make_no_request() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("no request expected")

    reranker = _openai(handler)
    assert reranker.rerank("query", [], 5) == []


def test_reranker_health_is_configuration_only() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("health must not perform a request")

    openai_reranker = _openai(handler)
    assert openai_reranker.health() == {
        "available": True,
        "provider": "openai-compatible-reranker",
        "model": "rerank-1",
    }
    lexical = LexicalReranker(_config(provider="lexical"))
    assert lexical.health() == {"available": True, "provider": "lexical"}


def test_openai_reranker_missing_base_url_is_unavailable() -> None:
    reranker = OpenAICompatibleReranker(
        _config(base_url=""),
        transport=httpx2.MockTransport(lambda request: httpx2.Response(200, json={})),
    )
    assert reranker.available is False
    with pytest.raises(ServiceError) as excinfo:
        reranker.rerank("query", [_candidate("c1")], 1)
    assert excinfo.value.code is ErrorCode.BACKEND_UNAVAILABLE
