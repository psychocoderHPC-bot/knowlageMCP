# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""OpenAI-compatible embedding adapter.

The adapter sends text only when explicitly configured (``base_url`` is set
and the operator selected the provider). It never inspects or logs the API
key; :class:`~knowlage_mcp.integrations.http.JsonHttpClient` reads it from the
named environment variable per request.

Requests are split into batches of ``batch_size`` inputs. Each batch is one
``POST`` to ``{base_url}/embeddings``; ``POST`` is not retried by the HTTP
helper, so a failed batch fails the call instead of multiplying provider work.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import httpx2

from ..config import EmbeddingIntegrationConfig
from ..models import ErrorCode, ServiceError
from .http import JsonHttpClient

#: Only these models accept the explicit ``dimensions`` request field. Sending
#: it to another model is rejected by some providers, so it is conditional.
_DIMENSIONS_MODEL_PREFIX = "text-embedding-3"


class OpenAICompatibleEmbeddingProvider:
    """Embedding backend speaking the OpenAI ``/embeddings`` JSON shape."""

    name = "openai-compatible"

    def __init__(
        self,
        cfg: EmbeddingIntegrationConfig,
        *,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        self._cfg = cfg
        self.model = cfg.model
        self.dimensions = cfg.dimensions
        self._base_url = cfg.base_url.rstrip("/")
        self.available = bool(self._base_url)
        self._client: JsonHttpClient | None = None
        if self.available:
            self._client = JsonHttpClient(
                self._base_url,
                cfg.timeout_s,
                api_key_env=cfg.api_key_env,
                transport=transport,
            )

    def _payload(self, batch: Sequence[str]) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": self.model, "input": list(batch)}
        if self.model.startswith(_DIMENSIONS_MODEL_PREFIX):
            payload["dimensions"] = self.dimensions
        return payload

    def _parse(self, body: Any, expected: int) -> list[list[float]]:
        if not isinstance(body, Mapping):
            raise ServiceError(
                ErrorCode.BACKEND_UNAVAILABLE,
                "embedding provider returned an invalid response",
            )
        data = body.get("data")
        if not isinstance(data, list):
            raise ServiceError(
                ErrorCode.BACKEND_UNAVAILABLE,
                "embedding provider returned an invalid response",
            )
        by_index: dict[int, list[float]] = {}
        for item in data:
            if not isinstance(item, Mapping):
                raise ServiceError(
                    ErrorCode.BACKEND_UNAVAILABLE,
                    "embedding provider returned an invalid response",
                )
            index = item.get("index")
            embedding = item.get("embedding")
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or not isinstance(embedding, list)
                or index in by_index
            ):
                raise ServiceError(
                    ErrorCode.BACKEND_UNAVAILABLE,
                    "embedding provider returned an invalid response",
                )
            vector: list[float] = []
            for value in embedding:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ServiceError(
                        ErrorCode.BACKEND_UNAVAILABLE,
                        "embedding provider returned an invalid response",
                    )
                vector.append(float(value))
            by_index[index] = vector

        if len(by_index) != expected or set(by_index) != set(range(expected)):
            raise ServiceError(
                ErrorCode.BACKEND_UNAVAILABLE,
                f"embedding count mismatch: expected {expected}, received {len(by_index)}",
            )
        for index in range(expected):
            length = len(by_index[index])
            if length != self.dimensions:
                raise ServiceError(
                    ErrorCode.BACKEND_UNAVAILABLE,
                    f"embedding dimension mismatch: expected {self.dimensions}, received {length}",
                )
        return [by_index[index] for index in range(expected)]

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if isinstance(texts, (str, bytes, bytearray)) or not isinstance(texts, Sequence):
            raise ServiceError(ErrorCode.INVALID_ARGUMENT, "texts must be a sequence of strings")
        if not texts:
            return []
        normalized: list[str] = []
        for text in texts:
            if not isinstance(text, str):
                raise ServiceError(ErrorCode.INVALID_ARGUMENT, "texts must be a sequence of strings")
            # A provider commonly rejects an empty input with HTTP 400. Replace
            # it with one space and keep the output count aligned with the input.
            normalized.append(text if text.strip() else " ")
        if self._client is None:
            raise ServiceError(
                ErrorCode.BACKEND_UNAVAILABLE,
                "embedding provider is not configured; set a base_url to enable it",
            )

        batch_size = max(1, int(self._cfg.batch_size))
        vectors: list[list[float]] = []
        for start in range(0, len(normalized), batch_size):
            batch = normalized[start : start + batch_size]
            body = self._client.request("POST", "/embeddings", json=self._payload(batch))
            vectors.extend(self._parse(body, len(batch)))
        return vectors

    def health(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "provider": self.name,
            "model": self.model,
            "dimensions": self.dimensions,
        }

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


__all__ = ["OpenAICompatibleEmbeddingProvider"]
