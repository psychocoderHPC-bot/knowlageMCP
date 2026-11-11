# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Agents, sessions, delegation tickets, mentor requests, grants, and revocation.

Actor handles are per-session routing capabilities, not authentication factors.
The gateway owns connection grouping; :meth:`IdentityManager.resolve_actor`
accepts the handles visible on one connection so that an ambiguous request fails
closed. Session tokens are opaque 32-byte secrets and only their SHA-256 digest
is stored.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import string
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Any, cast

from .config import Config
from .db import Database, iso, parse_iso
from .models import (
    Delegation,
    ErrorCode,
    Grant,
    MentorRequest,
    NamespaceRole,
    Registration,
    ServiceError,
    SessionInfo,
)
from .policy import (
    DEFAULT_ROOT_ROLES,
    SlidingWindowLimiter,
    TokenBucketLimiter,
    audit,
    can_mentor,
    can_read,
    effective_grants,
    namespace_kind,
    now_from,
    parse_role,
    require_namespace,
    role_rank,
)

EventSink = Callable[[dict[str, Any]], None]


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(12)}"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_session_token() -> str:
    """Return a 32-byte URL-safe secret (43 characters, no padding)."""
    return secrets.token_urlsafe(32)


def generate_actor_handle() -> str:
    return "act_" + secrets.token_hex(4)


def _mentor_code() -> str:
    letters = "".join(secrets.choice(string.ascii_uppercase) for _ in range(4))
    digits = "".join(secrets.choice(string.digits) for _ in range(4))
    return f"{letters}-{digits}"


