# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Unit tests for the Mem0 proposal-only extractor."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx2
import pytest

from knowlage_mcp.config import Mem0IntegrationConfig
from knowlage_mcp.integrations.mem0 import Mem0Extractor
from knowlage_mcp.models import ErrorCode, Kind, ServiceError


def _extractor(handler: Any, **overrides: Any) -> Mem0Extractor:
    config = Mem0IntegrationConfig(base_url="https://mem0.example.invalid", **overrides)
    return Mem0Extractor(config, transport=httpx2.MockTransport(handler))


class _Source:
    def __init__(self, role: str) -> None:
        self.role = role


def test_extract_request_shape_uses_source_role() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, json={"memories": []})

    extractor = _extractor(handler, max_items=7)
    assert extractor.extract("conversation", task="remember this", sources=[_Source("assistant")]) == []
    extractor.close()
    assert seen == {
        "method": "POST",
        "path": "/memories/extract",
        "body": {
            "messages": [{"role": "assistant", "content": "conversation"}],
            "task": "remember this",
            "max_items": 7,
        },
    }


def test_extract_request_without_sources_uses_unknown_role() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.update(json.loads(request.content))
        return httpx2.Response(200, json={"memories": []})

    extractor = _extractor(handler)
    extractor.extract("conversation")
    extractor.close()
    assert seen["messages"] == [{"role": "unknown", "content": "conversation"}]
    assert seen["task"] is None
    assert seen["max_items"] == 20


def test_extract_maps_memories_and_preserves_source_role() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(
            200,
            json={
                "memories": [
                    {
                        "kind": "preference",
                        "title": "Short answers",
                        "claim": "The user prefers short answers",
                        "conditions": "chat",
                        "expected_result": "brief replies",
                        "limitations": "code still needs detail",
                        "tags": ["style", "style", ""],
                        "scope": {"domain": "writing"},
                        "source_role": "user_message",
                    }
                ]
            },
        )

    extractor = _extractor(handler)
    proposals = extractor.extract("text")
    extractor.close()
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.kind == "preference"
    assert proposal.title == "Short answers"
    assert proposal.claim == "The user prefers short answers"
    assert proposal.conditions == "chat"
    assert proposal.expected_result == "brief replies"
    assert proposal.limitations == "code still needs detail"
    assert proposal.tags == ("style",)
    assert proposal.scope == {"domain": "writing"}
    assert proposal.source_role == "user_message"
    assert not hasattr(proposal, "lifecycle")
    assert not hasattr(proposal, "evidence_class")
    assert not hasattr(proposal, "confidence")


@pytest.mark.parametrize("kind", [member.value for member in Kind])
def test_every_known_kind_is_accepted(kind: str) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(200, json={"memories": [{"kind": kind, "title": "t", "claim": "c"}]})

    extractor = _extractor(handler)
    proposals = extractor.extract("text")
    extractor.close()
    assert proposals[0].kind == kind


@pytest.mark.parametrize("kind", ["bogus", "", None, 3])
def test_unknown_kind_falls_back_to_observation(kind: Any) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(200, json={"memories": [{"kind": kind, "title": "t", "claim": "c"}]})

    extractor = _extractor(handler)
    proposals = extractor.extract("text")
    extractor.close()
    assert proposals[0].kind == "observation"


def test_source_role_defaults_to_first_source_when_missing() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(200, json={"memories": [{"title": "t", "claim": "c"}]})

    extractor = _extractor(handler)
    proposals = extractor.extract("text", sources=[_Source("system_message"), {"role": "user_message"}])
    extractor.close()
    assert proposals[0].source_role == "system_message"


def test_source_role_is_preserved_verbatim_when_present() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(
            200,
            json={
                "memories": [
                    {"title": "t", "claim": "c", "source_role": "custom_role"},
                ]
            },
        )

    extractor = _extractor(handler)
    proposals = extractor.extract("text", sources=[{"role": "assistant"}])
    extractor.close()
    assert proposals[0].source_role == "custom_role"


