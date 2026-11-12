# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Qdrant vector adapter: a derived, rebuildable index over canonical entries.

The adapter owns no authority. Queries always carry a namespace filter, but the
caller re-applies the canonical ACL from SQLite before displaying anything. A
point payload only helps deduplicate and label a hit; it never decides
visibility or lifecycle. API keys are read from the environment by name on each
request and are never stored or logged.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from typing import Any

import httpx2

from ..config import VectorIntegrationConfig
from ..models import ErrorCode, ServiceError
from .base import VectorHit, VectorItem
from .http import JsonHttpClient

#: Namespace for deterministic point UUIDs. It keeps point ids stable across
#: processes so a delete never needs to search first.
_POINT_NAMESPACE = uuid.NAMESPACE_URL
#: Bound one upsert request. The index is derived, so chunking is harmless.
_UPSERT_BATCH = 256
#: Payload keys the adapter keeps. Anything else is dropped deliberately.
_PAYLOAD_KEYS = (
    "entry_id",
    "revision_id",
    "namespace",
    "lifecycle",
    "evidence_class",
    "content_hash",
    "scope",
    "author_root_id",
)


def point_id(revision_id: str) -> str:
    """Return the deterministic Qdrant point id for one revision."""
    return str(uuid.uuid5(_POINT_NAMESPACE, revision_id))


