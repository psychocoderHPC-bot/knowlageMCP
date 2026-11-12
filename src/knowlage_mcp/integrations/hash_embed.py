# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Deterministic feature-hashing embeddings for development and tests.

This provider is **not** a semantic model. It hashes lowercased word tokens and
character trigrams into a fixed-size vector with :func:`hashlib.blake2b`, so
identical text always produces the identical vector across processes, hosts,
and interpreter runs. Similar surface strings receive similar vectors; synonyms
and paraphrases do not. Use it to exercise the pipeline offline, never as a
quality benchmark for semantic retrieval.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterator, Sequence
from typing import Any

_WORD_RE = re.compile(r"\w+", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+", re.UNICODE)

#: Relative weight of a character trigram against a whole-word feature.
_TRIGRAM_WEIGHT = 0.5
_TOKEN_WEIGHT = 1.0


class HashEmbeddingProvider:
    """Deterministic, offline, L2-normalized feature hashing."""

    name = "hash"
    available = True

    def __init__(self, dimensions: int = 1536, model: str = "hash-blake2b-v1") -> None:
        if dimensions <= 0:
            raise ValueError("dimensions must be positive")
        self.dimensions = dimensions
        self.model = model

    def _features(self, text: str) -> Iterator[tuple[str, float]]:
        normalized = _WHITESPACE_RE.sub(" ", text.casefold()).strip()
        for token in _WORD_RE.findall(normalized):
            yield f"w:{token}", _TOKEN_WEIGHT
        for index in range(max(0, len(normalized) - 2)):
            yield f"c:{normalized[index : index + 3]}", _TRIGRAM_WEIGHT

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for feature, weight in self._features(text):
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=9).digest()
            slot = int.from_bytes(digest[:8], "big") % self.dimensions
            sign = 1.0 if digest[8] & 1 else -1.0
            vector[slot] += sign * weight
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return vector
        return [value / norm for value in vector]

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def health(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "model": self.model,
            "dimensions": self.dimensions,
            "available": True,
        }


__all__ = ["HashEmbeddingProvider"]
