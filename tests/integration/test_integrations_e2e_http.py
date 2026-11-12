# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""True over-the-wire integration tests for the optional adapters.

The gateway is started from a YAML/TOML-round-tripped configuration with the
real HTTP adapters. Every backend is a fake ``http.server`` on loopback, so the
full production path runs: config -> registry -> adapter -> socket -> service.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fake_backends import (
    FakeEmbeddingServer,
    FakeGraphitiServer,
    FakeMem0Server,
    FakeQdrantServer,
)

from conftest import RunningGateway, stop_gateway
from knowlage_mcp import gatewayctl
from knowlage_mcp.admin_client import AdminClient
from knowlage_mcp.config import (
    Config,
    EmbeddingIntegrationConfig,
    GraphitiIntegrationConfig,
    IntegrationsConfig,
    Mem0IntegrationConfig,
    RerankerIntegrationConfig,
    VectorIntegrationConfig,
    load_config,
    write_config_atomic,
)
from knowlage_mcp.integrations.hash_embed import HashEmbeddingProvider
from knowlage_mcp.integrations.qdrant import point_id

pytestmark = pytest.mark.integration

EMBED_KEY = "sk-e2e-embed-7c1f4a9e"
EMBED_KEY_ENV = "KMCP_TEST_EMBED_KEY"
DIMENSIONS = 16


@pytest.fixture
def fake_embeddings() -> Iterator[FakeEmbeddingServer]:
    server = FakeEmbeddingServer(expected_key=EMBED_KEY)
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def fake_qdrant() -> Iterator[FakeQdrantServer]:
    server = FakeQdrantServer()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def fake_graphiti() -> Iterator[FakeGraphitiServer]:
    server = FakeGraphitiServer()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def fake_mem0() -> Iterator[FakeMem0Server]:
    server = FakeMem0Server()
    try:
        yield server
    finally:
        server.stop()


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _register(client: httpx2.Client) -> dict[str, str]:
    response = client.post("/v1/bridge/register", json={"mode": "root", "label": "e2e-over-wire"})
    assert response.status_code == 200, response.text
    return _headers(response.json()["session_token"])


