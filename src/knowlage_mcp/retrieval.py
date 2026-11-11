# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""ACL-first FTS5 retrieval, bounded context, cursor paging, and entry detail."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

from .config import Config
from .db import Database, iso, parse_iso
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

    def __init__(self, config: Config, db: Database, *, clock: Callable[[], float]) -> None:
        self.config = config
        self.db = db
        self._clock = clock

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
    ) -> dict[str, dict[str, Any]]:
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
                    """,
                    (match, *namespaces),
                ).fetchall()
            except sqlite3.OperationalError as exc:
                raise ServiceError(ErrorCode.INVALID_ARGUMENT, "search query is not valid") from exc
            for row in rows:
                candidate = _row_to_candidate(row)
                candidates[candidate["entry_id"]] = candidate

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
                """,
                (*namespaces, *params),
            ).fetchall()
            for row in rows:
                candidate = _row_to_candidate(row)
                candidates.setdefault(candidate["entry_id"], candidate)

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
                LIMIT 200
                """,
                tuple(namespaces),
            ).fetchall()
            for row in rows:
                candidate = _row_to_candidate(row)
                candidates.setdefault(candidate["entry_id"], candidate)
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
        scope_ok = filters.scope.is_empty() or candidate["scope"].satisfies(filters.scope)
        return scope_ok

    @staticmethod
    def _lifecycle_allowed(filters: SearchFilters, include_candidates: bool) -> set[Lifecycle]:
        if filters.lifecycle:
            return set(filters.lifecycle)
        allowed = {Lifecycle.PUBLISHED, Lifecycle.DISPUTED}
        if include_candidates:
            allowed.add(Lifecycle.CANDIDATE)
        return allowed

    def _contradictions(self, revision_id: str) -> tuple[str, ...]:
        rows = (
            self.db.connect()
            .execute(
                "SELECT from_revision_id, to_revision_id FROM provenance_edges "
                "WHERE relation = 'contradicts' AND (from_revision_id = ? OR to_revision_id = ?)",
                (revision_id, revision_id),
            )
            .fetchall()
        )
        found: list[str] = []
        for row in rows:
            other = (
                str(row["to_revision_id"])
                if str(row["from_revision_id"]) == revision_id
                else str(row["from_revision_id"])
            )
            if other != revision_id and other not in found:
                found.append(other)
        return tuple(found)

    def _rank(
        self,
        candidates: Iterable[dict[str, Any]],
        filters: SearchFilters,
        include_candidates: bool,
    ) -> list[SearchResult]:
        candidate_list = list(candidates)
        allowed = self._lifecycle_allowed(filters, include_candidates)
        results: list[SearchResult] = []
        for candidate in candidate_list:
            lifecycle = candidate["lifecycle"]
            if lifecycle not in allowed:
                continue
            if not self._passes_filters(candidate, filters):
                continue
            contradictions = self._contradictions(candidate["revision_id"])
            warnings: list[str] = []
            if contradictions:
                warnings.extend(f"contradicts revision {other}" for other in contradictions)
            if lifecycle == Lifecycle.DISPUTED:
                warnings.append("disputed: credible conflicting evidence exists")
            if lifecycle == Lifecycle.CANDIDATE:
                warnings.append("low trust: candidate has not been reviewed")
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
                )
            )

        # bm25 is not stored on the result; recover it from the candidate score.
        by_entry = {candidate["entry_id"]: candidate for candidate in candidate_list}
        results.sort(
            key=lambda item: (
                _LIFECYCLE_RANK.get(item.lifecycle, 9),
                -sum(1 for value in item.scope.model_dump().values() if value is not None),
                -_EVIDENCE_STRENGTH[item.evidence_class],
                (by_entry[item.entry_id]["rank"] if by_entry[item.entry_id]["rank"] is not None else 0.0),
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
                updated = primary.model_copy(update={"warnings": (*primary.warnings, note)})
                seen[lineage_key] = updated
                index = kept.index(primary)
                kept[index] = updated
                continue
            seen[lineage_key] = item
            kept.append(item)
        return kept

    # -- public search ---------------------------------------------------
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
    ) -> SearchResponse:
        candidates = self._fetch_candidates(namespaces, query, exact_phrase=filters.exact_phrase)
        ranked = self._rank(candidates.values(), filters, include_candidates)
        total = len(ranked)
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
        return SearchResponse(
            results=tuple(selected),
            total=total,
            truncated=truncated,
            next_cursor=next_cursor,
            approx_bytes=approx_bytes,
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
        )
        return ContextBundle(
            results=response.results,
            namespaces=tuple(sorted(readable)),
            approx_bytes=response.approx_bytes,
            truncated=response.truncated,
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
            return {"indexed": len(rows)}


__all__ = [
    "RetrievalManager",
    "fts_query",
    "like_pattern",
    "tokenize",
]
