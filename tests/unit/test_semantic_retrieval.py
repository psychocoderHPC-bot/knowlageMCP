# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Hybrid retrieval: ACL-first merging, fail-open behaviour, and metadata."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import httpx2
import pytest

from knowlage_mcp.config import (
    Config,
    EmbeddingIntegrationConfig,
    IntegrationsConfig,
    RerankerIntegrationConfig,
    RetrievalConfig,
    StorageConfig,
    VectorIntegrationConfig,
    resolve_paths,
)
from knowlage_mcp.db import Database
from knowlage_mcp.evidence import EvidenceStore
from knowlage_mcp.integrations.qdrant import point_id
from knowlage_mcp.integrations.registry import IntegrationRegistry
from knowlage_mcp.models import (
    EntryPayload,
    Kind,
    RecordRequest,
    Scope,
    SearchFilters,
    SearchRequest,
    SessionInfo,
)
from knowlage_mcp.service import KnowledgeService

DIMENSIONS = 16


class Clock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    norm_left = math.sqrt(sum(value * value for value in left))
    norm_right = math.sqrt(sum(value * value for value in right))
    if not norm_left or not norm_right:
        return 0.0
    return dot / (norm_left * norm_right)


class FakeQdrant:
    """A tiny in-memory Qdrant that honours the filters the adapter sends."""

    def __init__(self) -> None:
        self.collections: dict[str, dict[str, Any]] = {}
        self.fail_search = False
        self.requests = 0

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests += 1
        parts = [part for part in request.url.path.split("/") if part]
        if request.method == "PUT" and len(parts) == 2 and parts[0] == "collections":
            body = json.loads(request.content)
            self.collections.setdefault(
                parts[1],
                {"size": int(body["vectors"]["size"]), "distance": body["vectors"]["distance"], "points": {}},
            )
            return httpx2.Response(200, json={"result": True})
        if request.method == "GET" and len(parts) == 2 and parts[0] == "collections":
            collection = self.collections.get(parts[1])
            if collection is None:
                return httpx2.Response(404, json={"status": {"error": "not found"}})
            return httpx2.Response(
                200,
                json={
                    "result": {
                        "status": "green",
                        "config": {
                            "params": {
                                "vectors": {
                                    "size": collection["size"],
                                    "distance": collection["distance"],
                                }
                            }
                        },
                    }
                },
            )
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
        if len(parts) == 4 and parts[3] == "search":
            if self.fail_search:
                return httpx2.Response(500, json={"status": {"error": "down"}})
            return self._search(collection, json.loads(request.content))
        if len(parts) == 4 and parts[3] == "delete":
            body = json.loads(request.content)
            for condition in body.get("filter", {}).get("must", []):
                key = condition.get("key")
                values = set((condition.get("match") or {}).get("any") or [])
                for point_id_value, point in list(collection["points"].items()):
                    if point["payload"].get(key) in values:
                        del collection["points"][point_id_value]
            return httpx2.Response(200, json={"result": {"status": "completed"}})
        if len(parts) == 4 and parts[3] == "payload":
            body = json.loads(request.content)
            payload_update = body.get("payload") or {}
            for condition in body.get("filter", {}).get("must", []):
                key = condition.get("key")
                match = condition.get("match") or {}
                wanted = match.get("value")
                for point in collection["points"].values():
                    if point["payload"].get(key) == wanted:
                        point["payload"].update(payload_update)
            return httpx2.Response(200, json={"result": {"status": "completed"}})
        return httpx2.Response(404, json={"status": {"error": "unknown"}})

    def _search(self, collection: dict[str, Any], body: dict[str, Any]) -> httpx2.Response:
        points = list(collection["points"].items())
        must = body.get("filter", {}).get("must", [])
        must_not = body.get("filter", {}).get("must_not", [])
        for condition in must:
            if condition.get("key") == "namespace":
                allowed = set((condition.get("match") or {}).get("any") or [])
                points = [item for item in points if item[1]["payload"].get("namespace") in allowed]
        for condition in must_not:
            values = set((condition.get("match") or {}).get("any") or [])
            points = [item for item in points if item[1]["payload"].get(condition.get("key")) not in values]
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

    def add_point(
        self,
        entry_id: str,
        revision_id: str,
        vector: list[float],
        *,
        namespace: str = "shared",
        lifecycle: str = "published",
        evidence_class: str = "source_checked",
        content_hash: str = "hash",
        scope: dict[str, str] | None = None,
    ) -> None:
        if not self.collections:
            raise AssertionError("call index_rebuild before adding points")
        collection = next(iter(self.collections.values()))
        collection["points"][point_id(revision_id)] = {
            "vector": list(vector),
            "payload": {
                "entry_id": entry_id,
                "revision_id": revision_id,
                "namespace": namespace,
                "lifecycle": lifecycle,
                "evidence_class": evidence_class,
                "content_hash": content_hash,
                "scope": scope or {},
                "author_root_id": "root",
            },
        }

    def entry_ids(self) -> set[str]:
        return {
            str(point["payload"].get("entry_id"))
            for collection in self.collections.values()
            for point in collection["points"].values()
        }


