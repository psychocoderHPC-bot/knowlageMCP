# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Transport-independent service facade.

Every authorization decision, budget, lease, and lifecycle transition lives in
this module or the focused modules it composes. Transports resolve a session,
call one method, and map :class:`ServiceError` to an envelope.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.resources
import json
import os
import queue
import shutil
import socket
import sqlite3
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from . import PROTOCOL_VERSION, SKILL_RESOURCE_NAME, SKILL_RESOURCE_URI, TOOL_NAMES, __version__
from .backup import BackupManager
from .config import (
    Config,
    config_dir,
    diff_keys,
    load_config,
    parse_duration,
    show_config,
    validate_config,
)
from .db import Database, iso
from .evidence import EvidenceStore
from .identity import IdentityManager
from .knowledge import KnowledgeManager, build_entry_detail
from .migrations import MAX_VERSION
from .models import (
    ContextBundle,
    Delegation,
    EntryDetail,
    EntryPayload,
    ErrorCode,
    Kind,
    MentorRequest,
    ProposeRevisionRequest,
    ProtocolInfo,
    RecordOutcomeRequest,
    RecordRequest,
    Registration,
    ReviewFilters,
    ReviewJob,
    Scope,
    SearchFilters,
    SearchRequest,
    SearchResponse,
    ServiceError,
    SessionInfo,
    SubmitReviewRequest,
    SubmitReviewResult,
    TaskOutcome,
    WriteResult,
)
from .policy import audit, namespace_kind, now_from, require_namespace
from .retrieval import RetrievalManager
from .review import ReviewManager

#: Doctor thresholds. A warning is informational for operators, never a gate.
DOCTOR_MIN_FREE_BYTES = 1024**3
DOCTOR_MIN_FREE_FRACTION = 0.05
DOCTOR_PROBE_TIMEOUT_S = 0.25
CONTAINER_HOST = "host.containers.internal"
TEROK_SHIELD_HINT = "the terok Shield grant localhost:2230 may require task recreation after a network change"


class _DryRunRollback(Exception):
    """Internal sentinel that rolls back a dry-run import transaction."""


class EventBus:
    """Thread-safe, bounded fan-out bus with drop-oldest backpressure.

    Each subscriber gets its own queue. A slow consumer never blocks writers:
    when a queue is full its oldest event is discarded first.
    """

    def __init__(self, maxsize: int = 256, *, max_queue: int | None = None) -> None:
        self.max_queue = max(1, maxsize if max_queue is None else max_queue)
        self._subscribers: set[queue.Queue[dict[str, Any]]] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        subscriber: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=self.max_queue)
        with self._lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                with contextlib.suppress(queue.Empty):
                    subscriber.get_nowait()
                with contextlib.suppress(queue.Full):
                    subscriber.put_nowait(event)

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)


