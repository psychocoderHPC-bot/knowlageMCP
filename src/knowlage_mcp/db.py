# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""SQLite connection management, transactions, migrations, and writer locking."""

from __future__ import annotations

import fcntl
import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from . import migrations
from .models import ErrorCode, ServiceError

#: Busy timeout enforced on every connection.
DEFAULT_BUSY_TIMEOUT_MS = 5000


class DatabaseError(RuntimeError):
    """Base class for database infrastructure failures."""


class DatabaseLockedError(DatabaseError):
    """Another process already owns the database writer lock."""


class MigrationError(DatabaseError):
    """A migration could not be applied."""


class SchemaTooNewError(MigrationError):
    """The database schema is newer than this build understands."""


def utcnow() -> datetime:
    """Return the current timezone-aware UTC time."""
    return datetime.now(UTC)


def iso(value: datetime) -> str:
    """Serialize a datetime as ISO-8601 UTC with a ``Z`` suffix."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_iso(text: str) -> datetime:
    """Parse an ISO-8601 timestamp, accepting a trailing ``Z``."""
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


_STORAGE_FULL_MARKERS = (
    "readonly",
    "read-only",
    "disk is full",
    "database or disk is full",
    "no space left",
)
_STORAGE_UNAVAILABLE_MARKERS = (
    "disk i/o error",
    "unable to open database",
    "database is locked",
    "database table is locked",
)


def _storage_service_error(exc: sqlite3.OperationalError) -> ServiceError | None:
    """Translate storage-level SQLite failures into structured service errors."""
    text = str(exc).lower()
    if any(marker in text for marker in _STORAGE_FULL_MARKERS):
        return ServiceError(ErrorCode.STORAGE_FULL, "storage is read-only or full")
    if any(marker in text for marker in _STORAGE_UNAVAILABLE_MARKERS):
        return ServiceError(ErrorCode.BACKEND_UNAVAILABLE, "storage backend is unavailable")
    return None


def _split_statements(script: str) -> list[str]:
    """Split a SQL script into complete statements without an implicit commit."""
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                statements.append(statement)
            buffer = ""
    if buffer.strip():
        raise MigrationError(f"incomplete SQL statement in migration: {buffer.strip()[:80]!r}")
    return statements


class lock_file:
    """Exclusive, non-blocking process lock for the database writer.

    The lock is advisory and intended to prevent two independent gateway
    writers from opening the same database. Locks are released automatically
    when the file descriptor closes, including on process crash.
    """

    def __init__(self, path: str | Path, *, blocking: bool = False) -> None:
        self.path = Path(path)
        self.blocking = blocking
        self._fd: int | None = None

    def __enter__(self) -> lock_file:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        flags = fcntl.LOCK_EX if self.blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(fd, flags)
        except BlockingIOError as exc:
            os.close(fd)
            raise DatabaseLockedError(
                f"database writer lock is already held by another process: {self.path}"
            ) from exc
        self._fd = fd
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        fd = self._fd
        self._fd = None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


class Database:
    """Thread-safe SQLite access for one writer process."""

    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        check_same_thread: bool = False,
    ) -> None:
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms
        self.check_same_thread = check_same_thread
        self._memory = str(path) == ":memory:"
        self._local = threading.local()
        self._shared: sqlite3.Connection | None = None
        self._shared_guard = threading.Lock()
        if not self._memory:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _open(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.path),
            timeout=self.busy_timeout_ms / 1000.0,
            check_same_thread=self.check_same_thread,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        if not self._memory:
            connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_ms)}")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def connect(self) -> sqlite3.Connection:
        """Return this thread's configured connection, opening it on demand."""
        if self._memory:
            with self._shared_guard:
                if self._shared is None:
                    self._shared = self._open()
                return self._shared
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = self._open()
            self._local.connection = connection
        return connection

    def close(self) -> None:
        """Close the current thread's connection (and a shared memory DB)."""
        if self._memory:
            with self._shared_guard:
                if self._shared is not None:
                    self._shared.close()
                    self._shared = None
            return
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            connection.close()
            self._local.connection = None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a block inside ``BEGIN IMMEDIATE`` and commit or roll back.

        Nested calls join the outer transaction; the outermost call owns the
        commit. This keeps service helpers composable without savepoint drift.
        """
        connection = self.connect()
        if connection.in_transaction:
            yield connection
            return
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            mapped = _storage_service_error(exc)
            if mapped is not None:
                raise mapped from exc
            raise
        try:
            yield connection
        except BaseException as exc:
            connection.rollback()
            if isinstance(exc, sqlite3.OperationalError):
                mapped = _storage_service_error(exc)
                if mapped is not None:
                    raise mapped from exc
            raise
        try:
            connection.commit()
        except sqlite3.OperationalError as exc:
            connection.rollback()
            mapped = _storage_service_error(exc)
            if mapped is not None:
                raise mapped from exc
            raise

    def schema_version(self) -> int:
        connection = self.connect()
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        if row is None:
            return 0
        version_row = connection.execute(
            "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
        ).fetchone()
        assert version_row is not None
        return int(version_row["version"])

    def migrate(self) -> int:
        """Apply all pending migrations in order and return the new version."""
        connection = self.connect()
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        versions = [version for version, _, _ in migrations.MIGRATIONS]
        if versions != sorted(set(versions)):
            raise MigrationError("migration versions must be unique and ascending")
        current = self.schema_version()
        if current > migrations.MAX_VERSION:
            raise SchemaTooNewError(
                f"database schema version {current} is newer than supported version {migrations.MAX_VERSION}"
            )
        for version, description, script in migrations.MIGRATIONS:
            if version <= current:
                continue
            statements = _split_statements(script)
            connection.execute("BEGIN IMMEDIATE")
            try:
                for statement in statements:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (version, iso(utcnow())),
                )
                connection.commit()
            except BaseException as exc:
                connection.rollback()
                raise MigrationError(f"migration {version} ({description}) failed: {exc}") from exc
            current = version
        return current

    def backup_to(self, path: str | Path) -> Path:
        """Create an online backup using SQLite's backup API."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = sqlite3.connect(str(target))
        try:
            self.connect().backup(destination)
            destination.commit()
        finally:
            destination.close()
        return target

    def integrity_check(self) -> str:
        """Return ``ok`` for a healthy database, otherwise the reported problems."""
        rows = self.connect().execute("PRAGMA integrity_check").fetchall()
        if not rows:
            return "no result"
        messages = [str(row[0]) for row in rows]
        if messages == ["ok"]:
            return "ok"
        return "\n".join(messages)


__all__ = [
    "DEFAULT_BUSY_TIMEOUT_MS",
    "Database",
    "DatabaseError",
    "DatabaseLockedError",
    "MigrationError",
    "SchemaTooNewError",
    "iso",
    "lock_file",
    "parse_iso",
    "utcnow",
]