class Env:
    def __init__(
        self,
        service: KnowledgeService,
        registry: IntegrationRegistry,
        fake: FakeQdrant,
        db: Database,
    ) -> None:
        self.service = service
        self.registry = registry
        self.fake = fake
        self.db = db

    def session(self) -> SessionInfo:
        registration = self.service.register(label="semantic-reader", source="test")
        return self.service.authenticate(registration.session_token)

    def close(self) -> None:
        self.service.close()


def build_env(tmp_path: Path, *, disabled: bool = False, **integration_overrides: Any) -> Env:
    data = tmp_path / "data"
    config = Config(
        storage=StorageConfig(
            database=data / "knowledge.sqlite3",
            evidence_dir=data / "evidence",
            backup_dir=data / "backups",
        ),
        retrieval=RetrievalConfig(include_candidates=True),
        integrations=IntegrationsConfig(
            enabled=not disabled,
            embedding=EmbeddingIntegrationConfig(provider="hash", dimensions=DIMENSIONS),
            vector=VectorIntegrationConfig(
                backend="qdrant",
                url="https://qdrant.example.invalid",
                vector_size=DIMENSIONS,
                top_k=10,
                hybrid=True,
            ),
            reranker=RerankerIntegrationConfig(provider="none", top_n=20),
            **integration_overrides,
        ),
    )
    config = resolve_paths(config)
    db = Database(config.storage.database)
    db.migrate()
    evidence = EvidenceStore(
        config.storage.evidence_dir,
        quota_bytes=8 * 1024 * 1024,
        max_object_bytes=1024 * 1024,
    )
    fake = FakeQdrant()
    clock = Clock()
    registry = IntegrationRegistry(config, http_transport=httpx2.MockTransport(fake.handler))
    service = KnowledgeService(
        config,
        db,
        evidence,
        clock=clock,
        monotonic=clock,
        integrations=registry,
    )
    service.bootstrap()
    return Env(service=service, registry=registry, fake=fake, db=db)


def _search(env: Env, session: SessionInfo, query: str, **kwargs: Any):
    return env.service.search(session, SearchRequest(query=query, **kwargs))


