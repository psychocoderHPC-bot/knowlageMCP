# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Graphiti temporal-graph extraction adapter.

The adapter is proposal-only. It forwards bounded text to a configured
Graphiti-compatible HTTP endpoint and maps returned edges to
:class:`MemoryProposal` objects. It never writes to SQLite, publishes entries,
attaches confidence scores, or creates user instructions; the caller stores the
proposals as ``proposed`` and routes them through the normal review path.

A call sends exactly one HTTP request. Empty or whitespace-only input returns
an empty list without touching the network.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

import httpx2

from ..config import GraphitiIntegrationConfig
from ..models import ErrorCode, ServiceError
from .base import MemoryProposal
from .http import JsonHttpClient

#: Maximum bytes kept from any single string (request or response).
MAX_STRING_BYTES = 4096
#: Maximum characters kept in a generated title.
MAX_TITLE_CHARS = 200
#: Maximum number of edges converted per call.
MAX_EDGES = 32

#: Canonical, short role labels accepted from source metadata.
_ROLE_ALIASES = {
    "user": "user_message",
    "human": "user_message",
    "user_message": "user_message",
    "human_message": "user_message",
    "assistant": "agent_observation",
    "agent": "agent_observation",
    "agent_message": "agent_observation",
    "agent_observation": "agent_observation",
    "model": "agent_observation",
    "agent_reported_user": "agent_reported_user",
    "system": "system_message",
    "system_message": "system_message",
    "tool": "tool_output",
    "tool_output": "tool_output",
    "tool_result": "tool_output",
    "function": "tool_output",
    "operator": "operator",
    "operator_confirmed": "operator",
    "supporting": "supporting",
    "unknown": "unknown",
}

_ROLE_CLEAN_RE = re.compile(r"[^a-z0-9]+")


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


def _normalize_role(value: Any) -> str:
    """Map a source role to a short safe label, defaulting to ``unknown``."""
    if not isinstance(value, str):
        return "unknown"
    key = _ROLE_CLEAN_RE.sub("_", value.strip().casefold()).strip("_")
    if not key:
        return "unknown"
    return _ROLE_ALIASES.get(key, "unknown")


def _source_roles(sources: Sequence[Any]) -> list[str]:
    labels: list[str] = []
    for source in sources:
        role = source.get("role") if isinstance(source, Mapping) else getattr(source, "role", None)
        labels.append(_normalize_role(role))
    return labels


def _edge_proposal(edge: Any) -> MemoryProposal | None:
    """Convert one response edge, dropping anything that is not well formed."""
    if not isinstance(edge, Mapping):
        return None
    subject = _text(edge.get("subject"))
    relation = _text(edge.get("relation"))
    obj = _text(edge.get("object"))
    if subject is None or relation is None or obj is None:
        return None
    detail = _text(edge.get("detail"))
    sentence = f"{subject} {relation} {obj}"
    title = " ".join(sentence.split())[:MAX_TITLE_CHARS].rstrip()
    if not title:
        return None
    claim = _clamp(detail if detail is not None else sentence)
    return MemoryProposal(
        kind="observation",
        title=title,
        claim=claim,
        relation="extracted",
        subject=subject,
        object=obj,
        detail=detail,
        tags=("graphiti",),
        source_role="agent_observation",
    )


class GraphitiExtractor:
    """Proposal-only Graphiti edge extractor over a bounded HTTP endpoint."""

    name = "graphiti"

    def __init__(
        self,
        cfg: GraphitiIntegrationConfig,
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
        """Return proposed observations from one Graphiti edge-extraction call."""
        if not text.strip():
            return []
        if self._client is None:
            raise ServiceError(
                ErrorCode.BACKEND_UNAVAILABLE,
                "graphiti extractor is not configured with a base_url",
            )
        payload = {
            "text": _clamp(text),
            "task": _text(task),
            "source_roles": _source_roles(sources),
        }
        body = self._client.request("POST", "/extract", json=payload)
        return self._parse(body)

    def _parse(self, body: Any) -> list[MemoryProposal]:
        if isinstance(body, Mapping):
            edges = body.get("edges")
        elif isinstance(body, list):
            edges = body
        else:
            return []
        if not isinstance(edges, list):
            return []
        proposals: list[MemoryProposal] = []
        for edge in edges:
            proposal = _edge_proposal(edge)
            if proposal is None:
                continue
            proposals.append(proposal)
            if len(proposals) >= MAX_EDGES:
                break
        return proposals

    def health(self) -> dict[str, Any]:
        """Report configuration state only; no network probe is performed."""
        return {"name": self.name, "available": self.available}

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


__all__ = ["GraphitiExtractor"]
