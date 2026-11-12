# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Qdrant adapter contract tests against an in-process mock transport."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx2
import pytest

from knowlage_mcp.config import VectorIntegrationConfig
from knowlage_mcp.integrations.base import VectorItem
from knowlage_mcp.integrations.qdrant import QdrantVectorIndex, point_id
from knowlage_mcp.models import ErrorCode, ServiceError


def _cfg(**overrides: Any) -> VectorIntegrationConfig:
    values: dict[str, Any] = {
        "backend": "qdrant",
        "url": "https://qdrant.example.invalid",
        "vector_size": 3,
        "distance": "Cosine",
        "collection_prefix": "test",
        "timeout_s": 1.0,
    }
    values.update(overrides)
    return VectorIntegrationConfig(**values)


def _adapter(handler: Any, **overrides: Any) -> tuple[QdrantVectorIndex, VectorIntegrationConfig]:
    cfg = _cfg(**overrides)
    return QdrantVectorIndex(cfg, transport=httpx2.MockTransport(handler)), cfg


def _generation(adapter: QdrantVectorIndex) -> str:
    return adapter.ensure_generation("hash-blake2b-v1", 3)


def test_ensure_generation_payload_and_idempotent_put() -> None:
    seen: list[tuple[str, str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append((request.method, request.url.path, request.content))
        return httpx2.Response(200, json={"result": True})

    adapter, cfg = _adapter(handler)
    generation = _generation(adapter)
    assert generation == QdrantVectorIndex.generation_id_for(cfg, "hash-blake2b-v1", 3)
    assert adapter.generation_id == generation
    assert adapter.available is True
    collection = f"test_{generation}"
    assert seen[0][0] == "PUT"
    assert seen[0][1] == f"/collections/{collection}"
    assert json.loads(seen[0][2]) == {"vectors": {"size": 3, "distance": "Cosine"}}
    assert _generation(adapter) == generation


def test_ensure_generation_treats_409_as_success() -> None:
    calls: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.method == "PUT":
            return httpx2.Response(409, json={"status": {"error": "exists"}})
        return httpx2.Response(200, json={"result": {"status": "green"}})

    adapter, _ = _adapter(handler)
    generation = _generation(adapter)
    assert generation.startswith("gen_")
    assert calls[0].startswith("PUT ")
    assert calls[1].startswith("GET ")
    adapter.close()


def test_generation_id_is_stable_across_processes(repo_root: Path) -> None:
    adapter, cfg = _adapter(lambda request: httpx2.Response(200, json={}))
    in_process = QdrantVectorIndex.generation_id_for(cfg, "m", 3)
    adapter.close()
    script = (
        "from knowlage_mcp.config import VectorIntegrationConfig\n"
        "from knowlage_mcp.integrations.qdrant import QdrantVectorIndex\n"
        "cfg = VectorIntegrationConfig(backend='qdrant', collection_prefix='test')\n"
        "print(QdrantVectorIndex.generation_id_for(cfg, 'm', 3))\n"
    )
    env = {**os.environ, "PYTHONPATH": str(repo_root / "src")}
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert completed.stdout.strip() == in_process


def test_upsert_uses_deterministic_uuid_and_small_payload() -> None:
    bodies: list[dict[str, Any]] = []
    params: list[dict[str, str]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "PUT" and request.url.path.count("/") == 2:
            return httpx2.Response(200, json={"result": True})
        bodies.append(json.loads(request.content))
        params.append(dict(request.url.params))
        return httpx2.Response(200, json={"result": {}})

    adapter, _ = _adapter(handler)
    _generation(adapter)
    item = VectorItem(
        entry_id="ent-1",
        revision_id="rev-1",
        namespace="shared",
        vector=(0.1, 0.2, 0.3),
        payload={
            "lifecycle": "published",
            "evidence_class": "source_checked",
            "content_hash": "abc",
            "scope": {"repository": "alpaka"},
            "author_root_id": "root-1",
            "secret_field": "must not leave the host",
        },
    )
    adapter.upsert([item])
    assert len(bodies) == 1
    point = bodies[0]["points"][0]
    assert point["id"] == point_id("rev-1")
    assert point["vector"] == [0.1, 0.2, 0.3]
    assert point["payload"] == {
        "entry_id": "ent-1",
        "revision_id": "rev-1",
        "namespace": "shared",
        "lifecycle": "published",
        "evidence_class": "source_checked",
        "content_hash": "abc",
        "scope": {"repository": "alpaka"},
        "author_root_id": "root-1",
    }
    assert params[0] == {"wait": "true"}
    adapter.close()


def test_delete_filter_shape() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "PUT" and request.url.path.count("/") == 2:
            return httpx2.Response(200, json={"result": True})
        bodies.append(json.loads(request.content))
        return httpx2.Response(200, json={"result": {}})

    adapter, _ = _adapter(handler)
    _generation(adapter)
    adapter.delete(["ent-1", "ent-2"])
    assert bodies[0] == {"filter": {"must": [{"key": "entry_id", "match": {"any": ["ent-1", "ent-2"]}}]}}
    adapter.delete_revisions(["rev-1"])
    assert bodies[1] == {"filter": {"must": [{"key": "revision_id", "match": {"any": ["rev-1"]}}]}}
    adapter.close()


def test_search_filter_includes_namespaces_and_lifecycle() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "PUT" and request.url.path.count("/") == 2:
            return httpx2.Response(200, json={"result": True})
        bodies.append(json.loads(request.content))
        return httpx2.Response(200, json={"result": []})

    adapter, _ = _adapter(handler)
    _generation(adapter)
    adapter.search([0.1, 0.2, 0.3], 5, ["shared", "personal"], allow_candidates=False)
    adapter.search([0.1, 0.2, 0.3], 5, ["shared"], allow_candidates=True)
    must_not = bodies[0]["filter"]["must_not"]
    assert bodies[0]["filter"]["must"] == [{"key": "namespace", "match": {"any": ["shared", "personal"]}}]
    assert {"key": "lifecycle", "match": {"any": ["archived", "superseded"]}} in must_not
    assert {"key": "lifecycle", "match": {"any": ["candidate"]}} in must_not
    assert bodies[1]["filter"]["must_not"] == [
        {"key": "lifecycle", "match": {"any": ["archived", "superseded"]}}
    ]
    adapter.close()


def test_search_parses_payload_and_skips_malformed_points() -> None:
    good = {
        "id": point_id("rev-1"),
        "score": 0.75,
        "payload": {"entry_id": "ent-1", "revision_id": "rev-1"},
    }

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "PUT" and request.url.path.count("/") == 2:
            return httpx2.Response(200, json={"result": True})
        return httpx2.Response(
            200,
            json={
                "result": [
                    good,
                    {"id": point_id("rev-2"), "score": 0.5},
                    {
                        "id": point_id("rev-2"),
                        "score": 0.4,
                        "payload": {"entry_id": "ent-3", "revision_id": "rev-3"},
                    },
                    {
                        "id": point_id("rev-4"),
                        "score": "not-a-number",
                        "payload": {"entry_id": "ent-4", "revision_id": "rev-4"},
                    },
                    "not-a-point",
                ]
            },
        )

    adapter, _ = _adapter(handler)
    _generation(adapter)
    hits = adapter.search([0.1, 0.2, 0.3], 5, ["shared"])
    assert [(hit.entry_id, hit.revision_id, hit.score) for hit in hits] == [("ent-1", "rev-1", 0.75)]
    adapter.close()


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, ErrorCode.FORBIDDEN), (500, ErrorCode.BACKEND_UNAVAILABLE)],
)
def test_search_status_mapping(status: int, code: ErrorCode) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "PUT" and request.url.path.count("/") == 2:
            return httpx2.Response(200, json={"result": True})
        return httpx2.Response(status, json={"error": "boom"})

    adapter, _ = _adapter(handler)
    _generation(adapter)
    with pytest.raises(ServiceError) as excinfo:
        adapter.search([0.1, 0.2, 0.3], 5, ["shared"])
    assert excinfo.value.code is code
    adapter.close()