def test_hybrid_merge_and_vector_only_hit(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    try:
        session = env.session()
        first = env.service.memory_add(
            namespace="shared", kind="observation", text="alpha semantic probe", title="alpha"
        )
        hidden = env.service.memory_add(
            namespace="shared", kind="observation", text="gamma hidden unrelated words", title="gamma"
        )
        env.service.index_rebuild()
        query = "alpha semantic probe"
        vector = env.registry.embedding.embed([query])[0]
        env.fake.add_point(
            hidden.entry.entry_id,
            hidden.entry.revision_id,
            list(vector),
            evidence_class="operator_confirmed",
        )
        response = _search(env, session, query)
        assert response.retrieval_backend == "hybrid"
        assert response.semantic_used is True
        assert response.index_generation == env.registry.vector.generation_id
        ids = [result.entry_id for result in response.results]
        assert hidden.entry.entry_id in ids
        assert first.entry.entry_id in ids
        semantic = next(result for result in response.results if result.entry_id == hidden.entry.entry_id)
        assert "semantic match" in semantic.selection_reasons
        assert "semantic match" in semantic.warnings
    finally:
        env.close()


def test_payload_namespace_is_not_trusted_for_acl(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    try:
        session = env.session()
        secret = env.service.memory_add(
            namespace="personal", kind="observation", text="personal semantic secret", title="secret"
        )
        env.service.index_rebuild()
        query = "personal semantic secret"
        vector = env.registry.embedding.embed([query])[0]
        # A stale or malicious payload claims a readable namespace.
        env.fake.add_point(secret.entry.entry_id, secret.entry.revision_id, list(vector), namespace="shared")
        response = _search(env, session, query)
        assert secret.entry.entry_id not in [result.entry_id for result in response.results]
    finally:
        env.close()


def test_scope_incompatible_hit_is_dropped(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    try:
        session = env.session()
        write = env.service.record(
            session,
            RecordRequest(
                namespace="shared",
                entry=EntryPayload(
                    kind=Kind.OBSERVATION,
                    title="scoped",
                    claim="delta unrelated scope words",
                    scope=Scope(repository="picongpu"),
                ),
            ),
        )
        env.service.index_rebuild()
        query = "alpha semantic probe"
        vector = env.registry.embedding.embed([query])[0]
        env.fake.add_point(write.entry.entry_id, write.entry.revision_id, list(vector), lifecycle="candidate")
        response = env.service.search(
            session,
            SearchRequest(
                query=query,
                filters=SearchFilters(scope=Scope(repository="alpaka"), include_candidates=True),
            ),
        )
        assert write.entry.entry_id not in [result.entry_id for result in response.results]
    finally:
        env.close()


@pytest.mark.parametrize("lifecycle", ["archived", "superseded"])
def test_archived_and_superseded_hits_are_dropped(tmp_path: Path, lifecycle: str) -> None:
    env = build_env(tmp_path)
    try:
        session = env.session()
        write = env.service.memory_add(
            namespace="shared", kind="observation", text="visibility lifecycle probe", title="life"
        )
        env.service.index_rebuild()
        with env.db.transaction() as conn:
            conn.execute(
                "UPDATE entry_revisions SET lifecycle = ? WHERE revision_id = ?",
                (lifecycle, write.entry.revision_id),
            )
        query = "alpha semantic probe"
        vector = env.registry.embedding.embed([query])[0]
        env.fake.add_point(write.entry.entry_id, write.entry.revision_id, list(vector))
        response = _search(env, session, query)
        assert write.entry.entry_id not in [result.entry_id for result in response.results]
    finally:
        env.close()


def test_candidate_is_low_trust_and_excluded_when_configured(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    try:
        session = env.session()
        write = env.service.record(
            session,
            RecordRequest(
                namespace="shared",
                entry=EntryPayload(
                    kind=Kind.OBSERVATION,
                    title="candidate",
                    claim="candidate semantic probe",
                ),
            ),
        )
        env.service.index_rebuild()
        query = "candidate semantic probe"
        vector = env.registry.embedding.embed([query])[0]
        env.fake.add_point(
            write.entry.entry_id,
            write.entry.revision_id,
            list(vector),
            lifecycle="candidate",
            evidence_class="unverified",
        )
        included = _search(env, session, query)
        result = next(item for item in included.results if item.entry_id == write.entry.entry_id)
        assert result.low_trust is True
        assert any("low trust" in warning for warning in result.warnings)

        excluded = env.service.search(
            session,
            SearchRequest(query=query, filters=SearchFilters(include_candidates=False)),
        )
        assert write.entry.entry_id not in [item.entry_id for item in excluded.results]
    finally:
        env.close()


class StubReranker:
    name = "stub"
    available = True

    def __init__(self, extra: str) -> None:
        self.extra = extra
        self.last: list[str] = []

    def rerank(self, query: str, candidates: Any, top_n: int) -> list[str]:
        del query, top_n
        self.last = [candidate.entry_id for candidate in candidates]
        return [self.extra, *reversed(self.last)]

    def health(self) -> dict[str, Any]:
        return {"name": self.name, "available": True}


def test_reranker_reorders_but_cannot_add_an_entry(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    try:
        session = env.session()
        writes = [
            env.service.memory_add(
                namespace="shared", kind="observation", text=f"alpha rerank probe {index}", title=f"t{index}"
            )
            for index in range(3)
        ]
        env.service.index_rebuild()
        stub = StubReranker("unauthorized-entry")
        env.registry.reranker = stub
        response = _search(env, session, "alpha rerank probe")
        assert response.reranked is True
        ids = [result.entry_id for result in response.results]
        assert "unauthorized-entry" not in ids
        assert ids == list(reversed(stub.last))
        assert set(ids) <= {write.entry.entry_id for write in writes}
    finally:
        env.close()


def test_backend_failure_returns_lexical_results_with_warning(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    try:
        session = env.session()
        write = env.service.memory_add(
            namespace="shared", kind="observation", text="alpha lexical fallback", title="fallback"
        )
        env.service.index_rebuild()
        env.fake.fail_search = True
        response = _search(env, session, "alpha lexical fallback")
        assert response.retrieval_backend == "fts5"
        assert response.semantic_used is False
        assert response.warnings == ("semantic backend unavailable: BACKEND_UNAVAILABLE",)
        assert write.entry.entry_id in [result.entry_id for result in response.results]
    finally:
        env.close()


def test_disabled_integrations_make_zero_http_requests(tmp_path: Path) -> None:
    calls: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return httpx2.Response(500, json={"error": "should not be called"})

    env = build_env(tmp_path, disabled=True)
    try:
        # Replace the null registry with one that would fail loudly if called.
        env.service.integrations = IntegrationRegistry(
            env.service.config, http_transport=httpx2.MockTransport(handler)
        )
        env.service.retrieval.integrations = env.service.integrations
        session = env.session()
        env.service.memory_add(namespace="shared", kind="observation", text="disabled probe", title="d")
        response = _search(env, session, "disabled probe")
        assert response.retrieval_backend == "fts5"
        assert response.semantic_used is False
        assert response.warnings == ()
        assert calls == []
    finally:
        env.close()


class BadEmbedding:
    name = "bad"
    model = "bad"
    dimensions = DIMENSIONS
    available = True

    def embed(self, texts: Any) -> list[list[float]]:
        return [[0.0, 0.0, 0.0] for _ in texts]

    def health(self) -> dict[str, Any]:
        return {"name": self.name, "available": True}


def test_dimension_mismatch_is_backend_unavailable(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    try:
        session = env.session()
        write = env.service.memory_add(
            namespace="shared", kind="observation", text="alpha dimension probe", title="dim"
        )
        env.service.index_rebuild()
        env.registry.embedding = BadEmbedding()
        response = _search(env, session, "alpha dimension probe")
        assert response.retrieval_backend == "fts5"
        assert response.semantic_used is False
        assert response.warnings == ("semantic backend unavailable: BACKEND_UNAVAILABLE",)
        assert write.entry.entry_id in [result.entry_id for result in response.results]
    finally:
        env.close()


def test_index_rebuild_marks_personal_skipped_for_remote_embeddings(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    try:
        env.service.memory_add(namespace="personal", kind="observation", text="personal private", title="p")
        env.registry.embedding = type(
            "RemoteEmbedding",
            (),
            {
                "name": "openai",
                "model": "remote",
                "dimensions": DIMENSIONS,
                "available": True,
                "embed": lambda self, texts: [[1.0] * DIMENSIONS for _ in texts],
                "health": lambda self: {"available": True},
            },
        )()
        env.service.config.integrations.embedding.provider = "openai"
        env.service.config.integrations.embedding.allow_private_text = False
        result = env.service.index_rebuild()
        assert result["skipped_private"] >= 1
    finally:
        env.close()


def test_vector_only_hits_are_upserted_by_rebuild(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    try:
        write = env.service.memory_add(
            namespace="shared", kind="observation", text="upsert shape probe", title="upsert"
        )
        result = env.service.index_rebuild()
        assert result["generation"] == env.registry.vector.generation_id
        assert result["indexed"] >= 1
        assert write.entry.entry_id in env.fake.entry_ids()
    finally:
        env.close()


def test_vector_item_payload_shape(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    try:
        write = env.service.memory_add(
            namespace="shared", kind="observation", text="payload shape probe", title="payload"
        )
        env.service.index_rebuild()
        collection = next(iter(env.fake.collections.values()))
        payload = collection["points"][point_id(write.entry.revision_id)]["payload"]
        assert set(payload) == {
            "entry_id",
            "revision_id",
            "namespace",
            "lifecycle",
            "evidence_class",
            "content_hash",
            "scope",
            "author_root_id",
        }
    finally:
        env.close()


def _open_restarted(
    config: Config,
    fake: FakeQdrant,
    *,
    registry: IntegrationRegistry | None = None,
) -> tuple[KnowledgeService, IntegrationRegistry, Database]:
    db = Database(config.storage.database)
    evidence = EvidenceStore(
        config.storage.evidence_dir,
        quota_bytes=8 * 1024 * 1024,
        max_object_bytes=1024 * 1024,
    )
    clock = Clock()
    built = registry or IntegrationRegistry(config, http_transport=httpx2.MockTransport(fake.handler))
    service = KnowledgeService(
        config,
        db,
        evidence,
        clock=clock,
        monotonic=clock,
        integrations=built,
    )
    return service, built, db


def test_bootstrap_adopts_ready_generation_after_restart(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    env.session()
    env.service.memory_add(
        namespace="shared",
        kind="observation",
        text="alpha semantic probe",
        title="alpha",
    )
    env.service.index_rebuild()
    generation = env.registry.vector.generation_id
    config = env.service.config
    fake = env.fake
    env.close()

    restarted, registry, db = _open_restarted(config, fake)
    restarted.bootstrap()
    try:
        assert registry.vector.available is True
        assert registry.vector.generation_id == generation
        registration = restarted.register(label="restart-reader", source="test")
        restarted_session = restarted.authenticate(registration.session_token)
        response = restarted.search(restarted_session, SearchRequest(query="alpha semantic probe"))
        assert response.retrieval_backend == "hybrid"
        assert response.semantic_used is True
        assert response.index_generation == generation
    finally:
        restarted.close()
        db.close()


def test_bootstrap_does_not_adopt_generation_with_different_embedding_model(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    env = build_env(tmp_path)
    env.service.memory_add(
        namespace="shared",
        kind="observation",
        text="alpha semantic probe",
        title="alpha",
    )
    env.service.index_rebuild()
    fake = env.fake
    config = env.service.config.model_copy(deep=True)
    env.close()

    config.integrations.embedding.provider = "openai"
    config.integrations.embedding.base_url = "https://api.example.invalid/v1"
    config.integrations.embedding.model = "different-model"
    config.integrations.embedding.allow_private_text = False

    restarted, registry, db = _open_restarted(config, fake)
    with caplog.at_level("WARNING"):
        restarted.bootstrap()
    try:
        assert registry.vector.available is False
        assert registry.vector.generation_id == ""
        assert "not adopted" in caplog.text
        registration = restarted.register(label="restart-reader", source="test")
        restarted_session = restarted.authenticate(registration.session_token)
        response = restarted.search(restarted_session, SearchRequest(query="alpha semantic probe"))
        assert response.retrieval_backend == "fts5"
        assert response.semantic_used is False
    finally:
        restarted.close()
        db.close()


class RemoteEmbedding:
    name = "openai"
    model = "remote-embed"
    dimensions = DIMENSIONS
    available = True

    def embed(self, texts: Any) -> list[list[float]]:
        return [[1.0 / math.sqrt(DIMENSIONS)] * DIMENSIONS for _ in texts]

    def health(self) -> dict[str, Any]:
        return {"name": self.name, "model": self.model, "available": True}


def test_remote_embeddings_do_not_gate_semantic_queries_on_allow_private_text(
    tmp_path: Path,
) -> None:
    env = build_env(tmp_path)
    try:
        env.registry.embedding = RemoteEmbedding()
        env.service.config.integrations.embedding.provider = "openai"
        env.service.config.integrations.embedding.allow_private_text = False
        session = env.session()
        env.service.memory_add(
            namespace="shared",
            kind="observation",
            text="alpha remote probe",
            title="alpha",
        )
        env.service.index_rebuild()
        response = _search(env, session, "alpha remote probe")
        assert response.retrieval_backend == "hybrid"
        assert response.semantic_used is True
        assert not any("allow_private_text" in warning for warning in response.warnings)
    finally:
        env.close()


def test_unavailable_semantic_index_returns_lexical_with_reindex_warning(tmp_path: Path) -> None:
    env = build_env(tmp_path)
    try:
        session = env.session()
        write = env.service.memory_add(
            namespace="shared",
            kind="observation",
            text="alpha unavailable index probe",
            title="unavailable",
        )
        assert env.registry.vector.available is False
        response = _search(env, session, "alpha unavailable index probe")
        assert response.retrieval_backend == "fts5"
        assert response.semantic_used is False
        assert response.warnings == (
            "semantic index unavailable: run knowlageMCP-gatewayctl integrations reindex",
        )
        assert write.entry.entry_id in [result.entry_id for result in response.results]
        assert env.fake.requests == 0
    finally:
        env.close()


def test_forget_survives_index_bookkeeping_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = build_env(tmp_path)
    try:
        write = env.service.memory_add(
            namespace="shared",
            kind="observation",
            text="alpha forget bookkeeping probe",
            title="forget",
        )
        env.service.index_rebuild()

        class BrokenDatabase:
            def transaction(self) -> Any:
                raise RuntimeError("bookkeeping is down")

        monkeypatch.setattr(env.service.retrieval, "db", BrokenDatabase())
        result = env.service.memory_forget(write.entry.entry_id)
        assert result["forgotten"] is True
        assert result["warning"] == "vector index cleanup failed: INTERNAL"
    finally:
        env.close()