class IdentityManager:
    """Owns the identity tables. All methods are safe to call from one process."""

    def __init__(
        self,
        config: Config,
        db: Database,
        *,
        clock: Callable[[], float],
        monotonic: Callable[[], float],
        event_sink: EventSink | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self._clock = clock
        self._monotonic = monotonic
        self._events = event_sink
        self._limiter = SlidingWindowLimiter(monotonic)
        self._request_limiter = TokenBucketLimiter(monotonic)

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
        event = audit(
            conn,
            at or self._now(),
            actor_agent_id=actor,
            action=action,
            object_type=object_type,
            object_id=object_id,
            details=details,
        )
        self._emit(event)

    @staticmethod
    def _grants(conn: sqlite3.Connection, agent_id: str, at: datetime) -> tuple[Grant, ...]:
        rows = conn.execute(
            """
            SELECT grant_id, agent_id, namespace, role, created_at, expires_at, revoked_at
            FROM grants
            WHERE agent_id = ?
              AND revoked_at IS NULL
              AND (expires_at IS NULL OR expires_at > ?)
            ORDER BY namespace, role
            """,
            (agent_id, iso(at)),
        ).fetchall()
        return tuple(
            Grant(
                grant_id=str(row["grant_id"]),
                agent_id=str(row["agent_id"]),
                namespace=str(row["namespace"]),
                role=NamespaceRole(str(row["role"])),
                created_at=parse_iso(str(row["created_at"])),
                expires_at=parse_iso(str(row["expires_at"])) if row["expires_at"] else None,
                revoked_at=parse_iso(str(row["revoked_at"])) if row["revoked_at"] else None,
            )
            for row in rows
        )

    def _session_info(self, conn: sqlite3.Connection, row: sqlite3.Row, at: datetime) -> SessionInfo:
        return SessionInfo(
            session_id=str(row["session_id"]),
            agent_id=str(row["agent_id"]),
            root_agent_id=str(row["root_agent_id"]),
            actor_handle=str(row["actor_handle"]),
            created_at=parse_iso(str(row["created_at"])),
            last_seen_at=parse_iso(str(row["last_seen_at"])),
            idle_expires_at=parse_iso(str(row["idle_expires_at"])),
            absolute_expires_at=parse_iso(str(row["absolute_expires_at"])),
            revoked_at=parse_iso(str(row["revoked_at"])) if row["revoked_at"] else None,
            grants=self._grants(conn, str(row["agent_id"]), at),
        )

    def _live_session(self, conn: sqlite3.Connection, session_id: str, at: datetime) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        if row is None or row["revoked_at"] is not None:
            raise ServiceError(ErrorCode.SESSION_EXPIRED, "session is no longer active")
        if parse_iso(str(row["absolute_expires_at"])) <= at:
            raise ServiceError(ErrorCode.SESSION_EXPIRED, "session absolute lifetime expired")
        if parse_iso(str(row["idle_expires_at"])) <= at:
            raise ServiceError(ErrorCode.SESSION_EXPIRED, "session idle timeout expired")
        return cast(sqlite3.Row, row)

    # -- startup ---------------------------------------------------------
    def ensure_default_namespaces(self, conn: sqlite3.Connection, at: datetime) -> None:
        identity = self.config.identity
        conn.execute(
            "INSERT OR IGNORE INTO namespaces (name, kind, description, created_at) VALUES (?, ?, ?, ?)",
            (identity.initial_namespace, "shared", "Shared team knowledge", iso(at)),
        )
        conn.execute(
            "INSERT OR IGNORE INTO namespaces (name, kind, description, created_at) VALUES (?, ?, ?, ?)",
            (identity.personal_namespace, "personal", "Operator-only personal knowledge", iso(at)),
        )

    def startup_reset(self) -> dict[str, int]:
        """Invalidate live sessions, expired tickets, and stale review leases."""
        at = self._now()
        counts: dict[str, int] = {}
        with self.db.transaction() as conn:
            self.ensure_default_namespaces(conn, at)
            counts["sessions_revoked"] = conn.execute(
                "UPDATE sessions SET revoked_at = ? WHERE revoked_at IS NULL", (iso(at),)
            ).rowcount
            counts["tickets_invalidated"] = conn.execute(
                "UPDATE delegation_tickets SET used_at = ? WHERE used_at IS NULL AND expires_at <= ?",
                (iso(at), iso(at)),
            ).rowcount
            counts["leases_released"] = conn.execute(
                "UPDATE review_jobs SET state = 'queued', lease_owner = NULL, "
                "lease_id = NULL, lease_expires_at = NULL WHERE state = 'leased'"
            ).rowcount
            self._audit(
                conn,
                actor=None,
                action="bootstrap",
                object_type="gateway",
                details=counts,
                at=at,
            )
        return counts

    # -- registration ----------------------------------------------------
    def register(
        self,
        *,
        mode: str = "root",
        ticket: str | None = None,
        label: str | None = None,
        model: str | None = None,
        provider: str | None = None,
        task: str | None = None,
        source: str = "unknown",
    ) -> Registration:
        if mode not in {"root", "child"}:
            raise ServiceError(ErrorCode.INVALID_ARGUMENT, "mode must be 'root' or 'child'")
        identity = self.config.identity
        limits = self.config.limits
        if mode == "root" and not identity.allow_self_registration:
            raise ServiceError(ErrorCode.FORBIDDEN, "self-registration is disabled")
        if mode == "child" and not ticket:
            raise ServiceError(ErrorCode.INVALID_ARGUMENT, "child registration requires a ticket")

        allowed, retry = self._limiter.allow(
            f"register:source:{source}",
            limits.registration_per_minute_per_source,
            60,
        )
        if not allowed:
            raise ServiceError(
                ErrorCode.RATE_LIMITED,
                "registration rate limit exceeded for this source",
                retry_after_s=retry,
            )
        allowed, retry = self._limiter.allow("register:global", limits.registration_per_minute_global, 60)
        if not allowed:
            raise ServiceError(
                ErrorCode.RATE_LIMITED,
                "global registration rate limit exceeded",
                retry_after_s=retry,
            )

        at = self._now()
        token = generate_session_token()
        actor_handle = generate_actor_handle()
        absolute_expires = at + timedelta(seconds=int(identity.session_absolute_lifetime))
        idle_expires = min(at + timedelta(seconds=int(identity.session_idle_timeout)), absolute_expires)

        with self.db.transaction() as conn:
            active = conn.execute(
                "SELECT COUNT(*) AS n FROM sessions WHERE revoked_at IS NULL "
                "AND absolute_expires_at > ? AND idle_expires_at > ?",
                (iso(at), iso(at)),
            ).fetchone()
            if int(active["n"]) >= identity.max_sessions:
                raise ServiceError(
                    ErrorCode.RATE_LIMITED,
                    "maximum number of active sessions reached",
                    retry_after_s=60,
                    details={"max_sessions": identity.max_sessions},
                )

            if mode == "child":
                assert ticket is not None
                changed = conn.execute(
                    "UPDATE delegation_tickets SET used_at = ? "
                    "WHERE ticket_id = ? AND used_at IS NULL AND expires_at > ?",
                    (iso(at), ticket, iso(at)),
                ).rowcount
                if changed != 1:
                    raise ServiceError(
                        ErrorCode.INVALID_ARGUMENT,
                        "delegation ticket is invalid, expired, or already used",
                    )
                ticket_row = conn.execute(
                    "SELECT * FROM delegation_tickets WHERE ticket_id = ?", (ticket,)
                ).fetchone()
                assert ticket_row is not None
                parent_agent_id = str(ticket_row["parent_agent_id"])
                root_agent_id = str(ticket_row["root_agent_id"])
                parent = conn.execute(
                    "SELECT * FROM agents WHERE agent_id = ?", (parent_agent_id,)
                ).fetchone()
                if parent is None or parent["revoked_at"] is not None:
                    raise ServiceError(ErrorCode.FORBIDDEN, "parent agent is not active")
                if str(parent["root_agent_id"]) != root_agent_id:
                    raise ServiceError(ErrorCode.FORBIDDEN, "ticket root family mismatch")
                agent_id = new_id("agt")
                conn.execute(
                    """
                    INSERT INTO agents (agent_id, root_agent_id, parent_agent_id, label, model,
                                        provider, task, enrollment_mode, created_at, revoked_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'child', ?, NULL)
                    """,
                    (agent_id, root_agent_id, parent_agent_id, label, model, provider, task, iso(at)),
                )
                parent_grants = effective_grants(conn, parent_agent_id, at)
                for namespace, role in parent_grants.items():
                    if namespace_kind(conn, namespace) is None:
                        continue
                    self._insert_grant(conn, agent_id, namespace, role, at)
            else:
                agent_id = new_id("agt")
                root_agent_id = agent_id
                conn.execute(
                    """
                    INSERT INTO agents (agent_id, root_agent_id, parent_agent_id, label, model,
                                        provider, task, enrollment_mode, created_at, revoked_at)
                    VALUES (?, ?, NULL, ?, ?, ?, ?, 'self', ?, NULL)
                    """,
                    (agent_id, root_agent_id, label, model, provider, task, iso(at)),
                )
                for role in DEFAULT_ROOT_ROLES:
                    self._insert_grant(conn, agent_id, identity.initial_namespace, role, at)

            session_id = new_id("ses")
            conn.execute(
                """
                INSERT INTO sessions (session_id, agent_id, root_agent_id, token_hash, actor_handle,
                                      created_at, last_seen_at, idle_expires_at, absolute_expires_at,
                                      revoked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    session_id,
                    agent_id,
                    root_agent_id,
                    hash_token(token),
                    actor_handle,
                    iso(at),
                    iso(at),
                    iso(idle_expires),
                    iso(absolute_expires),
                ),
            )
            grants = self._grants(conn, agent_id, at)
            self._audit(
                conn,
                actor=agent_id,
                action="agent.register",
                object_type="agent",
                object_id=agent_id,
                details={"mode": mode, "root_agent_id": root_agent_id, "session_id": session_id},
                at=at,
            )
        return Registration(
            agent_id=agent_id,
            root_agent_id=root_agent_id,
            session_id=session_id,
            actor_handle=actor_handle,
            session_token=token,
            expires_at=absolute_expires,
            grants=grants,
        )

    @staticmethod
    def _insert_grant(
        conn: sqlite3.Connection,
        agent_id: str,
        namespace: str,
        role: NamespaceRole,
        at: datetime,
        *,
        expires_at: datetime | None = None,
    ) -> str:
        existing = conn.execute(
            "SELECT grant_id FROM grants WHERE agent_id = ? AND namespace = ? AND role = ? "
            "AND revoked_at IS NULL",
            (agent_id, namespace, role.value),
        ).fetchone()
        if existing is not None:
            if expires_at is not None:
                conn.execute(
                    "UPDATE grants SET expires_at = ? WHERE grant_id = ?",
                    (iso(expires_at), existing["grant_id"]),
                )
            return str(existing["grant_id"])
        grant_id = new_id("grt")
        conn.execute(
            """
            INSERT INTO grants (grant_id, agent_id, namespace, role, created_at, expires_at, revoked_at)
            VALUES (?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                grant_id,
                agent_id,
                namespace,
                role.value,
                iso(at),
                iso(expires_at) if expires_at is not None else None,
            ),
        )
        return grant_id

    # -- delegation ------------------------------------------------------
    def delegate_child(self, session: SessionInfo, ttl_s: int | None = None) -> Delegation:
        at = self._now()
        ttl = int(ttl_s if ttl_s is not None else self.config.identity.child_ticket_lifetime)
        if ttl <= 0:
            raise ServiceError(ErrorCode.INVALID_ARGUMENT, "ticket lifetime must be positive")
        with self.db.transaction() as conn:
            self._live_session(conn, session.session_id, at)
            grants = effective_grants(conn, session.agent_id, at)
            if not any(role_rank(role) >= role_rank(NamespaceRole.CONTRIBUTOR) for role in grants.values()):
                raise ServiceError(ErrorCode.FORBIDDEN, "contributor role is required to delegate a child")
            expires_at = at + timedelta(seconds=ttl)
            ticket_id = new_id("tkt")
            conn.execute(
                """
                INSERT INTO delegation_tickets
                    (ticket_id, parent_agent_id, root_agent_id, created_at, expires_at, used_at)
                VALUES (?, ?, ?, ?, ?, NULL)
                """,
                (ticket_id, session.agent_id, session.root_agent_id, iso(at), iso(expires_at)),
            )
            self._audit(
                conn,
                actor=session.agent_id,
                action="agent.delegate",
                object_type="delegation_ticket",
                object_id=ticket_id,
                details={"expires_at": iso(expires_at)},
                at=at,
            )
        return Delegation(
            ticket=ticket_id,
            parent_agent_id=session.agent_id,
            root_agent_id=session.root_agent_id,
            expires_at=expires_at,
        )

    # -- authentication --------------------------------------------------
    def authenticate(self, token: str | None) -> SessionInfo:
        if not token:
            raise ServiceError(ErrorCode.AUTH_REQUIRED, "a session token is required")
        digest = hash_token(token)
        at = self._now()
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE token_hash = ?", (digest,)).fetchone()
            if row is None or not hmac.compare_digest(str(row["token_hash"]), digest):
                raise ServiceError(ErrorCode.AUTH_REQUIRED, "session token is not registered")
            if row["revoked_at"] is not None:
                raise ServiceError(ErrorCode.SESSION_EXPIRED, "session was revoked")
            absolute = parse_iso(str(row["absolute_expires_at"]))
            idle = parse_iso(str(row["idle_expires_at"]))
            if absolute <= at:
                raise ServiceError(ErrorCode.SESSION_EXPIRED, "session absolute lifetime expired")
            if idle <= at:
                raise ServiceError(ErrorCode.SESSION_EXPIRED, "session idle timeout expired")
            agent = conn.execute(
                "SELECT revoked_at FROM agents WHERE agent_id = ?", (row["agent_id"],)
            ).fetchone()
            if agent is None or agent["revoked_at"] is not None:
                raise ServiceError(ErrorCode.SESSION_EXPIRED, "agent was revoked")
            refreshed = min(
                at + timedelta(seconds=int(self.config.identity.session_idle_timeout)),
                absolute,
            )
            conn.execute(
                "UPDATE sessions SET last_seen_at = ?, idle_expires_at = ? WHERE session_id = ?",
                (iso(at), iso(refreshed), row["session_id"]),
            )
            row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (row["session_id"],)).fetchone()
            assert row is not None
            return self._session_info(conn, row, at)

    def resolve_actor(
        self,
        token: str | None,
        actor_handle: str | None = None,
        *,
        connection_actors: Iterable[str] | None = None,
    ) -> SessionInfo:
        """Resolve the acting session for a token and optional actor handle.

        The gateway groups sessions into a transport connection. When it knows
        more than one actor is active on that connection it passes
        ``connection_actors``; a missing or foreign handle then fails closed with
        ``AUTH_REQUIRED``. With a single actor the handle may be omitted.
        """
        session = self.authenticate(token)
        handles = set(connection_actors) if connection_actors is not None else {session.actor_handle}
        if len(handles) > 1 and actor_handle is None:
            raise ServiceError(
                ErrorCode.AUTH_REQUIRED,
                "an actor handle is required when a connection has multiple actors",
            )
        if actor_handle is not None and (
            actor_handle not in handles or not hmac.compare_digest(actor_handle, session.actor_handle)
        ):
            raise ServiceError(ErrorCode.AUTH_REQUIRED, "actor handle does not belong to this session")
        return session

    def enforce_request_rate(self, session: SessionInfo) -> None:
        """Consume one per-session tool request token from ``[limits]``."""
        limits = self.config.limits
        allowed, retry = self._request_limiter.allow(
            f"request:{session.session_id}",
            limits.requests_per_minute / 60.0,
            float(limits.request_burst),
        )
        if not allowed:
            raise ServiceError(
                ErrorCode.RATE_LIMITED,
                "per-session request rate limit exceeded",
                retry_after_s=retry,
            )

    def session_close(self, session: SessionInfo) -> dict[str, Any]:
        at = self._now()
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session.session_id,)
            ).fetchone()
            if row is None:
                raise ServiceError(ErrorCode.NOT_FOUND, "session not found")
            released = conn.execute(
                "UPDATE review_jobs SET state = 'queued', lease_owner = NULL, lease_id = NULL, "
                "lease_expires_at = NULL WHERE lease_owner = ? AND state = 'leased'",
                (session.session_id,),
            ).rowcount
            conn.execute(
                "UPDATE sessions SET revoked_at = ? WHERE session_id = ? AND revoked_at IS NULL",
                (iso(at), session.session_id),
            )
            self._audit(
                conn,
                actor=session.agent_id,
                action="session.close",
                object_type="session",
                object_id=session.session_id,
                details={"leases_released": released},
                at=at,
            )
        return {"closed": True, "session_id": session.session_id, "leases_released": released}

    # -- mentor enrollment ----------------------------------------------
    @staticmethod
    def _mentor_from_row(row: sqlite3.Row) -> MentorRequest:
        scopes_raw = json.loads(str(row["scopes"]))
        scopes = tuple(str(item) for item in scopes_raw) if isinstance(scopes_raw, list) else ()
        return MentorRequest(
            request_id=str(row["request_id"]),
            code=str(row["code"]),
            state=str(row["state"]),
            namespace=str(row["namespace"]),
            scopes=scopes,
            expires_at=parse_iso(str(row["expires_at"])),
            approved_at=(
                parse_iso(str(row["decided_at"]))
                if row["decided_at"] and str(row["state"]) == "approved"
                else None
            ),
        )

    def request_mentor(
        self, session: SessionInfo, namespace: str, scopes: Iterable[str] = ()
    ) -> MentorRequest:
        at = self._now()
        cleaned = tuple(dict.fromkeys(str(scope).strip() for scope in scopes if str(scope).strip()))
        with self.db.transaction() as conn:
            self._live_session(conn, session.session_id, at)
            require_namespace(conn, namespace)
            if not can_read(conn, session.agent_id, namespace, at):
                raise ServiceError(ErrorCode.FORBIDDEN, "read access to the namespace is required")
            expires_at = at + timedelta(seconds=int(self.config.identity.mentor_request_lifetime))
            request_id = new_id("req")
            code = _mentor_code()
            conn.execute(
                """
                INSERT INTO mentor_requests
                    (request_id, agent_id, session_id, namespace, code, scopes, state, created_at,
                     expires_at, decided_at, decided_by)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, NULL, NULL)
                """,
                (
                    request_id,
                    session.agent_id,
                    session.session_id,
                    namespace,
                    code,
                    json.dumps(list(cleaned)),
                    iso(at),
                    iso(expires_at),
                ),
            )
            self._audit(
                conn,
                actor=session.agent_id,
                action="mentor.request",
                object_type="mentor_request",
                object_id=request_id,
                details={"namespace": namespace, "scopes": list(cleaned)},
                at=at,
            )
            row = conn.execute("SELECT * FROM mentor_requests WHERE request_id = ?", (request_id,)).fetchone()
            assert row is not None
            return self._mentor_from_row(row)

    def enrollment_status(self, session: SessionInfo, request_id: str) -> MentorRequest:
        at = self._now()
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM mentor_requests WHERE request_id = ? AND agent_id = ? AND session_id = ?",
                (request_id, session.agent_id, session.session_id),
            ).fetchone()
            if row is None:
                raise ServiceError(ErrorCode.NOT_FOUND, "mentor request not found")
            if row["state"] == "pending" and parse_iso(str(row["expires_at"])) <= at:
                conn.execute(
                    "UPDATE mentor_requests SET state = 'expired' WHERE request_id = ?",
                    (request_id,),
                )
                row = conn.execute(
                    "SELECT * FROM mentor_requests WHERE request_id = ?", (request_id,)
                ).fetchone()
                assert row is not None
            return self._mentor_from_row(row)

    def approve_mentor(
        self,
        request_id: str,
        role: str = "mentor",
        ttl_s: int | None = None,
        namespace: str | None = None,
        decided_by: str = "operator",
    ) -> MentorRequest:
        parsed_role = parse_role(role)
        at = self._now()
        ttl = int(ttl_s if ttl_s is not None else self.config.identity.mentor_approved_lifetime)
        if ttl <= 0:
            raise ServiceError(ErrorCode.INVALID_ARGUMENT, "approval lifetime must be positive")
        pending_error: ServiceError | None = None
        result: MentorRequest | None = None
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM mentor_requests WHERE request_id = ?", (request_id,)).fetchone()
            if row is None:
                raise ServiceError(ErrorCode.NOT_FOUND, "mentor request not found")
            state = str(row["state"])
            if state == "pending" and parse_iso(str(row["expires_at"])) <= at:
                conn.execute(
                    "UPDATE mentor_requests SET state = 'expired' WHERE request_id = ?",
                    (request_id,),
                )
                pending_error = ServiceError(ErrorCode.CONFLICT, "mentor request expired before approval")
            elif state != "pending":
                pending_error = ServiceError(ErrorCode.CONFLICT, f"mentor request is {state}, not pending")
            else:
                session_row = conn.execute(
                    "SELECT * FROM sessions WHERE session_id = ?", (row["session_id"],)
                ).fetchone()
                if session_row is None or session_row["revoked_at"] is not None:
                    raise ServiceError(ErrorCode.CONFLICT, "the requesting session is no longer active")
                target_namespace = namespace or str(row["namespace"])
                require_namespace(conn, target_namespace)
                expires_at = at + timedelta(seconds=ttl)
                self._insert_grant(
                    conn,
                    str(row["agent_id"]),
                    target_namespace,
                    parsed_role,
                    at,
                    expires_at=expires_at,
                )
                conn.execute(
                    "UPDATE mentor_requests SET state = 'approved', decided_at = ?, decided_by = ? "
                    "WHERE request_id = ?",
                    (iso(at), decided_by, request_id),
                )
                self._audit(
                    conn,
                    actor=None,
                    action="mentor.approve",
                    object_type="mentor_request",
                    object_id=request_id,
                    details={
                        "agent_id": str(row["agent_id"]),
                        "namespace": target_namespace,
                        "role": parsed_role.value,
                        "expires_at": iso(expires_at),
                        "decided_by": decided_by,
                    },
                    at=at,
                )
                refreshed = conn.execute(
                    "SELECT * FROM mentor_requests WHERE request_id = ?", (request_id,)
                ).fetchone()
                assert refreshed is not None
                result = self._mentor_from_row(refreshed)
        if pending_error is not None:
            raise pending_error
        assert result is not None
        return result

    def deny_mentor(self, request_id: str, decided_by: str = "operator") -> MentorRequest:
        at = self._now()
        pending_error: ServiceError | None = None
        result: MentorRequest | None = None
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM mentor_requests WHERE request_id = ?", (request_id,)).fetchone()
            if row is None:
                raise ServiceError(ErrorCode.NOT_FOUND, "mentor request not found")
            if str(row["state"]) != "pending":
                pending_error = ServiceError(
                    ErrorCode.CONFLICT, f"mentor request is {row['state']}, not pending"
                )
            else:
                conn.execute(
                    "UPDATE mentor_requests SET state = 'denied', decided_at = ?, decided_by = ? "
                    "WHERE request_id = ?",
                    (iso(at), decided_by, request_id),
                )
                self._audit(
                    conn,
                    actor=None,
                    action="mentor.deny",
                    object_type="mentor_request",
                    object_id=request_id,
                    details={"decided_by": decided_by},
                    at=at,
                )
                refreshed = conn.execute(
                    "SELECT * FROM mentor_requests WHERE request_id = ?", (request_id,)
                ).fetchone()
                assert refreshed is not None
                result = self._mentor_from_row(refreshed)
        if pending_error is not None:
            raise pending_error
        assert result is not None
        return result

    # -- grants and revocation ------------------------------------------
    def grants_add(self, agent_id: str, namespace: str, role: str) -> Grant:
        parsed_role = parse_role(role)
        at = self._now()
        with self.db.transaction() as conn:
            agent = conn.execute("SELECT agent_id FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
            if agent is None:
                raise ServiceError(ErrorCode.NOT_FOUND, "agent not found")
            require_namespace(conn, namespace)
            grant_id = self._insert_grant(conn, agent_id, namespace, parsed_role, at)
            self._audit(
                conn,
                actor=None,
                action="grant.add",
                object_type="grant",
                object_id=grant_id,
                details={"agent_id": agent_id, "namespace": namespace, "role": parsed_role.value},
                at=at,
            )
            row = conn.execute("SELECT * FROM grants WHERE grant_id = ?", (grant_id,)).fetchone()
            assert row is not None
            return Grant(
                grant_id=str(row["grant_id"]),
                agent_id=str(row["agent_id"]),
                namespace=str(row["namespace"]),
                role=NamespaceRole(str(row["role"])),
                created_at=parse_iso(str(row["created_at"])),
                expires_at=parse_iso(str(row["expires_at"])) if row["expires_at"] else None,
                revoked_at=None,
            )

    def revoke_session(self, session_or_id: SessionInfo | str) -> dict[str, Any]:
        session_id = session_or_id.session_id if isinstance(session_or_id, SessionInfo) else session_or_id
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
            if row is None:
                raise ServiceError(ErrorCode.NOT_FOUND, "session not found")
            return self._revoke_agent_family(conn, str(row["agent_id"]), actor=None)

    def revoke_agent(self, agent_id: str) -> dict[str, Any]:
        with self.db.transaction() as conn:
            return self._revoke_agent_family(conn, agent_id, actor=None)

    def _revoke_agent_family(
        self, conn: sqlite3.Connection, agent_id: str, *, actor: str | None
    ) -> dict[str, Any]:
        at = self._now()
        root = conn.execute("SELECT agent_id FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
        if root is None:
            raise ServiceError(ErrorCode.NOT_FOUND, "agent not found")
        rows = conn.execute(
            """
            WITH RECURSIVE family(agent_id) AS (
                SELECT agent_id FROM agents WHERE agent_id = ?
                UNION ALL
                SELECT a.agent_id FROM agents a JOIN family f ON a.parent_agent_id = f.agent_id
            )
            SELECT agent_id FROM family
            """,
            (agent_id,),
        ).fetchall()
        ids = [str(row["agent_id"]) for row in rows]
        placeholders = ",".join("?" for _ in ids)
        counts = {
            "agents": conn.execute(
                f"UPDATE agents SET revoked_at = ? WHERE agent_id IN ({placeholders}) AND revoked_at IS NULL",
                (iso(at), *ids),
            ).rowcount,
            "sessions": conn.execute(
                f"UPDATE sessions SET revoked_at = ? WHERE agent_id IN ({placeholders}) "
                "AND revoked_at IS NULL",
                (iso(at), *ids),
            ).rowcount,
            "grants": conn.execute(
                f"UPDATE grants SET revoked_at = ? WHERE agent_id IN ({placeholders}) AND revoked_at IS NULL",
                (iso(at), *ids),
            ).rowcount,
            "tickets": conn.execute(
                f"UPDATE delegation_tickets SET used_at = ? WHERE parent_agent_id IN ({placeholders}) "
                "AND used_at IS NULL",
                (iso(at), *ids),
            ).rowcount,
            "mentor_requests": conn.execute(
                f"UPDATE mentor_requests SET state = 'denied', decided_at = ?, decided_by = 'operator' "
                f"WHERE agent_id IN ({placeholders}) AND state = 'pending'",
                (iso(at), *ids),
            ).rowcount,
        }
        conn.execute(
            f"UPDATE review_jobs SET state = 'queued', lease_owner = NULL, lease_id = NULL, "
            f"lease_expires_at = NULL WHERE state = 'leased' AND lease_owner IN "
            f"(SELECT session_id FROM sessions WHERE agent_id IN ({placeholders}))",
            tuple(ids),
        )
        self._audit(
            conn,
            actor=actor,
            action="agent.revoke",
            object_type="agent",
            object_id=agent_id,
            details={"agent_ids": ids, **counts},
            at=at,
        )
        return {"agent_ids": ids, **counts}

    def list_agents(self) -> list[dict[str, Any]]:
        conn = self.db.connect()
        rows = conn.execute(
            "SELECT agent_id, root_agent_id, parent_agent_id, label, model, provider, task, "
            "enrollment_mode, created_at, revoked_at FROM agents ORDER BY created_at"
        ).fetchall()
        return [dict(row) for row in rows]

    def list_enrollments(self) -> list[dict[str, Any]]:
        conn = self.db.connect()
        rows = conn.execute(
            "SELECT request_id, agent_id, session_id, namespace, code, scopes, state, created_at, "
            "expires_at, decided_at, decided_by FROM mentor_requests ORDER BY created_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    def list_grants(self, agent_id: str | None = None) -> list[dict[str, Any]]:
        conn = self.db.connect()
        if agent_id is None:
            rows = conn.execute(
                "SELECT * FROM grants WHERE revoked_at IS NULL ORDER BY agent_id, namespace"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM grants WHERE agent_id = ? AND revoked_at IS NULL ORDER BY namespace",
                (agent_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def session_by_id(self, session_id: str) -> sqlite3.Row | None:
        row = (
            self.db.connect().execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        )
        return cast(sqlite3.Row | None, row)

    def agent_exists(self, agent_id: str) -> bool:
        return (
            self.db.connect().execute("SELECT 1 FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
            is not None
        )

    def can_mentor_namespace(self, agent_id: str, namespace: str) -> bool:
        return can_mentor(self.db.connect(), agent_id, namespace, self._now())


__all__ = [
    "IdentityManager",
    "generate_actor_handle",
    "generate_session_token",
    "hash_token",
    "new_id",
]
