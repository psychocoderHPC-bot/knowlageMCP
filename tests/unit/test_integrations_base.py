# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Unit tests for optional protocol boundaries, clients, hash embeddings, and registry."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError
from typing import Any

import httpx2
import pytest

from knowlage_mcp.config import Config, IntegrationsConfig, validate_config
from knowlage_mcp.integrations import (
    EmbeddingProvider,
    HashEmbeddingProvider,
    IntegrationRegistry,
    JsonHttpClient,
    MemoryExtractor,
    MemoryProposal,
    NullEmbeddingProvider,
    NullMemoryExtractor,
    NullReranker,
    NullVectorIndex,
    RerankCandidate,
    Reranker,
    VectorHit,
    VectorIndex,
    VectorItem,
)
from knowlage_mcp.models import ErrorCode, ServiceError


def _client(
    handler: Any,
    *,
    api_key_env: str | None = None,
    attempts: int = 3,
) -> JsonHttpClient:
    transport = httpx2.MockTransport(handler)
    return JsonHttpClient(
        "https://api.example.invalid",
        1.0,
        api_key_env=api_key_env,
        transport=transport,
        attempts=attempts,
        sleep=lambda _seconds: None,
    )


def test_http_success_uses_headers_without_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["authorization"] = request.headers.get("authorization")
        seen["body"] = request.content
        return httpx2.Response(200, json={"ok": True})

    monkeypatch.setenv("KNOWLAGEMCP_TEST_KEY", "top-secret")
    client = _client(handler, api_key_env="KNOWLAGEMCP_TEST_KEY")
    assert client.request("POST", "/v1/embed", json={"input": ["x"]}) == {"ok": True}
    assert seen["method"] == "POST"
    assert seen["path"] == "/v1/embed"
    assert seen["authorization"] == "Bearer top-secret"
    assert json.loads(seen["body"]) == {"input": ["x"]}
    client.close()


def test_http_api_key_is_read_at_request_time(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str | None] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers.get("authorization"))
        return httpx2.Response(200, json={})

    monkeypatch.delenv("KNOWLAGEMCP_TEST_KEY", raising=False)
    client = _client(handler, api_key_env="KNOWLAGEMCP_TEST_KEY")
    client.request("GET", "/health")
    monkeypatch.setenv("KNOWLAGEMCP_TEST_KEY", "late-secret")
    client.request("GET", "/health")
    assert seen == [None, "Bearer late-secret"]


def test_http_get_retries_with_capped_backoff() -> None:
    calls: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request.method)
        if len(calls) < 3:
            return httpx2.Response(503)
        return httpx2.Response(200, json={"ok": 1})

    client = _client(handler)
    assert client.request("GET", "/ready") == {"ok": 1}
    assert calls == ["GET", "GET", "GET"]


def test_http_post_never_retries() -> None:
    calls: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request.method)
        return httpx2.Response(500, content=b"do-not-leak-this")

    client = _client(handler)
    with pytest.raises(ServiceError) as failure:
        client.request("POST", "/v1/write", json={"x": 1})
    assert failure.value.code is ErrorCode.BACKEND_UNAVAILABLE
    assert calls == ["POST"]
    assert "do-not-leak-this" not in str(failure.value)


def test_http_transport_errors_map_to_backend_unavailable() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("timed out", request=request)

    client = _client(handler)
    with pytest.raises(ServiceError) as failure:
        client.request("GET", "/slow")
    assert failure.value.code is ErrorCode.BACKEND_UNAVAILABLE
    assert "unreachable" in failure.value.message


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, ErrorCode.INVALID_ARGUMENT),
        (401, ErrorCode.FORBIDDEN),
        (403, ErrorCode.FORBIDDEN),
        (404, ErrorCode.INVALID_ARGUMENT),
        (429, ErrorCode.BACKEND_UNAVAILABLE),
        (500, ErrorCode.BACKEND_UNAVAILABLE),
    ],
)
def test_http_status_mapping(status: int, code: ErrorCode) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, content=b"secret-body")

    client = _client(handler)
    with pytest.raises(ServiceError) as failure:
        client.request("POST", "/v1/x")
    assert failure.value.code is code
    assert "secret-body" not in str(failure.value)


def test_http_invalid_json_is_backend_failure() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=b"not json")

    client = _client(handler)
    with pytest.raises(ServiceError) as failure:
        client.request("GET", "/v1/x")
    assert failure.value.code is ErrorCode.BACKEND_UNAVAILABLE
    assert "invalid JSON" in failure.value.message


