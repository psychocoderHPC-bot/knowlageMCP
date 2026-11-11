# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Small runtime-checkable boundaries for optional integrations.

The core release needs none of these implementations. Canonical entries and
review decisions always stay in the gateway-controlled SQLite store.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from typing import Protocol, runtime_checkable

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


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Optional vector backend. Disabled by default."""

    name: str
    dimensions: int
    available: bool

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


@runtime_checkable
class MemoryExtractor(Protocol):
    """Optional LLM-assisted extraction. Disabled by default."""

    name: str

    def extract(self, text: str, *, task: str | None = None) -> list[EntryPayload]: ...


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
    "KnowledgeStore",
    "MemoryExtractor",
    "SearchIndex",
]
