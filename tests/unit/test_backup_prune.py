# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Retention and pruning of backup snapshots."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import ServiceEnv
from knowlage_mcp.backup import prune_backups
from knowlage_mcp.models import ErrorCode, ServiceError


def _snapshot(root: Path, stamp: str, suffix: str) -> Path:
    path = root / f"snapshot-{stamp}-{suffix}"
    path.mkdir(parents=True)
    (path / "manifest.json").write_text("{}", encoding="utf-8")
    return path


def _names(values: list[str]) -> set[str]:
    return {Path(value).name for value in values}


def _mixed_snapshots(root: Path) -> list[Path]:
    """Three older weekly snapshots plus two snapshots a day for ten days."""
    created = [
        _snapshot(root, "20251215T120000", "week51"),
        _snapshot(root, "20251222T120000", "week52"),
        _snapshot(root, "20251229T120000", "week01-old"),
    ]
    for day in range(1, 11):
        for hour in ("08", "18"):
            created.append(_snapshot(root, f"202601{day:02d}T{hour}0000", f"jan{day:02d}-{hour}"))
    return created


def test_daily_and_weekly_retention(tmp_path: Path) -> None:
    root = tmp_path / "backups"
    root.mkdir()
    _mixed_snapshots(root)

    result = prune_backups(root, 7, 4)

    kept = _names(result["kept"])
    assert len(kept) == 9
    assert {
        "snapshot-20251215T120000-week51",
        "snapshot-20251222T120000-week52",
        "snapshot-20260104T180000-jan04-18",
        "snapshot-20260105T180000-jan05-18",
        "snapshot-20260106T180000-jan06-18",
        "snapshot-20260107T180000-jan07-18",
        "snapshot-20260108T180000-jan08-18",
        "snapshot-20260109T180000-jan09-18",
        "snapshot-20260110T180000-jan10-18",
    } == kept
    assert len(result["removed"]) == 14
    assert {path.name for path in root.iterdir()} == kept
    assert result["dry_run"] is False
    assert result["keep_daily"] == 7
    assert result["keep_weekly"] == 4


def test_newest_snapshot_is_never_removed(tmp_path: Path) -> None:
    root = tmp_path / "backups"
    root.mkdir()
    created = _mixed_snapshots(root)
    newest = max(created, key=lambda path: path.name)

    result = prune_backups(root, 0, 0)

    assert result["kept"] == [str(newest)]
    assert newest.exists()
    assert len(result["removed"]) == len(created) - 1


def test_dry_run_removes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "backups"
    root.mkdir()
    created = _mixed_snapshots(root)

    result = prune_backups(root, 2, 1, dry_run=True)

    assert result["dry_run"] is True
    assert len(result["removed"]) == len(created) - 2
    assert all(path.exists() for path in created)


def test_symlinked_snapshot_is_ignored(tmp_path: Path) -> None:
    root = tmp_path / "backups"
    root.mkdir()
    real = _snapshot(root, "20260101T120000", "real")
    linked = root / "snapshot-20260102T120000-linked"
    linked.symlink_to(real, target_is_directory=True)

    result = prune_backups(root, 0, 0)

    assert _names(result["kept"]) == {real.name}
    assert _names(result["removed"]) == set()
    assert real.is_dir() and not real.is_symlink()
    assert linked.is_symlink()


def test_negative_keep_values_fail(env: ServiceEnv, tmp_path: Path) -> None:
    root = tmp_path / "backups"
    root.mkdir()

    with pytest.raises(ServiceError) as direct:
        prune_backups(root, -1, 0)
    assert direct.value.code is ErrorCode.INVALID_ARGUMENT

    with pytest.raises(ServiceError) as service:
        env.service.backup_prune(keep_daily=-1)
    assert service.value.code is ErrorCode.INVALID_ARGUMENT

    with pytest.raises(ServiceError) as admin:
        env.service.admin_dispatch("backup.prune", {"keep_weekly": -1})
    assert admin.value.code is ErrorCode.INVALID_ARGUMENT
