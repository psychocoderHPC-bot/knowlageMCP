# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""ACL-first FTS5 retrieval, bounded context, cursor paging, and entry detail."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any, TypeVar

from .config import Config
from .db import Database, iso, parse_iso
from .integrations.base import RerankCandidate, VectorHit, VectorItem
from .integrations.registry import IntegrationRegistry
from .knowledge import build_entry_detail
from .models import (
    ContextBundle,
    EntryDetail,
    ErrorCode,
    EvidenceClass,
    Kind,
    Lifecycle,
    Scope,
    SearchFilters,
    SearchRequest,
    SearchResponse,
    SearchResult,
    ServiceError,
    SessionInfo,
)
from .policy import PERSONAL_KIND, namespace_kind, now_from, readable_namespaces, require_read

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_QUOTED_RE = re.compile(r'"([^"]+)"')
_MAX_LIKE_TERMS = 16
#: Bound the candidate pool fed to ranking. Ranking is cheap over a few dozen
#: rows; the expensive part is materializing every FTS/LIKE match first.
_MAX_CANDIDATE_POOL = 100
#: Reciprocal-rank-fusion constant. The standardized value damps the top ranks
#: so a lexical and a vector hit agree on ordering without either dominating.
_RRF_K = 60
#: Keep provenance lookups under SQLite's default parameter limit.
_CONTRADICTION_CHUNK = 400

_LIFECYCLE_RANK = {
    Lifecycle.PUBLISHED: 0,
    Lifecycle.DISPUTED: 1,
    Lifecycle.CANDIDATE: 2,
    Lifecycle.SUPERSEDED: 3,
    Lifecycle.ARCHIVED: 4,
}
_EVIDENCE_STRENGTH = {
    EvidenceClass.UNVERIFIED: 1,
    EvidenceClass.SOURCE_CHECKED: 2,
    EvidenceClass.REPRODUCED: 3,
    EvidenceClass.OPERATOR_CONFIRMED: 4,
}

#: A result may state at most this many selection reasons. The set is a short
#: explanation, not a ranking trace.
_MAX_SELECTION_REASONS = 5

_ResponseT = TypeVar("_ResponseT", SearchResponse, ContextBundle)


def _bounded_reasons(reasons: Iterable[str], *, limit: int = _MAX_SELECTION_REASONS) -> tuple[str, ...]:
    return tuple(dict.fromkeys(reasons))[:limit]


def _identifier_tokens(value: str) -> set[str]:
    """Casefold identifier-shaped tokens: punctuation or letters plus digits."""
    found: set[str] = set()
    for match in re.finditer(r"[A-Za-z_][A-Za-z0-9_.:/\\-]*", value):
        token = match.group(0)
        has_marker = any(char in token for char in "_.:/\\-")
        mixed = any(char.isalpha() for char in token) and any(char.isdigit() for char in token)
        if has_marker or mixed:
            found.add(token.casefold())
    return found


def _exact_identifier_match(query: str, candidate: dict[str, Any]) -> bool:
    wanted = _identifier_tokens(query)
    if not wanted:
        return False
    haystack = f"{candidate['title']} {candidate['claim']}"
    return bool(wanted & _identifier_tokens(haystack))


