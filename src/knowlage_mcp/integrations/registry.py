# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Lazy construction of optional adapters from validated configuration.

Nothing here opens a network connection at import time. A registry built from
``integrations.enabled = false`` constructs only offline null adapters, so no
client with network capability exists while the layer is disabled.
"""

from __future__ import annotations

import contextlib
import importlib
from typing import Any

import httpx2

from ..config import Config, IntegrationsConfig
from ..models import ErrorCode, ServiceError
from .base import (
    EmbeddingProvider,
    IntegrationStatus,
    MemoryExtractor,
    Reranker,
    VectorIndex,
)
from .hash_embed import HashEmbeddingProvider
from .null import (
    NullEmbeddingProvider,
    NullMemoryExtractor,
    NullReranker,
    NullVectorIndex,
)


def _load_adapter(module_name: str, class_name: str, *args: Any, **kwargs: Any) -> Any:
    """Import an optional adapter module lazily.

    A missing module or class becomes ``BACKEND_UNAVAILABLE`` with a message
    that names the expected adapter. Import failures never leak a traceback or
    a configured secret to a caller.
    """
    try:
        module = importlib.import_module(f".{module_name}", __package__)
    except ModuleNotFoundError as exc:
        raise ServiceError(
            ErrorCode.BACKEND_UNAVAILABLE,
            f"optional integration module {module_name!r} is unavailable in this build",
        ) from exc
    adapter = getattr(module, class_name, None)
    if adapter is None:
        raise ServiceError(
            ErrorCode.BACKEND_UNAVAILABLE,
            f"optional integration module {module_name!r} does not provide {class_name!r}",
        )
    return adapter(*args, **kwargs)


class IntegrationRegistry:
    """Owns the optional adapter set for one configuration snapshot."""

    def __init__(
        self,
        config: Config | IntegrationsConfig,
        *,
        http_transport: httpx2.BaseTransport | None = None,
    ) -> None:
        self._config = config.integrations if isinstance(config, Config) else config
        self._enabled = bool(self._config.enabled)
        self._http_transport = http_transport
        if not self._enabled:
            self.embedding: EmbeddingProvider = NullEmbeddingProvider()
            self.vector: VectorIndex = NullVectorIndex()
            self.reranker: Reranker = NullReranker()
            self.extractor: MemoryExtractor = NullMemoryExtractor()
            return
        self.embedding = self._build_embedding()
        self.vector = self._build_vector()
        self.reranker = self._build_reranker()
        self.extractor = self._build_extractor()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _build_embedding(self) -> EmbeddingProvider:
        embedding = self._config.embedding
        if embedding.provider == "none":
            return NullEmbeddingProvider()
        if embedding.provider == "hash":
            return HashEmbeddingProvider(dimensions=embedding.dimensions)
        return _load_adapter(  # type: ignore[no-any-return]
            "embeddings",
            "OpenAICompatibleEmbeddingProvider",
            embedding,
            transport=self._http_transport,
        )

    def _build_vector(self) -> VectorIndex:
        vector = self._config.vector
        if vector.backend == "none":
            return NullVectorIndex()
        return _load_adapter(  # type: ignore[no-any-return]
            "qdrant",
            "QdrantVectorIndex",
            vector,
            transport=self._http_transport,
        )

    def _build_reranker(self) -> Reranker:
        reranker = self._config.reranker
        if reranker.provider == "none":
            return NullReranker()
        if reranker.provider == "lexical":
            return _load_adapter("rerank", "LexicalReranker", reranker)  # type: ignore[no-any-return]
        return _load_adapter(  # type: ignore[no-any-return]
            "rerank",
            "OpenAICompatibleReranker",
            reranker,
            transport=self._http_transport,
        )

    def _build_extractor(self) -> MemoryExtractor:
        if self._config.graphiti.enabled:
            return _load_adapter(  # type: ignore[no-any-return]
                "graphiti",
                "GraphitiExtractor",
                self._config.graphiti,
                transport=self._http_transport,
            )
        if self._config.mem0.enabled:
            return _load_adapter(  # type: ignore[no-any-return]
                "mem0",
                "Mem0Extractor",
                self._config.mem0,
                transport=self._http_transport,
            )
        return NullMemoryExtractor()

    def status(self) -> IntegrationStatus:
        """Return adapter names and availability without probing the network."""
        generation = None
        if self._enabled and self.vector.available:
            generation = self.vector.generation_id or None
        details: dict[str, Any] = {
            "embedding_model": getattr(self.embedding, "model", None),
            "vector_backend": self._config.vector.backend,
            "vector_top_k": self._config.vector.top_k,
            "vector_hybrid": self._config.vector.hybrid,
            "reranker_provider": self._config.reranker.provider,
            "graphiti_enabled": self._config.graphiti.enabled,
            "mem0_enabled": self._config.mem0.enabled,
        }
        if not self._enabled:
            details["reason"] = "integrations.enabled is false"
        return IntegrationStatus(
            enabled=self._enabled,
            embedding=self.embedding.name,
            embedding_available=bool(self.embedding.available),
            vector=self.vector.name,
            vector_available=bool(self.vector.available),
            reranker=self.reranker.name,
            reranker_available=bool(self.reranker.available),
            extractor=self.extractor.name,
            extractor_available=bool(self.extractor.available),
            generation=generation,
            details=details,
        )

    def probe(self) -> dict[str, dict[str, Any]]:
        """Health of the enabled adapters. A disabled registry performs no work."""
        if not self._enabled:
            return {}
        result: dict[str, dict[str, Any]] = {}
        adapters: dict[str, EmbeddingProvider | VectorIndex | Reranker | MemoryExtractor] = {
            "embedding": self.embedding,
            "vector": self.vector,
            "reranker": self.reranker,
            "extractor": self.extractor,
        }
        for role, adapter in adapters.items():
            try:
                result[role] = dict(adapter.health())
            except ServiceError as exc:
                result[role] = {"available": False, "error": exc.code.value}
            except Exception:
                result[role] = {"available": False, "error": ErrorCode.INTERNAL.value}
        return result

    def close(self) -> None:
        """Close every adapter that owns a client. Safe to call repeatedly."""
        for adapter in (self.embedding, self.vector, self.reranker, self.extractor):
            close = getattr(adapter, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    close()


__all__ = ["IntegrationRegistry"]
