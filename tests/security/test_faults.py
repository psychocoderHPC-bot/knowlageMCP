# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Storage faults, crashes, leases, locking, and graceful shutdown stay safe."""

from __future__ import annotations

import errno
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx2
import pytest
from secutil import (
    REPO_ROOT,
    CtlEnv,
    RunningGateway,
    auth,
    count_rows,
    free_port,
    record,
    register,
    sqlite_exec,
    sqlite_one,
    wait_until,
)

from knowlage_mcp import evidence as evidence_module
from knowlage_mcp.admin_client import AdminError
from knowlage_mcp.config import Config, ServerConfig, StorageConfig, resolve_paths, write_config_atomic
from knowlage_mcp.db import Database, lock_file
from knowlage_mcp.evidence import EvidenceStore
from knowlage_mcp.models import (
    EntryPayload,
    ErrorCode,
    Kind,
    RecordRequest,
    ServiceError,
)
from knowlage_mcp.service import KnowledgeService

pytestmark = pytest.mark.security


def test_read_only_database_is_a_structured_storage_error(tmp_path: Path) -> None:
    base = tmp_path / "readonly"
    config = resolve_paths(
        Config(
            storage=StorageConfig(
                database=base / "knowledge.sqlite3",
                evidence_dir=base / "evidence",
                backup_dir=base / "backups",
                runtime_socket=base / "admin.sock",
            )
        )
    )
    database = Database(config.storage.database)
    database.migrate()
    store = EvidenceStore(config.storage.evidence_dir, quota_bytes=1 << 20, max_object_bytes=1 << 20)
    service = KnowledgeService(config, database, store)
    try:
        service.bootstrap()
        registration = service.register(mode="root")
        session = service.authenticate(registration.session_token)
        database.connect().execute("PRAGMA query_only=ON")
        with pytest.raises(ServiceError) as error:
            service.record(
                session,
                RecordRequest(
                    namespace="shared",
                    entry=EntryPayload(kind=Kind.OBSERVATION, title="readonly", claim="readonly"),
                ),
            )
        assert error.value.code is ErrorCode.STORAGE_FULL
        assert count_rows(config.storage.database, "entries") == 0
    finally:
        database.close()