class QdrantVectorIndex:
    """Filtered vector search over a generation-specific Qdrant collection."""

    name = "qdrant"

    def __init__(
        self,
        cfg: VectorIntegrationConfig,
        *,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        self._cfg = cfg
        self._client = JsonHttpClient(
            cfg.url,
            cfg.timeout_s,
            cfg.api_key_env,
            transport=transport,
        )
        self.generation_id = ""
        self.available = False
        self._collection: str | None = None

    # -- identity --------------------------------------------------------
    @staticmethod
    def generation_id_for(
        cfg: VectorIntegrationConfig,
        model: str,
        dimensions: int,
    ) -> str:
        """Deterministic generation id for a backend/model/dimension triple."""
        material = f"{cfg.backend}|{model}|{dimensions}|{cfg.collection_prefix}".encode()
        return "gen_" + hashlib.sha256(material).hexdigest()[:16]

    def _collection_name(self, generation_id: str) -> str:
        return f"{self._cfg.collection_prefix}_{generation_id}"

    @staticmethod
    def _vector_size(data: Any) -> int | None:
        """Read the unnamed vector size from a collection-info response."""
        if not isinstance(data, dict):
            return None
        result = data.get("result")
        if not isinstance(result, dict):
            return None
        config = result.get("config")
        if not isinstance(config, dict):
            return None
        params = config.get("params")
        if not isinstance(params, dict):
            return None
        vectors = params.get("vectors")
        if not isinstance(vectors, dict):
            return None
        size = vectors.get("size")
        if isinstance(size, int) and not isinstance(size, bool):
            return size
        # Named vectors are not created by this adapter; accept a single entry
        # only so an unexpected shape cannot be mistaken for a match.
        sizes = [
            value.get("size")
            for value in vectors.values()
            if isinstance(value, dict) and isinstance(value.get("size"), int)
        ]
        if len(sizes) == 1 and not isinstance(sizes[0], bool):
            return sizes[0]
        return None

    # -- generation lifecycle -------------------------------------------
    def ensure_generation(self, model: str, dimensions: int) -> str:
        generation_id = self.generation_id_for(self._cfg, model, dimensions)
        collection = self._collection_name(generation_id)
        body = {"vectors": {"size": dimensions, "distance": self._cfg.distance}}
        try:
            self._client.request("PUT", f"/collections/{collection}", json=body)
        except ServiceError:
            # Qdrant answers 409 when the collection already exists with a
            # conflicting configuration. A successful GET proves the collection
            # is present, which is all this idempotent PUT needs.
            try:
                self._client.request("GET", f"/collections/{collection}")
            except ServiceError:
                raise
        self.generation_id = generation_id
        self._collection = collection
        self.available = True
        return generation_id

    def adopt_generation(self, generation_id: str) -> None:
        """Re-attach to a generation recorded by a previous process.

        The collection is probed instead of created. Any missing collection,
        malformed response, or dimension mismatch leaves the index unavailable;
        this is a best-effort restart path and never raises.
        """
        collection = self._collection_name(generation_id)
        self.generation_id = generation_id
        self._collection = collection
        self.available = False
        try:
            data = self._client.request("GET", f"/collections/{collection}")
            if self._vector_size(data) == int(self._cfg.vector_size):
                self.available = True
        except Exception:
            self.available = False

    # -- writes ----------------------------------------------------------
    @staticmethod
    def _payload(item: VectorItem) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for key in _PAYLOAD_KEYS:
            if key in item.payload:
                payload[key] = item.payload[key]
        payload["entry_id"] = item.entry_id
        payload["revision_id"] = item.revision_id
        payload["namespace"] = item.namespace
        return payload

    def upsert(self, items: Sequence[VectorItem]) -> None:
        if not self._collection:
            raise ServiceError(ErrorCode.BACKEND_UNAVAILABLE, "vector generation is not initialized")
        for start in range(0, len(items), _UPSERT_BATCH):
            chunk = items[start : start + _UPSERT_BATCH]
            points = [
                {
                    "id": point_id(item.revision_id),
                    "vector": list(item.vector),
                    "payload": self._payload(item),
                }
                for item in chunk
            ]
            self._client.request(
                "PUT",
                f"/collections/{self._collection}/points",
                json={"points": points},
                params={"wait": "true"},
            )

    def delete(self, entry_ids: Sequence[str]) -> None:
        self._delete_by("entry_id", entry_ids)

    def delete_revisions(self, revision_ids: Sequence[str]) -> None:
        self._delete_by("revision_id", revision_ids)

    def _delete_by(self, key: str, values: Sequence[str]) -> None:
        if not self._collection or not values:
            return
        body = {"filter": {"must": [{"key": key, "match": {"any": list(values)}}]}}
        self._client.request(
            "POST",
            f"/collections/{self._collection}/points/delete",
            json=body,
            params={"wait": "true"},
        )

    def update_lifecycle(self, entry_id: str, revision_id: str, lifecycle: str) -> None:
        if not self._collection:
            return
        must: list[dict[str, Any]] = [{"key": "entry_id", "match": {"value": entry_id}}]
        if revision_id:
            must.append({"key": "revision_id", "match": {"value": revision_id}})
        body = {"payload": {"lifecycle": lifecycle}, "filter": {"must": must}}
        self._client.request(
            "POST",
            f"/collections/{self._collection}/points/payload",
            json=body,
            params={"wait": "true"},
        )

    # -- search ----------------------------------------------------------
    def search(
        self,
        vector: Sequence[float],
        limit: int,
        namespaces: Sequence[str],
        allow_candidates: bool = False,
    ) -> list[VectorHit]:
        if not self._collection:
            return []
        must_not: list[dict[str, Any]] = [{"key": "lifecycle", "match": {"any": ["archived", "superseded"]}}]
        if not allow_candidates:
            must_not.append({"key": "lifecycle", "match": {"any": ["candidate"]}})
        body = {
            "vector": list(vector),
            "limit": limit,
            "with_payload": True,
            "filter": {
                "must": [{"key": "namespace", "match": {"any": list(namespaces)}}],
                "must_not": must_not,
            },
        }
        data = self._client.request(
            "POST",
            f"/collections/{self._collection}/points/search",
            json=body,
        )
        return self._parse_hits(data)

    @staticmethod
    def _parse_hits(data: Any) -> list[VectorHit]:
        if not isinstance(data, dict):
            return []
        raw_points = data.get("result")
        if not isinstance(raw_points, list):
            return []
        hits: list[VectorHit] = []
        for point in raw_points:
            if not isinstance(point, dict):
                continue
            payload = point.get("payload")
            if not isinstance(payload, dict):
                continue
            entry_id = payload.get("entry_id")
            revision_id = payload.get("revision_id")
            score = point.get("score")
            if not isinstance(entry_id, str) or not isinstance(revision_id, str):
                continue
            if not isinstance(score, int | float):
                continue
            # The point id must be the deterministic UUID of the payload
            # revision; a mismatch means the point is malformed.
            if str(point.get("id")) != point_id(revision_id):
                continue
            hits.append(VectorHit(entry_id=entry_id, revision_id=revision_id, score=float(score)))
        return hits

    # -- health and shutdown --------------------------------------------
    def health(self) -> dict[str, Any]:
        if not self._collection:
            return {"name": self.name, "available": bool(self.available), "generation": None}
        try:
            self._client.request("GET", f"/collections/{self._collection}")
        except ServiceError as exc:
            self.available = False
            return {
                "name": self.name,
                "available": False,
                "generation": self.generation_id,
                "error": exc.code.value,
            }
        except Exception:
            self.available = False
            return {
                "name": self.name,
                "available": False,
                "generation": self.generation_id,
                "error": ErrorCode.INTERNAL.value,
            }
        self.available = True
        return {"name": self.name, "available": True, "generation": self.generation_id}

    def close(self) -> None:
        self._client.close()


__all__ = ["QdrantVectorIndex", "point_id"]
