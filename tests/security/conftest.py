# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Security fixtures and helpers: real gateway, admin socket, and SQLite.

Test modules import the helper functions at module load time, which binds them
before any other test directory can replace ``sys.modules["conftest"]``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from knowlage_mcp.admin import AdminServer
from knowlage_mcp.admin_client import AdminClient
from knowlage_mcp.config import (
    Config,
    ServerConfig,
    StorageConfig,
    resolve_paths,
    write_config_atomic,
)
from knowlage_mcp.db import Database
from knowlage_mcp.evidence import EvidenceStore
from knowlage_mcp.gateway import create_app
from knowlage_mcp.service import KnowledgeService

REPO_ROOT = Path(__file__).resolve().parents[2]


def free_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
    finally:
        probe.close()


def wait_until(predicate: Callable[[], bool], timeout: float = 8.0, interval: float = 0.02) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError("condition was not met before the deadline")


@dataclass
class RunningGateway:
    config: Config
    base_url: str
    admin_socket: Path
    service: KnowledgeService
    server: uvicorn.Server
    thread: threading.Thread
    error: list[BaseException]


def start_gateway(config: Config) -> RunningGateway:
    database = Database(config.storage.database)
    evidence = EvidenceStore(
        config.storage.evidence_dir,
        quota_bytes=config.storage.evidence_quota_bytes,
        max_object_bytes=config.limits.evidence_object_bytes,
    )
    service = KnowledgeService(config, database, evidence)
    app = create_app(config, service)
    admin = AdminServer(service, config.storage.runtime_socket)
    uv_config = uvicorn.Config(
        app,
        host=config.server.host,
        port=config.server.port,
        log_level="warning",
        access_log=False,
        lifespan="on",
    )
    server = uvicorn.Server(uv_config)
    error: list[BaseException] = []

    async def run() -> None:
        try:
            await admin.start()
            try:
                await server.serve()
            finally:
                await admin.stop()
        except BaseException as exc:
            error.append(exc)

    thread = threading.Thread(target=lambda: asyncio.run(run()), daemon=True)
    thread.start()
    running = RunningGateway(
        config=config,
        base_url=f"http://{config.server.host}:{config.server.port}",
        admin_socket=Path(config.storage.runtime_socket),
        service=service,
        server=server,
        thread=thread,
        error=error,
    )
    with contextlib.suppress(AssertionError):
        wait_until(
            lambda: (server.started and running.admin_socket.exists()) or bool(error) or not thread.is_alive()
        )
    if error:
        raise error[0]
    if not server.started:
        raise AssertionError("gateway did not start")
    return running


def stop_gateway(running: RunningGateway) -> None:
    running.server.should_exit = True
    running.thread.join(timeout=10)
    assert not running.thread.is_alive(), "gateway thread did not stop"


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def register(client: Any, **extra: Any) -> dict[str, Any]:
    response = client.post("/v1/bridge/register", json={"mode": "root", "label": "sec", **extra})
    assert response.status_code == 200, response.text
    return response.json()


def record(client: Any, token: str, claim: str, *, title: str = "sec", **entry: Any) -> dict[str, Any]:
    payload = {"namespace": "shared", "entry": {"kind": "observation", "title": title, "claim": claim}}
    payload["entry"].update(entry)
    response = client.post("/v1/tools/record", headers=auth(token), json=payload)
    assert response.status_code == 200, response.text
    envelope = response.json()
    assert envelope["ok"] is True, envelope
    return envelope["data"]


def sqlite_exec(path: str | Path, statement: str, parameters: tuple[Any, ...] = ()) -> None:
    import sqlite3

    connection = sqlite3.connect(str(path), timeout=5.0)
    try:
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute(statement, parameters)
        connection.commit()
    finally:
        connection.close()


def sqlite_one(path: str | Path, statement: str, parameters: tuple[Any, ...] = ()) -> Any:
    import sqlite3

    connection = sqlite3.connect(str(path), timeout=5.0)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(statement, parameters).fetchone()
    finally:
        connection.close()


def table_names(path: str | Path) -> set[str]:
    import sqlite3

    connection = sqlite3.connect(str(path), timeout=5.0)
    try:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        return {str(row[0]) for row in rows}
    finally:
        connection.close()


def count_rows(path: str | Path, table: str) -> int:
    import sqlite3

    connection = sqlite3.connect(str(path), timeout=5.0)
    try:
        row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return int(row[0]) if row is not None else 0
    finally:
        connection.close()


@dataclass
class CtlEnv:
    config_path: Path
    env: dict[str, str]

    def run(self, args: list[str], timeout: float = 25.0) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "knowlage_mcp.gatewayctl", *args],
            cwd=str(REPO_ROOT),
            env=self.env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )


@pytest.fixture
def short_runtime_dir() -> Iterator[Path]:
    directory = Path(tempfile.mkdtemp(prefix="kmcp-sec-rt-", dir="/tmp"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def security_config(tmp_path: Path, short_runtime_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(short_runtime_dir))
    data = tmp_path / "data"
    config = Config(
        server=ServerConfig(
            host="127.0.0.1",
            port=free_port(),
            allowed_hosts=["127.0.0.1", "localhost"],
            allowed_origins=[],
            max_request_bytes=131072,
        ),
        storage=StorageConfig(
            database=data / "knowledge.sqlite3",
            evidence_dir=data / "evidence",
            backup_dir=data / "backups",
            runtime_socket=short_runtime_dir / "admin.sock",
            evidence_quota_bytes=32 * 1024 * 1024,
        ),
    )
    return resolve_paths(config)


@pytest.fixture
def gateway_factory() -> Iterator[Callable[[Config], RunningGateway]]:
    started: list[RunningGateway] = []

    def factory(config: Config) -> RunningGateway:
        running = start_gateway(config)
        started.append(running)
        return running

    try:
        yield factory
    finally:
        for running in started:
            with contextlib.suppress(Exception):
                if running.thread.is_alive():
                    stop_gateway(running)


@pytest.fixture
def gateway_server(
    security_config: Config, gateway_factory: Callable[[Config], RunningGateway]
) -> RunningGateway:
    return gateway_factory(security_config)


@pytest.fixture
def admin(gateway_server: RunningGateway) -> AdminClient:
    return AdminClient(gateway_server.admin_socket, timeout=30.0)


@pytest.fixture
def ctl_env(tmp_path: Path, gateway_server: RunningGateway) -> CtlEnv:
    config_home = tmp_path / "ctl-config"
    config_path = config_home / "knowlageMCP" / "config.toml"
    write_config_atomic(config_path, gateway_server.config)
    env = {**os.environ, "XDG_CONFIG_HOME": str(config_home)}
    return CtlEnv(config_path=config_path, env=env)
