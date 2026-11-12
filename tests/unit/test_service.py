# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Facade tests: status, admin dispatch, transfer, backup, events, and skill."""

from __future__ import annotations

import json
import socket
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from conftest import ServiceEnv, build_env
from knowlage_mcp.config import write_config_atomic
from knowlage_mcp.integrations.base import IntegrationStatus, MemoryProposal
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


def _listening_socket() -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    return listener


def _closed_port() -> int:
    probe = _listening_socket()
    try:
        return int(probe.getsockname()[1])
    finally:
        probe.close()


def _admin_socket(path: Path) -> socket.socket:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    unix = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    unix.bind(str(path))
    path.chmod(0o600)
    return unix


def _config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int) -> Path:
    config_home = tmp_path / "config-home"
    app_dir = config_home / "knowlageMCP"
    app_dir.mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    config_file = app_dir / "config.toml"
    config_file.write_text("", encoding="utf-8")
    config_file.chmod(mode)
    return config_file


def test_status_protocol_and_doctor(env: ServiceEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    info = env.service.get_protocol_info()
    assert info.schema_version >= 1
    assert "search" in info.tool_names
    assert info.resource_uri == "knowlageMCP://skill"

    status = env.service.status()
    assert status["schema_version"] >= 1
    assert status["namespaces"] == 2
    assert status["uptime_s"] >= 0

    _config_file(tmp_path, monkeypatch, 0o600)
    listener = _listening_socket()
    admin_path = tmp_path / "runtime" / "admin.sock"
    unix = _admin_socket(admin_path)
    env.service.config.server.port = int(listener.getsockname()[1])
    env.service.config.storage.runtime_socket = admin_path
    try:
        doctor = env.service.doctor()
    finally:
        unix.close()
        admin_path.unlink(missing_ok=True)
        listener.close()

    assert doctor["ok"] is True, doctor
    names = {check["name"] for check in doctor["checks"]}
    assert {
        "config",
        "config_file",
        "data_dir",
        "evidence_dir",
        "backup_dir",
        "database",
        "schema_version",
        "integrity",
        "fts5",
        "foreign_keys",
        "free_space",
        "listener",
        "listener_exposure",
        "admin_socket",
        "container_reachability",
    } <= names


def test_doctor_reports_config_mode_and_missing_runtime_assets(
    env: ServiceEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = _config_file(tmp_path, monkeypatch, 0o644)
    env.service.config.server.port = _closed_port()
    env.service.config.storage.runtime_socket = tmp_path / "runtime" / "admin.sock"

    doctor = env.service.doctor()
    by_name = {check["name"]: check for check in doctor["checks"]}
    assert doctor["ok"] is False
    assert by_name["config_file"]["ok"] is False
    assert by_name["listener"]["ok"] is False
    assert by_name["admin_socket"]["ok"] is False

    config_file.chmod(0o600)
    refreshed = {check["name"]: check for check in env.service.doctor()["checks"]}
    assert refreshed["config_file"]["ok"] is True


def test_doctor_reports_listener_exposure(env: ServiceEnv) -> None:
    env.service.config.server.host = "127.0.0.1"
    doctor = env.service.doctor()
    names = [check["name"] for check in doctor["checks"]]
    checks = {check["name"]: check for check in doctor["checks"]}
    assert checks["listener_exposure"] == {
        "name": "listener_exposure",
        "ok": True,
        "detail": "loopback only",
    }
    assert names.index("listener_exposure") == names.index("listener") + 1

    env.service.config.server.host = "0.0.0.0"
    env.service.config.server.allowed_hosts = ["10.0.0.5"]
    env.service.config.server.allowed_origins = ["https://ui.example"]
    checks = {check["name"]: check for check in env.service.doctor()["checks"]}
    check = checks["listener_exposure"]
    assert check["name"] == "listener_exposure"
    assert check["ok"] is True
    assert check["detail"].startswith(
        "WARNING: bound to 0.0.0.0; require firewall, TLS or a trusted tunnel, "
        "and an exact allowed_hosts list"
    )
    assert "allowed_hosts=[10.0.0.5]" in check["detail"]
    assert "allowed_origins=[https://ui.example]" in check["detail"]


def test_doctor_reports_disabled_integrations(env: ServiceEnv) -> None:
    checks = {check["name"]: check for check in env.service.doctor()["checks"]}
    assert checks["integrations"] == {"name": "integrations", "ok": True, "detail": "disabled"}


def test_doctor_reports_available_integrations(env: ServiceEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    status = IntegrationStatus(
        enabled=True,
        embedding="hash",
        embedding_available=True,
        vector="qdrant",
        vector_available=True,
        reranker="lexical",
        reranker_available=True,
        extractor="graphiti",
        extractor_available=True,
        generation="gen_0123456789abcdef",
    )
    monkeypatch.setattr(env.service.integrations, "status", lambda: status)

    checks = {check["name"]: check for check in env.service.doctor()["checks"]}
    check = checks["integrations"]
    assert check["ok"] is True
    for fragment in (
        "enabled",
        "embedding=hash",
        "vector=qdrant",
        "reranker=lexical",
        "extractor=graphiti",
        "generation=gen_0123456789abcdef",
    ):
        assert fragment in check["detail"]
    assert "http" not in check["detail"]
    assert "unavailable" not in check["detail"]


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

    rebuilt = env.service.admin_dispatch("index.rebuild")
    assert rebuilt["generation"] is None
    assert rebuilt["backend"] == "none"
    assert rebuilt["indexed"] >= 1
    forwarded = env.service.admin_dispatch("integrations.reindex")
    assert set(forwarded) == {"generation", "backend", "indexed"}
    assert forwarded["generation"] is None

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


def _config_holders(env: ServiceEnv):
    service = env.service
    return (service, service.identity, service.knowledge, service.retrieval, service.review, service.backup)


@pytest.mark.parametrize("text", ["[server]\nport = 0\n", "[server\nport = 2230\n"])
def test_reload_invalid_config_is_all_or_nothing(env: ServiceEnv, tmp_path: Path, text: str) -> None:
    path = tmp_path / "invalid.toml"
    path.write_text(text, encoding="utf-8")
    before = [holder.config for holder in _config_holders(env)]

    with pytest.raises(ServiceError) as excinfo:
        env.service.reload_config(path)
    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENT

    assert [holder.config for holder in _config_holders(env)] == before
    assert all(holder.config is old for holder, old in zip(_config_holders(env), before, strict=True))


def test_reload_swap_failure_restores_every_holder(
    env: ServiceEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FlakyHolder:
        def __init__(self, config) -> None:
            self._config = config
            self.set_calls = 0

        @property
        def config(self):
            return self._config

        @config.setter
        def config(self, value) -> None:
            self.set_calls += 1
            if self.set_calls == 1:
                raise RuntimeError("simulated swap failure")
            self._config = value

    replacement = env.config.model_copy(deep=True)
    replacement.server.port = env.config.server.port + 1
    path = tmp_path / "reload.toml"
    write_config_atomic(path, replacement)

    flaky = FlakyHolder(env.config)
    monkeypatch.setattr(env.service, "backup", flaky)
    before = [holder.config for holder in _config_holders(env)]

    with pytest.raises(RuntimeError, match="simulated swap failure"):
        env.service.reload_config(path)

    assert [holder.config for holder in _config_holders(env)] == before
    assert all(holder.config is old for holder, old in zip(_config_holders(env), before, strict=True))


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


def _import_header() -> dict:
    return {
        "type": "header",
        "schema_version": 1,
        "app_version": "0.1.0",
        "created_at": "2026-01-01T00:00:00.000000Z",
    }


def _import_entry(entry_id: str, revisions: list, *, namespace: str = "shared") -> dict:
    return {
        "type": "entry",
        "entry_id": entry_id,
        "namespace": namespace,
        "created_at": "2026-01-01T00:00:00.000000Z",
        "created_by_agent_id": "import",
        "revisions": revisions,
    }


def _import_revision(revision_id: str, claim: str, **overrides) -> dict:
    revision = {
        "revision_id": revision_id,
        "kind": "observation",
        "title": claim,
        "claim": claim,
        "observed_at": "2026-01-01T00:00:00.000000Z",
        "author_agent_id": "import",
        "author_root_id": "import",
        "created_at": "2026-01-01T00:00:00.000000Z",
        "sources": [],
    }
    revision.update(overrides)
    return revision


def test_jsonl_import_skips_path_traversal_revision_and_commits(env: ServiceEnv, tmp_path: Path) -> None:
    good = _import_revision("rev_good", "valid revision")
    bad = _import_revision(
        "rev_bad", "traversal revision", sources=[{"kind": "log", "storage_path": "../escape"}]
    )
    entry = _import_entry("ent_mixed", [good, bad])
    path = tmp_path / "mixed.jsonl"
    path.write_text(json.dumps(_import_header()) + "\n" + json.dumps(entry) + "\n", encoding="utf-8")

    result = env.service.import_jsonl(path)

    assert result["created_entries"] == 1
    assert result["created_revisions"] == 1
    assert result["malformed"] == 1
    assert result["conflicts"] == 0
    assert result["creates"] == result["created_revisions"]
    connection = env.db.connect()
    assert connection.execute("SELECT 1 FROM entries WHERE entry_id = 'ent_mixed'").fetchone() is not None
    assert (
        connection.execute("SELECT 1 FROM entry_revisions WHERE revision_id = 'rev_good'").fetchone()
        is not None
    )
    assert (
        connection.execute("SELECT 1 FROM entry_revisions WHERE revision_id = 'rev_bad'").fetchone() is None
    )


def test_jsonl_import_reports_downgraded_trust(env: ServiceEnv, tmp_path: Path) -> None:
    revision = _import_revision(
        "rev_downgraded",
        "published and verified at the source",
        lifecycle="published",
        evidence_class="verified",
    )
    entry = _import_entry("ent_downgraded", [revision])
    path = tmp_path / "downgraded.jsonl"
    path.write_text(json.dumps(_import_header()) + "\n" + json.dumps(entry) + "\n", encoding="utf-8")

    result = env.service.import_jsonl(path)

    assert result["created_revisions"] == 1
    assert result["downgraded"] == 1
    assert result["malformed"] == 0
    row = (
        env.db.connect()
        .execute("SELECT lifecycle, evidence_class FROM entry_revisions WHERE revision_id = 'rev_downgraded'")
        .fetchone()
    )
    assert row["lifecycle"] == "candidate"
    assert row["evidence_class"] == "unverified"


def test_jsonl_import_counts_structural_errors_as_malformed(env: ServiceEnv, tmp_path: Path) -> None:
    entry = _import_entry(
        "ent_structural",
        [
            "not-a-dict",
            _import_revision("", "missing revision id"),
            _import_revision("rev_tags", "bad tags", tags="not-a-list"),
            _import_revision("rev_ok", "survives malformed neighbours"),
        ],
    )
    path = tmp_path / "structural.jsonl"
    path.write_text(
        json.dumps(_import_header())
        + "\n"
        + json.dumps({"type": "unknown-record"})
        + "\n"
        + json.dumps(entry)
        + "\n",
        encoding="utf-8",
    )

    result = env.service.import_jsonl(path)

    assert result["created_revisions"] == 1
    assert result["malformed"] == 4
    assert result["conflicts"] == 0
    assert (
        env.db.connect().execute("SELECT 1 FROM entry_revisions WHERE revision_id = 'rev_ok'").fetchone()
        is not None
    )


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


class _RecordingExtractor:
    name = "stub"
    available = True

    def __init__(self, proposals: list[MemoryProposal] | None = None) -> None:
        self.proposals = proposals or []
        self.calls = 0
        self.sources: list[Any] = []

    def extract(self, text: str, task: str | None = None, sources: Any = ()) -> list[MemoryProposal]:
        del text, task
        self.calls += 1
        self.sources.append(list(sources))
        return list(self.proposals)

    def health(self) -> dict[str, Any]:
        return {"name": self.name, "available": self.available}


def _stub_proposal() -> MemoryProposal:
    return MemoryProposal(kind="observation", title="stub", claim="stub claim", tags=("stub",))


def test_integrations_extract_single_entry_keeps_compatibility(env: ServiceEnv) -> None:
    entry = env.service.memory_add("observation", "shared", "single extract source", title="single")
    extractor = _RecordingExtractor([_stub_proposal()])
    env.service.integrations.extractor = extractor

    result = env.service.integrations_extract(entry.entry.entry_id)

    assert result["entry_id"] == entry.entry.entry_id
    assert result["revision_id"] == entry.entry.revision_id
    assert result["extractor"] == "stub"
    assert result["available"] is True
    assert len(result["proposals"]) == 1
    assert result["calls"] == 1
    assert result["cap"] == env.config.integrations.graphiti.max_calls_per_task
    assert len(result["entries"]) == 1


def test_integrations_extract_batch_makes_one_call_per_entry(env: ServiceEnv) -> None:
    first = env.service.memory_add("observation", "shared", "first source", title="first")
    second = env.service.memory_add("observation", "shared", "second source", title="second")
    extractor = _RecordingExtractor([_stub_proposal()])
    env.service.integrations.extractor = extractor

    result = env.service.integrations_extract(
        [first.entry.entry_id, second.entry.entry_id, first.entry.entry_id]
    )

    assert extractor.calls == 2
    assert result["calls"] == 2
    assert [row["entry_id"] for row in result["entries"]] == [
        first.entry.entry_id,
        second.entry.entry_id,
    ]
    assert [row["proposals"] for row in result["entries"]] == [1, 1]
    assert [row["stored"] for row in result["entries"]] == [1, 1]
    assert "entry_id" not in result


def test_integrations_extract_rejects_batches_over_the_cap(env: ServiceEnv) -> None:
    ids = [
        env.service.memory_add(
            "observation", "shared", f"source {index}", title=f"source {index}"
        ).entry.entry_id
        for index in range(2)
    ]
    env.service.integrations.extractor = _RecordingExtractor([_stub_proposal()])
    env.service.config.integrations.graphiti.max_calls_per_task = 1

    with pytest.raises(ServiceError) as excinfo:
        env.service.integrations_extract(ids)

    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENT
    assert "max_calls_per_task cap (1)" in str(excinfo.value)


def test_integrations_extract_fails_closed_for_configured_unavailable(env: ServiceEnv) -> None:
    entry = env.service.memory_add("observation", "shared", "unavailable source", title="unavailable")
    extractor = _RecordingExtractor()
    extractor.available = False
    env.service.integrations.extractor = extractor

    with pytest.raises(ServiceError) as excinfo:
        env.service.integrations_extract(entry.entry.entry_id)

    assert excinfo.value.code is ErrorCode.BACKEND_UNAVAILABLE


def test_integrations_extract_null_extractor_returns_soft_result(env: ServiceEnv) -> None:
    entry = env.service.memory_add("observation", "shared", "null source", title="null")

    result = env.service.integrations_extract(entry.entry.entry_id)

    assert result["available"] is False
    assert result["proposals"] == []
    assert result["calls"] == 0
    assert result["entries"][0]["available"] is False


def test_integrations_import_memories_budgets_calls_and_preserves_role(
    env: ServiceEnv,
    tmp_path: Path,
) -> None:
    path = tmp_path / "many.jsonl"
    path.write_text(
        "\n".join(json.dumps({"role": "user", "text": f"memory {index}"}) for index in range(25)) + "\n",
        encoding="utf-8",
    )
    extractor = _RecordingExtractor()
    env.service.integrations.extractor = extractor
    env.service.config.integrations.mem0.max_items = 5

    counts = env.service.integrations_import_memories(path, dry_run=True)

    assert counts["calls"] == 5
    assert counts["proposals"] == 0
    assert extractor.calls == 5
    assert extractor.sources == [[{"role": "user"}] for _ in range(5)]


def test_integrations_import_memories_fails_closed_for_configured_unavailable(
    env: ServiceEnv,
    tmp_path: Path,
) -> None:
    path = tmp_path / "memories.jsonl"
    path.write_text(json.dumps({"role": "user", "text": "hello"}) + "\n", encoding="utf-8")
    extractor = _RecordingExtractor()
    extractor.available = False
    env.service.integrations.extractor = extractor

    with pytest.raises(ServiceError) as excinfo:
        env.service.integrations_import_memories(path, dry_run=True)

    assert excinfo.value.code is ErrorCode.BACKEND_UNAVAILABLE