class KnowledgeService:
    """Single facade used by HTTP, MCP, admin socket, and CLI transports."""

    def __init__(
        self,
        config: Config,
        db: Database,
        evidence: EvidenceStore,
        *,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        config_path: Path | str | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self.evidence = evidence
        self._config_path = Path(config_path) if config_path is not None else None
        self._clock = clock
        self._monotonic = monotonic
        self._started = monotonic()
        self._event_bus: EventBus | None = None
        self._bus_lock = threading.Lock()
        self.identity = IdentityManager(
            config, db, clock=clock, monotonic=monotonic, event_sink=self._publish
        )
        self.knowledge = KnowledgeManager(config, db, evidence, clock=clock, event_sink=self._publish)
        self.retrieval = RetrievalManager(config, db, clock=clock)
        self.review = ReviewManager(config, db, clock=clock, event_sink=self._publish)
        self.backup = BackupManager(config, db, evidence, clock=clock)

    # -- event bus -------------------------------------------------------
    def _bus(self) -> EventBus:
        if self._event_bus is None:
            with self._bus_lock:
                if self._event_bus is None:
                    self._event_bus = EventBus()
        return self._event_bus

    def _publish(self, event: dict[str, Any]) -> None:
        self._bus().publish(event)

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        """Return a bounded event queue used by ``gatewayctl watch``."""
        return self._bus().subscribe()

    def unsubscribe(self, subscriber: queue.Queue[dict[str, Any]]) -> None:
        self._bus().unsubscribe(subscriber)

    def publish(self, event: dict[str, Any]) -> None:
        """Publish one event to all watchers (used by internal audit paths)."""
        self._bus().publish(event)

    def _now(self) -> datetime:
        return now_from(self._clock)

    def close(self) -> None:
        self.db.close()

    # -- startup ---------------------------------------------------------
    def bootstrap(self) -> dict[str, Any]:
        version = self.db.migrate()
        counts = self.identity.startup_reset()
        return {
            "schema_version": version,
            "namespaces": [
                self.config.identity.initial_namespace,
                self.config.identity.personal_namespace,
            ],
            **counts,
        }

    # -- identity --------------------------------------------------------
    def register(
        self,
        mode: str = "root",
        ticket: str | None = None,
        label: str | None = None,
        model: str | None = None,
        provider: str | None = None,
        task: str | None = None,
        source: str = "unknown",
    ) -> Registration:
        return self.identity.register(
            mode=mode,
            ticket=ticket,
            label=label,
            model=model,
            provider=provider,
            task=task,
            source=source,
        )

    def delegate_child(self, session: SessionInfo, ttl_s: int | None = None) -> Delegation:
        return self.identity.delegate_child(session, ttl_s)

    def request_mentor(
        self, session: SessionInfo, namespace: str, scopes: Iterable[str] = ()
    ) -> MentorRequest:
        return self.identity.request_mentor(session, namespace, scopes)

    def enrollment_status(self, session: SessionInfo, request_id: str) -> MentorRequest:
        return self.identity.enrollment_status(session, request_id)

    def authenticate(self, token: str | None) -> SessionInfo:
        return self.identity.authenticate(token)

    def resolve_actor(
        self,
        token: str | None,
        actor_handle: str | None = None,
        *,
        connection_actors: Iterable[str] | None = None,
    ) -> SessionInfo:
        return self.identity.resolve_actor(token, actor_handle, connection_actors=connection_actors)

    def enforce_request_rate(self, session: SessionInfo) -> None:
        """Consume one per-session and one process-wide request token."""
        self.identity.enforce_request_rate(session)

    def session_close(self, session: SessionInfo) -> dict[str, Any]:
        return self.identity.session_close(session)

    def get_protocol_info(self) -> ProtocolInfo:
        return ProtocolInfo(
            version=__version__,
            protocol_version=PROTOCOL_VERSION,
            schema_version=MAX_VERSION,
            tool_names=TOOL_NAMES,
            resource_uri=SKILL_RESOURCE_URI,
            features=(
                "fts5",
                "exact_phrase",
                "delegation",
                "mentor_enrollment",
                "lease_reviews",
                "deletion_ledger",
                "sqlite_online_backup",
                "jsonl_transfer",
            ),
        )

    # -- knowledge -------------------------------------------------------
    def record(self, session: SessionInfo, request: RecordRequest) -> WriteResult:
        return self.knowledge.record(session, request)

    def propose_revision(self, session: SessionInfo, request: ProposeRevisionRequest) -> WriteResult:
        return self.knowledge.propose_revision(session, request)

    def record_outcome(self, session: SessionInfo, request: RecordOutcomeRequest) -> TaskOutcome:
        return self.knowledge.record_outcome(session, request)

    # -- retrieval -------------------------------------------------------
    def search(
        self,
        session: SessionInfo,
        request: SearchRequest | str | None = None,
        filters: SearchFilters | None = None,
        budget: int | None = None,
        *,
        query: str | None = None,
        max_results: int | None = None,
        cursor: str | None = None,
    ) -> SearchResponse:
        if isinstance(request, SearchRequest):
            resolved = request
        else:
            text = query if query is not None else (request if isinstance(request, str) else "")
            resolved = SearchRequest(
                query=text,
                filters=filters or SearchFilters(),
                max_results=max_results,
                max_bytes=budget,
                cursor=cursor,
            )
        return self.retrieval.search(session, resolved)

    def get_context(
        self,
        session: SessionInfo,
        task: str,
        scope: Scope | Mapping[str, Any] | None = None,
    ) -> ContextBundle:
        resolved = scope if isinstance(scope, Scope) else Scope.model_validate(scope or {})
        return self.retrieval.get_context(session, task, resolved)

    def get_entry(
        self,
        session: SessionInfo,
        entry_id: str,
        revision_id: str | None = None,
        page: int | None = None,
        cursor: str | None = None,
    ) -> EntryDetail:
        return self.retrieval.get_entry(session, entry_id, revision_id, page, cursor)

    # -- review ----------------------------------------------------------
    def review_next(
        self,
        session: SessionInfo,
        filters: ReviewFilters | Mapping[str, Any] | None = None,
    ) -> ReviewJob | None:
        resolved = (
            filters if isinstance(filters, ReviewFilters) else ReviewFilters.model_validate(filters or {})
        )
        return self.review.review_next(session, resolved)

    def submit_review(self, session: SessionInfo, request: SubmitReviewRequest) -> SubmitReviewResult:
        return self.review.submit_review(session, request)

    # -- admin: status and diagnostics ----------------------------------
    def status(self) -> dict[str, Any]:
        conn = self.db.connect()
        now = self._now()

        def count(sql: str, *params: Any) -> int:
            row = conn.execute(sql, params).fetchone()
            return int(row[0]) if row is not None else 0

        database_bytes = 0
        with contextlib.suppress(OSError):
            database_bytes = Path(self.db.path).stat().st_size
        return {
            "version": __version__,
            "protocol_version": PROTOCOL_VERSION,
            "schema_version": self.db.schema_version(),
            "uptime_s": max(0.0, self._monotonic() - self._started),
            "now": iso(now),
            "agents_active": count("SELECT COUNT(*) FROM agents WHERE revoked_at IS NULL"),
            "sessions_active": count(
                "SELECT COUNT(*) FROM sessions WHERE revoked_at IS NULL "
                "AND absolute_expires_at > ? AND idle_expires_at > ?",
                iso(now),
                iso(now),
            ),
            "namespaces": count("SELECT COUNT(*) FROM namespaces"),
            "entries": count("SELECT COUNT(*) FROM entries"),
            "revisions": count("SELECT COUNT(*) FROM entry_revisions"),
            "reviews": count("SELECT COUNT(*) FROM reviews"),
            "review_jobs_queued": count(
                "SELECT COUNT(*) FROM review_jobs WHERE state IN ('queued', 'leased')"
            ),
            "pending_enrollments": count("SELECT COUNT(*) FROM mentor_requests WHERE state = 'pending'"),
            "audit_events": count("SELECT COUNT(*) FROM audit_events"),
            "deletion_ledger": count("SELECT COUNT(*) FROM deletion_ledger"),
            "database_bytes": database_bytes,
            "evidence_bytes": self.evidence.total_bytes(),
            "event_subscribers": self._bus().subscriber_count(),
        }

    def doctor(self) -> dict[str, Any]:
        """Run the documented diagnostics surface without leaking secrets."""
        checks: list[dict[str, Any]] = []

        def add(name: str, ok: bool, detail: str = "") -> None:
            checks.append({"name": name, "ok": bool(ok), "detail": detail})

        problems = validate_config(self.config)
        add("config", not problems, "; ".join(problems) if problems else "valid")

        config_file = self._config_path or (config_dir() / "config.toml")
        try:
            if config_file.exists():
                mode = stat.S_IMODE(config_file.stat().st_mode)
                add(
                    "config_file",
                    mode == 0o600,
                    f"{config_file} mode {mode:04o}" + ("" if mode == 0o600 else " (expected 0600)"),
                )
            else:
                add("config_file", True, f"{config_file} not present (built-in defaults)")
        except OSError as exc:
            add("config_file", False, f"{config_file}: {exc}")

        directories = (
            ("data_dir", Path(self.db.path).parent),
            ("evidence_dir", Path(self.config.storage.evidence_dir)),
            ("backup_dir", Path(self.config.storage.backup_dir)),
        )
        for name, directory in directories:
            try:
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                mode = stat.S_IMODE(directory.stat().st_mode)
                probe = directory / ".doctor-probe"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink()
                ok = mode == 0o700
                add(name, ok, f"{directory} mode {mode:04o}" + ("" if ok else " (expected 0700)"))
            except OSError as exc:
                add(name, False, f"{directory}: {exc}")

        try:
            conn = self.db.connect()
            add("database", True, str(self.db.path))
            version = self.db.schema_version()
            add("schema_version", version == MAX_VERSION, f"{version} (expected {MAX_VERSION})")
            # One integrity pass only; it is the most expensive pragma here.
            integrity = self.db.integrity_check()
            add("integrity", integrity == "ok", integrity)
            fts_rows = conn.execute("SELECT COUNT(*) FROM entry_fts").fetchone()
            add("fts5", fts_rows is not None, "entry_fts queryable")
            fk = conn.execute("PRAGMA foreign_key_check").fetchall()
            add("foreign_keys", not fk, f"{len(fk)} violations")
        except (sqlite3.DatabaseError, OSError) as exc:
            add("database", False, str(exc))

        try:
            usage = shutil.disk_usage(Path(self.db.path).parent)
            fraction = (usage.free / usage.total) if usage.total else 0.0
            free_ok = usage.free >= DOCTOR_MIN_FREE_BYTES and fraction >= DOCTOR_MIN_FREE_FRACTION
            add(
                "free_space",
                free_ok,
                f"{usage.free / 1024**3:.2f} GiB free ({fraction * 100:.1f}%); warn under 1 GiB or 5%",
            )
        except OSError as exc:
            add("free_space", False, str(exc))

        host = self.config.server.host
        port = self.config.server.port
        probe_host = host
        if host in {"", "0.0.0.0"}:
            probe_host = "127.0.0.1"
        elif host == "::":
            probe_host = "::1"
        try:
            with socket.create_connection((probe_host, port), timeout=DOCTOR_PROBE_TIMEOUT_S):
                add("listener", True, f"accepting connections at {host}:{port}")
        except OSError as exc:
            add("listener", False, f"{host}:{port} not accepting connections ({exc})")

        socket_path = Path(self.config.storage.runtime_socket)
        try:
            status = socket_path.stat()
            mode = stat.S_IMODE(status.st_mode)
            ok = stat.S_ISSOCK(status.st_mode) and mode == 0o600 and status.st_uid == os.geteuid()
            add(
                "admin_socket",
                ok,
                f"{socket_path} mode {mode:04o} owner {status.st_uid}",
            )
        except FileNotFoundError:
            add("admin_socket", False, f"{socket_path} not present")
        except OSError as exc:
            add("admin_socket", False, f"{socket_path}: {exc}")

        configured_container = host == CONTAINER_HOST or CONTAINER_HOST in self.config.server.allowed_hosts
        if not configured_container:
            add("container_reachability", True, f"{CONTAINER_HOST} not configured; {TEROK_SHIELD_HINT}")
        else:
            try:
                socket.getaddrinfo(CONTAINER_HOST, None)
                add("container_reachability", True, f"{CONTAINER_HOST} resolves; {TEROK_SHIELD_HINT}")
            except OSError as exc:
                # A bind-only target is a client-side hint, not a host failure.
                required = host == CONTAINER_HOST
                add(
                    "container_reachability",
                    not required,
                    f"{CONTAINER_HOST} not resolvable ({exc}); {TEROK_SHIELD_HINT}",
                )

        return {"ok": all(check["ok"] for check in checks), "checks": checks}

    def list_agents(self) -> list[dict[str, Any]]:
        return self.identity.list_agents()

    def list_enrollments(self) -> list[dict[str, Any]]:
        return self.identity.list_enrollments()

    def list_reviews(self) -> list[dict[str, Any]]:
        return self.review.list_reviews()

    def list_review_jobs(self) -> list[dict[str, Any]]:
        return self.review.list_jobs()

    # -- admin: enrollment and revocation --------------------------------
    def approve(
        self,
        request_id: str,
        role: str = "mentor",
        ttl: int | str | None = None,
        namespace: str | None = None,
    ) -> MentorRequest:
        ttl_s: int | None
        ttl_s = parse_duration(ttl) if isinstance(ttl, str) else ttl
        return self.identity.approve_mentor(request_id, role=role, ttl_s=ttl_s, namespace=namespace)

    def deny(self, request_id: str) -> MentorRequest:
        return self.identity.deny_mentor(request_id)

    def revoke(self, session_or_agent_id: str) -> dict[str, Any]:
        conn = self.db.connect()
        session = conn.execute(
            "SELECT session_id FROM sessions WHERE session_id = ?", (session_or_agent_id,)
        ).fetchone()
        if session is not None:
            return self.identity.revoke_session(str(session["session_id"]))
        agent = conn.execute(
            "SELECT agent_id FROM agents WHERE agent_id = ?", (session_or_agent_id,)
        ).fetchone()
        if agent is not None:
            return self.identity.revoke_agent(str(agent["agent_id"]))
        raise ServiceError(ErrorCode.NOT_FOUND, "session or agent not found")

    # -- admin: namespaces and grants -----------------------------------
    @staticmethod
    def _namespace_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "name": str(row["name"]),
            "kind": str(row["kind"]),
            "description": row["description"],
            "created_at": row["created_at"],
        }

    def namespaces_list(self) -> list[dict[str, Any]]:
        rows = (
            self.db.connect()
            .execute("SELECT name, kind, description, created_at FROM namespaces ORDER BY name")
            .fetchall()
        )
        return [self._namespace_row(row) for row in rows]

    def namespace_create(
        self, name: str, kind: str = "shared", description: str | None = None
    ) -> dict[str, Any]:
        cleaned = name.strip()
        if not cleaned or any(char in cleaned for char in ("/", "\\", "\x00")) or cleaned in {".", ".."}:
            raise ServiceError(ErrorCode.INVALID_ARGUMENT, "invalid namespace name")
        at = self._now()
        with self.db.transaction() as conn:
            existing = namespace_kind(conn, cleaned)
            if existing is not None:
                if existing != kind:
                    raise ServiceError(ErrorCode.CONFLICT, "namespace already exists with a different kind")
                row = conn.execute(
                    "SELECT name, kind, description, created_at FROM namespaces WHERE name = ?",
                    (cleaned,),
                ).fetchone()
                assert row is not None
                return self._namespace_row(row)
            conn.execute(
                "INSERT INTO namespaces (name, kind, description, created_at) VALUES (?, ?, ?, ?)",
                (cleaned, kind, description, iso(at)),
            )
            self._publish(
                audit(
                    conn,
                    at,
                    actor_agent_id=None,
                    action="namespace.create",
                    object_type="namespace",
                    object_id=cleaned,
                    details={"kind": kind},
                )
            )
            return {
                "name": cleaned,
                "kind": kind,
                "description": description,
                "created_at": iso(at),
            }

    def grants_add(self, agent_id: str, namespace: str, role: str) -> dict[str, Any]:
        grant = self.identity.grants_add(agent_id, namespace, role)
        return grant.model_dump(mode="json")

    def admin_search(self, query: str, namespace: str | None = None) -> SearchResponse:
        return self.retrieval.admin_search(query, namespace=namespace)

    def inspect(self, entry_id: str) -> dict[str, Any]:
        conn = self.db.connect()
        detail = build_entry_detail(conn, entry_id)
        revisions = conn.execute(
            "SELECT revision_id, lifecycle, evidence_class, title, author_agent_id, "
            "author_root_id, created_at, last_verified_at FROM entry_revisions "
            "WHERE entry_id = ? ORDER BY created_at DESC",
            (entry_id,),
        ).fetchall()
        return {
            "entry": detail.model_dump(mode="json"),
            "revisions": [dict(row) for row in revisions],
            "namespaces": self.namespaces_list(),
        }

    # -- admin: operator memory -----------------------------------------
    def memory_add(
        self,
        kind: str,
        namespace: str,
        text: str,
        title: str | None = None,
        source_role: str = "operator",
    ) -> WriteResult:
        return self.knowledge.memory_add(
            kind=kind, namespace=namespace, text=text, title=title, source_role=source_role
        )

    def memory_correct(self, entry_id: str, expected_revision: str, text: str) -> WriteResult:
        return self.knowledge.memory_correct(entry_id, expected_revision, text)

    def memory_retract(self, entry_id: str, reason: str) -> dict[str, Any]:
        return self.knowledge.memory_retract(entry_id, reason)

    def memory_forget(self, entry_id: str, dry_run: bool = False) -> dict[str, Any]:
        return self.knowledge.memory_forget(entry_id, dry_run)

    # -- admin: backup ---------------------------------------------------
    def backup_create(self) -> dict[str, Any]:
        return self.backup.create()

    def backup_verify(self, path: str | Path) -> dict[str, Any]:
        return self.backup.verify(path)

    def backup_restore(self, path: str | Path, destination: str | Path) -> dict[str, Any]:
        return self.backup.restore(path, destination)

    # -- admin: index and config ----------------------------------------
    def index_rebuild(self) -> dict[str, Any]:
        return self.retrieval.index_rebuild()

    def config_check(self) -> dict[str, Any]:
        problems = validate_config(self.config)
        return {"ok": not problems, "problems": problems}

    def config_show(self, redacted: bool = True) -> str:
        return show_config(self.config, redacted=redacted)

    def reload_config(self, path: str | Path | None = None) -> dict[str, Any]:
        new_config = load_config(Path(path) if path is not None else None)
        problems = validate_config(new_config)
        if problems:
            raise ServiceError(ErrorCode.INVALID_ARGUMENT, "; ".join(problems))
        changes = diff_keys(self.config, new_config)
        self.config = new_config
        self.identity.config = new_config
        self.knowledge.config = new_config
        self.retrieval.config = new_config
        self.review.config = new_config
        self.backup.config = new_config
        return {
            "reloaded": True,
            "changed_keys": sorted(changes),
            "changes": {key: list(value) for key, value in changes.items()},
        }

    # -- admin: smoke test ----------------------------------------------
    def smoke_test(self) -> dict[str, Any]:
        namespace = self.config.identity.initial_namespace
        probe = "smoke test probe " + iso(self._now())
        registration = self.register(
            mode="root", label="smoke-test", model="gateway", provider="gateway", source="smoke-test"
        )
        session = self.authenticate(registration.session_token)
        entry_id: str | None = None
        found = False
        try:
            write = self.record(
                session,
                RecordRequest(
                    namespace=namespace,
                    entry=EntryPayload(kind=Kind.OBSERVATION, title="gateway smoke test", claim=probe),
                ),
            )
            entry_id = write.entry.entry_id
            response = self.search(
                session,
                SearchRequest(
                    query=probe,
                    filters=SearchFilters(namespace=namespace),
                ),
            )
            found = any(result.entry_id == entry_id for result in response.results)
        finally:
            with contextlib.suppress(ServiceError):
                self.session_close(session)
            if entry_id is not None:
                with contextlib.suppress(ServiceError):
                    self.memory_forget(entry_id, dry_run=False)
        return {"ok": found, "namespace": namespace, "recorded": entry_id is not None, "searchable": found}

    # -- admin: JSONL transfer ------------------------------------------
    @staticmethod
    def _json_error(message: str) -> ServiceError:
        return ServiceError(ErrorCode.INVALID_ARGUMENT, message)

    @staticmethod
    def _safe_relative(value: str) -> bool:
        if not value:
            return True
        path = Path(value)
        if path.is_absolute():
            return False
        return ".." not in path.parts

    def _export_revision(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        revision_id = str(row["revision_id"])
        sources = [
            {
                "source_id": str(source["source_id"]),
                "kind": str(source["kind"]),
                "role": str(source["role"]),
                "identity": source["identity"],
                "uri": source["uri"],
                "snapshot_hash": source["snapshot_hash"],
                "trust": str(source["trust"]),
                "detail": source["detail"],
                "created_at": source["created_at"],
            }
            for source in conn.execute(
                "SELECT * FROM sources WHERE revision_id = ? ORDER BY created_at", (revision_id,)
            ).fetchall()
        ]
        reviews = [
            {
                "review_id": str(review["review_id"]),
                "reviewer_agent_id": str(review["reviewer_agent_id"]),
                "reviewer_root_id": str(review["reviewer_root_id"]),
                "result": str(review["result"]),
                "method": str(review["method"]),
                "evidence": review["evidence"],
                "conditions": review["conditions"],
                "limitations": review["limitations"],
                "created_at": review["created_at"],
            }
            for review in conn.execute(
                "SELECT * FROM reviews WHERE revision_id = ? ORDER BY created_at", (revision_id,)
            ).fetchall()
        ]
        return {
            "revision_id": revision_id,
            "prev_revision_id": row["prev_revision_id"],
            "kind": str(row["kind"]),
            "title": str(row["title"]),
            "claim": str(row["claim"]),
            "conditions": row["conditions"],
            "expected_result": row["expected_result"],
            "limitations": row["limitations"],
            "tags": json.loads(str(row["tags"] or "[]")),
            "scope": json.loads(str(row["scope"] or "{}")),
            "observed_at": row["observed_at"],
            "valid_from": row["valid_from"],
            "valid_until": row["valid_until"],
            "last_verified_at": row["last_verified_at"],
            "author_agent_id": str(row["author_agent_id"]),
            "author_root_id": str(row["author_root_id"]),
            "contributors": json.loads(str(row["contributors"] or "[]")),
            "source_role": str(row["source_role"]),
            "evidence_class": str(row["evidence_class"]),
            "sensitivity": str(row["sensitivity"]),
            "lifecycle": str(row["lifecycle"]),
            "content_hash": str(row["content_hash"]),
            "created_at": row["created_at"],
            "sources": sources,
            "reviews": reviews,
        }

    def export_jsonl(self, namespace: str | None, path: str | Path) -> dict[str, Any]:
        conn = self.db.connect()
        if namespace is not None:
            require_namespace(conn, namespace)
        target = Path(path)
        if target.exists() and target.is_dir():
            raise self._json_error("export path must be a file")
        at = self._now()
        lines: list[str] = [
            json.dumps(
                {
                    "type": "header",
                    "schema_version": 1,
                    "app_version": __version__,
                    "created_at": iso(at),
                },
                sort_keys=True,
            )
        ]
        if namespace is None:
            namespace_rows = conn.execute(
                "SELECT name, kind, description, created_at FROM namespaces ORDER BY name"
            ).fetchall()
            entries = conn.execute("SELECT * FROM entries ORDER BY created_at").fetchall()
        else:
            namespace_rows = conn.execute(
                "SELECT name, kind, description, created_at FROM namespaces WHERE name = ?",
                (namespace,),
            ).fetchall()
            entries = conn.execute(
                "SELECT * FROM entries WHERE namespace = ? ORDER BY created_at", (namespace,)
            ).fetchall()
        for row in namespace_rows:
            lines.append(
                json.dumps(
                    {
                        "type": "namespace",
                        "name": str(row["name"]),
                        "kind": str(row["kind"]),
                        "description": row["description"],
                        "created_at": row["created_at"],
                    },
                    sort_keys=True,
                )
            )
        revision_count = 0
        for entry in entries:
            revisions = conn.execute(
                "SELECT * FROM entry_revisions WHERE entry_id = ? ORDER BY created_at",
                (str(entry["entry_id"]),),
            ).fetchall()
            revision_count += len(revisions)
            lines.append(
                json.dumps(
                    {
                        "type": "entry",
                        "entry_id": str(entry["entry_id"]),
                        "namespace": str(entry["namespace"]),
                        "created_at": entry["created_at"],
                        "created_by_agent_id": str(entry["created_by_agent_id"]),
                        "current_revision_id": entry["current_revision_id"],
                        "revisions": [self._export_revision(conn, revision) for revision in revisions],
                    },
                    sort_keys=True,
                )
            )
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".export-", suffix=".jsonl")
        tmp_path = Path(tmp_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, target)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                tmp_path.unlink()
            raise
        return {
            "path": str(target),
            "entries": len(entries),
            "revisions": revision_count,
            "namespaces": len(namespace_rows),
        }

    def import_jsonl(self, path: str | Path, dry_run: bool = False) -> dict[str, Any]:
        source = Path(path)
        if not source.is_file():
            raise self._json_error("import path must be an existing file")
        records: list[dict[str, Any]] = []
        try:
            with source.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    text = line.strip()
                    if not text:
                        continue
                    parsed = json.loads(text)
                    if not isinstance(parsed, dict):
                        raise self._json_error(f"line {line_number} is not an object")
                    records.append(parsed)
        except (OSError, ValueError) as exc:
            raise self._json_error("import file is not valid JSONL") from exc
        if not records or records[0].get("type") != "header":
            raise self._json_error("import file is missing a header")
        try:
            schema_version = int(records[0].get("schema_version", 0))
        except (TypeError, ValueError) as exc:
            raise self._json_error("invalid import schema version") from exc
        if schema_version < 1 or schema_version > 1:
            raise self._json_error("unsupported import schema version")

        at = self._now()
        counts = {
            "created_entries": 0,
            "created_revisions": 0,
            "skipped": 0,
            "conflicts": 0,
            "namespaces_created": 0,
            "reviews": 0,
            "dry_run": dry_run,
        }
        try:
            with self.db.transaction() as conn:
                for record in records[1:]:
                    record_type = record.get("type")
                    if record_type == "namespace":
                        name = str(record.get("name", "")).strip()
                        if not name or name in {".", ".."} or any(c in name for c in ("/", "\\", "\x00")):
                            counts["conflicts"] += 1
                            continue
                        if namespace_kind(conn, name) is None:
                            conn.execute(
                                "INSERT INTO namespaces (name, kind, description, created_at) "
                                "VALUES (?, ?, ?, ?)",
                                (
                                    name,
                                    str(record.get("kind", "shared")),
                                    record.get("description"),
                                    iso(at),
                                ),
                            )
                            counts["namespaces_created"] += 1
                        continue
                    if record_type != "entry":
                        counts["conflicts"] += 1
                        continue
                    namespace = str(record.get("namespace", "")).strip()
                    entry_id = str(record.get("entry_id", "")).strip()
                    if not namespace or not entry_id:
                        counts["conflicts"] += 1
                        continue
                    if namespace_kind(conn, namespace) is None:
                        conn.execute(
                            "INSERT INTO namespaces (name, kind, description, created_at) "
                            "VALUES (?, ?, NULL, ?)",
                            (namespace, "shared", iso(at)),
                        )
                        counts["namespaces_created"] += 1
                    entry_row = conn.execute(
                        "SELECT * FROM entries WHERE entry_id = ?", (entry_id,)
                    ).fetchone()
                    if entry_row is not None and str(entry_row["namespace"]) != namespace:
                        counts["conflicts"] += 1
                        continue
                    if entry_row is None:
                        conn.execute(
                            "INSERT INTO entries (entry_id, namespace, created_at, "
                            "created_by_agent_id, current_revision_id) VALUES (?, ?, ?, ?, NULL)",
                            (
                                entry_id,
                                namespace,
                                str(record.get("created_at") or iso(at)),
                                str(record.get("created_by_agent_id") or "import"),
                            ),
                        )
                        counts["created_entries"] += 1
                    for revision in record.get("revisions", []) or []:
                        if not isinstance(revision, dict):
                            counts["conflicts"] += 1
                            continue
                        revision_id = str(revision.get("revision_id", "")).strip()
                        if not revision_id:
                            counts["conflicts"] += 1
                            continue
                        if (
                            conn.execute(
                                "SELECT 1 FROM entry_revisions WHERE revision_id = ?",
                                (revision_id,),
                            ).fetchone()
                            is not None
                        ):
                            counts["skipped"] += 1
                            continue
                        for source_ref in revision.get("sources", []) or []:
                            for key in ("storage_path", "path"):
                                if key in source_ref and not self._safe_relative(str(source_ref[key])):
                                    raise self._json_error("import contains a path traversal reference")
                        kind_value = str(revision.get("kind", "observation"))
                        try:
                            kind = Kind(kind_value)
                        except ValueError:
                            kind = Kind.OBSERVATION
                        tags = revision.get("tags") or []
                        scope = revision.get("scope") or {}
                        if not isinstance(tags, list) or not isinstance(scope, dict):
                            counts["conflicts"] += 1
                            continue
                        claim_text = str(revision.get("claim", ""))
                        if len(claim_text.encode("utf-8")) > self.config.limits.claim_body_bytes:
                            counts["conflicts"] += 1
                            continue
                        prev_candidate = revision.get("prev_revision_id")
                        prev_revision_id = None
                        if (
                            prev_candidate
                            and conn.execute(
                                "SELECT 1 FROM entry_revisions WHERE revision_id = ?",
                                (str(prev_candidate),),
                            ).fetchone()
                            is not None
                        ):
                            prev_revision_id = str(prev_candidate)
                        conn.execute(
                            """
                            INSERT INTO entry_revisions
                                (revision_id, entry_id, prev_revision_id, kind, title, claim,
                                 conditions, expected_result, limitations, tags, scope, observed_at,
                                 valid_from, valid_until, last_verified_at, author_agent_id,
                                 author_root_id, contributors, source_role, evidence_class,
                                 sensitivity, lifecycle, content_hash, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, 'unverified',
                                    ?, 'candidate', ?, ?)
                            """,
                            (
                                revision_id,
                                entry_id,
                                prev_revision_id,
                                kind.value,
                                str(revision.get("title", "")) or "imported entry",
                                claim_text,
                                revision.get("conditions"),
                                revision.get("expected_result"),
                                revision.get("limitations"),
                                json.dumps([str(tag) for tag in tags]),
                                json.dumps(scope),
                                str(revision.get("observed_at") or iso(at)),
                                revision.get("valid_from"),
                                revision.get("valid_until"),
                                str(revision.get("author_agent_id") or "import"),
                                str(revision.get("author_root_id") or "import"),
                                json.dumps([str(item) for item in revision.get("contributors", []) or []]),
                                str(revision.get("source_role") or "imported"),
                                str(revision.get("sensitivity") or "normal"),
                                str(revision.get("content_hash") or revision_id),
                                str(revision.get("created_at") or iso(at)),
                            ),
                        )
                        counts["created_revisions"] += 1
                        for source_ref in revision.get("sources", []) or []:
                            source_id = str(
                                source_ref.get("source_id")
                                or "src_"
                                + hashlib.sha256(
                                    (revision_id + str(source_ref.get("kind"))).encode()
                                ).hexdigest()[:24]
                            )
                            if (
                                conn.execute(
                                    "SELECT 1 FROM sources WHERE source_id = ?", (source_id,)
                                ).fetchone()
                                is not None
                            ):
                                continue
                            conn.execute(
                                "INSERT INTO sources (source_id, revision_id, kind, role, "
                                "identity, uri, snapshot_hash, trust, detail, created_at) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                (
                                    source_id,
                                    revision_id,
                                    str(source_ref.get("kind") or "imported"),
                                    str(source_ref.get("role") or "supporting"),
                                    source_ref.get("identity"),
                                    source_ref.get("uri"),
                                    source_ref.get("snapshot_hash"),
                                    str(source_ref.get("trust") or "unverified"),
                                    source_ref.get("detail"),
                                    str(source_ref.get("created_at") or iso(at)),
                                ),
                            )
                        conn.execute(
                            "INSERT INTO entry_fts (entry_id, revision_id, title, claim, conditions, tags) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (
                                entry_id,
                                revision_id,
                                str(revision.get("title", "")) or "imported entry",
                                claim_text,
                                revision.get("conditions"),
                                json.dumps([str(tag) for tag in tags]),
                            ),
                        )
                        KnowledgeManager._enqueue_review(conn, entry_id, revision_id, at)
                        current = conn.execute(
                            "SELECT current_revision_id FROM entries WHERE entry_id = ?",
                            (entry_id,),
                        ).fetchone()
                        if current is not None and current["current_revision_id"] is None:
                            conn.execute(
                                "UPDATE entries SET current_revision_id = ? WHERE entry_id = ?",
                                (revision_id, entry_id),
                            )
                        for review in revision.get("reviews", []) or []:
                            review_id = str(review.get("review_id", "")).strip()
                            if not review_id:
                                continue
                            if (
                                conn.execute(
                                    "SELECT 1 FROM reviews WHERE review_id = ?", (review_id,)
                                ).fetchone()
                                is not None
                            ):
                                continue
                            conn.execute(
                                """
                                INSERT INTO reviews
                                    (review_id, entry_id, revision_id, reviewer_agent_id,
                                     reviewer_root_id, result, method, evidence, conditions,
                                     limitations, lease_id, created_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                                """,
                                (
                                    review_id,
                                    entry_id,
                                    revision_id,
                                    str(review.get("reviewer_agent_id") or "import"),
                                    str(review.get("reviewer_root_id") or "import"),
                                    str(review.get("result") or "inconclusive"),
                                    str(review.get("method") or "imported"),
                                    review.get("evidence"),
                                    review.get("conditions"),
                                    review.get("limitations"),
                                    str(review.get("created_at") or iso(at)),
                                ),
                            )
                            counts["reviews"] += 1
                if dry_run:
                    raise _DryRunRollback
        except _DryRunRollback:
            pass
        counts["creates"] = counts["created_revisions"]
        counts["skips"] = counts["skipped"]
        return counts

    # -- admin: dispatch -------------------------------------------------
    def admin_dispatch(self, cmd: str, args: Mapping[str, Any] | None = None) -> Any:
        arguments = dict(args or {})
        normalized = cmd.strip().lower().replace(" ", ".").replace("_", ".")
        aliases = {
            "agents": "list.agents",
            "enrollments": "list.enrollments",
            "reviews": "list.reviews",
            "namespaces.list": "namespaces.list",
            "namespaces.create": "namespaces.create",
            "grants.add": "grants.add",
            "smoke.test": "smoke.test",
            "smoke-test": "smoke.test",
            "config.check": "config.check",
            "config.show": "config.show",
            "index.rebuild": "index.rebuild",
            "backup.create": "backup.create",
            "backup.verify": "backup.verify",
            "backup.restore": "backup.restore",
            "memory.add": "memory.add",
            "memory.correct": "memory.correct",
            "memory.retract": "memory.retract",
            "memory.forget": "memory.forget",
        }
        name = aliases.get(normalized, normalized)

        def request_id() -> str:
            value = arguments.get("request_id") or arguments.get("request") or arguments.get("id")
            if not value:
                raise self._json_error("request_id is required")
            return str(value)

        if name in {
            "status",
            "doctor",
            "list.agents",
            "list.enrollments",
            "list.reviews",
            "list.review.jobs",
            "namespaces.list",
            "config.check",
            "backup.create",
            "index.rebuild",
            "smoke.test",
        }:
            if name == "status":
                return self.status()
            if name == "doctor":
                return self.doctor()
            if name == "list.agents":
                return self.list_agents()
            if name == "list.enrollments":
                return self.list_enrollments()
            if name == "list.reviews":
                return self.list_reviews()
            if name == "list.review.jobs":
                return self.list_review_jobs()
            if name == "namespaces.list":
                return self.namespaces_list()
            if name == "config.check":
                return self.config_check()
            if name == "backup.create":
                return self.backup_create()
            if name == "index.rebuild":
                return self.index_rebuild()
            if name == "smoke.test":
                return self.smoke_test()
        if name == "config.show":
            return {"config": self.config_show(redacted=bool(arguments.get("redacted", True)))}
        if name == "reload":
            return self.reload_config(arguments.get("path"))
        if name == "approve":
            return self.approve(
                request_id(),
                role=str(arguments.get("role", "mentor")),
                ttl=arguments.get("ttl", arguments.get("ttl_s")),
                namespace=arguments.get("namespace"),
            ).model_dump(mode="json")
        if name == "deny":
            return self.deny(request_id()).model_dump(mode="json")
        if name == "revoke":
            target = arguments.get("target") or arguments.get("session_or_agent_id") or arguments.get("id")
            if not target:
                raise self._json_error("target is required")
            return self.revoke(str(target))
        if name == "namespaces.create":
            value = arguments.get("name")
            if not value:
                raise self._json_error("name is required")
            return self.namespace_create(
                str(value),
                kind=str(arguments.get("kind", "shared")),
                description=arguments.get("description"),
            )
        if name == "grants.add":
            for required in ("agent_id", "namespace", "role"):
                if not arguments.get(required):
                    raise self._json_error(f"{required} is required")
            return self.grants_add(
                str(arguments["agent_id"]),
                str(arguments["namespace"]),
                str(arguments["role"]),
            )
        if name == "search":
            query = arguments.get("query")
            if query is None:
                raise self._json_error("query is required")
            return self.admin_search(str(query), arguments.get("namespace")).model_dump(mode="json")
        if name == "inspect":
            value = arguments.get("entry_id") or arguments.get("entry")
            if not value:
                raise self._json_error("entry_id is required")
            return self.inspect(str(value))
        if name == "memory.add":
            for required in ("kind", "namespace", "text"):
                if not arguments.get(required):
                    raise self._json_error(f"{required} is required")
            return self.memory_add(
                str(arguments["kind"]),
                str(arguments["namespace"]),
                str(arguments["text"]),
                title=arguments.get("title"),
                source_role=str(arguments.get("source_role", "operator")),
            ).model_dump(mode="json")
        if name == "memory.correct":
            for required in ("entry_id", "expected_revision", "text"):
                if not arguments.get(required):
                    raise self._json_error(f"{required} is required")
            return self.memory_correct(
                str(arguments["entry_id"]),
                str(arguments["expected_revision"]),
                str(arguments["text"]),
            ).model_dump(mode="json")
        if name == "memory.retract":
            value = arguments.get("entry_id")
            reason = arguments.get("reason")
            if not value or not reason:
                raise self._json_error("entry_id and reason are required")
            return self.memory_retract(str(value), str(reason))
        if name == "memory.forget":
            value = arguments.get("entry_id")
            if not value:
                raise self._json_error("entry_id is required")
            return self.memory_forget(str(value), bool(arguments.get("dry_run", False)))
        if name == "backup.verify":
            value = arguments.get("path") or arguments.get("snapshot")
            if not value:
                raise self._json_error("path is required")
            return self.backup_verify(str(value))
        if name == "backup.restore":
            value = arguments.get("path") or arguments.get("snapshot")
            destination = arguments.get("destination")
            if not value or not destination:
                raise self._json_error("path and destination are required")
            return self.backup_restore(str(value), str(destination))
        if name == "export":
            output = arguments.get("path") or arguments.get("output")
            if not output:
                raise self._json_error("path is required")
            return self.export_jsonl(arguments.get("namespace"), str(output))
        if name == "import":
            value = arguments.get("path") or arguments.get("file")
            if not value:
                raise self._json_error("path is required")
            return self.import_jsonl(str(value), bool(arguments.get("dry_run", False)))
        raise ServiceError(ErrorCode.INVALID_ARGUMENT, f"unknown admin command: {cmd}")

    # -- compatibility aliases ------------------------------------------
    def revoke_session(self, session_or_id: SessionInfo | str) -> dict[str, Any]:
        return self.identity.revoke_session(session_or_id)

    def revoke_agent(self, agent_id: str) -> dict[str, Any]:
        return self.identity.revoke_agent(agent_id)

    def agents(self) -> list[dict[str, Any]]:
        return self.list_agents()

    def enrollments(self) -> list[dict[str, Any]]:
        return self.list_enrollments()

    def reviews_list(self) -> list[dict[str, Any]]:
        return self.list_reviews()

    def reload(self, path: str | Path | None = None) -> dict[str, Any]:
        return self.reload_config(path)

    def watch_events(self) -> queue.Queue[dict[str, Any]]:
        return self.subscribe()

    # -- skill -----------------------------------------------------------
    def get_skill_text(self) -> str:
        try:
            resource = importlib.resources.files("knowlage_mcp.resources").joinpath(SKILL_RESOURCE_NAME)
            return resource.read_text(encoding="utf-8")
        except (OSError, ModuleNotFoundError) as exc:
            raise ServiceError(
                ErrorCode.BACKEND_UNAVAILABLE, "packaged skill resource is unavailable"
            ) from exc


__all__ = ["EventBus", "KnowledgeService"]
