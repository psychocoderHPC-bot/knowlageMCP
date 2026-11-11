# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Authorization, access-control, rate-limit, and review-eligibility policy.

Every rule here is transport independent. Transports must never implement their
own authorization checks: they call into the service, which calls these helpers.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from .db import iso
from .models import ErrorCode, NamespaceRole, ServiceError

#: Total order over namespace roles. Higher is more privileged.
ROLE_RANK: dict[NamespaceRole, int] = {
    NamespaceRole.READER: 1,
    NamespaceRole.CONTRIBUTOR: 2,
    NamespaceRole.REVIEWER: 3,
    NamespaceRole.MENTOR: 4,
    NamespaceRole.OPERATOR: 5,
}

#: Roles a self-registering root agent receives on the shared namespace.
DEFAULT_ROOT_ROLES: tuple[NamespaceRole, ...] = (
    NamespaceRole.READER,
    NamespaceRole.CONTRIBUTOR,
    NamespaceRole.REVIEWER,
)

#: Namespaces whose contents are operator-owned.
PERSONAL_KIND = "personal"


def now_from(clock: Callable[[], float]) -> datetime:
    """Convert an injectable epoch-seconds clock into an aware UTC datetime."""
    return datetime.fromtimestamp(clock(), UTC)


def role_rank(role: NamespaceRole | str) -> int:
    try:
        return ROLE_RANK[NamespaceRole(role)]
    except (ValueError, KeyError):
        return 0


def role_at_least(role: NamespaceRole | str, minimum: NamespaceRole) -> bool:
    return role_rank(role) >= ROLE_RANK[minimum]


def parse_role(role: str) -> NamespaceRole:
    try:
        return NamespaceRole(role)
    except ValueError as exc:
        raise ServiceError(ErrorCode.INVALID_ARGUMENT, f"unknown namespace role: {role!r}") from exc


def namespace_kind(conn: sqlite3.Connection, namespace: str) -> str | None:
    row = conn.execute("SELECT kind FROM namespaces WHERE name = ?", (namespace,)).fetchone()
    return None if row is None else str(row["kind"])


def require_namespace(conn: sqlite3.Connection, namespace: str) -> str:
    kind = namespace_kind(conn, namespace)
    if kind is None:
        raise ServiceError(ErrorCode.NOT_FOUND, f"namespace not found: {namespace}")
    return kind


def effective_grants(
    conn: sqlite3.Connection,
    agent_id: str,
    at: datetime,
) -> dict[str, NamespaceRole]:
    """Return the strongest live role per namespace for ``agent_id``."""
    rows = conn.execute(
        """
        SELECT namespace, role
        FROM grants
        WHERE agent_id = ?
          AND revoked_at IS NULL
          AND (expires_at IS NULL OR expires_at > ?)
        """,
        (agent_id, iso(at)),
    ).fetchall()
    best: dict[str, NamespaceRole] = {}
    for row in rows:
        namespace = str(row["namespace"])
        try:
            role = NamespaceRole(str(row["role"]))
        except ValueError:
            continue
        if namespace not in best or ROLE_RANK[role] > ROLE_RANK[best[namespace]]:
            best[namespace] = role
    return best


def effective_role(
    conn: sqlite3.Connection,
    agent_id: str,
    namespace: str,
    at: datetime,
) -> NamespaceRole | None:
    return effective_grants(conn, agent_id, at).get(namespace)


def readable_namespaces(conn: sqlite3.Connection, agent_id: str, at: datetime) -> dict[str, NamespaceRole]:
    """Return namespaces the agent may read, with the role that permits it."""
    readable: dict[str, NamespaceRole] = {}
    for namespace, role in effective_grants(conn, agent_id, at).items():
        kind = namespace_kind(conn, namespace)
        if kind is None:
            continue
        if kind == PERSONAL_KIND:
            if role_rank(role) >= ROLE_RANK[NamespaceRole.MENTOR]:
                readable[namespace] = role
        elif role_rank(role) >= ROLE_RANK[NamespaceRole.READER]:
            readable[namespace] = role
    return readable


def can_read(conn: sqlite3.Connection, agent_id: str, namespace: str, at: datetime) -> bool:
    kind = namespace_kind(conn, namespace)
    if kind is None:
        return False
    role = effective_role(conn, agent_id, namespace, at)
    if role is None:
        return False
    if kind == PERSONAL_KIND:
        return role_rank(role) >= ROLE_RANK[NamespaceRole.MENTOR]
    return role_rank(role) >= ROLE_RANK[NamespaceRole.READER]


def can_write(conn: sqlite3.Connection, agent_id: str, namespace: str, at: datetime) -> bool:
    kind = namespace_kind(conn, namespace)
    if kind is None:
        return False
    role = effective_role(conn, agent_id, namespace, at)
    if role is None:
        return False
    if kind == PERSONAL_KIND:
        return role_rank(role) >= ROLE_RANK[NamespaceRole.OPERATOR]
    return role_rank(role) >= ROLE_RANK[NamespaceRole.CONTRIBUTOR]


