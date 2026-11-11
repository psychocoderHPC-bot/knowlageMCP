# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Entry and revision storage, dedupe, provenance, outcomes, retract, and forget."""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Any

from .config import Config
from .db import Database, iso, parse_iso
from .evidence import EvidenceStore
from .models import (
    EntryDetail,
    EntryPayload,
    ErrorCode,
    EvidenceClass,
    EvidenceRef,
    Kind,
    Lifecycle,
    NamespaceRole,
    ProposeRevisionRequest,
    RecordOutcomeRequest,
    RecordRequest,
    ReviewResult,
    ReviewView,
    RevisionSummary,
    Scope,
    ServiceError,
    SourceRef,
    TaskOutcome,
    WriteResult,
)
from .policy import (
    ROLE_RANK,
    audit,
    effective_role,
    now_from,
    require_namespace,
    require_read,
    require_write,
)

EventSink = Callable[[dict[str, Any]], None]

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Bidi overrides, zero-width joiners, and other invisible formatting controls
# can spoof rendered memory text; reject them in author-supplied fields.
_HOSTILE_FORMATTING = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]")
_MAX_TAGS = 32
_MAX_TAG_BYTES = 64
_MAX_TITLE_BYTES = 2048
_OUTCOMES = {"success", "failure", "unknown"}
_TRUSTED_SOURCE_ROLES = {"operator", "operator_confirmed"}


def normalize_text(value: str) -> str:
    """Lowercase and collapse all whitespace runs to a single space."""
    return " ".join(value.split()).lower()


