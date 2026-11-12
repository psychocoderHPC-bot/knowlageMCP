# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Non-interactive bootstrap: files, modes, re-run safety, port conflicts."""

from __future__ import annotations

import os
import socket
import stat
from pathlib import Path
from typing import Any

import pytest

from conftest import free_port
from knowlage_mcp.bootstrap import run_bootstrap
from knowlage_mcp.config import load_config
from knowlage_mcp.db import Database, iso, utcnow
from knowlage_mcp.migrations import MAX_VERSION

pytestmark = pytest.mark.integration


def _rows(database: Path, sql: str) -> list[Any]:
    db = Database(database)
    try:
        return db.connect().execute(sql).fetchall()
    finally:
        db.close()


def _bootstrap(env: dict[str, Any], **overrides: Any) -> int:
    options = {"port": free_port(), "install_systemd": False, **overrides}
    return run_bootstrap(env["config_path"], non_interactive=True, overrides=options)


def test_non_interactive_creates_private_layout(bootstrap_env: dict[str, Any]) -> None:
    assert _bootstrap(bootstrap_env) == 0
    config_path = bootstrap_env["config_path"]
    config = load_config(config_path)
    assert stat.S_IMODE(os.stat(config_path).st_mode) == 0o600
    for directory in (
        Path(config.storage.database).parent,
        Path(config.storage.evidence_dir),
        Path(config.storage.backup_dir),
        Path(config.storage.runtime_socket).parent,
    ):
        assert directory.is_dir()
        assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700
    assert Path(config.storage.database).is_file()
    assert stat.S_IMODE(os.stat(config.storage.database).st_mode) == 0o600
    assert (
        _rows(config.storage.database, "SELECT MAX(version) AS v FROM schema_migrations")[0]["v"]
        == MAX_VERSION
    )


def test_rerun_preserves_configuration_and_data(bootstrap_env: dict[str, Any]) -> None:
    assert _bootstrap(bootstrap_env) == 0
    config_path = bootstrap_env["config_path"]
    config = load_config(config_path)
    original = config_path.read_bytes()
    db = Database(config.storage.database)
    try:
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO namespaces (name, kind, description, created_at) VALUES (?, ?, ?, ?)",
                ("sentinel", "shared", "preserved", iso(utcnow())),
            )
    finally:
        db.close()

    assert (
        run_bootstrap(
            config_path,
            non_interactive=True,
            overrides={"install_systemd": False},
        )
        == 0
    )
    assert config_path.read_bytes() == original
    rows = _rows(config.storage.database, "SELECT name FROM namespaces WHERE name = 'sentinel'")
    assert len(rows) == 1


def test_port_conflict_is_reported_without_writing(
    bootstrap_env: dict[str, Any],
) -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    probe.listen(1)
    port = int(probe.getsockname()[1])
    try:
        code = run_bootstrap(
            bootstrap_env["config_path"],
            non_interactive=True,
            overrides={"port": port, "install_systemd": False},
        )
    finally:
        probe.close()
    assert code == 1
    assert not bootstrap_env["config_path"].exists()


def test_force_rewrites_config_but_never_the_database(bootstrap_env: dict[str, Any]) -> None:
    assert _bootstrap(bootstrap_env) == 0
    config_path = bootstrap_env["config_path"]
    config = load_config(config_path)
    db = Database(config.storage.database)
    try:
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO namespaces (name, kind, description, created_at) VALUES (?, ?, ?, ?)",
                ("keep-me", "shared", "force", iso(utcnow())),
            )
    finally:
        db.close()

    new_port = free_port()
    assert _bootstrap(bootstrap_env, port=new_port, force=True) == 0
    reloaded = load_config(config_path)
    assert reloaded.server.port == new_port
    assert reloaded.storage.database == config.storage.database
    assert _rows(config.storage.database, "SELECT name FROM namespaces WHERE name = 'keep-me'")


def test_packaged_systemd_unit_matches_repository_copy() -> None:
    from importlib import resources

    packaged = (
        resources.files("knowlage_mcp.resources")
        .joinpath("knowlage-mcp-gateway.service")
        .read_text(encoding="utf-8")
    )
    repository = (Path(__file__).resolve().parents[2] / "systemd" / "knowlage-mcp-gateway.service").read_text(
        encoding="utf-8"
    )
    assert packaged == repository