def test_hash_embedding_dimension_and_normalization() -> None:
    provider = HashEmbeddingProvider(dimensions=64)
    vector = provider.embed(["compiler missing include stdio.h"])[0]
    assert len(vector) == 64
    assert math.isclose(sum(value * value for value in vector), 1.0, rel_tol=1e-9)
    assert provider.health()["dimensions"] == 64


def test_hash_embedding_empty_text_is_zero_vector() -> None:
    provider = HashEmbeddingProvider(dimensions=32)
    assert provider.embed([""]) == [[0.0] * 32]
    assert provider.embed(["   \n\t "]) == [[0.0] * 32]


def test_hash_embedding_similarity_sanity() -> None:
    provider = HashEmbeddingProvider(dimensions=256)
    left, right, unrelated = provider.embed(
        [
            "gcc 13 cannot find include stdio.h",
            "gcc 13 cannot find include stdlib.h",
            "quantum chromodynamics lattice gauge theory",
        ]
    )

    def cosine(a: list[float], b: list[float]) -> float:
        return sum(x * y for x, y in zip(a, b, strict=True))

    assert cosine(left, right) > cosine(left, unrelated)


def test_hash_embedding_is_deterministic_across_processes() -> None:
    program = (
        "import json;"
        "from knowlage_mcp.integrations.hash_embed import HashEmbeddingProvider;"
        "print(json.dumps(HashEmbeddingProvider(64).embed(['Compiler missing include stdio.h'])[0]))"
    )
    outputs: list[str] = []
    for seed in ("1", "2"):
        environment = dict(os.environ)
        environment["PYTHONHASHSEED"] = seed
        completed = subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
            env=environment,
        )
        outputs.append(completed.stdout.strip())
    assert outputs[0] == outputs[1]


def test_null_implementations_satisfy_new_protocols() -> None:
    embedding = NullEmbeddingProvider()
    vector = NullVectorIndex()
    reranker = NullReranker()
    extractor = NullMemoryExtractor()
    assert isinstance(embedding, EmbeddingProvider)
    assert isinstance(vector, VectorIndex)
    assert isinstance(reranker, Reranker)
    assert isinstance(extractor, MemoryExtractor)
    assert embedding.available is False
    assert vector.search([0.0], limit=5, namespaces=["shared"]) == []
    assert vector.ensure_generation("m", 8) == "null"
    assert vector.adopt_generation("gen_external") is None
    assert vector.available is False
    assert vector.generation_id == "null"
    assert extractor.extract("text", "task", ()) == []
    candidates = [
        RerankCandidate(
            entry_id="e1",
            revision_id="r1",
            title="t",
            claim="c",
            scope={"compiler": "gcc"},
            evidence_class="unverified",
            tags=("gcc",),
        ),
        RerankCandidate(
            entry_id="e2",
            revision_id="r2",
            title="t",
            claim="c",
            scope={},
            evidence_class="verified",
        ),
    ]
    assert candidates[0].tags == ("gcc",)
    assert candidates[1].tags == ()
    assert reranker.rerank("q", candidates, 1) == ["e1"]
    assert embedding.health()["available"] is False


def test_dataclasses_are_frozen_and_carry_expected_fields() -> None:
    item = VectorItem(
        entry_id="e",
        revision_id="r",
        namespace="shared",
        vector=(0.5, 0.5),
        payload={"scope": {}},
    )
    hit = VectorHit(entry_id="e", revision_id="r", score=0.9)
    proposal = MemoryProposal(
        kind="failure",
        title="title",
        claim="claim",
        conditions="cond",
        expected_result="result",
        limitations="limits",
        tags=("gcc",),
        scope={"compiler": "gcc"},
        relation="causes",
        subject="a",
        object="b",
        detail="detail",
        source_role="agent_reported",
    )
    assert hit.score == 0.9
    assert proposal.tags == ("gcc",)
    with pytest.raises(FrozenInstanceError):
        item.entry_id = "other"  # type: ignore[misc]


def test_disabled_registry_builds_no_network_adapter() -> None:
    calls: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(200, json={})

    registry = IntegrationRegistry(Config(), http_transport=httpx2.MockTransport(handler))
    status = registry.status()
    assert registry.enabled is False
    assert status.enabled is False
    assert status.embedding == "null"
    assert status.vector == "null"
    assert status.reranker == "null"
    assert status.extractor == "null"
    assert status.embedding_available is False
    assert status.generation is None
    assert registry.probe() == {}
    registry.close()
    assert calls == []