def _with_token_estimate(model: _ResponseT) -> _ResponseT:
    """Attach a byte-derived token estimate fixed to the serialized payload.

    Iterating once or twice makes the number consistent with the payload that
    actually includes ``estimated_tokens``; it is an estimate, not a tokenizer
    count, and the response says so in ``token_estimate_method``.
    """
    estimate = 0
    candidate = model
    for _ in range(4):
        candidate = model.model_copy(update={"estimated_tokens": estimate})
        payload = json.dumps(
            candidate.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        next_estimate = (len(payload) + 3) // 4
        if next_estimate == estimate:
            return candidate
        estimate = next_estimate
    return candidate.model_copy(update={"estimated_tokens": estimate})


def tokenize(query: str) -> list[str]:
    """Split user input into plain word tokens; never pass raw FTS syntax."""
    return [match.group(0) for match in _TOKEN_RE.finditer(query) if match.group(0)]


def fts_query(query: str, *, exact_phrase: bool = False) -> str | None:
    """Build a safe FTS5 MATCH expression from quoted tokens."""
    stripped = query.strip()
    if not stripped:
        return None
    if exact_phrase:
        return '"' + stripped.replace('"', '""') + '"'
    quoted = _QUOTED_RE.search(stripped)
    if quoted is not None:
        # A caller-quoted fragment is treated as an exact phrase.
        return '"' + quoted.group(1).replace('"', '""') + '"'
    tokens = tokenize(stripped)
    if not tokens:
        return None
    return " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)


def like_pattern(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _cursor_encode(offset: int, query_hash: str) -> str:
    payload = json.dumps({"offset": offset, "q": query_hash}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode("ascii")


def _cursor_decode(cursor: str, query_hash: str) -> int:
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
        offset = int(payload["offset"])
        stored_hash = str(payload["q"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ServiceError(ErrorCode.INVALID_ARGUMENT, "invalid search cursor") from exc
    if not isinstance(offset, int) or offset < 0:
        raise ServiceError(ErrorCode.INVALID_ARGUMENT, "invalid search cursor")
    if stored_hash != query_hash:
        raise ServiceError(ErrorCode.INVALID_ARGUMENT, "cursor does not match this query")
    return offset


def _row_to_candidate(row: sqlite3.Row) -> dict[str, Any]:
    tags_raw = row["tags"]
    tags: list[str] = []
    if tags_raw:
        try:
            parsed = json.loads(str(tags_raw))
            if isinstance(parsed, list):
                tags = [str(item) for item in parsed]
        except (TypeError, ValueError):
            tags = []
    scope_raw: dict[str, Any] = {}
    if row["scope"]:
        try:
            parsed_scope = json.loads(str(row["scope"]))
            if isinstance(parsed_scope, dict):
                scope_raw = parsed_scope
        except (TypeError, ValueError):
            scope_raw = {}
    rank = row["rank"]
    return {
        "entry_id": str(row["entry_id"]),
        "namespace": str(row["namespace"]),
        "revision_id": str(row["revision_id"]),
        "kind": Kind(str(row["kind"])),
        "lifecycle": Lifecycle(str(row["lifecycle"])),
        "evidence_class": EvidenceClass(str(row["evidence_class"])),
        "title": str(row["title"]),
        "claim": str(row["claim"]),
        "tags": tuple(tags),
        "scope": Scope.model_validate(scope_raw),
        "author_agent_id": str(row["author_agent_id"]),
        "author_root_id": str(row["author_root_id"]),
        "contributors": str(row["contributors"] or "[]"),
        "content_hash": str(row["content_hash"]),
        "created_at": parse_iso(str(row["created_at"])),
        "rank": float(rank) if rank is not None else None,
    }


class RetrievalManager:
    """Retrieval is ACL-first; ranking never widens visibility."""

    def __init__(
        self,
        config: Config,
        db: Database,
        *,
        clock: Callable[[], float],
        integrations: IntegrationRegistry | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self._clock = clock
        self.integrations = integrations if integrations is not None else IntegrationRegistry(config)

    def _now(self) -> datetime:
        return now_from(self._clock)

    # -- candidate gathering --------------------------------------------
    def _namespaces_for(self, session: SessionInfo, requested: str | None) -> list[str]:
        at = self._now()
        conn = self.db.connect()
        readable = readable_namespaces(conn, session.agent_id, at)
        if requested is not None:
            if requested not in readable:
                raise ServiceError(ErrorCode.FORBIDDEN, f"not authorized to read namespace {requested!r}")
            return [requested]
        return sorted(readable)

    def _fetch_candidates(
        self,
        namespaces: list[str],
        query: str,
        *,
        exact_phrase: bool,
        limit: int,
    ) -> dict[str, dict[str, Any]]:
        """Return a bounded candidate pool, ACL-filtered by namespace already.

        FTS rows arrive best-bm25-first so the cap keeps the most relevant
        lexical matches. LIKE is only a fallback for tokens that FTS tokenizes
        poorly (long identifiers and punctuation) and is capped the same way.
        """
        if not namespaces:
            return {}
        placeholders = ",".join("?" for _ in namespaces)
        conn = self.db.connect()
        candidates: dict[str, dict[str, Any]] = {}
        match = fts_query(query, exact_phrase=exact_phrase)
        if match is not None:
            try:
                rows = conn.execute(
                    f"""
                    SELECT e.entry_id, e.namespace, r.revision_id, r.kind, r.lifecycle,
                           r.evidence_class, r.title, r.claim, r.tags, r.scope, r.author_agent_id,
                           r.author_root_id, r.contributors, r.content_hash, r.created_at,
                           bm25(entry_fts) AS rank
                    FROM entry_fts
                    JOIN entry_revisions r ON r.revision_id = entry_fts.revision_id
                    JOIN entries e ON e.current_revision_id = r.revision_id
                    WHERE entry_fts MATCH ? AND e.namespace IN ({placeholders})
                    ORDER BY bm25(entry_fts)
                    LIMIT ?
                    """,
                    (match, *namespaces, limit),
                ).fetchall()
            except sqlite3.OperationalError as exc:
                raise ServiceError(ErrorCode.INVALID_ARGUMENT, "search query is not valid") from exc
            for row in rows:
                entry_id = str(row["entry_id"])
                if entry_id not in candidates:
                    candidates[entry_id] = _row_to_candidate(row)

        stripped = " ".join(query.split())
        if stripped:
            terms = [stripped]
            if not exact_phrase:
                for token in tokenize(stripped):
                    if len(token) >= 12 or any(char in token for char in "_.:/\\-"):
                        terms.append(token)
            unique_terms = list(dict.fromkeys(terms))[:_MAX_LIKE_TERMS]
            conditions = " OR ".join(
                "(r.title LIKE ? ESCAPE '\\' OR r.claim LIKE ? ESCAPE '\\')" for _ in unique_terms
            )
            params: list[Any] = []
            for term in unique_terms:
                pattern = like_pattern(term)
                params.extend((pattern, pattern))
            rows = conn.execute(
                f"""
                SELECT e.entry_id, e.namespace, r.revision_id, r.kind, r.lifecycle,
                       r.evidence_class, r.title, r.claim, r.tags, r.scope, r.author_agent_id,
                       r.author_root_id, r.contributors, r.content_hash, r.created_at,
                       NULL AS rank
                FROM entries e
                JOIN entry_revisions r ON r.revision_id = e.current_revision_id
                WHERE e.namespace IN ({placeholders}) AND ({conditions})
                ORDER BY r.created_at DESC
                LIMIT ?
                """,
                (*namespaces, *params, limit),
            ).fetchall()
            for row in rows:
                entry_id = str(row["entry_id"])
                # FTS already supplied a scored row for this entry.
                if entry_id not in candidates:
                    candidates[entry_id] = _row_to_candidate(row)

        if not stripped:
            rows = conn.execute(
                f"""
                SELECT e.entry_id, e.namespace, r.revision_id, r.kind, r.lifecycle,
                       r.evidence_class, r.title, r.claim, r.tags, r.scope, r.author_agent_id,
                       r.author_root_id, r.contributors, r.content_hash, r.created_at,
                       NULL AS rank
                FROM entries e
                JOIN entry_revisions r ON r.revision_id = e.current_revision_id
                WHERE e.namespace IN ({placeholders})
                ORDER BY r.created_at DESC
                LIMIT ?
                """,
                (*namespaces, limit),
            ).fetchall()
            for row in rows:
                entry_id = str(row["entry_id"])
                if entry_id not in candidates:
                    candidates[entry_id] = _row_to_candidate(row)
        return candidates

    # -- ranking ---------------------------------------------------------
    def _passes_filters(self, candidate: dict[str, Any], filters: SearchFilters) -> bool:
        if candidate["kind"] not in filters.kinds and filters.kinds:
            return False
        if filters.author_agent_id and candidate["author_agent_id"] != filters.author_agent_id:
            return False
        if filters.tags and not set(filters.tags).issubset(set(candidate["tags"])):
            return False
        if filters.evidence_class is not None:
            candidate_strength = _EVIDENCE_STRENGTH[candidate["evidence_class"]]
            if candidate_strength < _EVIDENCE_STRENGTH[filters.evidence_class]:
                return False
        return True

    @staticmethod
    def _applicability(candidate_scope: Scope, query_scope: Scope) -> int:
        """Scope applicability against the query, never against field count.

        0: the query is unconstrained, or the entry scope satisfies it.
        1: the entry leaves a required dimension unknown.
        2: the entry names a different value for a required dimension.

        Ranking never treats an unknown scope as universal applicability.
        Compatibility is enforced before ranking (see :meth:`_rank`), so only
        bucket 0 reaches the sort today; the buckets stay explicit so the
        ordering cannot silently drift back to a scope-field count.
        """
        if query_scope.is_empty() or candidate_scope.satisfies(query_scope):
            return 0
        if candidate_scope.is_empty():
            return 1
        return 2

    @staticmethod
    def _lifecycle_allowed(filters: SearchFilters, include_candidates: bool) -> set[Lifecycle]:
        if filters.lifecycle:
            return set(filters.lifecycle)
        allowed = {Lifecycle.PUBLISHED, Lifecycle.DISPUTED}
        if include_candidates:
            allowed.add(Lifecycle.CANDIDATE)
        return allowed

    def _contradictions_for(self, revision_ids: Iterable[str]) -> dict[str, tuple[str, ...]]:
        """Batch provenance lookups for every candidate revision.

        One query covers the normal candidate pool; larger pools are split only
        to stay under SQLite's default bound-parameter limit.
        """
        unique = list(dict.fromkeys(revision_ids))
        found: dict[str, list[str]] = {revision_id: [] for revision_id in unique}
        if not unique:
            return {}
        conn = self.db.connect()
        for start in range(0, len(unique), _CONTRADICTION_CHUNK):
            chunk = unique[start : start + _CONTRADICTION_CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"""
                SELECT from_revision_id, to_revision_id FROM provenance_edges
                WHERE relation = 'contradicts'
                  AND (from_revision_id IN ({placeholders}) OR to_revision_id IN ({placeholders}))
                """,
                (*chunk, *chunk),
            ).fetchall()
            for row in rows:
                left = str(row["from_revision_id"])
                right = str(row["to_revision_id"])
                if left == right:
                    continue
                if left in found and right not in found[left]:
                    found[left].append(right)
                if right in found and left not in found[right]:
                    found[right].append(left)
        return {revision_id: tuple(others) for revision_id, others in found.items()}

    def _rank(
        self,
        candidates: Iterable[dict[str, Any]],
        filters: SearchFilters,
        include_candidates: bool,
        *,
        query: str,
    ) -> list[SearchResult]:
        candidate_list = list(candidates)
        allowed = self._lifecycle_allowed(filters, include_candidates)
        compatible: list[tuple[dict[str, Any], int]] = []
        for candidate in candidate_list:
            lifecycle = candidate["lifecycle"]
            if lifecycle not in allowed:
                continue
            if not self._passes_filters(candidate, filters):
                continue
            bucket = self._applicability(candidate["scope"], filters.scope)
            if bucket != 0:
                # Unknown (1) or conflicting (2) scopes are excluded before
                # ranking: an unconstrained entry is not universally applicable
                # to a query that names a scope dimension.
                continue
            compatible.append((candidate, bucket))

        contradictions_by_revision = self._contradictions_for(
            candidate["revision_id"] for candidate, _ in compatible
        )
        by_entry: dict[str, dict[str, Any]] = {}
        applicability: dict[str, int] = {}
        results: list[SearchResult] = []
        for candidate, bucket in compatible:
            lifecycle = candidate["lifecycle"]
            contradictions = contradictions_by_revision.get(candidate["revision_id"], ())
            warnings: list[str] = []
            if contradictions:
                warnings.extend(f"contradicts revision {other}" for other in contradictions)
            if lifecycle == Lifecycle.DISPUTED:
                warnings.append("disputed: credible conflicting evidence exists")
            if lifecycle == Lifecycle.CANDIDATE:
                warnings.append("low trust: candidate has not been reviewed")
            by_entry[candidate["entry_id"]] = candidate
            applicability[candidate["entry_id"]] = bucket
            reasons: list[str] = []
            if lifecycle == Lifecycle.PUBLISHED:
                reasons.append("published")
            if not filters.scope.is_empty():
                reasons.append("scope matches query")
            if _exact_identifier_match(query, candidate):
                reasons.append("exact identifier match")
            if candidate["rank"] is not None:
                reasons.append("fts match")
            if candidate["evidence_class"] is not EvidenceClass.UNVERIFIED:
                reasons.append(f"evidence: {candidate['evidence_class'].value}")
            if contradictions:
                reasons.append("contradiction visible")
            if lifecycle == Lifecycle.CANDIDATE:
                reasons.append("candidate low trust")
            results.append(
                SearchResult(
                    entry_id=candidate["entry_id"],
                    revision_id=candidate["revision_id"],
                    namespace=candidate["namespace"],
                    kind=candidate["kind"],
                    lifecycle=lifecycle,
                    evidence_class=candidate["evidence_class"],
                    title=candidate["title"],
                    claim=candidate["claim"],
                    tags=candidate["tags"],
                    scope=candidate["scope"],
                    score=-(candidate["rank"] or 0.0),
                    updated_at=candidate["created_at"],
                    low_trust=lifecycle == Lifecycle.CANDIDATE,
                    warnings=tuple(warnings),
                    contradictions=contradictions,
                    selection_reasons=_bounded_reasons(reasons),
                )
            )

        def bm25_key(entry_id: str) -> float:
            # bm25() is lower-is-better (negative); LIKE-only rows sort last.
            rank = by_entry[entry_id]["rank"]
            return float(rank) if rank is not None else float("inf")

        results.sort(
            key=lambda item: (
                _LIFECYCLE_RANK.get(item.lifecycle, 9),
                applicability[item.entry_id],
                bm25_key(item.entry_id),
                -_EVIDENCE_STRENGTH[item.evidence_class],
                -(item.updated_at.timestamp() if item.updated_at is not None else 0.0),
            )
        )

        kept: list[SearchResult] = []
        seen: dict[str, SearchResult] = {}
        for item in results:
            candidate = by_entry[item.entry_id]
            lineage_key = "|".join(
                (
                    candidate["namespace"],
                    " ".join(item.claim.split()).lower(),
                    json.dumps(candidate["scope"].model_dump(exclude_none=True), sort_keys=True),
                )
            )
            if lineage_key in seen:
                primary = seen[lineage_key]
                note = f"duplicate lineage merged into {primary.entry_id}"
                updated = primary.model_copy(
                    update={
                        "warnings": (*primary.warnings, note),
                        "selection_reasons": _bounded_reasons(
                            (*primary.selection_reasons, "duplicate lineage merged")
                        ),
                    }
                )
                seen[lineage_key] = updated
                index = kept.index(primary)
                kept[index] = updated
                continue
            seen[lineage_key] = item
            kept.append(item)
        return kept

    # -- public search ---------------------------------------------------
    @staticmethod
    def _rrf(rank: int) -> float:
        return 1.0 / (_RRF_K + rank)

    def _readable_for(self, session: SessionInfo) -> set[str]:
        conn = self.db.connect()
        return set(readable_namespaces(conn, session.agent_id, self._now()))

    def _hydrate_vector_hits(
        self,
        hits: Iterable[VectorHit],
        *,
        filters: SearchFilters,
        include_candidates: bool,
        readable: set[str],
        query: str,
    ) -> list[SearchResult]:
        ordered = list(hits)
        revision_ids = list(dict.fromkeys(hit.revision_id for hit in ordered))
        if not revision_ids:
            return []
        placeholders = ",".join("?" for _ in revision_ids)
        conn = self.db.connect()
        rows = conn.execute(
            f"""
            SELECT e.entry_id, e.namespace, e.current_revision_id, r.revision_id, r.kind,
                   r.lifecycle, r.evidence_class, r.title, r.claim, r.tags, r.scope,
                   r.author_agent_id, r.author_root_id, r.contributors, r.content_hash,
                   r.created_at, NULL AS rank
            FROM entry_revisions r
            JOIN entries e ON e.entry_id = r.entry_id
            WHERE r.revision_id IN ({placeholders})
            """,
            tuple(revision_ids),
        ).fetchall()
        by_revision = {str(row["revision_id"]): row for row in rows}
        allowed = self._lifecycle_allowed(filters, include_candidates)
        accepted: list[tuple[dict[str, Any], VectorHit]] = []
        for hit in ordered:
            row = by_revision.get(hit.revision_id)
            if row is None:
                continue
            # The canonical row is authoritative; a payload that disagrees with
            # SQLite is treated as a malformed point.
            if str(row["entry_id"]) != hit.entry_id:
                continue
            namespace = str(row["namespace"])
            if namespace not in readable:
                continue
            if str(row["revision_id"]) != str(row["current_revision_id"]):
                continue
            candidate = _row_to_candidate(row)
            if candidate["lifecycle"] not in allowed:
                continue
            if not self._passes_filters(candidate, filters):
                continue
            if self._applicability(candidate["scope"], filters.scope) != 0:
                continue
            accepted.append((candidate, hit))

        contradictions_by_revision = self._contradictions_for(
            candidate["revision_id"] for candidate, _hit in accepted
        )
        results: list[SearchResult] = []
        seen: set[str] = set()
        for candidate, _hit in accepted:
            if candidate["entry_id"] in seen:
                continue
            seen.add(candidate["entry_id"])
            lifecycle = candidate["lifecycle"]
            contradictions = contradictions_by_revision.get(candidate["revision_id"], ())
            warnings: list[str] = ["semantic match"]
            warnings.extend(f"contradicts revision {other}" for other in contradictions)
            if lifecycle == Lifecycle.DISPUTED:
                warnings.append("disputed: credible conflicting evidence exists")
            if lifecycle == Lifecycle.CANDIDATE:
                warnings.append("low trust: candidate has not been reviewed")
            reasons: list[str] = ["semantic match"]
            if lifecycle == Lifecycle.PUBLISHED:
                reasons.append("published")
            if not filters.scope.is_empty():
                reasons.append("scope matches query")
            if _exact_identifier_match(query, candidate):
                reasons.append("exact identifier match")
            if candidate["evidence_class"] is not EvidenceClass.UNVERIFIED:
                reasons.append(f"evidence: {candidate['evidence_class'].value}")
            if contradictions:
                reasons.append("contradiction visible")
            if lifecycle == Lifecycle.CANDIDATE:
                reasons.append("candidate low trust")
            results.append(
                SearchResult(
                    entry_id=candidate["entry_id"],
                    revision_id=candidate["revision_id"],
                    namespace=candidate["namespace"],
                    kind=candidate["kind"],
                    lifecycle=lifecycle,
                    evidence_class=candidate["evidence_class"],
                    title=candidate["title"],
                    claim=candidate["claim"],
                    tags=candidate["tags"],
                    scope=candidate["scope"],
                    score=0.0,
                    updated_at=candidate["created_at"],
                    low_trust=lifecycle == Lifecycle.CANDIDATE,
                    warnings=tuple(warnings),
                    contradictions=contradictions,
                    selection_reasons=_bounded_reasons(reasons),
                )
            )
        return results

    def _semantic_merge(
        self,
        *,
        session: SessionInfo,
        namespaces: list[str],
        query: str,
        filters: SearchFilters,
        include_candidates: bool,
        ranked: list[SearchResult],
        max_results: int,
    ) -> tuple[list[SearchResult], dict[str, Any]]:
        """Merge lexical and vector candidates. Returns a warning on skip."""
        meta: dict[str, Any] = {
            "retrieval_backend": "fts5",
            "semantic_used": False,
            "reranked": False,
            "index_generation": None,
            "warnings": (),
        }
        if not self.integrations.enabled:
            return ranked, meta
        vector = self.integrations.vector
        if self.config.integrations.vector.backend != "none" and not vector.available:
            meta.update(
                {
                    "retrieval_backend": "fts5",
                    "semantic_used": False,
                    "warnings": (
                        "semantic index unavailable: run knowlageMCP-gatewayctl integrations reindex",
                    ),
                }
            )
            return ranked, meta
        if not self.config.integrations.vector.hybrid:
            return ranked, meta
        embedding = self.integrations.embedding
        if not embedding.available or not vector.available:
            return ranked, meta

        vectors = embedding.embed([query])
        if not vectors:
            raise ServiceError(ErrorCode.BACKEND_UNAVAILABLE, "embedding returned no vector")
        vector_values = list(vectors[0])
        expected = int(self.config.integrations.vector.vector_size)
        if len(vector_values) != expected:
            raise ServiceError(
                ErrorCode.BACKEND_UNAVAILABLE,
                "embedding dimension does not match the vector index",
            )
        readable = self._readable_for(session)
        names = [namespace for namespace in namespaces if namespace in readable]
        if not names:
            return ranked, meta
        limit = min(max(self.config.integrations.vector.top_k, 4 * max_results), 100)
        hits = vector.search(
            vector_values,
            limit=limit,
            namespaces=names,
            allow_candidates=include_candidates,
        )
        vector_results = self._hydrate_vector_hits(
            hits,
            filters=filters,
            include_candidates=include_candidates,
            readable=readable,
            query=query,
        )
        if not vector_results:
            meta.update(
                {
                    "retrieval_backend": "hybrid",
                    "semantic_used": True,
                    "index_generation": vector.generation_id or None,
                }
            )
            return ranked, meta

        lexical_by_id = {result.entry_id: result for result in ranked}
        lexical_rank = {result.entry_id: index for index, result in enumerate(ranked)}
        vector_by_id = {result.entry_id: result for result in vector_results}
        vector_rank = {result.entry_id: index for index, result in enumerate(vector_results)}
        merged_ids = set(lexical_by_id) | set(vector_by_id)

        def fused_score(entry_id: str) -> float:
            score = 0.0
            if entry_id in lexical_rank:
                score += self._rrf(lexical_rank[entry_id] + 1)
            if entry_id in vector_rank:
                score += self._rrf(vector_rank[entry_id] + 1)
            return score

        ordered_ids = sorted(
            merged_ids,
            key=lambda entry_id: (
                -fused_score(entry_id),
                lexical_rank.get(entry_id, _MAX_CANDIDATE_POOL + 1),
                vector_rank.get(entry_id, _MAX_CANDIDATE_POOL + 1),
                entry_id,
            ),
        )
        merged: list[SearchResult] = []
        for entry_id in ordered_ids:
            result = lexical_by_id.get(entry_id)
            if result is None:
                result = vector_by_id[entry_id].model_copy(update={"score": fused_score(entry_id)})
            merged.append(result)

        reranker = self.integrations.reranker
        reranked = False
        if reranker.available:
            top_n = max(1, self.config.integrations.reranker.top_n)
            head = merged[:top_n]
            tail = merged[top_n:]
            candidates = [
                RerankCandidate(
                    entry_id=result.entry_id,
                    revision_id=result.revision_id,
                    title=result.title,
                    claim=result.claim,
                    scope=result.scope.model_dump(exclude_none=True),
                    evidence_class=result.evidence_class.value,
                    tags=tuple(result.tags),
                )
                for result in head
            ]
            order = reranker.rerank(query, candidates, top_n)
            allowed_ids = {candidate.entry_id for candidate in candidates}
            by_id = {result.entry_id: result for result in head}
            reordered: list[SearchResult] = []
            seen: set[str] = set()
            for entry_id in order:
                # A reranker reorders the authorized set only. Unknown ids and
                # duplicates are ignored; it can never add or remove an entry.
                if entry_id in allowed_ids and entry_id in by_id and entry_id not in seen:
                    reordered.append(by_id[entry_id])
                    seen.add(entry_id)
            for result in head:
                if result.entry_id not in seen:
                    reordered.append(result)
            merged = reordered + tail
            reranked = True

        meta.update(
            {
                "retrieval_backend": "hybrid",
                "semantic_used": True,
                "reranked": reranked,
                "index_generation": vector.generation_id or None,
            }
        )
        return merged, meta

    def _run_search(
        self,
        namespaces: list[str],
        query: str,
        filters: SearchFilters,
        *,
        max_results: int,
        max_bytes: int,
        cursor: str | None,
        include_candidates: bool,
        session: SessionInfo | None = None,
        semantic: bool = False,
    ) -> SearchResponse:
        query_hash = hashlib.sha256(
            json.dumps(
                {
                    "q": query,
                    "filters": filters.model_dump(mode="json"),
                    "namespaces": namespaces,
                    "max_results": max_results,
                    "max_bytes": max_bytes,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        offset = _cursor_decode(cursor, query_hash) if cursor else 0
        # Bounded over-fetch: rank a small multiple of the requested window,
        # never unbounded, so the N+1-free ranking stays cheap on large corpora.
        candidate_limit = min(
            _MAX_CANDIDATE_POOL,
            max(max_results * 4, offset + max_results),
        )
        candidates = self._fetch_candidates(
            namespaces,
            query,
            exact_phrase=filters.exact_phrase,
            limit=candidate_limit,
        )
        ranked = self._rank(candidates.values(), filters, include_candidates, query=query)
        metadata: dict[str, Any] = {}
        if semantic and session is not None and query.strip():
            try:
                ranked, metadata = self._semantic_merge(
                    session=session,
                    namespaces=namespaces,
                    query=query,
                    filters=filters,
                    include_candidates=include_candidates,
                    ranked=ranked,
                    max_results=max_results,
                )
            except ServiceError as exc:
                metadata = {"warnings": (f"semantic backend unavailable: {exc.code.value}",)}
            except Exception:
                metadata = {"warnings": ("semantic backend unavailable: INTERNAL",)}
        total = len(ranked)
        if offset > total:
            offset = total
        window = ranked[offset : offset + max_results]
        selected: list[SearchResult] = []
        approx_bytes = 0
        truncated = False
        for item in window:
            size = len(item.model_dump_json().encode("utf-8"))
            if approx_bytes + size > max_bytes:
                truncated = True
                break
            selected.append(item)
            approx_bytes += size
        consumed = offset + len(selected)
        next_cursor = None
        if consumed < total and consumed > offset:
            next_cursor = _cursor_encode(consumed, query_hash)
            truncated = True
        elif consumed < total:
            # Not even one whole result fits the byte budget; do not emit a
            # cursor that cannot make progress. Callers may retry with a larger
            # budget or a narrower query.
            truncated = True
        return _with_token_estimate(
            SearchResponse(
                results=tuple(selected),
                total=total,
                truncated=truncated,
                next_cursor=next_cursor,
                approx_bytes=approx_bytes,
                **metadata,
            )
        )

    def search(self, session: SessionInfo, request: SearchRequest) -> SearchResponse:
        filters = request.filters
        namespaces = self._namespaces_for(session, filters.namespace)
        max_results = min(
            request.max_results or self.config.limits.search_default_results,
            self.config.limits.search_max_results,
        )
        max_bytes = min(
            request.max_bytes or self.config.limits.search_default_bytes,
            self.config.limits.search_max_bytes,
        )
        include_candidates = (
            self.config.retrieval.include_candidates
            if filters.include_candidates is None
            else filters.include_candidates
        )
        return self._run_search(
            namespaces,
            request.query,
            filters,
            max_results=max_results,
            max_bytes=max_bytes,
            cursor=request.cursor,
            include_candidates=include_candidates,
            session=session,
            semantic=True,
        )

    def admin_search(
        self,
        query: str,
        *,
        namespace: str | None = None,
        max_results: int | None = None,
    ) -> SearchResponse:
        conn = self.db.connect()
        if namespace is not None:
            namespaces = [namespace]
        else:
            namespaces = [str(row["name"]) for row in conn.execute("SELECT name FROM namespaces").fetchall()]
        return self._run_search(
            namespaces,
            query,
            SearchFilters(include_candidates=True),
            max_results=max_results or self.config.limits.search_max_results,
            max_bytes=self.config.limits.search_max_bytes,
            cursor=None,
            include_candidates=True,
        )

    def get_context(self, session: SessionInfo, task: str, scope: Scope) -> ContextBundle:
        at = self._now()
        conn = self.db.connect()
        readable = readable_namespaces(conn, session.agent_id, at)
        shared_namespaces = [
            namespace for namespace in readable if namespace_kind(conn, namespace) != PERSONAL_KIND
        ]
        filters = SearchFilters(scope=scope, lifecycle=(Lifecycle.PUBLISHED,), include_candidates=False)
        response = self._run_search(
            sorted(shared_namespaces),
            task,
            filters,
            max_results=min(5, self.config.limits.search_max_results),
            max_bytes=min(2000, self.config.limits.search_max_bytes),
            cursor=None,
            include_candidates=False,
            session=session,
            semantic=True,
        )
        return _with_token_estimate(
            ContextBundle(
                results=response.results,
                namespaces=tuple(sorted(readable)),
                approx_bytes=response.approx_bytes,
                truncated=response.truncated,
                retrieval_backend=response.retrieval_backend,
                semantic_used=response.semantic_used,
                reranked=response.reranked,
                index_generation=response.index_generation,
                warnings=response.warnings,
            )
        )

    def get_entry(
        self,
        session: SessionInfo,
        entry_id: str,
        revision_id: str | None = None,
        page: int | None = None,
        cursor: str | None = None,
    ) -> EntryDetail:
        at = self._now()
        conn = self.db.connect()
        entry = conn.execute("SELECT * FROM entries WHERE entry_id = ?", (entry_id,)).fetchone()
        if entry is None:
            raise ServiceError(ErrorCode.NOT_FOUND, "entry not found")
        require_read(conn, session.agent_id, str(entry["namespace"]), at)
        detail = build_entry_detail(conn, entry_id, revision_id)
        if revision_id is None and detail.lifecycle in {
            Lifecycle.ARCHIVED,
            Lifecycle.SUPERSEDED,
        }:
            raise ServiceError(ErrorCode.NOT_FOUND, "entry is not currently visible")
        query_hash = hashlib.sha256(
            f"entry:{entry_id}:{revision_id or detail.current_revision_id}".encode()
        ).hexdigest()
        offset = _cursor_decode(cursor, query_hash) if cursor else 0
        if page is not None and page > 0:
            offset = (page - 1) * 10
        budget = self.config.limits.detail_page_bytes
        full_bytes = len(detail.model_dump_json().encode("utf-8"))
        if full_bytes <= budget:
            return detail.model_copy(update={"approx_bytes": full_bytes})
        history = list(detail.history)[offset:]
        while history:
            candidate = detail.model_copy(update={"history": tuple(history)})
            size = len(candidate.model_dump_json().encode("utf-8"))
            if size <= budget:
                next_offset = offset + len(history)
                next_cursor = (
                    _cursor_encode(next_offset, query_hash) if next_offset < len(detail.history) else None
                )
                return candidate.model_copy(update={"next_cursor": next_cursor, "approx_bytes": size})
            history.pop()
        # Not even one history item fits alongside the base detail; return the
        # base detail without history rather than emit a non-progressing cursor.
        base = detail.model_copy(update={"history": ()})
        return base.model_copy(
            update={"next_cursor": None, "approx_bytes": len(base.model_dump_json().encode("utf-8"))}
        )

    # -- derived index maintenance ---------------------------------------
    def _vector_ready(self) -> bool:
        if not self.integrations.enabled:
            return False
        if self.config.integrations.vector.backend == "none":
            return False
        if not self.integrations.embedding.available:
            return False
        return bool(self.integrations.vector.generation_id) and bool(self.integrations.vector.available)

    def _mark_generation(self, generation: str, state: str, at: datetime) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE index_generations SET state = ?, updated_at = ? WHERE generation_id = ?",
                (state, iso(at), generation),
            )

    def _index_entry_row(self, row: sqlite3.Row) -> bool:
        """Embed and upsert one revision. Returns False when skipped."""
        namespace_kind_value = str(row["namespace_kind"])
        if not self.config.integrations.embedding.embedding_may_send(namespace_kind_value):
            return False
        lifecycle = str(row["lifecycle"])
        if lifecycle in {Lifecycle.ARCHIVED.value, Lifecycle.SUPERSEDED.value}:
            return False
        dimensions = int(self.integrations.embedding.dimensions)
        vectors = self.integrations.embedding.embed([f"{row['title']}\n{row['claim']}"])
        if not vectors or len(vectors[0]) != dimensions:
            raise ServiceError(
                ErrorCode.BACKEND_UNAVAILABLE,
                "embedding dimension does not match the vector index",
            )
        item = VectorItem(
            entry_id=str(row["entry_id"]),
            revision_id=str(row["revision_id"]),
            namespace=str(row["namespace"]),
            vector=tuple(float(value) for value in vectors[0]),
            payload={
                "entry_id": str(row["entry_id"]),
                "revision_id": str(row["revision_id"]),
                "namespace": str(row["namespace"]),
                "lifecycle": lifecycle,
                "evidence_class": str(row["evidence_class"]),
                "content_hash": str(row["content_hash"]),
                "scope": _row_scope_json(row["scope"]),
                "author_root_id": str(row["author_root_id"]),
            },
        )
        self.integrations.vector.upsert([item])
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO entry_index_state "
                "(generation_id, revision_id, backend, content_hash, indexed_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    self.integrations.vector.generation_id,
                    str(row["revision_id"]),
                    self.config.integrations.vector.backend,
                    str(row["content_hash"]),
                    iso(self._now()),
                ),
            )
        return True

    def index_entry_revision(self, entry_id: str, revision_id: str) -> None:
        """Best-effort index of one current revision after a write commits."""
        if not self._vector_ready():
            return
        conn = self.db.connect()
        row = conn.execute(
            """
            SELECT e.entry_id, e.namespace, r.revision_id, r.lifecycle, r.evidence_class,
                   r.title, r.claim, r.scope, r.content_hash, r.author_root_id,
                   n.kind AS namespace_kind
            FROM entry_revisions r
            JOIN entries e ON e.entry_id = r.entry_id
            JOIN namespaces n ON n.name = e.namespace
            WHERE r.revision_id = ? AND e.current_revision_id = r.revision_id
            """,
            (revision_id,),
        ).fetchone()
        if row is None:
            return
        try:
            self._index_entry_row(row)
        except Exception:
            # A derived index never fails a canonical write.
            return

    def remove_from_vector(self, entry_id: str) -> str | None:
        """Delete an entry from the derived index. Returns a warning on failure."""
        try:
            if not self.integrations.enabled:
                return None
            if self.config.integrations.vector.backend == "none":
                return None
            self.integrations.vector.delete([entry_id])
            with self.db.transaction() as conn:
                conn.execute(
                    "DELETE FROM entry_index_state WHERE revision_id IN "
                    "(SELECT revision_id FROM entry_revisions WHERE entry_id = ?)",
                    (entry_id,),
                )
        except ServiceError as exc:
            return f"vector index cleanup failed: {exc.code.value}"
        except Exception:
            return "vector index cleanup failed: INTERNAL"
        return None

    def index_rebuild(self) -> dict[str, Any]:
        at = self._now()
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM entry_fts")
            rows = conn.execute(
                "SELECT e.entry_id, r.revision_id, r.title, r.claim, r.conditions, r.tags "
                "FROM entry_revisions r JOIN entries e ON e.entry_id = r.entry_id"
            ).fetchall()
            for row in rows:
                conn.execute(
                    "INSERT INTO entry_fts (entry_id, revision_id, title, claim, conditions, tags) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        str(row["entry_id"]),
                        str(row["revision_id"]),
                        str(row["title"]),
                        str(row["claim"]),
                        row["conditions"],
                        str(row["tags"]),
                    ),
                )
            conn.execute(
                "INSERT INTO audit_events (ts, actor_agent_id, action, object_type, object_id, details) "
                "VALUES (?, NULL, 'index.rebuild', 'fts', NULL, ?)",
                (iso(at), json.dumps({"indexed": len(rows)})),
            )
        fts_count = len(rows)
        backend = self.config.integrations.vector.backend
        if not self.integrations.enabled or backend == "none" or not self.integrations.embedding.available:
            return {"generation": None, "backend": backend, "indexed": fts_count}
        return self._rebuild_vector(fts_count)

    def _rebuild_vector(self, fts_count: int) -> dict[str, Any]:
        embedding = self.integrations.embedding
        vector = self.integrations.vector
        backend = self.config.integrations.vector.backend
        model = embedding.model
        dimensions = int(embedding.dimensions)
        at = self._now()
        generation = ""
        try:
            generation = vector.ensure_generation(model, dimensions)
            with self.db.transaction() as conn:
                conn.execute(
                    "UPDATE index_generations SET state = 'stale', updated_at = ? "
                    "WHERE backend = ? AND state = 'ready' AND generation_id != ?",
                    (iso(at), backend, generation),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO index_generations "
                    "(generation_id, backend, model, dimensions, state, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 'building', ?, ?)",
                    (generation, backend, model, dimensions, iso(at), iso(at)),
                )
            conn = self.db.connect()
            rows = conn.execute(
                """
                SELECT e.entry_id, e.namespace, r.revision_id, r.lifecycle, r.evidence_class,
                       r.title, r.claim, r.scope, r.content_hash, r.author_root_id,
                       n.kind AS namespace_kind
                FROM entry_revisions r
                JOIN entries e ON e.entry_id = r.entry_id
                JOIN namespaces n ON n.name = e.namespace
                WHERE r.lifecycle NOT IN ('archived', 'superseded')
                ORDER BY r.created_at
                """
            ).fetchall()
            batch_size = max(1, int(self.config.integrations.embedding.batch_size))
            indexed = 0
            skipped_private = 0
            batches = 0
            pending: list[sqlite3.Row] = []
            for row in rows:
                if not self.config.integrations.embedding.embedding_may_send(str(row["namespace_kind"])):
                    skipped_private += 1
                    continue
                pending.append(row)
            for start in range(0, len(pending), batch_size):
                chunk = pending[start : start + batch_size]
                if not self._index_entry_chunk(chunk, generation):
                    continue
                indexed += len(chunk)
                batches += 1
            self._mark_generation(generation, "ready", self._now())
            with self.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO audit_events "
                    "(ts, actor_agent_id, action, object_type, object_id, details) "
                    "VALUES (?, NULL, 'index.rebuild', ?, NULL, ?)",
                    (
                        iso(self._now()),
                        backend,
                        json.dumps(
                            {
                                "generation": generation,
                                "indexed": indexed,
                                "skipped_private": skipped_private,
                                "batches": batches,
                            }
                        ),
                    ),
                )
            return {
                "generation": generation,
                "indexed": indexed,
                "skipped_private": skipped_private,
                "batches": batches,
                "backend": backend,
            }
        except Exception as exc:
            if generation:
                with contextlib.suppress(Exception):
                    self._mark_generation(generation, "failed", self._now())
            if isinstance(exc, ServiceError) and exc.code is ErrorCode.BACKEND_UNAVAILABLE:
                raise
            raise ServiceError(
                ErrorCode.BACKEND_UNAVAILABLE,
                "vector index rebuild failed",
            ) from exc

    def _index_entry_chunk(self, chunk: list[sqlite3.Row], generation: str) -> bool:
        dimensions = int(self.integrations.embedding.dimensions)
        texts = [f"{row['title']}\n{row['claim']}" for row in chunk]
        vectors = self.integrations.embedding.embed(texts)
        if len(vectors) != len(chunk):
            raise ServiceError(
                ErrorCode.BACKEND_UNAVAILABLE,
                "embedding returned the wrong number of vectors",
            )
        items: list[VectorItem] = []
        for row, vector in zip(chunk, vectors, strict=True):
            if len(vector) != dimensions:
                raise ServiceError(
                    ErrorCode.BACKEND_UNAVAILABLE,
                    "embedding dimension does not match the vector index",
                )
            items.append(
                VectorItem(
                    entry_id=str(row["entry_id"]),
                    revision_id=str(row["revision_id"]),
                    namespace=str(row["namespace"]),
                    vector=tuple(float(value) for value in vector),
                    payload={
                        "entry_id": str(row["entry_id"]),
                        "revision_id": str(row["revision_id"]),
                        "namespace": str(row["namespace"]),
                        "lifecycle": str(row["lifecycle"]),
                        "evidence_class": str(row["evidence_class"]),
                        "content_hash": str(row["content_hash"]),
                        "scope": _row_scope_json(row["scope"]),
                        "author_root_id": str(row["author_root_id"]),
                    },
                )
            )
        self.integrations.vector.upsert(items)
        conn = self.db.connect()
        with self.db.transaction():
            for row in chunk:
                conn.execute(
                    "INSERT OR REPLACE INTO entry_index_state "
                    "(generation_id, revision_id, backend, content_hash, indexed_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        generation,
                        str(row["revision_id"]),
                        self.config.integrations.vector.backend,
                        str(row["content_hash"]),
                        iso(self._now()),
                    ),
                )
        return True


def _row_scope_json(value: object) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


__all__ = [
    "RetrievalManager",
    "fts_query",
    "like_pattern",
    "tokenize",
]
