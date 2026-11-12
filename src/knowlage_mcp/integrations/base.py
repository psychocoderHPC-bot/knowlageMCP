# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Small runtime-checkable boundaries for optional integrations.

The core release needs none of these implementations. Canonical entries and
review decisions always stay in the gateway-controlled SQLite store; vector
indexes and extractors are derived views or proposals only.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..evidence import EvidenceRecord
from ..models import (
    EntryDetail,
    EntryPayload,
    ProposeRevisionRequest,
    SearchRequest,
    SearchResponse,
)


@runtime_checkable
class KnowledgeStore(Protocol):
    """Canonical entry storage. The SQLite-backed service is the reference."""

    def get_entry(self, entry_id: str, revision_id: str | None = None) -> EntryDetail | None: ...

    def record(
        self,
        namespace: str,
        payload: EntryPayload,
        *,
        author_agent_id: str,
        author_root_id: str,
    ) -> EntryDetail: ...

    def propose_revision(
        self,
        request: ProposeRevisionRequest,
        *,
        author_agent_id: str,
        author_root_id: str,
    ) -> EntryDetail: ...

    def search(self, request: SearchRequest) -> SearchResponse: ...


@runtime_checkable
class SearchIndex(Protocol):
    """Secondary full-text or vector index; rebuildable from canonical entries."""

    name: str

    def index(
        self,
        entry_id: str,
        revision_id: str,
        *,
        title: str,
        claim: str,
        conditions: str | None = None,
        tags: Sequence[str] = (),
    ) -> None: ...

    def remove(self, entry_id: str) -> None: ...

    def search(self, query: str, *, limit: int = 10) -> list[tuple[str, float]]: ...

    def rebuild(self) -> None: ...


@dataclass(frozen=True, slots=True)
class VectorItem:
    """One derived vector record. ``payload`` carries ACL/scope metadata."""

    entry_id: str
    revision_id: str
    namespace: str
    vector: tuple[float, ...]
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class VectorHit:
    """One vector search hit. Callers re-apply canonical ACLs before display."""

    entry_id: str
    revision_id: str
    score: float


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Optional embedding backend. Disabled by default."""

    name: str
    model: str
    dimensions: int
    available: bool

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...

    def health(self) -> dict[str, Any]: ...


@runtime_checkable
class VectorIndex(Protocol):
    """Derived vector index. Never authoritative for ACL or review state."""

    name: str
    generation_id: str
    available: bool

    def ensure_generation(self, model: str, dimensions: int) -> str: ...

    def adopt_generation(self, generation_id: str) -> None: ...

    def upsert(self, items: Sequence[VectorItem]) -> None: ...

    def delete(self, entry_ids: Sequence[str]) -> None: ...

    def update_lifecycle(self, entry_id: str, revision_id: str, lifecycle: str) -> None: ...

    def search(
        self,
        vector: Sequence[float],
        limit: int,
        namespaces: Sequence[str],
        allow_candidates: bool = False,
    ) -> list[VectorHit]: ...

    def health(self) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class RerankCandidate:
    """An ACL-filtered candidate offered to a bounded reranker."""

    entry_id: str
    revision_id: str
    title: str
    claim: str
    scope: dict[str, str]
    evidence_class: str
    tags: tuple[str, ...] = ()


@runtime_checkable
class Reranker(Protocol):
    """Optional reranker over already-authorized candidates."""

    name: str
    available: bool

    def rerank(self, query: str, candidates: Sequence[RerankCandidate], top_n: int) -> list[str]: ...

    def health(self) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class MemoryProposal:
    """A proposed memory. It is never promoted without the normal review path."""

    kind: str
    title: str
    claim: str
    conditions: str | None = None
    expected_result: str | None = None
    limitations: str | None = None
    tags: tuple[str, ...] = ()
    scope: dict[str, str] = field(default_factory=dict)
    relation: str | None = None
    subject: str | None = None
    object: str | None = None
    detail: str | None = None
    source_role: str = "unknown"


@runtime_checkable
class MemoryExtractor(Protocol):
    """Optional extraction backend. Output is proposal-only and disabled by default."""

    name: str
    available: bool

    def extract(
        self,
        text: str,
        task: str | None = None,
        sources: Sequence[Any] = (),
    ) -> list[MemoryProposal]: ...

    def health(self) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class IntegrationStatus:
    """Observable state of the optional adapters. Contains no secrets."""

    enabled: bool
    embedding: str
    embedding_available: bool
    vector: str
    vector_available: bool
    reranker: str
    reranker_available: bool
    extractor: str
    extractor_available: bool
    generation: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class EvidenceStoreBackend(Protocol):
    """Bounded blob storage for evidence objects."""

    def put(
        self,
        data: bytes,
        media_type: str,
        name: str | None = None,
        revision_id: str | None = None,
    ) -> EvidenceRecord: ...

    def get(self, evidence_id: str) -> bytes: ...

    def exists(self, evidence_id: str) -> bool: ...

    def total_bytes(self) -> int: ...

    def delete_unreferenced(
        self,
        conn: sqlite3.Connection,
        ledger: Iterable[tuple[str, str]] | None = None,
    ) -> int: ...


__all__ = [
    "EmbeddingProvider",
    "EvidenceStoreBackend",
    "IntegrationStatus",
    "KnowledgeStore",
    "MemoryExtractor",
    "MemoryProposal",
    "RerankCandidate",
    "Reranker",
    "SearchIndex",
    "VectorHit",
    "VectorIndex",
    "VectorItem",
]