def can_review(conn: sqlite3.Connection, agent_id: str, namespace: str, at: datetime) -> bool:
    role = effective_role(conn, agent_id, namespace, at)
    return role is not None and role_rank(role) >= ROLE_RANK[NamespaceRole.REVIEWER]


def can_mentor(conn: sqlite3.Connection, agent_id: str, namespace: str, at: datetime) -> bool:
    role = effective_role(conn, agent_id, namespace, at)
    return role is not None and role_rank(role) >= ROLE_RANK[NamespaceRole.MENTOR]


def require_read(conn: sqlite3.Connection, agent_id: str, namespace: str, at: datetime) -> None:
    if not can_read(conn, agent_id, namespace, at):
        raise ServiceError(ErrorCode.FORBIDDEN, f"not authorized to read namespace {namespace!r}")


def require_write(conn: sqlite3.Connection, agent_id: str, namespace: str, at: datetime) -> None:
    if not can_write(conn, agent_id, namespace, at):
        raise ServiceError(ErrorCode.FORBIDDEN, f"contributor role required in namespace {namespace!r}")


def require_review(conn: sqlite3.Connection, agent_id: str, namespace: str, at: datetime) -> None:
    if not can_review(conn, agent_id, namespace, at):
        raise ServiceError(ErrorCode.REVIEW_INELIGIBLE, f"reviewer role required in namespace {namespace!r}")


def _contributor_ids(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def family_roots(
    conn: sqlite3.Connection,
    *,
    author_root_id: str,
    contributors: str | Iterable[str] | None,
) -> set[str]:
    """Return the root-family ids that participated in a revision."""
    roots = {author_root_id}
    if isinstance(contributors, str):
        contributor_ids = _contributor_ids(contributors)
    else:
        contributor_ids = [str(item) for item in (contributors or ())]
    for agent_id in contributor_ids:
        if agent_id in roots:
            continue
        row = conn.execute("SELECT root_agent_id FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
        if row is not None:
            roots.add(str(row["root_agent_id"]))
        elif agent_id.startswith(("root_", "agt_")):
            # Imported or historical contributor identifiers may already be roots.
            roots.add(agent_id)
    return roots


def is_own_family(
    conn: sqlite3.Connection,
    *,
    agent_root_id: str,
    author_root_id: str,
    contributors: str | Iterable[str] | None,
) -> bool:
    return agent_root_id in family_roots(conn, author_root_id=author_root_id, contributors=contributors)


def audit(
    conn: sqlite3.Connection,
    at: datetime,
    *,
    actor_agent_id: str | None,
    action: str,
    object_type: str | None = None,
    object_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append an audit row and return the event payload for the event bus."""
    encoded = None if details is None else json.dumps(details, sort_keys=True, default=str)
    cursor = conn.execute(
        """
        INSERT INTO audit_events (ts, actor_agent_id, action, object_type, object_id, details)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (iso(at), actor_agent_id, action, object_type, object_id, encoded),
    )
    return {
        "event": "audit",
        "event_id": int(cursor.lastrowid or 0),
        "ts": iso(at),
        "actor_agent_id": actor_agent_id,
        "action": action,
        "object_type": object_type,
        "object_id": object_id,
        "details": details or {},
    }


class SlidingWindowLimiter:
    """Bounded in-memory sliding-window limiter with an injectable clock.

    It is intentionally process local: registration limits protect a single
    gateway process, while durable state lives in SQLite. A restart clears the
    window, which matches the boot policy of invalidating live sessions.
    """

    def __init__(self, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._now = monotonic
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window_s: float) -> tuple[bool, int]:
        """Record one hit for ``key``; return ``(allowed, retry_after_seconds)``."""
        if limit <= 0:
            return True, 0
        now = float(self._now())
        cutoff = now - window_s
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= limit:
                retry = max(1, math.ceil(hits[0] + window_s - now))
                return False, retry
            hits.append(now)
            return True, 0

    def peek(self, key: str, window_s: float) -> int:
        now = float(self._now())
        cutoff = now - window_s
        with self._lock:
            hits = self._hits.get(key)
            if not hits:
                return 0
            while hits and hits[0] <= cutoff:
                hits.popleft()
            return len(hits)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


__all__ = [
    "DEFAULT_ROOT_ROLES",
    "PERSONAL_KIND",
    "ROLE_RANK",
    "SlidingWindowLimiter",
    "audit",
    "can_mentor",
    "can_read",
    "can_review",
    "can_write",
    "effective_grants",
    "effective_role",
    "family_roots",
    "is_own_family",
    "namespace_kind",
    "now_from",
    "parse_role",
    "readable_namespaces",
    "require_namespace",
    "require_read",
    "require_review",
    "require_write",
    "role_at_least",
    "role_rank",
]
