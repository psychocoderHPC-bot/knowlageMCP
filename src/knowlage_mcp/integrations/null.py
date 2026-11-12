# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Offline null implementations of the optional integration boundaries."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..models import ErrorCode, ServiceError
from .base import (
    MemoryProposal,
    RerankCandidate,
    VectorHit,
    VectorItem,
)


class NullEmbeddingProvider:
    """Reports unavailable and fails closed when called."""

    name = "null"
    model = "none"
    dimensions = 0
    available = False

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        del texts
        raise ServiceError(
            ErrorCode.BACKEND_UNAVAILABLE,
            "no embedding backend is configured; embeddings are optional and disabled by default",
        )

    def health(self) -> dict[str, Any]:
        return {"name": self.name, "model": self.model, "dimensions": self.dimensions, "available": False}


class NullVectorIndex:
    """A no-op derived index. Canonical retrieval stays in SQLite FTS5."""

    name = "null"
    generation_id = "null"
    available = False

    def ensure_generation(self, model: str, dimensions: int) -> str:
        del model, dimensions
        return self.generation_id

    def adopt_generation(self, generation_id: str) -> None:
        del generation_id

    def upsert(self, items: Sequence[VectorItem]) -> None:
        del items

    def delete(self, entry_ids: Sequence[str]) -> None:
        del entry_ids

    def update_lifecycle(self, entry_id: str, revision_id: str, lifecycle: str) -> None:
        del entry_id, revision_id, lifecycle

    def search(
        self,
        vector: Sequence[float],
        limit: int,
        namespaces: Sequence[str],
        allow_candidates: bool = False,
    ) -> list[VectorHit]:
        del vector, limit, namespaces, allow_candidates
        return []

    def health(self) -> dict[str, Any]:
        return {"name": self.name, "generation": self.generation_id, "available": False}


class NullReranker:
    """Keeps the incoming order; no external service is consulted."""

    name = "null"
    available = False

    def rerank(self, query: str, candidates: Sequence[RerankCandidate], top_n: int) -> list[str]:
        del query
        return [candidate.entry_id for candidate in candidates[:top_n]]

    def health(self) -> dict[str, Any]:
        return {"name": self.name, "available": False}


class NullMemoryExtractor:
    """Never invents memories from free text."""

    name = "null"
    available = False

    def extract(
        self,
        text: str,
        task: str | None = None,
        sources: Sequence[Any] = (),
    ) -> list[MemoryProposal]:
        del text, task, sources
        return []

    def health(self) -> dict[str, Any]:
        return {"name": self.name, "available": False}


class NullSearchIndex:
    """A no-op secondary index; the core FTS5 search remains authoritative."""

    name = "null"

    def index(
        self,
        entry_id: str,
        revision_id: str,
        *,
        title: str,
        claim: str,
        conditions: str | None = None,
        tags: Sequence[str] = (),
    ) -> None:
        del entry_id, revision_id, title, claim, conditions, tags

    def remove(self, entry_id: str) -> None:
        del entry_id

    def search(self, query: str, *, limit: int = 10) -> list[tuple[str, float]]:
        del query, limit
        return []

    def rebuild(self) -> None:
        return None


def optional_backend(name: str, config: Any = None) -> Any:
    """Return the null backend for ``name``.

    No optional backend ships in the core release, so every known integration
    name resolves to its offline implementation. Unknown names raise
    ``ValueError`` so configuration typos fail loudly instead of silently
    disabling a feature.
    """
    del config
    normalized = name.strip().lower().replace("-", "_")
    if normalized in {"embedding", "embeddings", "embed"}:
        return NullEmbeddingProvider()
    if normalized in {"extractor", "extraction", "memory", "memory_extractor"}:
        return NullMemoryExtractor()
    if normalized in {"search", "search_index", "index"}:
        return NullSearchIndex()
    if normalized in {"vector", "vector_index", "qdrant"}:
        return NullVectorIndex()
    if normalized in {"reranker", "rerank"}:
        return NullReranker()
    raise ValueError(f"unknown optional integration: {name!r}")


__all__ = [
    "NullEmbeddingProvider",
    "NullMemoryExtractor",
    "NullReranker",
    "NullSearchIndex",
    "NullVectorIndex",
    "optional_backend",
]
