# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Offline null implementations of the optional integration boundaries."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..models import EntryPayload, ErrorCode, ServiceError


class NullEmbeddingProvider:
    """Reports unavailable and fails closed when called."""

    name = "null"
    dimensions = 0
    available = False

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        del texts
        raise ServiceError(
            ErrorCode.BACKEND_UNAVAILABLE,
            "no embedding backend is configured; embeddings are optional and disabled by default",
        )


class NullMemoryExtractor:
    """Never invents memories from free text."""

    name = "null"

    def extract(self, text: str, *, task: str | None = None) -> list[EntryPayload]:
        del text, task
        return []


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
    raise ValueError(f"unknown optional integration: {name!r}")


__all__ = [
    "NullEmbeddingProvider",
    "NullMemoryExtractor",
    "NullSearchIndex",
    "optional_backend",
]
