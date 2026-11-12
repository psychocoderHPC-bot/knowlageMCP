# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Mem0 conversation-to-memory extraction adapter.

The adapter is proposal-only. It forwards one bounded conversation message to a
configured Mem0-compatible HTTP endpoint and maps returned memories to
:class:`MemoryProposal` objects. It never writes to SQLite, publishes entries,
attaches confidence scores, or sets lifecycle/evidence state; the caller stores
the proposals as ``proposed`` and forces ``candidate``/``unverified`` through the
normal review path. Original source roles are preserved when present.

A call sends exactly one HTTP request.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import httpx2

from ..config import Mem0IntegrationConfig
from ..models import ErrorCode, Kind, ServiceError
from .base import MemoryProposal
from .http import JsonHttpClient

#: Maximum bytes kept from any single string (request or response).
MAX_STRING_BYTES = 4096
#: Maximum number of tags or scope keys retained per proposal.
MAX_TAGS = 32
MAX_SCOPE_KEYS = 16

_VALID_KINDS = frozenset(kind.value for kind in Kind)


def _clamp(value: str, limit: int = MAX_STRING_BYTES) -> str:
    """Truncate ``value`` to at most ``limit`` UTF-8 bytes."""
    if len(value.encode("utf-8")) <= limit:
        return value
    return value.encode("utf-8")[:limit].decode("utf-8", "ignore")


def _text(value: Any) -> str | None:
    """Return a bounded, stripped string, or ``None`` when unusable."""
    if not isinstance(value, str):
        return None
    cleaned = _clamp(value).strip()
    return cleaned or None


def _source_role(source: Any) -> str | None:
    """Return one source's role exactly as supplied, bounded and stripped."""
    role = source.get("role") if isinstance(source, Mapping) else getattr(source, "role", None)
    if isinstance(role, str):
        cleaned = _clamp(role).strip()
        return cleaned or None
    return None


def _default_role(sources: Sequence[Any]) -> str:
    """Use the first source role that is present, otherwise ``unknown``."""
    for source in sources:
        role = _source_role(source)
        if role is not None:
            return role
    return "unknown"


def _kind(value: Any) -> str:
    """Validate a returned kind, falling back to ``observation``."""
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in _VALID_KINDS:
            return normalized
    return Kind.OBSERVATION.value


def _tags(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    tags: list[str] = []
    for entry in value:
        if isinstance(entry, str):
            cleaned = _text(entry)
            if cleaned is not None and cleaned not in tags:
                tags.append(cleaned)
        if len(tags) >= MAX_TAGS:
            break
    return tuple(tags)


def _scope(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    scope: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        if isinstance(raw_key, str) and isinstance(raw_value, str):
            key = _text(raw_key)
            item = _text(raw_value)
            if key is not None and item is not None:
                scope[key] = item
        if len(scope) >= MAX_SCOPE_KEYS:
            break
    return scope


def _memory_proposal(item: Any, default_role: str) -> MemoryProposal | None:
    """Convert one returned memory, dropping malformed items."""
    if not isinstance(item, Mapping):
        return None
    title = _text(item.get("title"))
    claim = _text(item.get("claim"))
    if title is None or claim is None:
        return None
    source_role = item.get("source_role")
    role = (_clamp(source_role).strip() or default_role) if isinstance(source_role, str) else default_role
    return MemoryProposal(
        kind=_kind(item.get("kind")),
        title=title,
        claim=claim,
        conditions=_text(item.get("conditions")),
        expected_result=_text(item.get("expected_result")),
        limitations=_text(item.get("limitations")),
        tags=_tags(item.get("tags")),
        scope=_scope(item.get("scope")),
        source_role=role,
    )


class Mem0Extractor:
    """Proposal-only Mem0 memory extractor over a bounded HTTP endpoint."""

    name = "mem0"

    def __init__(
        self,
        cfg: Mem0IntegrationConfig,
        *,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        self._cfg = cfg
        self.available = bool(cfg.base_url)
        self._client: JsonHttpClient | None = None
        if self.available:
            self._client = JsonHttpClient(
                cfg.base_url,
                cfg.timeout_s,
                api_key_env=cfg.api_key_env,
                transport=transport,
            )

    def extract(
        self,
        text: str,
        task: str | None = None,
        sources: Sequence[Any] = (),
    ) -> list[MemoryProposal]:
        """Return proposed memories from one Mem0 extraction call."""
        if self._client is None:
            raise ServiceError(
                ErrorCode.BACKEND_UNAVAILABLE,
                "mem0 extractor is not configured with a base_url",
            )
        role = _default_role(sources)
        payload = {
            "messages": [{"role": role, "content": _clamp(text)}],
            "task": _text(task),
            "max_items": self._cfg.max_items,
        }
        body = self._client.request("POST", "/memories/extract", json=payload)
        return self._parse(body, role)

    def _parse(self, body: Any, default_role: str) -> list[MemoryProposal]:
        if isinstance(body, Mapping):
            memories = body.get("memories")
        elif isinstance(body, list):
            memories = body
        else:
            return []
        if not isinstance(memories, list):
            return []
        limit = max(0, self._cfg.max_items)
        proposals: list[MemoryProposal] = []
        seen: set[tuple[str, str, str]] = set()
        for item in memories:
            proposal = _memory_proposal(item, default_role)
            if proposal is None:
                continue
            key = (proposal.kind, proposal.title, proposal.claim)
            if key in seen:
                continue
            seen.add(key)
            proposals.append(proposal)
            if len(proposals) >= limit:
                break
        return proposals

    def health(self) -> dict[str, Any]:
        """Report configuration state only; no network probe is performed."""
        return {"name": self.name, "available": self.available}

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


__all__ = ["Mem0Extractor"]
