# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Online SQLite snapshots with manifests, verification, and ledger-safe restore."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__
from .config import Config
from .db import Database, DatabaseLockedError, iso, lock_file
from .evidence import EvidenceStore
from .models import ErrorCode, ServiceError
from .policy import now_from

_MANIFEST_NAME = "manifest.json"
_DB_NAME = "knowledge.sqlite3"
_EVIDENCE_DIR = "evidence"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class BackupManager:
    """Create, verify, and restore gateway snapshots.

    A snapshot is a directory containing ``knowledge.sqlite3``, an ``evidence``
    tree, and ``manifest.json``. Restore never writes into an existing database
    or a running gateway and reapplies the deletion ledger so forgotten content
    does not resurrect.
    """

    def __init__(
        self,
        config: Config,
        db: Database,
        evidence: EvidenceStore,
        *,
        clock: Callable[[], float],
    ) -> None:
        self.config = config
        self.db = db
        self.evidence = evidence
        self._clock = clock

    def _now(self) -> datetime:
        return now_from(self._clock)

    # -- create ----------------------------------------------------------
    def create(self) -> dict[str, Any]:
        backup_dir = Path(self.config.storage.backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        at = self._now()
        stamp = iso(at).replace(":", "").replace("-", "").replace(".", "")[:15]
        temporary = Path(tempfile.mkdtemp(dir=backup_dir, prefix=".snapshot-"))
        try:
            db_target = temporary / _DB_NAME
            self.db.backup_to(db_target)
            inventory: list[dict[str, Any]] = []
            evidence_root = Path(self.evidence.root)
            if evidence_root.exists():
                for source in sorted(evidence_root.rglob("*")):
                    if not source.is_file() or source.name.endswith(".part"):
                        continue
                    relative = source.relative_to(evidence_root)
                    destination = temporary / _EVIDENCE_DIR / relative
                    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    shutil.copy2(source, destination)
                    inventory.append(
                        {
                            "path": relative.as_posix(),
                            "sha256": _sha256_file(destination),
                            "size_bytes": destination.stat().st_size,
                        }
                    )
            ledger = [
                dict(row)
                for row in self.db.connect()
                .execute(
                    "SELECT object_type, object_id, deleted_at, reason FROM deletion_ledger "
                    "ORDER BY deleted_at"
                )
                .fetchall()
            ]
            manifest = {
                "schema_version": self.db.schema_version(),
                "app_version": __version__,
                "created_at": iso(at),
                "database": {
                    "path": _DB_NAME,
                    "sha256": _sha256_file(db_target),
                    "size_bytes": db_target.stat().st_size,
                },
                "database_sha256": _sha256_file(db_target),
                "evidence": inventory,
                "deletion_ledger": ledger,
            }
            manifest_path = temporary / _MANIFEST_NAME
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            os.chmod(manifest_path, 0o600)
            final = backup_dir / (
                f"snapshot-{stamp}-{hashlib.sha256(iso(at).encode()).hexdigest()[:8]}-{secrets.token_hex(3)}"
            )
            if final.exists():
                raise ServiceError(ErrorCode.CONFLICT, "backup snapshot already exists")
            os.replace(temporary, final)
            return {
                "path": str(final),
                "created_at": iso(at),
                "schema_version": manifest["schema_version"],
                "app_version": manifest["app_version"],
                "evidence_objects": len(inventory),
                "deletion_ledger_entries": len(ledger),
            }
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    # -- verify ----------------------------------------------------------
    def verify(self, path: str | Path) -> dict[str, Any]:
        snapshot = Path(path)
        manifest_path = snapshot / _MANIFEST_NAME
        if not snapshot.is_dir() or not manifest_path.is_file():
            raise ServiceError(ErrorCode.INVALID_ARGUMENT, "backup snapshot or manifest is missing")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ServiceError(ErrorCode.INVALID_ARGUMENT, "backup manifest is not valid JSON") from exc
        checks: dict[str, Any] = {}
        db_path = snapshot / str(manifest.get("database", {}).get("path", _DB_NAME))
        expected_db_hash = str(manifest.get("database", {}).get("sha256", ""))
        if not db_path.is_file():
            checks["database_present"] = False
        else:
            checks["database_present"] = True
            checks["database_sha256"] = _sha256_file(db_path) == expected_db_hash
            connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()
                checks["integrity"] = bool(integrity is not None and str(integrity[0]) == "ok")
                version_row = connection.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
                ).fetchone()
                checks["schema_version"] = int(version_row[0]) if version_row is not None else 0
            except sqlite3.DatabaseError:
                checks["integrity"] = False
            finally:
                connection.close()
        evidence_ok = True
        evidence_root = snapshot / _EVIDENCE_DIR
        for entry in manifest.get("evidence", []) or []:
            relative = str(entry.get("path", ""))
            candidate = evidence_root / relative
            if ".." in Path(relative).parts or Path(relative).is_absolute():
                evidence_ok = False
                break
            if not candidate.is_file() or _sha256_file(candidate) != str(entry.get("sha256", "")):
                evidence_ok = False
                break
        checks["evidence"] = evidence_ok
        ok = (
            checks.get("database_present") is True
            and checks.get("database_sha256") is True
            and checks.get("integrity") is True
            and evidence_ok
        )
        return {"ok": bool(ok), "path": str(snapshot), "checks": checks, "manifest": manifest}

    # -- restore ---------------------------------------------------------
    @staticmethod
    def _apply_ledger(
        conn: sqlite3.Connection,
        ledger: list[dict[str, Any]],
        evidence_root: Path,
    ) -> dict[str, int]:
        counts = {"entries": 0, "revisions": 0, "evidence": 0}
        for row in ledger:
            object_type = str(row.get("object_type", ""))
            object_id = str(row.get("object_id", ""))
            deleted_at = str(row.get("deleted_at", ""))
            reason = row.get("reason")
            if object_type == "entry":
                conn.execute(
                    "UPDATE entry_revisions SET prev_revision_id = NULL WHERE entry_id = ?",
                    (object_id,),
                )
                conn.execute("DELETE FROM entry_fts WHERE entry_id = ?", (object_id,))
                counts["revisions"] += conn.execute(
                    "DELETE FROM entry_revisions WHERE entry_id = ?", (object_id,)
                ).rowcount
                counts["entries"] += conn.execute(
                    "DELETE FROM entries WHERE entry_id = ?", (object_id,)
                ).rowcount
            elif object_type == "revision":
                conn.execute("DELETE FROM entry_fts WHERE revision_id = ?", (object_id,))
                conn.execute(
                    "UPDATE entry_revisions SET prev_revision_id = NULL WHERE prev_revision_id = ?",
                    (object_id,),
                )
                counts["revisions"] += conn.execute(
                    "DELETE FROM entry_revisions WHERE revision_id = ?", (object_id,)
                ).rowcount
            elif object_type == "evidence":
                found = conn.execute(
                    "SELECT storage_path FROM evidence_objects WHERE evidence_id = ?",
                    (object_id,),
                ).fetchone()
                if found is not None:
                    candidate = evidence_root / str(found["storage_path"])
                    with contextlib.suppress(FileNotFoundError):
                        candidate.unlink()
                    conn.execute("DELETE FROM evidence_objects WHERE evidence_id = ?", (object_id,))
                    counts["evidence"] += 1
            conn.execute(
                "INSERT OR IGNORE INTO deletion_ledger "
                "(object_type, object_id, deleted_at, reason) VALUES (?, ?, ?, ?)",
                (object_type, object_id, deleted_at, reason),
            )
        return counts

    def restore(self, path: str | Path, destination: str | Path) -> dict[str, Any]:
        snapshot = Path(path)
        target = Path(destination)
        running_db = Path(self.db.path).resolve()
        if target.is_symlink():
            raise ServiceError(ErrorCode.CONFLICT, "restore destination must not be a symlink")
        if target.exists():
            if not target.is_dir():
                raise ServiceError(ErrorCode.CONFLICT, "restore destination exists and is not a directory")
            if any(target.iterdir()):
                raise ServiceError(ErrorCode.CONFLICT, "restore destination is not empty")
        if (target / _DB_NAME).resolve() == running_db:
            raise ServiceError(ErrorCode.CONFLICT, "refusing to overwrite the running database")
        lock_path = target.parent / f".{target.name}.restore.lock"
        try:
            with lock_file(lock_path):
                pass
        except DatabaseLockedError as exc:
            raise ServiceError(
                ErrorCode.CONFLICT, "a gateway is already running against the restore destination"
            ) from exc
        finally:
            with contextlib.suppress(FileNotFoundError):
                lock_path.unlink()

        verification = self.verify(snapshot)
        if not verification["ok"]:
            raise ServiceError(ErrorCode.INVALID_ARGUMENT, "backup snapshot failed verification")
        manifest = verification["manifest"]
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = Path(tempfile.mkdtemp(dir=target.parent, prefix=".restore-"))
        try:
            shutil.copy2(snapshot / _DB_NAME, temporary / _DB_NAME)
            source_evidence = snapshot / _EVIDENCE_DIR
            restored_evidence = temporary / _EVIDENCE_DIR
            if source_evidence.is_dir():
                shutil.copytree(source_evidence, restored_evidence)
            else:
                restored_evidence.mkdir(parents=True, exist_ok=True)
            ledger_rows = list(manifest.get("deletion_ledger", []) or [])
            # The live ledger is authoritative: a snapshot taken before a
            # deletion must not resurrect that object after restore.
            live_ledger = [
                dict(row)
                for row in self.db.connect()
                .execute("SELECT object_type, object_id, deleted_at, reason FROM deletion_ledger")
                .fetchall()
            ]
            merged: dict[tuple[str, str], dict[str, Any]] = {}
            for row in [*ledger_rows, *live_ledger]:
                merged[(str(row.get("object_type", "")), str(row.get("object_id", "")))] = row
            ledger_rows = list(merged.values())
            connection = sqlite3.connect(temporary / _DB_NAME)
            try:
                connection.execute("PRAGMA foreign_keys = ON")
                counts = self._apply_ledger(connection, ledger_rows, restored_evidence)
                connection.commit()
            finally:
                connection.close()
            if target.exists():
                target.rmdir()
            os.replace(temporary, target)
            return {
                "restored": True,
                "destination": str(target),
                "ledger_reapplied": counts,
                "ledger_entries": len(ledger_rows),
            }
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise


__all__ = ["BackupManager"]
