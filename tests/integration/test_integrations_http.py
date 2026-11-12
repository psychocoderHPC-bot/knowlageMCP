# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""End-to-end hybrid retrieval over the real gateway with a mock Qdrant."""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from typing import Any

import httpx2
import pytest

from conftest import RunningGateway
from knowlage_mcp.admin_client import AdminClient
from knowlage_mcp.config import (
    Config,
    EmbeddingIntegrationConfig,
    IntegrationsConfig,
    RerankerIntegrationConfig,
    VectorIntegrationConfig,
)
from knowlage_mcp.integrations.registry import IntegrationRegistry

pytestmark = pytest.mark.integration

DIMENSIONS = 16


def _cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    norm_left = math.sqrt(sum(value * value for value in left))
    norm_right = math.sqrt(sum(value * value for value in right))
    if not norm_left or not norm_right:
        return 0.0
    return dot / (norm_left * norm_right)


class FakeQdrant:
    def __init__(self) -> None:
        self.collections: dict[str, dict[str, Any]] = {}

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        parts = [part for part in request.url.path.split("/") if part]
        if request.method == "PUT" and len(parts) == 2 and parts[0] == "collections":
            body = json.loads(request.content)
            self.collections.setdefault(
                parts[1],
                {"size": int(body["vectors"]["size"]), "points": {}},
            )
            return httpx2.Response(200, json={"result": True})
        if request.method == "GET" and len(parts) == 2 and parts[0] == "collections":
            if parts[1] not in self.collections:
                return httpx2.Response(404, json={"status": {"error": "not found"}})
            return httpx2.Response(200, json={"result": {"status": "green"}})
        if len(parts) < 3 or parts[0] != "collections" or parts[2] != "points":
            return httpx2.Response(404, json={"status": {"error": "unknown"}})
        collection = self.collections.get(parts[1])
        if collection is None:
            return httpx2.Response(404, json={"status": {"error": "not found"}})
        if request.method == "PUT" and len(parts) == 3:
            body = json.loads(request.content)
            for point in body.get("points", []):
                collection["points"][point["id"]] = {
                    "vector": list(point["vector"]),
                    "payload": dict(point.get("payload") or {}),
                }
            return httpx2.Response(200, json={"result": {"status": "completed"}})
        if len(parts) == 4 and parts[3] == "delete":
            body = json.loads(request.content)
            for condition in body.get("filter", {}).get("must", []):
                key = condition.get("key")
                values = set((condition.get("match") or {}).get("any") or [])
                for point_id_value, point in list(collection["points"].items()):
                    if point["payload"].get(key) in values:
                        del collection["points"][point_id_value]
            return httpx2.Response(200, json={"result": {"status": "completed"}})
        if len(parts) == 4 and parts[3] == "search":
            body = json.loads(request.content)
            points = list(collection["points"].items())
            for condition in body.get("filter", {}).get("must", []):
                if condition.get("key") == "namespace":
                    allowed = set((condition.get("match") or {}).get("any") or [])
                    points = [item for item in points if item[1]["payload"].get("namespace") in allowed]
            for condition in body.get("filter", {}).get("must_not", []):
                values = set((condition.get("match") or {}).get("any") or [])
                points = [
                    item for item in points if item[1]["payload"].get(condition.get("key")) not in values
                ]
            scored = [
                {
                    "id": point_id_value,
                    "score": _cosine(list(body["vector"]), point["vector"]),
                    "payload": dict(point["payload"]),
                }
                for point_id_value, point in points
            ]
            scored.sort(key=lambda item: item["score"], reverse=True)
            return httpx2.Response(200, json={"result": scored[: int(body.get("limit", 10))]})
        return httpx2.Response(404, json={"status": {"error": "unknown"}})

    def points_for(self, entry_id: str) -> list[dict[str, Any]]:
        return [
            point
            for collection in self.collections.values()
            for point in collection["points"].values()
            if point["payload"].get("entry_id") == entry_id
        ]


def test_register_record_reindex_search_forget(
    gateway_config: Config,
    gateway_factory: Callable[[Config], RunningGateway],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = gateway_config.model_copy(
        update={
            "integrations": IntegrationsConfig(
                enabled=True,
                embedding=EmbeddingIntegrationConfig(provider="hash", dimensions=DIMENSIONS),
                vector=VectorIntegrationConfig(
                    backend="qdrant",
                    url="https://qdrant.example.invalid",
                    vector_size=DIMENSIONS,
                    top_k=10,
                    hybrid=True,
                ),
                reranker=RerankerIntegrationConfig(provider="none"),
            )
        }
    )
    fake = FakeQdrant()
    registry = IntegrationRegistry(config, http_transport=httpx2.MockTransport(fake.handler))
    monkeypatch.setattr("knowlage_mcp.service.IntegrationRegistry", lambda _config: registry)
    running = gateway_factory(config)
    admin = AdminClient(running.admin_socket, timeout=30.0)

    with httpx2.Client(base_url=running.base_url) as http:
        registration = http.post(
            "/v1/bridge/register", json={"mode": "root", "label": "integration-e2e"}
        ).json()
        token = registration["session_token"]
        headers = {"Authorization": f"Bearer {token}"}
        recorded = http.post(
            "/v1/tools/record",
            headers=headers,
            json={
                "namespace": "shared",
                "entry": {
                    "kind": "observation",
                    "title": "qdrant end to end",
                    "claim": "qdrant hybrid retrieval probe",
                },
            },
        )
        assert recorded.status_code == 200, recorded.text
        entry = recorded.json()["data"]["entry"]
        assert admin.call("integrations.reindex")["generation"]
        # Candidate entries are indexed and visible while candidates are allowed.
        found = http.post(
            "/v1/tools/search",
            headers=headers,
            json={"query": "qdrant hybrid retrieval probe"},
        )
        assert found.status_code == 200, found.text
        data = found.json()["data"]
        assert data["retrieval_backend"] == "hybrid"
        assert data["semantic_used"] is True
        assert entry["entry_id"] in [result["entry_id"] for result in data["results"]]
        assert fake.points_for(entry["entry_id"])

        forgotten = admin.call("memory.forget", {"entry_id": entry["entry_id"]})
        assert forgotten["forgotten"] is True
        assert fake.points_for(entry["entry_id"]) == []

        gone = http.post(
            "/v1/tools/search",
            headers=headers,
            json={"query": "qdrant hybrid retrieval probe"},
        )
        assert gone.status_code == 200
        assert entry["entry_id"] not in [result["entry_id"] for result in gone.json()["data"]["results"]]
