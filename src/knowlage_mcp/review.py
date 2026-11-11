# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Review queue leases, mentor publication, peer evidence, and disputes."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, cast

from .config import Config
from .db import Database, iso, parse_iso
from .knowledge import stable_id
from .models import (
    ErrorCode,
    EvidenceClass,
    Kind,
    Lifecycle,
    NamespaceRole,
    ReviewFilters,
    ReviewJob,
    ReviewResult,
    Scope,
    ServiceError,
    SessionInfo,
    SubmitReviewRequest,
    SubmitReviewResult,
)
from .policy import (
    ROLE_RANK,
    audit,
    can_mentor,
    effective_grants,
    is_own_family,
    now_from,
    require_review,
    role_rank,
)

EventSink = Callable[[dict[str, Any]], None]

_WEAK_EVIDENCE = {EvidenceClass.UNVERIFIED, EvidenceClass.SOURCE_CHECKED}


def _row_scope(value: str | None) -> Scope:
    if not value:
        return Scope()
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return Scope()
    return Scope.model_validate(parsed) if isinstance(parsed, dict) else Scope()


def _row_tags(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(str(item) for item in parsed)


class ReviewManager:
    """Lease-based review queue and the only path that publishes a revision."""

    def __init__(
        self,
        config: Config,
        db: Database,
        *,
        clock: Callable[[], float],
        event_sink: EventSink | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self._clock = clock
        self._events = event_sink

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

    # -- queue -----------------------------------------------------------
    def _contradiction_count(self, conn: sqlite3.Connection, revision_id: str) -> int:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM provenance_edges WHERE relation = 'contradicts' "
            "AND (from_revision_id = ? OR to_revision_id = ?)",
            (revision_id, revision_id),
        ).fetchone()
        return int(row["n"]) if row is not None else 0

    def _usage_count(self, conn: sqlite3.Connection, revision_id: str) -> int:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM task_outcomes "
            "WHERE EXISTS (SELECT 1 FROM json_each(task_outcomes.revision_ids) WHERE value = ?)",
            (revision_id,),
        ).fetchone()
        return int(row["n"]) if row is not None else 0

    def _priority(
        self, conn: sqlite3.Connection, revision_id: str, evidence_class: str, age_hours: float
    ) -> float:
        contradictions = self._contradiction_count(conn, revision_id)
        usage = self._usage_count(conn, revision_id)
        try:
            strength = EvidenceClass(evidence_class)
        except ValueError:
            strength = EvidenceClass.UNVERIFIED
        weak_used = 1.0 if strength in _WEAK_EVIDENCE and usage > 0 else 0.0
        age_term = 0.2 * min(max(age_hours, 0.0), 48.0)
        return 3.0 * contradictions + 1.5 * usage + 1.0 * weak_used + age_term

    @staticmethod
    def _job_model(row: sqlite3.Row) -> ReviewJob:
        keys = set(row.keys())
        return ReviewJob(
            job_id=str(row["job_id"]),
            entry_id=str(row["entry_id"]),
            revision_id=str(row["revision_id"]),
            state=str(row["state"]),
            priority=float(row["priority"]),
            created_at=parse_iso(str(row["created_at"])),
            attempts=int(row["attempts"]),
            lease_owner=row["lease_owner"],
            lease_id=row["lease_id"],
            lease_expires_at=(parse_iso(str(row["lease_expires_at"])) if row["lease_expires_at"] else None),
            namespace=str(row["namespace"]) if "namespace" in keys else None,
            kind=Kind(str(row["kind"])) if "kind" in keys else None,
            title=str(row["title"]) if "title" in keys else None,
            claim=str(row["claim"]) if "claim" in keys else None,
            scope=_row_scope(str(row["scope"])) if "scope" in keys else Scope(),
        )

    def _select_job(self, conn: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None,
            conn.execute(
                """
            SELECT j.*, e.namespace, r.kind, r.title, r.claim, r.scope, r.tags,
                   r.author_agent_id, r.author_root_id, r.contributors, r.evidence_class,
                   r.lifecycle
            FROM review_jobs j
            JOIN entries e ON e.entry_id = j.entry_id
            JOIN entry_revisions r ON r.revision_id = j.revision_id
            WHERE j.job_id = ?
            """,
                (job_id,),
            ).fetchone(),
        )

    def review_next(self, session: SessionInfo, filters: ReviewFilters) -> ReviewJob | None:
        at = self._now()
        lease_seconds = self.config.review.lease_seconds
        with self.db.transaction() as conn:
            grants = effective_grants(conn, session.agent_id, at)
            eligible_namespaces = {
                namespace
                for namespace, role in grants.items()
                if role_rank(role) >= ROLE_RANK[NamespaceRole.REVIEWER]
                and (filters.namespace is None or namespace == filters.namespace)
            }
            if not eligible_namespaces:
                return None
            # Renewing an unexpired lease owned by this session is idempotent.
            renew = conn.execute(
                """
                SELECT j.job_id, e.namespace, r.lifecycle FROM review_jobs j
                JOIN entries e ON e.entry_id = j.entry_id
                JOIN entry_revisions r ON r.revision_id = j.revision_id
                WHERE j.state = 'leased' AND j.lease_owner = ?
                  AND j.lease_expires_at > ? AND j.attempts < ?
                  AND r.lifecycle = 'candidate' 
                """,
                (session.session_id, iso(at), self.config.review.max_attempts),
            ).fetchone()
            if renew is not None and str(renew["namespace"]) in eligible_namespaces:
                new_expiry = at + timedelta(seconds=lease_seconds)
                conn.execute(
                    "UPDATE review_jobs SET lease_expires_at = ? WHERE job_id = ?",
                    (iso(new_expiry), str(renew["job_id"])),
                )
                updated = self._select_job(conn, str(renew["job_id"]))
                assert updated is not None
                return self._job_model(updated)

            rows = conn.execute(
                """
                SELECT j.job_id FROM review_jobs j
                JOIN entries e ON e.entry_id = j.entry_id
                JOIN entry_revisions r ON r.revision_id = j.revision_id
                WHERE j.attempts < ?
                  AND r.lifecycle = 'candidate'
                  AND (j.state = 'queued' OR (j.state = 'leased' AND j.lease_expires_at <= ?))
                """,
                (self.config.review.max_attempts, iso(at)),
            ).fetchall()
            scored: list[tuple[float, sqlite3.Row]] = []
            for candidate in rows:
                job = self._select_job(conn, str(candidate["job_id"]))
                if job is None:
                    continue
                namespace = str(job["namespace"])
                if namespace not in eligible_namespaces:
                    continue
                if str(job["author_agent_id"]) == session.agent_id:
                    continue
                if is_own_family(
                    conn,
                    agent_root_id=session.root_agent_id,
                    author_root_id=str(job["author_root_id"]),
                    contributors=str(job["contributors"] or "[]"),
                ):
                    continue
                if filters.kinds and Kind(str(job["kind"])) not in filters.kinds:
                    continue
                if filters.tags and not set(filters.tags).issubset(set(_row_tags(job["tags"]))):
                    continue
                scope = _row_scope(job["scope"])
                if not filters.scope.is_empty() and not scope.satisfies(filters.scope):
                    continue
                age_hours = max(0.0, (at - parse_iso(str(job["created_at"]))).total_seconds() / 3600.0)
                priority = self._priority(
                    conn, str(job["revision_id"]), str(job["evidence_class"]), age_hours
                )
                scored.append((priority, job))
            if not scored:
                return None
            scored.sort(
                key=lambda item: (
                    item[0],
                    item[1]["created_at"],
                ),
                reverse=True,
            )
            priority, chosen = scored[0]
            job_id = str(chosen["job_id"])
            lease_expires = at + timedelta(seconds=lease_seconds)
            lease_id = stable_id("lse", job_id, session.session_id, iso(at))
            conn.execute(
                "UPDATE review_jobs SET state = 'leased', lease_owner = ?, lease_id = ?, "
                "lease_expires_at = ?, priority = ? WHERE job_id = ?",
                (session.session_id, lease_id, iso(lease_expires), priority, job_id),
            )
            self._audit(
                conn,
                actor=session.agent_id,
                action="review.lease",
                object_type="review_job",
                object_id=job_id,
                details={"revision_id": str(chosen["revision_id"]), "lease_id": lease_id},
                at=at,
            )
            leased = self._select_job(conn, job_id)
            assert leased is not None
            return self._job_model(leased)

    # -- submission ------------------------------------------------------
    def _fail_attempt(
        self,
        job_id: str,
        *,
        message: str,
        code: ErrorCode,
        actor: str | None,
    ) -> None:
        at = self._now()
        with self.db.transaction() as conn:
            row = conn.execute("SELECT attempts FROM review_jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is not None:
                attempts = int(row["attempts"]) + 1
                state = "exhausted" if attempts >= self.config.review.max_attempts else "queued"
                conn.execute(
                    "UPDATE review_jobs SET attempts = ?, state = ?, lease_owner = NULL, "
                    "lease_id = NULL, lease_expires_at = NULL WHERE job_id = ?",
                    (attempts, state, job_id),
                )
                self._audit(
                    conn,
                    actor=actor,
                    action="review.attempt_failed",
                    object_type="review_job",
                    object_id=job_id,
                    details={"attempts": attempts, "state": state},
                    at=at,
                )
        raise ServiceError(code, message)

    def _idempotent_lookup(self, key: str | None, scope: str) -> SubmitReviewResult | None:
        if not key:
            return None
        row = (
            self.db.connect()
            .execute("SELECT scope, response FROM idempotency_records WHERE key = ?", (key,))
            .fetchone()
        )
        if row is None:
            return None
        if str(row["scope"]) != scope:
            raise ServiceError(
                ErrorCode.CONFLICT, "idempotency key was already used for a different operation"
            )
        return SubmitReviewResult.model_validate(json.loads(str(row["response"])))

    def submit_review(self, session: SessionInfo, request: SubmitReviewRequest) -> SubmitReviewResult:
        at = self._now()
        scope_key = f"submit_review:{session.session_id}:{request.job_id or request.revision_id}"
        cached = self._idempotent_lookup(request.idempotency_key, scope_key)
        if cached is not None:
            return cached

        conn = self.db.connect()
        if request.job_id:
            job = self._select_job(conn, request.job_id)
        else:
            row = conn.execute(
                "SELECT job_id FROM review_jobs WHERE revision_id = ? AND lease_owner = ? "
                "AND state = 'leased' ORDER BY created_at DESC LIMIT 1",
                (request.revision_id, session.session_id),
            ).fetchone()
            job = self._select_job(conn, str(row["job_id"])) if row is not None else None
        if job is None:
            raise ServiceError(ErrorCode.REVIEW_INELIGIBLE, "no review lease is held for this revision")

        job_id = str(job["job_id"])
        if str(job["lease_owner"]) != session.session_id:
            self._fail_attempt(
                job_id,
                message="the review lease is owned by another session",
                code=ErrorCode.REVIEW_INELIGIBLE,
                actor=session.agent_id,
            )
        lease_expires = job["lease_expires_at"]
        if lease_expires is None or parse_iso(str(lease_expires)) <= at:
            self._fail_attempt(
                job_id,
                message="the review lease has expired",
                code=ErrorCode.REVIEW_INELIGIBLE,
                actor=session.agent_id,
            )
        if request.revision_id != str(job["revision_id"]):
            self._fail_attempt(
                job_id,
                message="the submitted revision does not match the leased revision",
                code=ErrorCode.INVALID_ARGUMENT,
                actor=session.agent_id,
            )
        if not request.evidence or not request.evidence.strip():
            raise ServiceError(ErrorCode.INVALID_ARGUMENT, "review evidence is required")

        entry = conn.execute("SELECT * FROM entries WHERE entry_id = ?", (str(job["entry_id"]),)).fetchone()
        if entry is None:
            raise ServiceError(ErrorCode.NOT_FOUND, "entry not found")
        namespace = str(entry["namespace"])
        require_review(conn, session.agent_id, namespace, at)
        if is_own_family(
            conn,
            agent_root_id=session.root_agent_id,
            author_root_id=str(job["author_root_id"]),
            contributors=str(job["contributors"] or "[]"),
        ):
            self._fail_attempt(
                job_id,
                message="reviewers may not review their own family's revisions",
                code=ErrorCode.REVIEW_INELIGIBLE,
                actor=session.agent_id,
            )

        mentor = can_mentor(conn, session.agent_id, namespace, at)
        revision_id = str(job["revision_id"])
        review_id = stable_id("rvw", job_id, session.agent_id, iso(at))
        published = False
        job_state = "done"
        lifecycle = Lifecycle(str(job["lifecycle"]))
        evidence_class = EvidenceClass(str(job["evidence_class"]))

        with self.db.transaction() as tx:
            refreshed = tx.execute("SELECT * FROM review_jobs WHERE job_id = ?", (job_id,)).fetchone()
            if refreshed is None:
                raise ServiceError(ErrorCode.NOT_FOUND, "review job not found")
            tx.execute(
                """
                INSERT INTO reviews
                    (review_id, entry_id, revision_id, reviewer_agent_id, reviewer_root_id,
                     result, method, evidence, conditions, limitations, lease_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review_id,
                    str(job["entry_id"]),
                    revision_id,
                    session.agent_id,
                    session.root_agent_id,
                    request.result.value,
                    request.method,
                    request.evidence,
                    request.conditions,
                    request.limitations,
                    job["lease_id"],
                    iso(at),
                ),
            )
            if not mentor:
                # Peer contributions add evidence only; the lifecycle is untouched
                # and the item returns to the queue for an authorized mentor.
                tx.execute(
                    "UPDATE review_jobs SET state = 'queued', lease_owner = NULL, lease_id = NULL, "
                    "lease_expires_at = NULL WHERE job_id = ?",
                    (job_id,),
                )
                job_state = "queued"
            elif request.result == ReviewResult.SUPPORTS:
                new_class = request.evidence_class or EvidenceClass.SOURCE_CHECKED
                tx.execute(
                    "UPDATE entry_revisions SET lifecycle = 'published', evidence_class = ?, "
                    "last_verified_at = ? WHERE revision_id = ?",
                    (new_class.value, iso(at), revision_id),
                )
                current = tx.execute(
                    "SELECT current_revision_id FROM entries WHERE entry_id = ?",
                    (str(job["entry_id"]),),
                ).fetchone()
                assert current is not None
                previous = current["current_revision_id"]
                if previous is not None and str(previous) != revision_id:
                    tx.execute(
                        "UPDATE entry_revisions SET lifecycle = 'superseded' "
                        "WHERE revision_id = ? AND lifecycle = 'published'",
                        (str(previous),),
                    )
                    tx.execute(
                        "INSERT INTO provenance_edges "
                        "(edge_id, from_revision_id, to_revision_id, relation, created_at) "
                        "VALUES (?, ?, ?, 'supersedes', ?)",
                        (
                            stable_id("prv", revision_id, str(previous), iso(at)),
                            revision_id,
                            str(previous),
                            iso(at),
                        ),
                    )
                tx.execute(
                    "UPDATE entries SET current_revision_id = ? WHERE entry_id = ?",
                    (revision_id, str(job["entry_id"])),
                )
                tx.execute(
                    "UPDATE review_jobs SET state = 'done', lease_owner = NULL, lease_id = NULL, "
                    "lease_expires_at = NULL WHERE job_id = ?",
                    (job_id,),
                )
                lifecycle = Lifecycle.PUBLISHED
                evidence_class = new_class
                published = True
            elif request.result == ReviewResult.CONTRADICTS:
                tx.execute(
                    "UPDATE entry_revisions SET lifecycle = 'disputed' WHERE revision_id = ?",
                    (revision_id,),
                )
                current = tx.execute(
                    "SELECT current_revision_id FROM entries WHERE entry_id = ?",
                    (str(job["entry_id"]),),
                ).fetchone()
                assert current is not None
                previous = current["current_revision_id"]
                if previous is not None and str(previous) != revision_id:
                    tx.execute(
                        "INSERT INTO provenance_edges "
                        "(edge_id, from_revision_id, to_revision_id, relation, created_at) "
                        "VALUES (?, ?, ?, 'contradicts', ?)",
                        (
                            stable_id("prv", revision_id, str(previous), iso(at)),
                            revision_id,
                            str(previous),
                            iso(at),
                        ),
                    )
                tx.execute(
                    "UPDATE review_jobs SET state = 'done', lease_owner = NULL, lease_id = NULL, "
                    "lease_expires_at = NULL WHERE job_id = ?",
                    (job_id,),
                )
                lifecycle = Lifecycle.DISPUTED
                evidence_class = request.evidence_class or EvidenceClass(str(job["evidence_class"]))
            else:
                # inconclusive, duplicate, out_of_scope: no publication.
                tx.execute(
                    "UPDATE review_jobs SET state = 'done', lease_owner = NULL, lease_id = NULL, "
                    "lease_expires_at = NULL WHERE job_id = ?",
                    (job_id,),
                )
                evidence_class = request.evidence_class or EvidenceClass(str(job["evidence_class"]))
            self._audit(
                tx,
                actor=session.agent_id,
                action="review.submit",
                object_type="review",
                object_id=review_id,
                details={
                    "job_id": job_id,
                    "revision_id": revision_id,
                    "result": request.result.value,
                    "mentor": mentor,
                    "published": published,
                },
                at=at,
            )
            result = SubmitReviewResult(
                review_id=review_id,
                entry_id=str(job["entry_id"]),
                revision_id=revision_id,
                lifecycle=lifecycle,
                evidence_class=evidence_class,
                published=published,
                job_state=job_state,
                entry_current_revision_id=(
                    revision_id
                    if published
                    else (str(entry["current_revision_id"]) if entry["current_revision_id"] else None)
                ),
            )
            if request.idempotency_key:
                tx.execute(
                    "INSERT INTO idempotency_records (key, scope, response, created_at) VALUES (?, ?, ?, ?)",
                    (
                        request.idempotency_key,
                        scope_key,
                        json.dumps(result.model_dump(mode="json"), sort_keys=True),
                        iso(at),
                    ),
                )
            return result

    # -- admin -----------------------------------------------------------
    def list_reviews(self) -> list[dict[str, Any]]:
        rows = (
            self.db.connect()
            .execute(
                """
            SELECT v.review_id, v.entry_id, v.revision_id, e.namespace, v.reviewer_agent_id,
                   v.reviewer_root_id, v.result, v.method, v.evidence, v.conditions,
                   v.limitations, v.lease_id, v.created_at
            FROM reviews v JOIN entries e ON e.entry_id = v.entry_id
            ORDER BY v.created_at DESC
            """
            )
            .fetchall()
        )
        return [dict(row) for row in rows]

    def list_jobs(self) -> list[dict[str, Any]]:
        rows = (
            self.db.connect()
            .execute(
                "SELECT job_id, entry_id, revision_id, state, priority, created_at, attempts, "
                "lease_owner, lease_id, lease_expires_at FROM review_jobs ORDER BY created_at DESC"
            )
            .fetchall()
        )
        return [dict(row) for row in rows]


__all__ = ["ReviewManager"]
