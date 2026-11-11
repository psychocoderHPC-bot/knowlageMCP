# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Facade tests: status, admin dispatch, transfer, backup, events, and skill."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from conftest import ServiceEnv, build_env
from knowlage_mcp.config import write_config_atomic
from knowlage_mcp.models import (
    EntryPayload,
    ErrorCode,
    EvidenceRef,
    Kind,
    RecordRequest,
    ServiceError,
)


def _record(env: ServiceEnv, claim: str, *, title: str = "service note", evidence=()):
    session = env.session(env.register())
    return env.service.record(
        session,
        RecordRequest(
            namespace="shared",
            entry=EntryPayload(kind=Kind.OBSERVATION, title=title, claim=claim, evidence=evidence),
        ),
    )


def test_status_protocol_and_doctor(env: ServiceEnv) -> None:
    info = env.service.get_protocol_info()
    assert info.schema_version >= 1
    assert "search" in info.tool_names
    assert info.resource_uri == "knowlageMCP://skill"

    status = env.service.status()
    assert status["schema_version"] >= 1
    assert status["namespaces"] == 2
    assert status["uptime_s"] >= 0

    doctor = env.service.doctor()
    assert doctor["ok"] is True
    names = {check["name"] for check in doctor["checks"]}
    assert {"config", "database", "fts5", "integrity"} <= names


def test_event_bus_publishes_registration_events(env: ServiceEnv) -> None:
    subscriber = env.service.subscribe()
    env.register(source="event")
    event = subscriber.get(timeout=2)
    assert event["event"] == "audit"
    assert event["action"] == "agent.register"
    env.service.unsubscribe(subscriber)


def test_admin_dispatch_command_names(env: ServiceEnv) -> None:
    assert env.service.admin_dispatch("status")["schema_version"] >= 1
    namespaces = env.service.admin_dispatch("namespaces.list")
    assert {item["name"] for item in namespaces} == {"shared", "personal"}

    created = env.service.admin_dispatch(
        "namespaces.create", {"name": "alpaka", "kind": "shared", "description": "HPC"}
    )
    assert created["name"] == "alpaka"
    with pytest.raises(ServiceError):
        env.service.admin_dispatch("namespaces.create", {"name": "alpaka", "kind": "personal"})

    registration = env.register(source="grant")
    grant = env.service.admin_dispatch(
        "grants.add",
        {"agent_id": registration.agent_id, "namespace": "alpaka", "role": "contributor"},
    )
    assert grant["namespace"] == "alpaka"

    write = env.service.admin_dispatch(
        "memory.add",
        {"kind": "preference", "namespace": "shared", "text": "admin dispatch hint"},
    )
    entry_id = write["entry"]["entry_id"]
    inspected = env.service.admin_dispatch("inspect", {"entry_id": entry_id})
    assert inspected["entry"]["entry_id"] == entry_id

    found = env.service.admin_dispatch("search", {"query": "admin dispatch hint"})
    assert any(item["entry_id"] == entry_id for item in found["results"])

    assert env.service.admin_dispatch("reviews") == []
    assert env.service.admin_dispatch("config.check")["ok"] is True
    assert "[server]" in env.service.admin_dispatch("config.show", {"redacted": True})["config"]

    smoke = env.service.admin_dispatch("smoke.test")
    assert smoke["ok"] is True

    with pytest.raises(ServiceError) as excinfo:
        env.service.admin_dispatch("not.a.command")
    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENT


