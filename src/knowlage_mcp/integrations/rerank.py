# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Bounded rerankers over already authorized candidates.

``LexicalReranker`` is deterministic and offline. ``OpenAICompatibleReranker``
speaks the common ``/rerank`` JSON shape and fails with
``BACKEND_UNAVAILABLE`` when a provider returns nothing usable, which lets the
caller fall back to the lexical order.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx2

from ..config import RerankerIntegrationConfig
from ..models import ErrorCode, ServiceError
from .base import RerankCandidate
from .http import JsonHttpClient

_WORD_RE = re.compile(r"\w+", re.UNICODE)

#: Weights of the lexical overlap components. The result is divided by the
#: number of distinct query tokens, so a long query cannot inflate a score.
_TITLE_WEIGHT = 2.0
_CLAIM_WEIGHT = 1.0
_TAG_WEIGHT = 1.5
_SCOPE_WEIGHT = 0.5


def _tokenize(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.casefold()))


def _candidate_tags(candidate: RerankCandidate) -> tuple[str, ...]:
    return tuple(str(tag) for tag in candidate.tags)


def _lexical_score(query_tokens: set[str], candidate: RerankCandidate) -> float:
    if not query_tokens:
        return 0.0
    title_overlap = len(query_tokens & _tokenize(candidate.title))
    claim_overlap = len(query_tokens & _tokenize(candidate.claim))
    tag_overlap = len(query_tokens & _tokenize(" ".join(_candidate_tags(candidate))))
    scope_text = " ".join(str(value) for value in candidate.scope.values())
    scope_overlap = len(query_tokens & _tokenize(scope_text))
    weighted = (
        _TITLE_WEIGHT * title_overlap
        + _CLAIM_WEIGHT * claim_overlap
        + _TAG_WEIGHT * tag_overlap
        + _SCOPE_WEIGHT * scope_overlap
    )
    return weighted / len(query_tokens)


class LexicalReranker:
    """Deterministic token-overlap reranker; no network, no model."""

    name = "lexical"

    def __init__(self, cfg: RerankerIntegrationConfig) -> None:
        self._cfg = cfg
        self.available = True

    def rerank(self, query: str, candidates: Sequence[RerankCandidate], top_n: int) -> list[str]:
        limit = min(max(0, top_n), len(candidates))
        if limit <= 0:
            return []
        query_tokens = _tokenize(query)
        if not query_tokens:
            return [candidate.entry_id for candidate in candidates[:limit]]
        ranked = sorted(
            enumerate(candidates),
            key=lambda pair: (-_lexical_score(query_tokens, pair[1]), pair[0]),
        )
        return [candidate.entry_id for _, candidate in ranked[:limit]]

    def health(self) -> dict[str, Any]:
        return {"available": True, "provider": self.name}


@dataclass(frozen=True, slots=True)
class _Document:
    order: int
    entry_id: str


class OpenAICompatibleReranker:
    """Reranker speaking the common ``/rerank`` service shape."""

    name = "openai-compatible-reranker"

    def __init__(
        self,
        cfg: RerankerIntegrationConfig,
        *,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        self._cfg = cfg
        self.model = cfg.model
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

    def _parse(self, body: Any, documents: Sequence[_Document], top_n: int) -> list[str]:
        if not isinstance(body, Mapping):
            raise ServiceError(ErrorCode.BACKEND_UNAVAILABLE, "reranker returned no usable results")
        results = body.get("results")
        if not isinstance(results, list):
            raise ServiceError(ErrorCode.BACKEND_UNAVAILABLE, "reranker returned no usable results")

        by_index = {document.order: document.entry_id for document in documents}
        known_ids = {document.entry_id for document in documents}
        seen: set[str] = set()
        scored: list[tuple[str, float]] = []
        for item in results:
            if not isinstance(item, Mapping):
                continue
            raw_score = item.get("relevance_score")
            if raw_score is None:
                raw_score = item.get("score")
            if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
                continue
            score = float(raw_score)
            if not math.isfinite(score):
                continue
            entry_id: str | None = None
            index = item.get("index")
            if isinstance(index, int) and not isinstance(index, bool):
                entry_id = by_index.get(index)
            if entry_id is None:
                raw_id = item.get("id")
                if isinstance(raw_id, str) and raw_id in known_ids:
                    entry_id = raw_id
            if entry_id is None or entry_id in seen:
                continue
            seen.add(entry_id)
            scored.append((entry_id, score))

        if not scored:
            raise ServiceError(ErrorCode.BACKEND_UNAVAILABLE, "reranker returned no usable results")
        scored.sort(key=lambda pair: -pair[1])
        limit = min(max(0, top_n), len(documents))
        return [entry_id for entry_id, _ in scored[:limit]]

    def rerank(self, query: str, candidates: Sequence[RerankCandidate], top_n: int) -> list[str]:
        limit = min(max(0, top_n), len(candidates))
        if limit <= 0:
            return []
        if self._client is None:
            raise ServiceError(
                ErrorCode.BACKEND_UNAVAILABLE,
                "reranker is not configured; set a base_url to enable it",
            )

        bounded = list(candidates[: max(0, int(self._cfg.top_n))])
        documents = [
            _Document(order=order, entry_id=candidate.entry_id) for order, candidate in enumerate(bounded)
        ]
        payload = {
            "model": self.model,
            "query": query,
            "documents": [
                {"id": candidate.entry_id, "text": candidate.title + "\n" + candidate.claim}
                for candidate in bounded
            ],
            "top_n": min(top_n, len(bounded)),
        }
        body = self._client.request("POST", "/rerank", json=payload)
        return self._parse(body, documents, top_n)

    def health(self) -> dict[str, Any]:
        return {"available": self.available, "provider": self.name, "model": self.model}

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


__all__ = ["LexicalReranker", "OpenAICompatibleReranker"]