def test_enabled_registry_reports_hash_status() -> None:
    config = Config.from_toml_dict({"integrations": {"enabled": True, "embedding": {"provider": "hash"}}})
    registry = IntegrationRegistry(config)
    status = registry.status()
    assert registry.enabled is True
    assert status.embedding == "hash"
    assert status.embedding_available is True
    assert status.vector == "null"
    assert status.vector_available is False
    assert status.generation is None
    probe = registry.probe()
    assert probe["embedding"]["available"] is True
    assert probe["vector"]["available"] is False
    registry.close()


@pytest.mark.parametrize(
    "payload",
    [
        {"embedding": {"provider": "openai", "base_url": "https://example.invalid/v1"}},
        {"vector": {"backend": "qdrant"}},
        {"reranker": {"provider": "lexical"}},
        {"reranker": {"provider": "openai", "base_url": "https://example.invalid/v1"}},
        {"graphiti": {"enabled": True}},
        {"mem0": {"enabled": True}},
    ],
)
def test_missing_adapter_module_is_backend_unavailable(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]
) -> None:
    import knowlage_mcp.integrations.registry as registry_module

    def missing(name: str, package: str | None = None) -> Any:
        raise ModuleNotFoundError(f"No module named {name!r}", name=name)

    monkeypatch.setattr(registry_module.importlib, "import_module", missing)
    config = Config.from_toml_dict({"integrations": {"enabled": True, **payload}})
    with pytest.raises(ServiceError) as failure:
        IntegrationRegistry(config)
    assert failure.value.code is ErrorCode.BACKEND_UNAVAILABLE
    assert "unavailable in this build" in failure.value.message


def test_unknown_provider_is_rejected_before_registry() -> None:
    with pytest.raises(ValueError):
        IntegrationsConfig(embedding={"provider": "bogus"})  # type: ignore[arg-type]
    bad = Config().model_copy(deep=True)
    bad.integrations = IntegrationsConfig.model_construct(
        enabled=True,
        inference=False,
        telemetry=False,
        embedding=IntegrationsConfig().embedding.model_copy(update={"provider": "bogus"}),
        vector=IntegrationsConfig().vector,
        reranker=IntegrationsConfig().reranker,
        graphiti=IntegrationsConfig().graphiti,
        mem0=IntegrationsConfig().mem0,
    )
    assert any("provider" in problem for problem in validate_config(bad))


def test_registry_probe_reports_service_errors() -> None:
    config = Config.from_toml_dict({"integrations": {"enabled": True, "embedding": {"provider": "hash"}}})
    registry = IntegrationRegistry(config)

    class BrokenEmbedding:
        name = "broken"
        model = "none"
        dimensions = 8
        available = True

        def embed(self, texts: Any) -> list[list[float]]:
            del texts
            return []

        def health(self) -> dict[str, Any]:
            raise ServiceError(ErrorCode.BACKEND_UNAVAILABLE, "down")

    registry.embedding = BrokenEmbedding()  # type: ignore[assignment]
    probe = registry.probe()
    assert probe["embedding"] == {"available": False, "error": ErrorCode.BACKEND_UNAVAILABLE.value}
    registry.close()


@pytest.mark.parametrize(
    "base_url",
    ["", "not-a-url", "ftp://example.invalid", "https://exa mple.invalid", "https:///no-host"],
)
def test_http_client_rejects_unusable_base_url(base_url: str) -> None:
    with pytest.raises(ServiceError) as failure:
        JsonHttpClient(
            base_url,
            1.0,
            transport=httpx2.MockTransport(lambda request: httpx2.Response(200, json={})),
        )
    assert failure.value.code is ErrorCode.INVALID_ARGUMENT


def test_http_client_rejects_userinfo_without_echoing_it() -> None:
    with pytest.raises(ServiceError) as failure:
        JsonHttpClient(
            "https://user:super-secret@example.invalid/v1",
            1.0,
            transport=httpx2.MockTransport(lambda request: httpx2.Response(200, json={})),
        )
    assert failure.value.code is ErrorCode.INVALID_ARGUMENT
    assert "super-secret" not in str(failure.value)
    assert "example.invalid" not in str(failure.value)