def test_config_reload_and_show(env: ServiceEnv, tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_config_atomic(path, env.config)
    result = env.service.reload_config(path)
    assert result["reloaded"] is True
    assert result["changed_keys"] == []
    shown = env.service.config_show(redacted=True)
    assert "[server]" in shown


def test_jsonl_export_import_roundtrip_and_idempotency(env: ServiceEnv, tmp_path: Path) -> None:
    _record(env, "transfer probe one", title="one")
    _record(env, "transfer probe two", title="two")
    published = env.service.memory_add(
        namespace="shared", kind="observation", text="transfer published entry"
    )
    export_path = tmp_path / "knowledge.jsonl"
    exported = env.service.export_jsonl("shared", export_path)
    assert exported["entries"] == 3
    assert exported["revisions"] == 3
    assert export_path.is_file()

    other = build_env(tmp_path / "other")
    try:
        imported = other.service.import_jsonl(export_path)
        assert imported["created_entries"] == 3
        assert imported["created_revisions"] == 3
        assert imported["skipped"] == 0
        rows = other.db.connect().execute("SELECT lifecycle, evidence_class FROM entry_revisions").fetchall()
        assert rows
        assert {row["lifecycle"] for row in rows} == {"candidate"}
        assert {row["evidence_class"] for row in rows} == {"unverified"}
        assert (
            other.db.connect()
            .execute("SELECT COUNT(*) FROM entries WHERE entry_id = ?", (published.entry.entry_id,))
            .fetchone()[0]
            == 1
        )

        again = other.service.import_jsonl(export_path)
        assert again["created_revisions"] == 0
        assert again["skipped"] == 3

        session = other.session(other.register(source="reader"))
        from knowlage_mcp.models import SearchRequest

        found = other.service.search(session, SearchRequest(query="transfer probe one"))
        assert found.results

        fresh = build_env(tmp_path / "dry")
        try:
            preview = fresh.service.import_jsonl(export_path, dry_run=True)
            assert preview["dry_run"] is True
            assert preview["created_revisions"] == 3
            assert fresh.db.connect().execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0
        finally:
            fresh.db.close()
    finally:
        other.db.close()


def test_jsonl_import_rejects_path_traversal(env: ServiceEnv, tmp_path: Path) -> None:
    payload = {
        "type": "header",
        "schema_version": 1,
        "app_version": "0.1.0",
        "created_at": "2026-01-01T00:00:00.000000Z",
    }
    entry = {
        "type": "entry",
        "entry_id": "ent_evil",
        "namespace": "shared",
        "created_at": "2026-01-01T00:00:00.000000Z",
        "created_by_agent_id": "import",
        "revisions": [
            {
                "revision_id": "rev_evil",
                "kind": "observation",
                "title": "evil",
                "claim": "evil",
                "observed_at": "2026-01-01T00:00:00.000000Z",
                "author_agent_id": "import",
                "author_root_id": "import",
                "created_at": "2026-01-01T00:00:00.000000Z",
                "sources": [{"kind": "log", "storage_path": "../escape"}],
            }
        ],
    }
    path = tmp_path / "evil.jsonl"
    path.write_text(json.dumps(payload) + "\n" + json.dumps(entry) + "\n", encoding="utf-8")
    with pytest.raises(ServiceError) as excinfo:
        env.service.import_jsonl(path)
    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENT


def test_backup_verify_restore_and_forgotten_stays_forgotten(env: ServiceEnv, tmp_path: Path) -> None:
    kept = env.service.memory_add(namespace="shared", kind="observation", text="kept during backup")
    sacrificed = _record(env, "forgotten after backup", title="forgotten")
    evidence_record = env.evidence.put(b"snapshot evidence\n", "text/plain")
    # Attach evidence through a second recorded entry so restore exercises the
    # evidence inventory as well.
    _record(
        env,
        "entry with evidence",
        title="evidence",
        evidence=(
            EvidenceRef(
                evidence_id=evidence_record.evidence_id,
                sha256=evidence_record.sha256,
                size_bytes=evidence_record.size_bytes,
                media_type="text/plain",
            ),
        ),
    )

    snapshot = env.service.backup_create()
    assert Path(snapshot["path"]).is_dir()
    verification = env.service.backup_verify(snapshot["path"])
    assert verification["ok"] is True
    assert verification["checks"]["integrity"] is True
    assert verification["checks"]["evidence"] is True

    env.service.memory_forget(sacrificed.entry.entry_id, dry_run=False)
    destination = tmp_path / "restored"
    restored = env.service.backup_restore(snapshot["path"], destination)
    assert restored["restored"] is True

    connection = sqlite3.connect(destination / "knowledge.sqlite3")
    try:
        entry_ids = {row[0] for row in connection.execute("SELECT entry_id FROM entries").fetchall()}
        assert kept.entry.entry_id in entry_ids
        assert sacrificed.entry.entry_id not in entry_ids
        ledger = {
            (row[0], row[1])
            for row in connection.execute("SELECT object_type, object_id FROM deletion_ledger").fetchall()
        }
        assert ("entry", sacrificed.entry.entry_id) in ledger
        fts = connection.execute(
            "SELECT COUNT(*) FROM entry_fts WHERE entry_id = ?",
            (sacrificed.entry.entry_id,),
        ).fetchone()[0]
        assert fts == 0
    finally:
        connection.close()
    assert (destination / "evidence").is_dir()

    with pytest.raises(ServiceError):
        env.service.backup_restore(snapshot["path"], destination)
    with pytest.raises(ServiceError):
        env.service.backup_restore(snapshot["path"], env.config.storage.database)
    with pytest.raises(ServiceError):
        env.service.backup_verify(tmp_path / "missing-snapshot")


def test_get_skill_text_is_packaged(env: ServiceEnv) -> None:
    text = env.service.get_skill_text()
    assert "knowlageMCP" in text
    assert len(text) > 200


def test_migration_foreign_keys_and_indices(env: ServiceEnv) -> None:
    connection = env.db.connect()
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    indices = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {
        "idx_grants_live_unique",
        "idx_mentor_requests_session_state",
        "idx_entries_namespace",
        "idx_entry_revisions_entry_created",
        "idx_reviews_revision",
        "idx_review_jobs_state_priority",
    } <= indices
    with pytest.raises(sqlite3.IntegrityError), env.db.transaction() as tx:
        tx.execute(
            "INSERT INTO sessions (session_id, agent_id, root_agent_id, token_hash, "
            "actor_handle, created_at, last_seen_at, idle_expires_at, absolute_expires_at) "
            "VALUES ('ses_bad', 'agt_missing', 'agt_missing', 'hash-bad', 'act_bad', "
            "'2026-01-01T00:00:00.000000Z', '2026-01-01T00:00:00.000000Z', "
            "'2026-01-01T00:00:00.000000Z', '2026-01-01T00:00:00.000000Z')"
        )
