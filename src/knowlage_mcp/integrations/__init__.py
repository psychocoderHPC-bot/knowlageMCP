# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Optional integration protocol boundaries and bundled implementations."""

from __future__ import annotations

from .base import (
    EmbeddingProvider,
    EvidenceStoreBackend,
    IntegrationStatus,
    KnowledgeStore,
    MemoryExtractor,
    MemoryProposal,
    RerankCandidate,
    Reranker,
    SearchIndex,
    VectorHit,
    VectorIndex,
    VectorItem,
)
from .hash_embed import HashEmbeddingProvider
from .http import JsonHttpClient
from .null import (
    NullEmbeddingProvider,
    NullMemoryExtractor,
    NullReranker,
    NullSearchIndex,
    NullVectorIndex,
    optional_backend,
)
from .registry import IntegrationRegistry

__all__ = [
    "EmbeddingProvider",
    "EvidenceStoreBackend",
    "HashEmbeddingProvider",
    "IntegrationRegistry",
    "IntegrationStatus",
    "JsonHttpClient",
    "KnowledgeStore",
    "MemoryExtractor",
    "MemoryProposal",
    "NullEmbeddingProvider",
    "NullMemoryExtractor",
    "NullReranker",
    "NullSearchIndex",
    "NullVectorIndex",
    "RerankCandidate",
    "Reranker",
    "SearchIndex",
    "VectorHit",
    "VectorIndex",
    "VectorItem",
    "optional_backend",
]
