# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Unit tests for the Graphiti proposal-only extractor."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx2
import pytest

from knowlage_mcp.config import GraphitiIntegrationConfig
from knowlage_mcp.integrations.graphiti import GraphitiExtractor
from knowlage_mcp.models import ErrorCode, ServiceError


def _extractor(handler: Any, **overrides: Any) -> GraphitiExtractor:
    config = GraphitiIntegrationConfig(base_url="https://graphiti.example.invalid", **overrides)
    return GraphitiExtractor(config, transport=httpx2.MockTransport(handler))


class _Source:
    def __init__(self, role: str) -> None:
        self.role = role


def test_extract_request_shape_includes_normalized_role_labels() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, json={"edges": []})

    extractor = _extractor(handler)
    result = extractor.extract(
        "build failed",
        task="fix build",
        sources=[
            {"role": "user_message"},
            _Source("assistant"),
            {"kind": "conversation"},
            "not-a-source",
        ],
    )
    assert result == []
    assert seen == {
        "method": "POST",
        "path": "/extract",
        "body": {
            "text": "build failed",
            "task": "fix build",
            "source_roles": ["user_message", "agent_observation", "unknown", "unknown"],
        },
    }
    extractor.close()


def test_extract_maps_edges_to_observations() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(
            200,
            json={
                "edges": [
                    {
                        "subject": "gcc-13",
                        "relation": "fails_on",
                        "object": "missing.h",
                        "detail": "error: missing.h not found",
                    },
                    {"subject": "A", "relation": "relates_to", "object": "B"},
                ]
            },
        )

    extractor = _extractor(handler)
    proposals = extractor.extract("text")
    extractor.close()
    assert len(proposals) == 2

    first = proposals[0]
    assert first.kind == "observation"
    assert first.title == "gcc-13 fails_on missing.h"
    assert first.claim == "error: missing.h not found"
    assert first.relation == "extracted"
    assert first.subject == "gcc-13"
    assert first.object == "missing.h"
    assert first.detail == "error: missing.h not found"
    assert first.tags == ("graphiti",)
    assert first.source_role == "agent_observation"
    assert not hasattr(first, "confidence")

    second = proposals[1]
    assert second.claim == "A relates_to B"
    assert second.detail is None


def test_extract_accepts_a_top_level_list() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(200, json=[{"subject": "s", "relation": "r", "object": "o"}])

    extractor = _extractor(handler)
    proposals = extractor.extract("text")
    extractor.close()
    assert [proposal.title for proposal in proposals] == ["s r o"]


@pytest.mark.parametrize(
    "body",
    [
        None,
        "not-json-object",
        42,
        {"edges": "not-a-list"},
        {"other": []},
    ],
)
def test_malformed_response_shapes_yield_no_proposals(body: Any) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(200, json=body)

    extractor = _extractor(handler)
    assert extractor.extract("text") == []
    extractor.close()


def test_edges_missing_required_fields_are_dropped() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(
            200,
            json={
                "edges": [
                    "not-an-edge",
                    {"subject": "s", "relation": "r"},
                    {"subject": "", "relation": "r", "object": "o"},
                    {"subject": "s", "relation": 7, "object": "o"},
                    {"subject": 1, "relation": "r", "object": "o"},
                    {"subject": "keep", "relation": "works", "object": "here"},
                ]
            },
        )

    extractor = _extractor(handler)
    proposals = extractor.extract("text")
    extractor.close()
    assert [proposal.title for proposal in proposals] == ["keep works here"]


def test_edges_are_capped_at_32() -> None:
    edges = [{"subject": f"s{index}", "relation": "r", "object": "o"} for index in range(50)]

    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(200, json={"edges": edges})

    extractor = _extractor(handler)
    proposals = extractor.extract("text")
    extractor.close()
    assert len(proposals) == 32
    assert proposals[-1].subject == "s31"


def test_request_and_response_strings_are_bounded() -> None:
    long = "x" * 9000
    captured: dict[str, Any] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured.update(json.loads(request.content))
        return httpx2.Response(
            200,
            json={
                "edges": [
                    {"subject": long, "relation": "r", "object": "o", "detail": long},
                ]
            },
        )

    extractor = _extractor(handler)
    proposals = extractor.extract(long)
    extractor.close()

    assert len(captured["text"].encode("utf-8")) <= 4096
    proposal = proposals[0]
    assert len(proposal.subject.encode("utf-8")) <= 4096  # type: ignore[union-attr]
    assert len(proposal.claim.encode("utf-8")) <= 4096
    assert len(proposal.detail.encode("utf-8")) <= 4096  # type: ignore[union-attr]
    assert len(proposal.title) <= 200


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, ErrorCode.INVALID_ARGUMENT),
        (401, ErrorCode.FORBIDDEN),
        (403, ErrorCode.FORBIDDEN),
        (500, ErrorCode.BACKEND_UNAVAILABLE),
        (503, ErrorCode.BACKEND_UNAVAILABLE),
    ],
)
def test_http_status_mapping(status: int, code: ErrorCode) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(status, content=b"secret-response-body")

    extractor = _extractor(handler)
    with pytest.raises(ServiceError) as failure:
        extractor.extract("text")
    extractor.close()
    assert failure.value.code is code
    assert "secret-response-body" not in str(failure.value)


def test_transport_failure_maps_to_backend_unavailable() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("unreachable", request=request)

    extractor = _extractor(handler)
    with pytest.raises(ServiceError) as failure:
        extractor.extract("text")
    extractor.close()
    assert failure.value.code is ErrorCode.BACKEND_UNAVAILABLE


def test_api_key_is_read_from_environment_and_never_leaked(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str | None] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers.get("authorization"))
        return httpx2.Response(401, content=b"secret-response-body")

    monkeypatch.setenv("KNOWLAGEMCP_TEST_GRAPHITI_KEY", "top-secret-value")
    extractor = _extractor(handler, api_key_env="KNOWLAGEMCP_TEST_GRAPHITI_KEY")
    with pytest.raises(ServiceError) as failure:
        extractor.extract("text")
    extractor.close()
    assert seen == ["Bearer top-secret-value"]
    assert "top-secret-value" not in str(failure.value)
    assert "secret-response-body" not in str(failure.value)


def test_health_is_configuration_only_and_performs_no_network_call() -> None:
    calls: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(200, json={"edges": []})

    extractor = _extractor(handler)
    assert extractor.health() == {"name": "graphiti", "available": True}
    assert calls == []
    extractor.close()

    unconfigured = GraphitiExtractor(GraphitiIntegrationConfig(base_url=""))
    assert unconfigured.health() == {"name": "graphiti", "available": False}
    unconfigured.close()


def test_empty_input_returns_no_proposals_without_a_call() -> None:
    calls: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(200, json={"edges": [{"subject": "s", "relation": "r", "object": "o"}]})

    extractor = _extractor(handler)
    assert extractor.extract("   \n\t ") == []
    assert calls == []
    extractor.close()


def test_unconfigured_extractor_fails_closed_without_network() -> None:
    extractor = GraphitiExtractor(GraphitiIntegrationConfig(base_url=""))
    with pytest.raises(ServiceError) as failure:
        extractor.extract("text")
    extractor.close()
    assert failure.value.code is ErrorCode.BACKEND_UNAVAILABLE


def test_module_does_not_import_sqlite3() -> None:
    import knowlage_mcp.integrations.graphiti as module

    assert module.__file__ is not None
    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "sqlite3" not in source
