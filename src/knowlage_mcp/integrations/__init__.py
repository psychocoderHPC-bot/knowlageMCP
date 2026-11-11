# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Optional integration protocol boundaries and bundled null implementations."""

from __future__ import annotations

from .base import (
    EmbeddingProvider,
    EvidenceStoreBackend,
    KnowledgeStore,
    MemoryExtractor,
    SearchIndex,
)
from .null import (
    NullEmbeddingProvider,
    NullMemoryExtractor,
    NullSearchIndex,
    optional_backend,
)

__all__ = [
    "EmbeddingProvider",
    "EvidenceStoreBackend",
    "KnowledgeStore",
    "MemoryExtractor",
    "NullEmbeddingProvider",
    "NullMemoryExtractor",
    "NullSearchIndex",
    "SearchIndex",
    "optional_backend",
]