def canonical_scope(scope: Scope) -> str:
    data: dict[str, str] = {}
    for key, value in scope.model_dump(exclude_none=True).items():
        if value is None:
            continue
        data[str(key)] = str(value).strip().casefold()
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def content_hash(namespace: str, kind: Kind | str, title: str, claim: str, scope: Scope) -> str:
    """Compute the normalized identity hash used for exact-repeat dedupe."""
    material = "|".join(
        (
            namespace,
            Kind(kind).value,
            normalize_text(title),
            normalize_text(claim),
            canonical_scope(scope),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def stable_id(prefix: str, *parts: str, length: int = 24) -> str:
    """Derive a deterministic identifier from immutable parts."""
    material = "|".join(parts)
    return f"{prefix}_{hashlib.sha256(material.encode('utf-8')).hexdigest()[:length]}"


def _json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]


def _row_scope(value: str | None) -> Scope:
    if not value:
        return Scope()
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return Scope()
    if not isinstance(parsed, dict):
        return Scope()
    return Scope.model_validate(parsed)


def _row_tags(value: str | None) -> tuple[str, ...]:
    return tuple(_json_list(value))


def _warnings_for(
    conn: sqlite3.Connection, revision_id: str, lifecycle: Lifecycle
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    warnings: list[str] = []
    contradictions: list[str] = []
    rows = conn.execute(
        "SELECT from_revision_id, to_revision_id FROM provenance_edges "
        "WHERE relation = 'contradicts' AND (from_revision_id = ? OR to_revision_id = ?)",
        (revision_id, revision_id),
    ).fetchall()
    for row in rows:
        other = (
            str(row["to_revision_id"])
            if str(row["from_revision_id"]) == revision_id
            else str(row["from_revision_id"])
        )
        if other != revision_id and other not in contradictions:
            contradictions.append(other)
            warnings.append(f"contradicts revision {other}")
    if lifecycle == Lifecycle.DISPUTED:
        warnings.append("disputed: credible conflicting evidence exists")
    if lifecycle == Lifecycle.SUPERSEDED:
        warnings.append("superseded revision")
    if lifecycle == Lifecycle.ARCHIVED:
        warnings.append("archived revision")
    return tuple(warnings), tuple(contradictions)


def _sources_for(conn: sqlite3.Connection, revision_id: str) -> tuple[SourceRef, ...]:
    rows = conn.execute(
        "SELECT source_id, kind, role, identity, uri, snapshot_hash, trust, detail, created_at "
        "FROM sources WHERE revision_id = ? ORDER BY created_at",
        (revision_id,),
    ).fetchall()
    return tuple(
        SourceRef(
            source_id=str(row["source_id"]),
            kind=str(row["kind"]),
            role=str(row["role"]),
            identity=row["identity"],
            uri=row["uri"],
            snapshot_hash=row["snapshot_hash"],
            trust=str(row["trust"]),
            detail=row["detail"],
            created_at=parse_iso(str(row["created_at"])),
        )
        for row in rows
    )


def _evidence_for(conn: sqlite3.Connection, revision_id: str) -> tuple[EvidenceRef, ...]:
    rows = conn.execute(
        "SELECT evidence_id, sha256, size_bytes, media_type, redacted FROM evidence_objects "
        "WHERE revision_id = ? ORDER BY created_at",
        (revision_id,),
    ).fetchall()
    return tuple(
        EvidenceRef(
            evidence_id=str(row["evidence_id"]),
            sha256=str(row["sha256"]),
            size_bytes=int(row["size_bytes"]),
            media_type=str(row["media_type"]),
            redacted=bool(row["redacted"]),
        )
        for row in rows
    )


def _reviews_for(conn: sqlite3.Connection, revision_id: str) -> tuple[ReviewView, ...]:
    rows = conn.execute(
        "SELECT review_id, reviewer_agent_id, result, method, created_at FROM reviews "
        "WHERE revision_id = ? ORDER BY created_at",
        (revision_id,),
    ).fetchall()
    return tuple(
        ReviewView(
            review_id=str(row["review_id"]),
            reviewer_agent_id=str(row["reviewer_agent_id"]),
            result=ReviewResult(str(row["result"])),
            method=str(row["method"]),
            created_at=parse_iso(str(row["created_at"])),
        )
        for row in rows
    )


def build_entry_detail(
    conn: sqlite3.Connection,
    entry_id: str,
    revision_id: str | None = None,
) -> EntryDetail:
    """Assemble an immutable detail view. Authorization belongs to the caller."""
    entry = conn.execute("SELECT * FROM entries WHERE entry_id = ?", (entry_id,)).fetchone()
    if entry is None:
        raise ServiceError(ErrorCode.NOT_FOUND, "entry not found")
    target = revision_id or entry["current_revision_id"]
    if target is None:
        raise ServiceError(ErrorCode.NOT_FOUND, "entry has no revision")
    revision = conn.execute(
        "SELECT * FROM entry_revisions WHERE revision_id = ? AND entry_id = ?",
        (target, entry_id),
    ).fetchone()
    if revision is None:
        raise ServiceError(ErrorCode.NOT_FOUND, "revision not found for this entry")

    lifecycle = Lifecycle(str(revision["lifecycle"]))
    warnings, contradictions = _warnings_for(conn, str(revision["revision_id"]), lifecycle)
    history_rows = conn.execute(
        "SELECT revision_id, lifecycle, evidence_class, title, author_agent_id, created_at "
        "FROM entry_revisions WHERE entry_id = ? ORDER BY created_at DESC",
        (entry_id,),
    ).fetchall()
    history = tuple(
        RevisionSummary(
            revision_id=str(row["revision_id"]),
            lifecycle=Lifecycle(str(row["lifecycle"])),
            evidence_class=EvidenceClass(str(row["evidence_class"])),
            title=str(row["title"]),
            author_agent_id=str(row["author_agent_id"]),
            created_at=parse_iso(str(row["created_at"])),
            is_current=str(row["revision_id"]) == entry["current_revision_id"],
        )
        for row in history_rows
    )
    duplicate = conn.execute(
        "SELECT e.entry_id FROM entries e JOIN entry_revisions r "
        "ON r.revision_id = e.current_revision_id "
        "WHERE e.namespace = ? AND e.entry_id != ? AND r.content_hash = ? LIMIT 1",
        (str(entry["namespace"]), entry_id, str(revision["content_hash"])),
    ).fetchone()
    return EntryDetail(
        entry_id=str(entry["entry_id"]),
        namespace=str(entry["namespace"]),
        current_revision_id=entry["current_revision_id"],
        revision_id=str(revision["revision_id"]),
        prev_revision_id=revision["prev_revision_id"],
        kind=Kind(str(revision["kind"])),
        lifecycle=lifecycle,
        evidence_class=EvidenceClass(str(revision["evidence_class"])),
        title=str(revision["title"]),
        claim=str(revision["claim"]),
        conditions=revision["conditions"],
        expected_result=revision["expected_result"],
        limitations=revision["limitations"],
        tags=_row_tags(revision["tags"]),
        scope=_row_scope(revision["scope"]),
        observed_at=parse_iso(str(revision["observed_at"])),
        valid_from=parse_iso(str(revision["valid_from"])) if revision["valid_from"] else None,
        valid_until=parse_iso(str(revision["valid_until"])) if revision["valid_until"] else None,
        last_verified_at=(
            parse_iso(str(revision["last_verified_at"])) if revision["last_verified_at"] else None
        ),
        sensitivity=str(revision["sensitivity"]),
        author_agent_id=str(revision["author_agent_id"]),
        author_root_id=str(revision["author_root_id"]),
        contributors=_row_tags(revision["contributors"]),
        source_role=str(revision["source_role"]),
        content_hash=str(revision["content_hash"]),
        created_at=parse_iso(str(revision["created_at"])),
        sources=_sources_for(conn, str(revision["revision_id"])),
        evidence=_evidence_for(conn, str(revision["revision_id"])),
        reviews=_reviews_for(conn, str(revision["revision_id"])),
        history=history,
        warnings=warnings,
        contradictions=contradictions,
        duplicate_of=str(duplicate["entry_id"]) if duplicate is not None else None,
    )


class KnowledgeManager:
    """Canonical knowledge writes. Every mutation runs in ``db.transaction()``."""

    def __init__(
        self,
        config: Config,
        db: Database,
        evidence: EvidenceStore,
        *,
        clock: Callable[[], float],
        event_sink: EventSink | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self.evidence = evidence
        self._clock = clock
        self._events = event_sink

    # -- helpers ---------------------------------------------------------
    def _now(self) -> datetime:
        return now_from(self._clock)

    def _emit(self, event: dict[str, Any]) -> None:
        if self._events is not None:
            self._events(event)

    def _audit(
        self,
        conn: sqlite3.Connection,
        *,
        actor: str | None,
        action: str,
        object_type: str | None = None,
        object_id: str | None = None,
        details: dict[str, Any] | None = None,
        at: datetime | None = None,
    ) -> None:
        self._emit(
            audit(
                conn,
                at or self._now(),
                actor_agent_id=actor,
                action=action,
                object_type=object_type,
                object_id=object_id,
                details=details,
            )
        )

    def _validate_payload(self, payload: EntryPayload) -> None:
        claim_bytes = len(payload.claim.encode("utf-8"))
        if claim_bytes > self.config.limits.claim_body_bytes:
            raise ServiceError(
                ErrorCode.BUDGET_EXCEEDED,
                f"claim exceeds the {self.config.limits.claim_body_bytes} byte limit",
                details={"claim_bytes": claim_bytes},
            )
        title_bytes = len(payload.title.encode("utf-8"))
        if title_bytes > _MAX_TITLE_BYTES:
            raise ServiceError(
                ErrorCode.INVALID_ARGUMENT,
                f"title exceeds the {_MAX_TITLE_BYTES} byte limit",
                details={"title_bytes": title_bytes},
            )
        if _CONTROL_CHARS.search(payload.title) or _CONTROL_CHARS.search(payload.claim):
            raise ServiceError(ErrorCode.INVALID_ARGUMENT, "control characters are not allowed")
        if _HOSTILE_FORMATTING.search(payload.title) or _HOSTILE_FORMATTING.search(payload.claim):
            raise ServiceError(ErrorCode.INVALID_ARGUMENT, "invisible formatting characters are not allowed")
        if len(payload.tags) > _MAX_TAGS:
            raise ServiceError(ErrorCode.INVALID_ARGUMENT, f"at most {_MAX_TAGS} tags are allowed")
        for tag in payload.tags:
            if len(tag.encode("utf-8")) > _MAX_TAG_BYTES:
                raise ServiceError(ErrorCode.INVALID_ARGUMENT, "a tag exceeds the 64 byte limit")
            if _CONTROL_CHARS.search(tag) or _HOSTILE_FORMATTING.search(tag):
                raise ServiceError(ErrorCode.INVALID_ARGUMENT, "control characters are not allowed in tags")

    def _idempotent_get(self, conn: sqlite3.Connection, key: str | None, scope: str) -> WriteResult | None:
        if not key:
            return None
        row = conn.execute("SELECT scope, response FROM idempotency_records WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        if str(row["scope"]) != scope:
            raise ServiceError(
                ErrorCode.CONFLICT, "idempotency key was already used for a different operation"
            )
        stored = json.loads(str(row["response"]))
        return WriteResult.model_validate(stored).model_copy(update={"idempotent": True})

    @staticmethod
    def _idempotent_put(
        conn: sqlite3.Connection, key: str | None, scope: str, result: WriteResult, at: datetime
    ) -> None:
        if not key:
            return
        payload = json.dumps(result.model_dump(mode="json"), sort_keys=True)
        conn.execute(
            "INSERT INTO idempotency_records (key, scope, response, created_at) VALUES (?, ?, ?, ?)",
            (key, scope, payload, iso(at)),
        )

    def _enforce_candidate_quota(self, conn: sqlite3.Connection, agent_id: str, at: datetime) -> None:
        window_start = iso(at - timedelta(hours=1))
        family = conn.execute("SELECT root_agent_id FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
        if family is not None:
            used = conn.execute(
                "SELECT COUNT(*) AS n FROM entry_revisions r "
                "JOIN agents a ON a.agent_id = r.author_agent_id "
                "WHERE r.lifecycle = 'candidate' AND r.created_at > ? AND a.root_agent_id = ?",
                (window_start, str(family["root_agent_id"])),
            ).fetchone()
            limit = self.config.limits.candidate_writes_per_hour
            if int(used["n"]) >= limit:
                raise ServiceError(
                    ErrorCode.RATE_LIMITED,
                    f"candidate write budget of {limit} per root family per hour is exhausted",
                    retry_after_s=60,
                )
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM entry_revisions WHERE lifecycle = 'candidate' AND created_at > ?",
            (window_start,),
        ).fetchone()
        global_limit = self.config.limits.candidate_writes_per_hour_global
        if int(total["n"]) >= global_limit:
            raise ServiceError(
                ErrorCode.RATE_LIMITED,
                "global candidate write budget is exhausted",
                retry_after_s=60,
            )

    def _insert_revision(
        self,
        conn: sqlite3.Connection,
        *,
        entry_id: str,
        prev_revision_id: str | None,
        namespace: str,
        payload: EntryPayload,
        author_agent_id: str,
        author_root_id: str,
        lifecycle: Lifecycle,
        evidence_class: EvidenceClass,
        last_verified_at: datetime | None,
        digest: str,
        at: datetime,
        revision_id: str | None = None,
    ) -> str:
        revision = revision_id or (stable_id("rev", entry_id, iso(at), digest, length=32))
        conn.execute(
            """
            INSERT INTO entry_revisions
                (revision_id, entry_id, prev_revision_id, kind, title, claim, conditions,
                 expected_result, limitations, tags, scope, observed_at, valid_from, valid_until,
                 last_verified_at, author_agent_id, author_root_id, contributors, source_role,
                 evidence_class, sensitivity, lifecycle, content_hash, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                revision,
                entry_id,
                prev_revision_id,
                payload.kind.value,
                payload.title,
                payload.claim,
                payload.conditions,
                payload.expected_result,
                payload.limitations,
                json.dumps(list(payload.tags)),
                canonical_scope(payload.scope),
                iso(payload.observed_at),
                iso(payload.valid_from) if payload.valid_from is not None else None,
                iso(payload.valid_until) if payload.valid_until is not None else None,
                iso(last_verified_at) if last_verified_at is not None else None,
                author_agent_id,
                author_root_id,
                json.dumps(list(payload.contributors)),
                payload.source_role,
                evidence_class.value,
                payload.sensitivity,
                lifecycle.value,
                digest,
                iso(at),
            ),
        )
        self._insert_sources(conn, revision, payload.sources, at)
        self._attach_evidence(conn, revision, payload.evidence, at)
        for relation in payload.provenance:
            if (
                conn.execute(
                    "SELECT 1 FROM entry_revisions WHERE revision_id = ?", (relation.to_revision_id,)
                ).fetchone()
                is None
            ):
                raise ServiceError(
                    ErrorCode.INVALID_ARGUMENT,
                    "provenance target revision does not exist",
                    details={"to_revision_id": relation.to_revision_id},
                )
            self._insert_provenance(conn, revision, relation.to_revision_id, relation.relation, at)
        self._insert_fts(conn, entry_id, revision, payload)
        return revision

    @staticmethod
    def _insert_provenance(
        conn: sqlite3.Connection,
        from_revision_id: str,
        to_revision_id: str,
        relation: str,
        at: datetime,
    ) -> None:
        conn.execute(
            "INSERT INTO provenance_edges (edge_id, from_revision_id, to_revision_id, relation, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                stable_id("prv", from_revision_id, to_revision_id, relation, iso(at)),
                from_revision_id,
                to_revision_id,
                relation,
                iso(at),
            ),
        )

    @staticmethod
    def _insert_sources(
        conn: sqlite3.Connection,
        revision_id: str,
        sources: Iterable[SourceRef],
        at: datetime,
    ) -> None:
        for source in sources:
            source_id = source.source_id or (stable_id("src", revision_id, source.kind, source.uri or ""))
            conn.execute(
                """
                INSERT INTO sources
                    (source_id, revision_id, kind, role, identity, uri, snapshot_hash, trust,
                     detail, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    revision_id,
                    source.kind,
                    source.role,
                    source.identity,
                    source.uri,
                    source.snapshot_hash,
                    source.trust,
                    source.detail,
                    iso(source.created_at or at),
                ),
            )

    def _attach_evidence(
        self,
        conn: sqlite3.Connection,
        revision_id: str,
        refs: Iterable[EvidenceRef],
        at: datetime,
    ) -> None:
        for ref in refs:
            row = conn.execute(
                "SELECT evidence_id, revision_id FROM evidence_objects WHERE evidence_id = ?",
                (ref.evidence_id,),
            ).fetchone()
            if row is None:
                if not self.evidence.exists(ref.evidence_id):
                    raise ServiceError(
                        ErrorCode.INVALID_ARGUMENT,
                        "evidence object is not registered",
                        details={"evidence_id": ref.evidence_id},
                    )
                sha = ref.evidence_id[3:]
                storage_path = f"{sha[:2]}/{sha[2:4]}/{sha}"
                try:
                    size_bytes = (self.evidence.root / storage_path).stat().st_size
                except OSError as exc:
                    raise ServiceError(ErrorCode.INVALID_ARGUMENT, "evidence object is not readable") from exc
                conn.execute(
                    """
                    INSERT INTO evidence_objects
                        (evidence_id, sha256, size_bytes, media_type, storage_path, redacted,
                         created_at, revision_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ref.evidence_id,
                        ref.sha256 or sha,
                        size_bytes,
                        ref.media_type or "application/octet-stream",
                        storage_path,
                        int(ref.redacted),
                        iso(at),
                        revision_id,
                    ),
                )
            else:
                attached = row["revision_id"]
                if attached is not None and str(attached) != revision_id:
                    raise ServiceError(
                        ErrorCode.INVALID_ARGUMENT,
                        "evidence object is already attached to another revision",
                        details={"evidence_id": ref.evidence_id},
                    )
                if attached is None:
                    conn.execute(
                        "UPDATE evidence_objects SET revision_id = ? WHERE evidence_id = ?",
                        (revision_id, ref.evidence_id),
                    )

    @staticmethod
    def _insert_fts(conn: sqlite3.Connection, entry_id: str, revision_id: str, payload: EntryPayload) -> None:
        conn.execute(
            "INSERT INTO entry_fts (entry_id, revision_id, title, claim, conditions, tags) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                entry_id,
                revision_id,
                payload.title,
                payload.claim,
                payload.conditions,
                json.dumps(list(payload.tags)),
            ),
        )

    @staticmethod
    def _enqueue_review(conn: sqlite3.Connection, entry_id: str, revision_id: str, at: datetime) -> None:
        conn.execute(
            "INSERT INTO review_jobs (job_id, entry_id, revision_id, state, priority, created_at, "
            "attempts, lease_owner, lease_id, lease_expires_at) "
            "VALUES (?, ?, ?, 'queued', 0.0, ?, 0, NULL, NULL, NULL)",
            (stable_id("job", revision_id, iso(at)), entry_id, revision_id, iso(at)),
        )

    @staticmethod
    def _dedupe_lookup(conn: sqlite3.Connection, namespace: str, digest: str) -> str | None:
        row = conn.execute(
            "SELECT e.entry_id FROM entries e JOIN entry_revisions r "
            "ON r.revision_id = e.current_revision_id "
            "WHERE e.namespace = ? AND r.content_hash = ? "
            "AND r.lifecycle != 'archived' ORDER BY e.created_at LIMIT 1",
            (namespace, digest),
        ).fetchone()
        return str(row["entry_id"]) if row is not None else None

    # -- public writes ---------------------------------------------------
    def record(self, session: Any, request: RecordRequest) -> WriteResult:
        at = self._now()
        self._validate_payload(request.entry)
        digest = content_hash(
            request.namespace,
            request.entry.kind,
            request.entry.title,
            request.entry.claim,
            request.entry.scope,
        )
        scope_key = f"record:{session.agent_id}:{request.namespace}"
        with self.db.transaction() as conn:
            require_namespace(conn, request.namespace)
            require_write(conn, session.agent_id, request.namespace, at)
            cached = self._idempotent_get(conn, request.idempotency_key, scope_key)
            if cached is not None:
                return cached
            existing = self._dedupe_lookup(conn, request.namespace, digest)
            if existing is not None:
                detail = build_entry_detail(conn, existing)
                result = WriteResult(entry=detail, duplicate=True, created=False)
                self._idempotent_put(conn, request.idempotency_key, scope_key, result, at)
                return result
            self._enforce_candidate_quota(conn, session.agent_id, at)
            role = effective_role(conn, session.agent_id, request.namespace, at)
            trusted = role is not None and ROLE_RANK[role] >= ROLE_RANK[NamespaceRole.OPERATOR]
            lifecycle = (request.entry.lifecycle or Lifecycle.PUBLISHED) if trusted else Lifecycle.CANDIDATE
            evidence_class = request.entry.evidence_class if trusted else EvidenceClass.UNVERIFIED
            if trusted and lifecycle == Lifecycle.PUBLISHED and evidence_class == EvidenceClass.UNVERIFIED:
                evidence_class = EvidenceClass.OPERATOR_CONFIRMED
            last_verified = request.entry.last_verified_at if trusted else None
            if trusted and last_verified is None:
                last_verified = at
            entry_id = stable_id("ent", request.namespace, digest, iso(at))
            conn.execute(
                "INSERT INTO entries (entry_id, namespace, created_at, created_by_agent_id, "
                "current_revision_id) VALUES (?, ?, ?, ?, NULL)",
                (entry_id, request.namespace, iso(at), session.agent_id),
            )
            revision_id = self._insert_revision(
                conn,
                entry_id=entry_id,
                prev_revision_id=None,
                namespace=request.namespace,
                payload=request.entry,
                author_agent_id=session.agent_id,
                author_root_id=session.root_agent_id,
                lifecycle=lifecycle,
                evidence_class=evidence_class,
                last_verified_at=last_verified,
                digest=digest,
                at=at,
            )
            conn.execute(
                "UPDATE entries SET current_revision_id = ? WHERE entry_id = ?",
                (revision_id, entry_id),
            )
            if lifecycle == Lifecycle.CANDIDATE:
                self._enqueue_review(conn, entry_id, revision_id, at)
            self._audit(
                conn,
                actor=session.agent_id,
                action="entry.record",
                object_type="entry",
                object_id=entry_id,
                details={
                    "namespace": request.namespace,
                    "revision_id": revision_id,
                    "lifecycle": lifecycle.value,
                },
                at=at,
            )
            detail = build_entry_detail(conn, entry_id, revision_id)
            result = WriteResult(entry=detail, duplicate=False, created=True)
            self._idempotent_put(conn, request.idempotency_key, scope_key, result, at)
            return result

    def propose_revision(self, session: Any, request: ProposeRevisionRequest) -> WriteResult:
        at = self._now()
        self._validate_payload(request.entry)
        scope_key = f"propose:{session.agent_id}:{request.entry_id}"
        with self.db.transaction() as conn:
            entry = conn.execute("SELECT * FROM entries WHERE entry_id = ?", (request.entry_id,)).fetchone()
            if entry is None:
                raise ServiceError(ErrorCode.NOT_FOUND, "entry not found")
            namespace = str(entry["namespace"])
            require_write(conn, session.agent_id, namespace, at)
            cached = self._idempotent_get(conn, request.idempotency_key, scope_key)
            if cached is not None:
                return cached
            current_revision_id = entry["current_revision_id"]
            if current_revision_id is None:
                raise ServiceError(ErrorCode.CONFLICT, "entry has no current revision")
            if str(current_revision_id) != request.expected_revision:
                raise ServiceError(
                    ErrorCode.REVISION_CONFLICT,
                    "expected_revision does not match the current revision",
                    details={"current_revision_id": str(current_revision_id)},
                )
            digest = content_hash(
                namespace,
                request.entry.kind,
                request.entry.title,
                request.entry.claim,
                request.entry.scope,
            )
            current = conn.execute(
                "SELECT content_hash FROM entry_revisions WHERE revision_id = ?",
                (current_revision_id,),
            ).fetchone()
            if current is not None and str(current["content_hash"]) == digest:
                detail = build_entry_detail(conn, request.entry_id, str(current_revision_id))
                result = WriteResult(entry=detail, duplicate=True, created=False)
                self._idempotent_put(conn, request.idempotency_key, scope_key, result, at)
                return result
            prior = conn.execute(
                "SELECT revision_id FROM entry_revisions WHERE entry_id = ? AND content_hash = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (request.entry_id, digest),
            ).fetchone()
            if prior is not None:
                detail = build_entry_detail(conn, request.entry_id, str(prior["revision_id"]))
                result = WriteResult(entry=detail, duplicate=True, created=False)
                self._idempotent_put(conn, request.idempotency_key, scope_key, result, at)
                return result
            self._enforce_candidate_quota(conn, session.agent_id, at)
            revision_id = self._insert_revision(
                conn,
                entry_id=request.entry_id,
                prev_revision_id=str(current_revision_id),
                namespace=namespace,
                payload=request.entry,
                author_agent_id=session.agent_id,
                author_root_id=session.root_agent_id,
                lifecycle=Lifecycle.CANDIDATE,
                evidence_class=EvidenceClass.UNVERIFIED,
                last_verified_at=None,
                digest=digest,
                at=at,
            )
            self._insert_provenance(conn, revision_id, str(current_revision_id), "derived_from", at)
            self._enqueue_review(conn, request.entry_id, revision_id, at)
            self._audit(
                conn,
                actor=session.agent_id,
                action="entry.propose_revision",
                object_type="entry",
                object_id=request.entry_id,
                details={"revision_id": revision_id, "expected_revision": request.expected_revision},
                at=at,
            )
            detail = build_entry_detail(conn, request.entry_id, revision_id)
            result = WriteResult(entry=detail, duplicate=False, created=True)
            self._idempotent_put(conn, request.idempotency_key, scope_key, result, at)
            return result

    def record_outcome(self, session: Any, request: RecordOutcomeRequest) -> TaskOutcome:
        outcome = request.outcome.strip().lower()
        if outcome not in _OUTCOMES:
            raise ServiceError(ErrorCode.INVALID_ARGUMENT, "outcome must be one of success, failure, unknown")
        at = self._now()
        revision_ids = tuple(dict.fromkeys(request.revision_ids))
        scope_key = f"outcome:{session.agent_id}"
        with self.db.transaction() as conn:
            if request.idempotency_key:
                row = conn.execute(
                    "SELECT scope, response FROM idempotency_records WHERE key = ?",
                    (request.idempotency_key,),
                ).fetchone()
                if row is not None:
                    if str(row["scope"]) != scope_key:
                        raise ServiceError(
                            ErrorCode.CONFLICT,
                            "idempotency key was already used for a different operation",
                        )
                    stored = json.loads(str(row["response"]))
                    return TaskOutcome.model_validate(stored)
            for revision_id in revision_ids:
                row = conn.execute(
                    "SELECT r.lifecycle, e.namespace FROM entry_revisions r "
                    "JOIN entries e ON e.entry_id = r.entry_id WHERE r.revision_id = ?",
                    (revision_id,),
                ).fetchone()
                if row is None:
                    raise ServiceError(
                        ErrorCode.NOT_FOUND,
                        "revision not found",
                        details={"revision_id": revision_id},
                    )
                if str(row["lifecycle"]) == Lifecycle.ARCHIVED.value:
                    raise ServiceError(
                        ErrorCode.NOT_FOUND,
                        "revision is not visible",
                        details={"revision_id": revision_id},
                    )
                require_read(conn, session.agent_id, str(row["namespace"]), at)
            outcome_id = stable_id("out", session.agent_id, session.session_id, iso(at), outcome)
            conn.execute(
                """
                INSERT INTO task_outcomes
                    (outcome_id, agent_id, session_id, task_label, outcome, revision_ids, notes,
                     created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    outcome_id,
                    session.agent_id,
                    session.session_id,
                    request.task_label,
                    outcome,
                    json.dumps(list(revision_ids)),
                    request.notes,
                    iso(at),
                ),
            )
            result = TaskOutcome(
                outcome_id=outcome_id,
                agent_id=session.agent_id,
                session_id=session.session_id,
                task_label=request.task_label,
                outcome=outcome,
                revision_ids=revision_ids,
                notes=request.notes,
                created_at=at,
            )
            if request.idempotency_key:
                conn.execute(
                    "INSERT INTO idempotency_records (key, scope, response, created_at) VALUES (?, ?, ?, ?)",
                    (
                        request.idempotency_key,
                        scope_key,
                        json.dumps(result.model_dump(mode="json"), sort_keys=True),
                        iso(at),
                    ),
                )
            self._audit(
                conn,
                actor=session.agent_id,
                action="outcome.record",
                object_type="task_outcome",
                object_id=outcome_id,
                details={"outcome": outcome, "revision_ids": list(revision_ids)},
                at=at,
            )
            return result

    # -- operator memory path -------------------------------------------
    def memory_add(
        self,
        *,
        kind: str,
        namespace: str,
        text: str,
        title: str | None = None,
        source_role: str = "operator",
    ) -> WriteResult:
        at = self._now()
        try:
            parsed_kind = Kind(kind)
        except ValueError as exc:
            raise ServiceError(ErrorCode.INVALID_ARGUMENT, f"unknown kind: {kind!r}") from exc
        derived_title = title or " ".join(text.split())[:120] or "operator note"
        payload = EntryPayload(
            kind=parsed_kind,
            title=derived_title,
            claim=text,
            source_role=(source_role if source_role in _TRUSTED_SOURCE_ROLES else "operator"),
            evidence_class=EvidenceClass.OPERATOR_CONFIRMED,
            lifecycle=Lifecycle.PUBLISHED,
        )
        self._validate_payload(payload)
        digest = content_hash(namespace, parsed_kind, payload.title, payload.claim, payload.scope)
        with self.db.transaction() as conn:
            require_namespace(conn, namespace)
            entry_id = stable_id("ent", namespace, digest, iso(at))
            conn.execute(
                "INSERT INTO entries (entry_id, namespace, created_at, created_by_agent_id, "
                "current_revision_id) VALUES (?, ?, ?, 'operator', NULL)",
                (entry_id, namespace, iso(at)),
            )
            revision_id = self._insert_revision(
                conn,
                entry_id=entry_id,
                prev_revision_id=None,
                namespace=namespace,
                payload=payload,
                author_agent_id="operator",
                author_root_id="operator",
                lifecycle=Lifecycle.PUBLISHED,
                evidence_class=EvidenceClass.OPERATOR_CONFIRMED,
                last_verified_at=at,
                digest=digest,
                at=at,
            )
            conn.execute(
                "UPDATE entries SET current_revision_id = ? WHERE entry_id = ?",
                (revision_id, entry_id),
            )
            self._audit(
                conn,
                actor=None,
                action="memory.add",
                object_type="entry",
                object_id=entry_id,
                details={"namespace": namespace, "revision_id": revision_id},
                at=at,
            )
            return WriteResult(entry=build_entry_detail(conn, entry_id, revision_id), created=True)

    def memory_correct(self, entry_id: str, expected_revision: str, text: str) -> WriteResult:
        at = self._now()
        with self.db.transaction() as conn:
            entry = conn.execute("SELECT * FROM entries WHERE entry_id = ?", (entry_id,)).fetchone()
            if entry is None:
                raise ServiceError(ErrorCode.NOT_FOUND, "entry not found")
            current_id = entry["current_revision_id"]
            if current_id is None:
                raise ServiceError(ErrorCode.CONFLICT, "entry has no current revision")
            if str(current_id) != expected_revision:
                raise ServiceError(
                    ErrorCode.REVISION_CONFLICT,
                    "expected_revision does not match the current revision",
                    details={"current_revision_id": str(current_id)},
                )
            current = conn.execute(
                "SELECT * FROM entry_revisions WHERE revision_id = ?", (current_id,)
            ).fetchone()
            assert current is not None
            payload = EntryPayload(
                kind=Kind(str(current["kind"])),
                title=str(current["title"]),
                claim=text,
                conditions=current["conditions"],
                expected_result=current["expected_result"],
                limitations=current["limitations"],
                tags=_row_tags(current["tags"]),
                scope=_row_scope(current["scope"]),
                source_role="operator",
                evidence_class=EvidenceClass.OPERATOR_CONFIRMED,
                lifecycle=Lifecycle.PUBLISHED,
            )
            self._validate_payload(payload)
            namespace = str(entry["namespace"])
            digest = content_hash(namespace, payload.kind, payload.title, payload.claim, payload.scope)
            revision_id = self._insert_revision(
                conn,
                entry_id=entry_id,
                prev_revision_id=str(current_id),
                namespace=namespace,
                payload=payload,
                author_agent_id="operator",
                author_root_id="operator",
                lifecycle=Lifecycle.PUBLISHED,
                evidence_class=EvidenceClass.OPERATOR_CONFIRMED,
                last_verified_at=at,
                digest=digest,
                at=at,
            )
            conn.execute(
                "UPDATE entry_revisions SET lifecycle = 'superseded' WHERE revision_id = ?",
                (current_id,),
            )
            self._insert_provenance(conn, revision_id, str(current_id), "supersedes", at)
            conn.execute(
                "UPDATE entries SET current_revision_id = ? WHERE entry_id = ?",
                (revision_id, entry_id),
            )
            self._audit(
                conn,
                actor=None,
                action="memory.correct",
                object_type="entry",
                object_id=entry_id,
                details={"revision_id": revision_id, "previous_revision_id": str(current_id)},
                at=at,
            )
            return WriteResult(entry=build_entry_detail(conn, entry_id, revision_id), created=True)

    def memory_retract(self, entry_id: str, reason: str) -> dict[str, Any]:
        at = self._now()
        if not reason.strip():
            raise ServiceError(ErrorCode.INVALID_ARGUMENT, "a retraction reason is required")
        with self.db.transaction() as conn:
            entry = conn.execute("SELECT * FROM entries WHERE entry_id = ?", (entry_id,)).fetchone()
            if entry is None:
                raise ServiceError(ErrorCode.NOT_FOUND, "entry not found")
            revisions = conn.execute(
                "SELECT revision_id FROM entry_revisions WHERE entry_id = ?", (entry_id,)
            ).fetchall()
            conn.execute("UPDATE entry_revisions SET lifecycle = 'archived' WHERE entry_id = ?", (entry_id,))
            conn.execute("DELETE FROM entry_fts WHERE entry_id = ?", (entry_id,))
            self._audit(
                conn,
                actor=None,
                action="memory.retract",
                object_type="entry",
                object_id=entry_id,
                details={"reason": reason, "revisions": [str(r["revision_id"]) for r in revisions]},
                at=at,
            )
            return {
                "entry_id": entry_id,
                "retracted": True,
                "reason": reason,
                "revisions": len(revisions),
            }

    def memory_forget(self, entry_id: str, dry_run: bool = False) -> dict[str, Any]:
        at = self._now()
        with self.db.transaction() as conn:
            entry = conn.execute("SELECT * FROM entries WHERE entry_id = ?", (entry_id,)).fetchone()
            if entry is None:
                raise ServiceError(ErrorCode.NOT_FOUND, "entry not found")
            revisions = [
                str(row["revision_id"])
                for row in conn.execute(
                    "SELECT revision_id FROM entry_revisions WHERE entry_id = ?", (entry_id,)
                ).fetchall()
            ]
            evidence_rows = []
            if revisions:
                placeholders = ",".join("?" for _ in revisions)
                evidence_rows = conn.execute(
                    f"SELECT evidence_id, storage_path FROM evidence_objects "
                    f"WHERE revision_id IN ({placeholders})",
                    tuple(revisions),
                ).fetchall()
            source_count = 0
            review_count = 0
            if revisions:
                placeholders = ",".join("?" for _ in revisions)
                source_count = int(
                    conn.execute(
                        f"SELECT COUNT(*) AS n FROM sources WHERE revision_id IN ({placeholders})",
                        tuple(revisions),
                    ).fetchone()["n"]
                )
                review_count = int(
                    conn.execute(
                        f"SELECT COUNT(*) AS n FROM reviews WHERE revision_id IN ({placeholders})",
                        tuple(revisions),
                    ).fetchone()["n"]
                )
            counts = {
                "entry_id": entry_id,
                "revisions": len(revisions),
                "sources": source_count,
                "reviews": review_count,
                "evidence": len(evidence_rows),
            }
            if dry_run:
                return {**counts, "dry_run": True, "forgotten": False}
            # Idempotency responses are a cache of the purged content.
            conn.execute("DELETE FROM idempotency_records WHERE response LIKE ?", (f"%{entry_id}%",))
            for revision_id in revisions:
                conn.execute(
                    "DELETE FROM idempotency_records WHERE response LIKE ?",
                    (f"%{revision_id}%",),
                )
            # FTS has no foreign key; remove rows explicitly.
            conn.execute("DELETE FROM entry_fts WHERE entry_id = ?", (entry_id,))
            for revision_id in revisions:
                conn.execute("DELETE FROM entry_fts WHERE revision_id = ?", (revision_id,))
            # Outcomes are free-form history; drop references to purged revisions.
            if revisions:
                placeholders = ",".join("?" for _ in revisions)
                conn.execute(
                    f"DELETE FROM task_outcomes WHERE EXISTS "
                    f"(SELECT 1 FROM json_each(task_outcomes.revision_ids) WHERE value IN ({placeholders}))",
                    tuple(revisions),
                )
            conn.execute("UPDATE entry_revisions SET prev_revision_id = NULL WHERE entry_id = ?", (entry_id,))
            conn.execute("DELETE FROM entry_revisions WHERE entry_id = ?", (entry_id,))
            conn.execute("DELETE FROM entries WHERE entry_id = ?", (entry_id,))
            deleted_evidence = 0
            for row in evidence_rows:
                evidence_id = str(row["evidence_id"])
                storage_path = str(row["storage_path"])
                with contextlib.suppress(FileNotFoundError):
                    (self.evidence.root / storage_path).unlink()
                conn.execute("DELETE FROM evidence_objects WHERE evidence_id = ?", (evidence_id,))
                conn.execute(
                    "INSERT OR IGNORE INTO deletion_ledger "
                    "(object_type, object_id, deleted_at, reason) VALUES ('evidence', ?, ?, ?)",
                    (evidence_id, iso(at), "forget"),
                )
                deleted_evidence += 1
            conn.execute(
                "INSERT OR REPLACE INTO deletion_ledger "
                "(object_type, object_id, deleted_at, reason) VALUES ('entry', ?, ?, ?)",
                (entry_id, iso(at), "forget"),
            )
            for revision_id in revisions:
                conn.execute(
                    "INSERT OR IGNORE INTO deletion_ledger "
                    "(object_type, object_id, deleted_at, reason) VALUES ('revision', ?, ?, ?)",
                    (revision_id, iso(at), "forget"),
                )
            self._audit(
                conn,
                actor=None,
                action="memory.forget",
                object_type="entry",
                object_id=entry_id,
                details={**counts, "evidence_deleted": deleted_evidence},
                at=at,
            )
            return {**counts, "dry_run": False, "forgotten": True, "evidence_deleted": deleted_evidence}


__all__ = [
    "KnowledgeManager",
    "build_entry_detail",
    "canonical_scope",
    "content_hash",
    "normalize_text",
]
