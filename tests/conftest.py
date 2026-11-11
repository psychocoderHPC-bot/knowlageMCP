# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Shared fixtures. Every fixture is isolated under ``tmp_path``."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from knowlage_mcp.config import Config, StorageConfig, resolve_paths
from knowlage_mcp.db import Database
from knowlage_mcp.evidence import EvidenceStore


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point XDG lookups at ``tmp_path`` so no user directory is touched."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg-run"))
    return tmp_path


@pytest.fixture
def tmp_config(isolated_env: Path) -> Config:
    config = Config(
        storage=StorageConfig(
            database=isolated_env / "data" / "knowledge.sqlite3",
            evidence_dir=isolated_env / "data" / "evidence",
            backup_dir=isolated_env / "data" / "backups",
        )
    )
    return resolve_paths(config)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "knowledge.sqlite3")
    database.migrate()
    yield database
    database.close()


@pytest.fixture
def evidence(tmp_path: Path) -> EvidenceStore:
    return EvidenceStore(
        tmp_path / "evidence",
        quota_bytes=1024 * 1024,
        max_object_bytes=64 * 1024,
    )


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]
