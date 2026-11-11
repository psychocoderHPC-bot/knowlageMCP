# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Integration fixtures: real uvicorn servers, real admin sockets, temp SQLite."""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import socket
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
    LimitsConfig,
    ServerConfig,
    StorageConfig,
    resolve_paths,
)
from knowlage_mcp.db import Database
from knowlage_mcp.evidence import EvidenceStore
from knowlage_mcp.gateway import create_app
from knowlage_mcp.service import KnowledgeService


def free_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
    finally:
        probe.close()


def wait_until(predicate: Callable[[], bool], timeout: float = 5.0, interval: float = 0.02) -> None:
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


@pytest.fixture
def short_runtime_dir() -> Iterator[Path]:
    directory = Path(tempfile.mkdtemp(prefix="kmcp-rt-", dir="/tmp"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def gateway_config(tmp_path: Path, short_runtime_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(short_runtime_dir))
    data = tmp_path / "data"
    config = Config(
        server=ServerConfig(
            host="127.0.0.1",
            port=free_port(),
            allowed_hosts=["127.0.0.1", "localhost"],
            max_request_bytes=8192,
        ),
        storage=StorageConfig(
            database=data / "knowledge.sqlite3",
            evidence_dir=data / "evidence",
            backup_dir=data / "backups",
            runtime_socket=short_runtime_dir / "admin.sock",
        ),
        limits=LimitsConfig(evidence_object_bytes=1024),
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
    gateway_config: Config, gateway_factory: Callable[[Config], RunningGateway]
) -> RunningGateway:
    return gateway_factory(gateway_config)


@pytest.fixture
def admin(gateway_server: RunningGateway) -> AdminClient:
    return AdminClient(gateway_server.admin_socket, timeout=30.0)


@pytest.fixture
def bootstrap_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    config_home = tmp_path / "config-home"
    data_home = tmp_path / "data-home"
    home = tmp_path / "home"
    runtime = Path(tempfile.mkdtemp(prefix="kmcp-boot-rt-", dir="/tmp"))
    for directory in (config_home, data_home, home, runtime):
        directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("HOME", str(home))
    try:
        yield {
            "config_path": config_home / "knowlageMCP" / "config.toml",
            "data_home": data_home,
            "home": home,
            "runtime": runtime,
        }
    finally:
        shutil.rmtree(runtime, ignore_errors=True)