def test_disk_full_evidence_upload_is_structured(
    gateway_server: RunningGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = gateway_server.config.storage.database

    def no_space(*_args: Any, **_kwargs: Any) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(evidence_module.os, "replace", no_space)
    with httpx2.Client(base_url=gateway_server.base_url) as client:
        token = register(client)["session_token"]
        before = count_rows(database, "evidence_objects")
        response = client.post(
            "/v1/bridge/evidence",
            headers={**auth(token), "Content-Type": "text/plain"},
            content=b"disk full probe",
        )
        assert response.status_code == 507, response.text
        assert response.json()["error"]["code"] == "STORAGE_FULL"
        assert count_rows(database, "evidence_objects") == before
    root = Path(gateway_server.config.storage.evidence_dir)
    assert not list(root.rglob("*.part"))


def test_missing_evidence_file_is_not_found(gateway_server: RunningGateway) -> None:
    root = Path(gateway_server.config.storage.evidence_dir)
    with httpx2.Client(base_url=gateway_server.base_url) as client:
        token = register(client)["session_token"]
        uploaded = client.post(
            "/v1/bridge/evidence",
            headers={**auth(token), "Content-Type": "text/plain"},
            content=b"will vanish",
        )
        assert uploaded.status_code == 200, uploaded.text
        sha = uploaded.json()["evidence_id"][3:]
        (root / sha[:2] / sha[2:4] / sha).unlink()
        response = client.get(f"/v1/bridge/evidence/ev_{sha}", headers=auth(token))
        assert response.status_code == 404, response.text
        assert response.json()["error"]["code"] == "NOT_FOUND"
        assert client.get("/v1/health").status_code == 200


def test_interrupted_backup_verification_fails_cleanly(gateway_server: RunningGateway, admin: Any) -> None:
    created = admin.call("backup.create")
    snapshot = Path(created["path"])
    (snapshot / "knowledge.sqlite3").write_bytes(b"truncated database")
    verified = admin.call("backup.verify", {"path": str(snapshot)})
    assert verified["ok"] is False
    assert verified["checks"]["database_sha256"] is False
    (snapshot / "manifest.json").write_text("{not valid json", encoding="utf-8")
    with pytest.raises(AdminError) as error:
        admin.call("backup.verify", {"path": str(snapshot)})
    assert error.value.code == "INVALID_ARGUMENT"


def test_client_disconnect_mid_response_server_survives(gateway_server: RunningGateway) -> None:
    host = gateway_server.config.server.host
    port = gateway_server.config.server.port
    probe = socket.create_connection((host, port), timeout=5.0)
    try:
        probe.sendall(b"GET /v1/health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
    finally:
        probe.close()
    with httpx2.Client(base_url=gateway_server.base_url) as client:
        statuses: list[int] = []

        def healthy() -> bool:
            statuses.append(client.get("/v1/health").status_code)
            return statuses[-1] == 200

        wait_until(healthy, timeout=6.0)


def test_stale_lease_is_requeued_not_double_submitted(gateway_server: RunningGateway) -> None:
    database = gateway_server.config.storage.database
    with httpx2.Client(base_url=gateway_server.base_url) as client:
        token = register(client)["session_token"]
        data = record(client, token, "stale lease candidate")
        entry_id = data["entry"]["entry_id"]
        revision_id = data["entry"]["revision_id"]
        job = sqlite_one(database, "SELECT job_id FROM review_jobs WHERE revision_id = ?", (revision_id,))
        assert job is not None
        job_id = str(job["job_id"])
        sqlite_exec(
            database,
            "UPDATE review_jobs SET state = 'leased', lease_owner = 'stale-session', "
            "lease_id = 'stale-lease', lease_expires_at = '2999-01-01T00:00:00.000000Z' "
            "WHERE job_id = ?",
            (job_id,),
        )
        gateway_server.service.identity.startup_reset()
        requeued = sqlite_one(
            database, "SELECT state, lease_owner, lease_id FROM review_jobs WHERE job_id = ?", (job_id,)
        )
        assert requeued is not None
        assert requeued["state"] == "queued"
        assert requeued["lease_owner"] is None and requeued["lease_id"] is None

        fresh = register(client, label="after-restart")
        submitted = client.post(
            "/v1/tools/submit_review",
            headers=auth(fresh["session_token"]),
            json={
                "entry_id": entry_id,
                "revision_id": revision_id,
                "job_id": job_id,
                "lease_id": "stale-lease",
                "result": "supports",
                "method": "manual",
                "evidence": "stale attempt",
            },
        )
        assert submitted.status_code == 403, submitted.text
        assert submitted.json()["error"]["code"] == "REVIEW_INELIGIBLE"
        detail = client.post(
            "/v1/tools/get_entry", headers=auth(fresh["session_token"]), json={"entry_id": entry_id}
        )
        assert detail.json()["data"]["lifecycle"] == "candidate"
    assert count_rows(database, "reviews") == 0


def test_second_gateway_process_refuses_the_database_lock(
    gateway_server: RunningGateway, ctl_env: CtlEnv
) -> None:
    database = gateway_server.config.storage.database
    lock_path = database.parent / f"{database.name}.lock"
    with lock_file(lock_path):
        result = subprocess.run(
            [sys.executable, "-m", "knowlage_mcp.cli", "--config", str(ctl_env.config_path)],
            cwd=str(REPO_ROOT),
            env=ctl_env.env,
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
        )
    assert result.returncode == 3, (result.returncode, result.stdout, result.stderr)
    assert "lock" in result.stderr.lower()


def test_sigterm_exits_zero_and_removes_the_socket(tmp_path: Path) -> None:
    config = resolve_paths(
        Config(
            server=ServerConfig(host="127.0.0.1", port=free_port()),
            storage=StorageConfig(
                database=tmp_path / "sig" / "knowledge.sqlite3",
                evidence_dir=tmp_path / "sig" / "evidence",
                backup_dir=tmp_path / "sig" / "backups",
                runtime_socket=tmp_path / "sig" / "run" / "admin.sock",
            ),
        )
    )
    config_path = tmp_path / "gateway.toml"
    write_config_atomic(config_path, config)
    process = subprocess.Popen(
        [sys.executable, "-m", "knowlage_mcp.cli", "--config", str(config_path)],
        cwd=str(REPO_ROOT),
        env={**os.environ},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wait_until(lambda: config.storage.runtime_socket.exists(), timeout=25.0)
        process.send_signal(signal.SIGTERM)
        returncode = process.wait(timeout=25.0)
        stdout, stderr = process.communicate()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    assert returncode == 0, (returncode, stdout, stderr)
    wait_until(lambda: not config.storage.runtime_socket.exists(), timeout=5.0)
