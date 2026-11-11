# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Security fixtures: real uvicorn gateway, real admin socket, real SQLite.

Helpers live in :mod:`secutil` so their import name never collides with the
other test directories' ``conftest`` modules.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from secutil import CtlEnv, RunningGateway, free_port, start_gateway, stop_gateway

from knowlage_mcp.admin_client import AdminClient
from knowlage_mcp.config import (
    Config,
    ServerConfig,
    StorageConfig,
    resolve_paths,
    write_config_atomic,
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
