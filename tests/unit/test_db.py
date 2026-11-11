# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Unit tests for connection pragmas, migrations, transactions, and locking."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from knowlage_mcp.db import (
    Database,
    DatabaseLockedError,
    SchemaTooNewError,
    iso,
    lock_file,
    parse_iso,
    utcnow,
)
from knowlage_mcp.migrations import MAX_VERSION, MIGRATIONS


def test_pragmas_and_row_factory(db: Database) -> None:
    conn = db.connect()
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
    row = conn.execute("SELECT 1 AS value").fetchone()
    assert row["value"] == 1


def test_migrations_idempotent_and_schema_version(db: Database) -> None:
    assert db.schema_version() == MAX_VERSION
    assert db.migrate() == MAX_VERSION
    assert db.schema_version() == MAX_VERSION
    assert [version for version, _, _ in MIGRATIONS] == sorted(version for version, _, _ in MIGRATIONS)


def test_expected_tables_and_indices_exist(db: Database) -> None:
    conn = db.connect()
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "schema_migrations",
        "namespaces",
        "agents",
        "sessions",
        "grants",
        "delegation_tickets",
        "mentor_requests",
        "entries",
        "entry_revisions",
        "sources",
        "evidence_objects",
        "provenance_edges",
        "reviews",
        "review_jobs",
        "task_outcomes",
        "audit_events",
        "idempotency_records",
        "deletion_ledger",
        "entry_fts",
    } <= tables
    indices = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {
        "idx_entries_namespace_current",
        "idx_sessions_token_hash",
        "idx_grants_agent_namespace",
        "idx_review_jobs_state_priority",
        "idx_sources_revision",
        "idx_provenance_from",
        "idx_evidence_revision",
    } <= indices


def test_fts5_present_and_searchable(db: Database) -> None:
    conn = db.connect()
    conn.execute(
        "INSERT INTO entry_fts (entry_id, revision_id, title, claim, conditions, tags) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("e1", "r1", "Naïve compiler note", "The naïve build failed with a missing include", None, "[]"),
    )
    rows = conn.execute("SELECT entry_id FROM entry_fts WHERE entry_fts MATCH ?", ("naive",)).fetchall()
    assert [row["entry_id"] for row in rows] == ["e1"]
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("SELECT * FROM entry_fts WHERE entry_fts MATCH ?", ("unbalanced(",)).fetchall()


def test_newer_schema_fails_safely(tmp_path: Path) -> None:
    database = Database(tmp_path / "future.sqlite3")
    conn = database.connect()
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    conn.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)", (999, iso(utcnow())))
    with pytest.raises(SchemaTooNewError):
        database.migrate()
    database.close()


def test_transaction_rolls_back_on_error(db: Database) -> None:
    with pytest.raises(RuntimeError), db.transaction() as conn:
        conn.execute(
            "INSERT INTO namespaces (name, kind, created_at) VALUES (?, ?, ?)",
            ("rolled-back", "shared", iso(utcnow())),
        )
        raise RuntimeError("abort")
    count = db.connect().execute("SELECT COUNT(*) AS n FROM namespaces").fetchone()["n"]
    assert count == 0


def test_transaction_commits(db: Database) -> None:
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO namespaces (name, kind, created_at) VALUES (?, ?, ?)",
            ("shared", "shared", iso(utcnow())),
        )
    count = db.connect().execute("SELECT COUNT(*) AS n FROM namespaces").fetchone()["n"]
    assert count == 1


def test_lock_file_rejected_in_same_process(tmp_path: Path) -> None:
    path = tmp_path / "writer.lock"
    with lock_file(path), pytest.raises(DatabaseLockedError), lock_file(path):
        pytest.fail("second lock must not be acquired")


def test_lock_file_rejected_in_second_process(tmp_path: Path) -> None:
    path = tmp_path / "writer.lock"
    script = (
        "import sys\n"
        "from knowlage_mcp.db import DatabaseLockedError, lock_file\n"
        "try:\n"
        f"    with lock_file({str(path)!r}):\n"
        "        pass\n"
        "except DatabaseLockedError:\n"
        "    print('locked')\n"
        "    sys.exit(0)\n"
        "sys.exit(3)\n"
    )
    with lock_file(path):
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    assert completed.returncode == 0, completed.stderr
    assert "locked" in completed.stdout


def test_backup_and_integrity(db: Database, tmp_path: Path) -> None:
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO namespaces (name, kind, created_at) VALUES (?, ?, ?)",
            ("shared", "shared", iso(utcnow())),
        )
    backup_path = db.backup_to(tmp_path / "snapshot.sqlite3")
    assert backup_path.is_file()
    backup = sqlite3.connect(backup_path)
    try:
        assert backup.execute("SELECT COUNT(*) FROM namespaces").fetchone()[0] == 1
    finally:
        backup.close()
    assert db.integrity_check() == "ok"


def test_utc_helpers_roundtrip() -> None:
    now = utcnow()
    later = now + timedelta(seconds=1)
    assert parse_iso(iso(now)) == now
    assert parse_iso(iso(later)) > parse_iso(iso(now))
    assert iso(now).endswith("Z")