def test_source_role_defaults_to_unknown_without_sources() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(200, json={"memories": [{"title": "t", "claim": "c"}]})

    extractor = _extractor(handler)
    proposals = extractor.extract("text")
    extractor.close()
    assert proposals[0].source_role == "unknown"


def test_identical_proposals_are_deduplicated() -> None:
    memory = {"kind": "observation", "title": "t", "claim": "c"}

    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(
            200,
            json={
                "memories": [
                    dict(memory),
                    dict(memory),
                    {"kind": "failure", "title": "t", "claim": "c"},
                    {"kind": "observation", "title": "t", "claim": "different"},
                ]
            },
        )

    extractor = _extractor(handler)
    proposals = extractor.extract("text")
    extractor.close()
    assert [(proposal.kind, proposal.title, proposal.claim) for proposal in proposals] == [
        ("observation", "t", "c"),
        ("failure", "t", "c"),
        ("observation", "t", "different"),
    ]


def test_max_items_caps_the_proposals() -> None:
    memories = [{"title": f"t{index}", "claim": "c"} for index in range(5)]

    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(200, json={"memories": memories})

    extractor = _extractor(handler, max_items=2)
    proposals = extractor.extract("text")
    extractor.close()
    assert [proposal.title for proposal in proposals] == ["t0", "t1"]


def test_request_and_response_strings_are_bounded() -> None:
    long = "x" * 9000
    captured: dict[str, Any] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured.update(json.loads(request.content))
        return httpx2.Response(
            200,
            json={
                "memories": [
                    {
                        "title": long,
                        "claim": long,
                        "conditions": long,
                        "tags": [long],
                    }
                ]
            },
        )

    extractor = _extractor(handler)
    proposals = extractor.extract(long)
    extractor.close()

    assert len(captured["messages"][0]["content"].encode("utf-8")) <= 4096
    proposal = proposals[0]
    assert len(proposal.title.encode("utf-8")) <= 4096
    assert len(proposal.claim.encode("utf-8")) <= 4096
    assert len(proposal.conditions.encode("utf-8")) <= 4096  # type: ignore[union-attr]
    assert len(proposal.tags[0].encode("utf-8")) <= 4096


def test_malformed_memories_are_ignored() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(
            200,
            json={
                "memories": [
                    "not-a-memory",
                    {"title": "no claim"},
                    {"claim": "no title"},
                    {"title": 3, "claim": "bad title"},
                    {"title": "keep", "claim": "works"},
                ]
            },
        )

    extractor = _extractor(handler)
    proposals = extractor.extract("text")
    extractor.close()
    assert [proposal.title for proposal in proposals] == ["keep"]


@pytest.mark.parametrize("body", [None, "not-a-list", 42, {"memories": "no"}])
def test_malformed_response_shapes_yield_no_proposals(body: Any) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        del request
        return httpx2.Response(200, json=body)

    extractor = _extractor(handler)
    assert extractor.extract("text") == []
    extractor.close()


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


def test_api_key_is_read_from_environment_and_never_leaked(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str | None] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers.get("authorization"))
        return httpx2.Response(401, content=b"secret-response-body")

    monkeypatch.setenv("KNOWLAGEMCP_TEST_MEM0_KEY", "top-secret-value")
    extractor = _extractor(handler, api_key_env="KNOWLAGEMCP_TEST_MEM0_KEY")
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
        return httpx2.Response(200, json={"memories": []})

    extractor = _extractor(handler)
    assert extractor.health() == {"name": "mem0", "available": True}
    assert calls == []
    extractor.close()

    unconfigured = Mem0Extractor(Mem0IntegrationConfig(base_url=""))
    assert unconfigured.health() == {"name": "mem0", "available": False}
    unconfigured.close()


def test_unconfigured_extractor_fails_closed_without_network() -> None:
    extractor = Mem0Extractor(Mem0IntegrationConfig(base_url=""))
    with pytest.raises(ServiceError) as failure:
        extractor.extract("text")
    extractor.close()
    assert failure.value.code is ErrorCode.BACKEND_UNAVAILABLE


def test_module_does_not_import_sqlite3() -> None:
    import knowlage_mcp.integrations.mem0 as module

    assert module.__file__ is not None
    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "sqlite3" not in source