def test_health_never_raises_and_reports_generation() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "PUT":
            return httpx2.Response(200, json={"result": True})
        return httpx2.Response(500, json={"error": "down"})

    adapter, _ = _adapter(handler)
    before = adapter.health()
    assert before["available"] is False
    assert before["generation"] is None
    generation = _generation(adapter)
    health = adapter.health()
    assert health == {
        "name": "qdrant",
        "available": False,
        "generation": generation,
        "error": ErrorCode.BACKEND_UNAVAILABLE.value,
    }
    adapter.close()


def test_health_returns_unavailable_without_generation_after_transport_error() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route")

    adapter, _ = _adapter(handler)
    assert adapter.health()["available"] is False
    adapter.close()


def test_adopt_generation_probes_existing_collection() -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append((request.method, request.url.path))
        return httpx2.Response(
            200,
            json={
                "result": {
                    "config": {"params": {"vectors": {"size": 3, "distance": "Cosine"}}},
                }
            },
        )

    adapter, _ = _adapter(handler)
    adapter.adopt_generation("gen_ready")
    assert adapter.generation_id == "gen_ready"
    assert adapter.available is True
    assert calls == [("GET", "/collections/test_gen_ready")]
    adapter.close()


def test_adopt_generation_stays_unavailable_on_size_mismatch_and_errors() -> None:
    def mismatch(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={"result": {"config": {"params": {"vectors": {"size": 4}}}}},
        )

    adapter, _ = _adapter(mismatch)
    adapter.adopt_generation("gen_ready")
    assert adapter.generation_id == "gen_ready"
    assert adapter.available is False
    adapter.close()

    adapter, _ = _adapter(lambda request: httpx2.Response(404, json={}))
    adapter.adopt_generation("gen_missing")
    assert adapter.generation_id == "gen_missing"
    assert adapter.available is False
    adapter.close()

    def broken(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route")

    adapter, _ = _adapter(broken)
    adapter.adopt_generation("gen_broken")
    assert adapter.available is False
    adapter.close()