def _record(client: httpx2.Client, headers: dict[str, str], title: str, claim: str) -> dict[str, Any]:
    response = client.post(
        "/v1/tools/record",
        headers=headers,
        json={"namespace": "shared", "entry": {"kind": "observation", "title": title, "claim": claim}},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["entry"]


def _search(client: httpx2.Client, headers: dict[str, str], query: str) -> tuple[int, str, dict[str, Any]]:
    response = client.post("/v1/tools/search", headers=headers, json={"query": query})
    return response.status_code, response.text, response.json()


def _write_and_load(path: Path, config: Config) -> Config:
    write_config_atomic(path, config)
    return load_config(path)


def _main_config(base: Config, embeddings_url: str, qdrant_url: str) -> Config:
    return base.model_copy(
        update={
            "integrations": IntegrationsConfig(
                enabled=True,
                embedding=EmbeddingIntegrationConfig(
                    provider="openai",
                    base_url=embeddings_url,
                    api_key_env=EMBED_KEY_ENV,
                    dimensions=DIMENSIONS,
                    batch_size=16,
                ),
                vector=VectorIntegrationConfig(
                    backend="qdrant",
                    url=qdrant_url,
                    vector_size=DIMENSIONS,
                    top_k=10,
                    hybrid=True,
                ),
                reranker=RerankerIntegrationConfig(provider="lexical"),
            )
        }
    )


def test_over_the_wire_hybrid_path_acl_delete_fail_open_restart(
    gateway_config: Config,
    gateway_factory: Callable[[Config], RunningGateway],
    fake_embeddings: FakeEmbeddingServer,
    fake_qdrant: FakeQdrantServer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(EMBED_KEY_ENV, EMBED_KEY)
    config_path = tmp_path / "config.toml"
    config = _main_config(gateway_config, fake_embeddings.url, fake_qdrant.url)
    loaded = _write_and_load(config_path, config)

    # The serialized configuration round-trips through load_config unchanged.
    assert loaded.integrations.enabled is True
    assert loaded.integrations.embedding.provider == "openai"
    assert loaded.integrations.embedding.base_url == fake_embeddings.url
    assert loaded.integrations.embedding.api_key_env == EMBED_KEY_ENV
    assert loaded.integrations.embedding.dimensions == DIMENSIONS
    assert loaded.integrations.vector.backend == "qdrant"
    assert loaded.integrations.vector.url == fake_qdrant.url
    assert loaded.integrations.vector.vector_size == DIMENSIONS
    assert loaded.integrations.vector.hybrid is True
    assert loaded.integrations.reranker.provider == "lexical"
    assert loaded.storage.database == gateway_config.storage.database
    assert EMBED_KEY not in config_path.read_text(encoding="utf-8")

    log_path = tmp_path / "gateway.log"
    log_handler = logging.FileHandler(log_path, encoding="utf-8")
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(log_handler)
    root.setLevel(logging.DEBUG)
    responses: list[str] = []
    try:
        running = gateway_factory(loaded)
        admin = AdminClient(running.admin_socket, timeout=30.0)
        with httpx2.Client(base_url=running.base_url, timeout=30.0) as client:
            headers = _register(client)
            entries = [
                _record(client, headers, "qdrant end to end", "qdrant hybrid retrieval probe"),
                _record(client, headers, "unrelated note", "bananas grow in tropical climates"),
                _record(client, headers, "semantic telemetry", "semantic vector search telemetry beta"),
            ]

            reindexed = admin.call("integrations.reindex")
            assert reindexed["generation"]
            assert reindexed["indexed"] == 3
            assert fake_qdrant.collection_creations >= 1
            assert fake_qdrant.upserts >= 1
            assert fake_qdrant.total_points() == 3
            assert fake_embeddings.requests >= 1
            assert fake_embeddings.authorization_headers
            assert set(fake_embeddings.authorization_headers) == {f"Bearer {EMBED_KEY}"}

            status, text, envelope = _search(client, headers, "qdrant hybrid retrieval probe")
            responses.append(text)
            assert status == 200, text
            data = envelope["data"]
            assert data["semantic_used"] is True
            assert data["retrieval_backend"] == "hybrid"
            assert data["estimated_tokens"] > 0
            by_id = {result["entry_id"]: result for result in data["results"]}
            assert entries[0]["entry_id"] in by_id
            reasons = by_id[entries[0]["entry_id"]]["selection_reasons"]
            assert any(reason in reasons for reason in ("semantic match", "fts match"))

            # A point that exists in Qdrant is still invisible to an agent that
            # cannot read the personal namespace. Qdrant answers a wide filter.
            personal = admin.call(
                "memory.add",
                {
                    "kind": "observation",
                    "namespace": "personal",
                    "title": "personal marker",
                    "text": "personal secret hybrid marker",
                },
            )["entry"]
            collection = f"{loaded.integrations.vector.collection_prefix}_{reindexed['generation']}"
            personal_vector = HashEmbeddingProvider(dimensions=DIMENSIONS).embed(
                [f"{personal['title']}\n{personal['claim']}"]
            )[0]
            personal_payload = {
                "entry_id": personal["entry_id"],
                "revision_id": personal["revision_id"],
                "namespace": "personal",
                "lifecycle": "published",
            }
            with httpx2.Client(timeout=10.0) as direct:
                upserted = direct.put(
                    f"{fake_qdrant.url}/collections/{collection}/points",
                    params={"wait": "true"},
                    json={
                        "points": [
                            {
                                "id": point_id(personal["revision_id"]),
                                "vector": personal_vector,
                                "payload": personal_payload,
                            }
                        ]
                    },
                )
                assert upserted.status_code == 200, upserted.text
                wide = direct.post(
                    f"{fake_qdrant.url}/collections/{collection}/points/search",
                    json={
                        "vector": personal_vector,
                        "limit": 10,
                        "with_payload": True,
                        "filter": {"must": [{"key": "namespace", "match": {"any": ["shared", "personal"]}}]},
                    },
                )
                assert wide.status_code == 200, wide.text
                wide_ids = [point["payload"]["entry_id"] for point in wide.json()["result"]]
                assert personal["entry_id"] in wide_ids

            status, text, envelope = _search(client, headers, "personal secret hybrid marker")
            responses.append(text)
            assert status == 200, text
            agent_ids = [result["entry_id"] for result in envelope["data"]["results"]]
            assert personal["entry_id"] not in agent_ids

            # Deletion removes the derived point and the canonical row.
            deletes_before = fake_qdrant.deletes
            forgotten = admin.call("memory.forget", {"entry_id": entries[2]["entry_id"]})
            assert forgotten["forgotten"] is True
            assert fake_qdrant.deletes > deletes_before
            assert fake_qdrant.points_for(entries[2]["entry_id"]) == []
            status, text, envelope = _search(client, headers, "semantic vector search telemetry beta")
            responses.append(text)
            assert status == 200, text
            assert entries[2]["entry_id"] not in [
                result["entry_id"] for result in envelope["data"]["results"]
            ]

            # Fail-open: a broken vector backend keeps lexical results.
            fake_qdrant.fail_next(500)
            status, text, envelope = _search(client, headers, "qdrant hybrid retrieval probe")
            responses.append(text)
            assert status == 200, text
            data = envelope["data"]
            assert entries[0]["entry_id"] in [result["entry_id"] for result in data["results"]]
            assert any("semantic backend unavailable" in warning for warning in data["warnings"])

            # Fail-open: a broken embedding backend behaves the same way.
            fake_embeddings.fail_next(500)
            status, text, envelope = _search(client, headers, "qdrant hybrid retrieval probe")
            responses.append(text)
            assert status == 200, text
            data = envelope["data"]
            assert entries[0]["entry_id"] in [result["entry_id"] for result in data["results"]]
            assert any("semantic backend unavailable" in warning for warning in data["warnings"])

            health = client.get("/v1/health")
            responses.append(health.text)

        # Restart rehydrates the recorded generation without a reindex.
        creations = fake_qdrant.collection_creations
        stop_gateway(running)
        restarted = gateway_factory(loaded)
        assert fake_qdrant.collection_creations == creations
        admin = AdminClient(restarted.admin_socket, timeout=30.0)
        with httpx2.Client(base_url=restarted.base_url, timeout=30.0) as client:
            headers = _register(client)
            status, text, envelope = _search(client, headers, "qdrant hybrid retrieval probe")
            responses.append(text)
            assert status == 200, text
            data = envelope["data"]
            assert data["semantic_used"] is True
            assert data["retrieval_backend"] == "hybrid"
            assert entries[0]["entry_id"] in [result["entry_id"] for result in data["results"]]

        admin_json = json.dumps(admin.call("integrations.status"), default=str) + json.dumps(
            admin.call("integrations.probe"), default=str
        )
        log_handler.flush()
        log_text = log_path.read_text(encoding="utf-8")
        assert EMBED_KEY not in admin_json
        assert EMBED_KEY not in log_text
        assert all(EMBED_KEY not in response for response in responses)
    finally:
        root.removeHandler(log_handler)
        root.setLevel(previous_level)
        log_handler.close()


def test_graphiti_extract_over_the_wire_keeps_lifecycle(
    gateway_config: Config,
    gateway_factory: Callable[[Config], RunningGateway],
    fake_graphiti: FakeGraphitiServer,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = gateway_config.model_copy(
        update={
            "integrations": IntegrationsConfig(
                enabled=True,
                graphiti=GraphitiIntegrationConfig(enabled=True, base_url=fake_graphiti.url),
            )
        }
    )
    config_path = tmp_path / "config.toml"
    loaded = _write_and_load(config_path, config)
    assert loaded.integrations.graphiti.enabled is True
    assert loaded.integrations.graphiti.base_url == fake_graphiti.url

    running = gateway_factory(loaded)
    admin = AdminClient(running.admin_socket, timeout=30.0)
    with httpx2.Client(base_url=running.base_url, timeout=30.0) as client:
        headers = _register(client)
        entry = _record(client, headers, "graphiti source", "alpha causes beta in the observed test")
    before = admin.call("inspect", {"entry_id": entry["entry_id"]})
    assert before["entry"]["lifecycle"] == "candidate"
    entries_before = admin.call("status")["entries"]

    assert (
        gatewayctl.main(
            ["--config", str(config_path), "integrations", "extract", entry["entry_id"], "--json"]
        )
        == 0
    )
    extracted = json.loads(capsys.readouterr().out)
    assert fake_graphiti.requests == 1
    assert len(extracted["proposals"]) == 1
    assert extracted["proposals"][0]["subject"] == "alpha"
    assert extracted["proposals"][0]["object"] == "beta"
    assert extracted["proposals"][0]["relation"] == "extracted"

    after = admin.call("inspect", {"entry_id": entry["entry_id"]})
    assert after["entry"]["lifecycle"] == before["entry"]["lifecycle"] == "candidate"
    assert after["entry"]["current_revision_id"] == before["entry"]["current_revision_id"]
    assert admin.call("status")["entries"] == entries_before

    assert (
        gatewayctl.main(
            [
                "--config",
                str(config_path),
                "integrations",
                "proposals",
                "--entry",
                entry["entry_id"],
                "--json",
            ]
        )
        == 0
    )
    listed = json.loads(capsys.readouterr().out)
    assert len(listed["proposals"]) == 1
    assert listed["proposals"][0]["extractor"] == "graphiti"
    assert listed["proposals"][0]["state"] == "proposed"


def test_mem0_import_over_the_wire_creates_candidates_idempotently(
    gateway_config: Config,
    gateway_factory: Callable[[Config], RunningGateway],
    fake_mem0: FakeMem0Server,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = gateway_config.model_copy(
        update={
            "integrations": IntegrationsConfig(
                enabled=True,
                mem0=Mem0IntegrationConfig(enabled=True, base_url=fake_mem0.url),
            )
        }
    )
    config_path = tmp_path / "config.toml"
    loaded = _write_and_load(config_path, config)
    assert loaded.integrations.mem0.enabled is True
    assert loaded.integrations.mem0.base_url == fake_mem0.url

    running = gateway_factory(loaded)
    admin = AdminClient(running.admin_socket, timeout=30.0)
    memories = tmp_path / "memories.jsonl"
    memories.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "role": "user",
                        "source_role": "user_message",
                        "text": "Please keep logs concise.",
                        "task": "logging",
                    }
                ),
                json.dumps(
                    {
                        "role": "user",
                        "source_role": "user_message",
                        "text": "Always report the failing test name.",
                        "task": "testing",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert (
        gatewayctl.main(
            [
                "--config",
                str(config_path),
                "integrations",
                "import-memories",
                str(memories),
                "--dry-run",
                "--json",
            ]
        )
        == 0
    )
    dry = json.loads(capsys.readouterr().out)
    assert dry["dry_run"] is True
    assert dry["created"] == 0
    assert dry["proposals"] >= 1
    assert fake_mem0.requests >= 1
    assert fake_mem0.message_roles
    assert set(fake_mem0.message_roles) == {"user_message"}
    assert dry["calls"] >= 1

    assert (
        gatewayctl.main(
            [
                "--config",
                str(config_path),
                "integrations",
                "import-memories",
                str(memories),
                "--json",
            ]
        )
        == 0
    )
    real = json.loads(capsys.readouterr().out)
    assert real["created"] >= 1
    entries_after_import = admin.call("status")["entries"]

    conn = running.service.db.connect()
    rows = conn.execute(
        "SELECT r.source_role, r.lifecycle, r.evidence_class, r.tags, e.created_by_agent_id "
        "FROM entry_revisions r JOIN entries e ON e.entry_id = r.entry_id "
        "WHERE e.created_by_agent_id = 'extractor:mem0'"
    ).fetchall()
    assert rows
    for row in rows:
        assert row["source_role"] == "user_message"
        assert row["lifecycle"] == "candidate"
        assert row["evidence_class"] == "unverified"
        assert "mem0" in json.loads(str(row["tags"]))

    assert (
        gatewayctl.main(
            [
                "--config",
                str(config_path),
                "integrations",
                "import-memories",
                str(memories),
                "--json",
            ]
        )
        == 0
    )
    again = json.loads(capsys.readouterr().out)
    assert again["created"] == 0
    assert again["skipped"] >= real["created"]
    assert admin.call("status")["entries"] == entries_after_import
