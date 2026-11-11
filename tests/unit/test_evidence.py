# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Unit tests for evidence dedupe, quota, size caps, and redaction."""

from __future__ import annotations

from pathlib import Path

import pytest

from knowlage_mcp.db import Database, iso, utcnow
from knowlage_mcp.evidence import EvidenceStore, redact_text, scan_for_secrets
from knowlage_mcp.models import ErrorCode, ServiceError

GITHUB_TOKEN = "ghp_" + "a1" * 18
AWS_KEY = "AKIA" + "B2" * 8


def test_put_get_roundtrip_and_dedupe(evidence: EvidenceStore) -> None:
    first = evidence.put(b"hello evidence\n", "text/plain", name="note.txt")
    second = evidence.put(b"hello evidence\n", "text/plain", name="copy.txt")
    assert first.evidence_id == second.evidence_id
    assert first.sha256 == second.sha256
    assert evidence.total_bytes() == len(b"hello evidence\n")
    assert evidence.exists(first.evidence_id)
    assert evidence.get(first.evidence_id) == b"hello evidence\n"
    assert first.storage_path == f"{first.sha256[:2]}/{first.sha256[2:4]}/{first.sha256}"


def test_size_cap(evidence: EvidenceStore) -> None:
    oversized = b"x" * (evidence.max_object_bytes + 1)
    with pytest.raises(ServiceError) as excinfo:
        evidence.put(oversized, "text/plain")
    assert excinfo.value.code is ErrorCode.BUDGET_EXCEEDED
    assert evidence.total_bytes() == 0


def test_quota_enforced(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "quota", quota_bytes=10, max_object_bytes=1024)
    store.put(b"0123456789", "text/plain")
    with pytest.raises(ServiceError) as excinfo:
        store.put(b"abc", "text/plain")
    assert excinfo.value.code is ErrorCode.STORAGE_FULL


def test_secret_redaction_on_put(evidence: EvidenceStore) -> None:
    payload = (
        f"token {GITHUB_TOKEN}\naws {AWS_KEY}\npassword = supersecret123\nheader Bearer abcdefghijklmnop\n"
    )
    record = evidence.put(payload.encode("utf-8"), "text/plain")
    assert record.redacted is True
    stored = evidence.get(record.evidence_id).decode("utf-8")
    assert GITHUB_TOKEN not in stored
    assert AWS_KEY not in stored
    assert "supersecret123" not in stored
    assert "abcdefghijklmnop" not in stored
    assert "<redacted:github_token>" in stored
    assert "<redacted:secret_assignment>" in stored
    labels = scan_for_secrets(payload)
    assert "github_token" in labels
    assert "aws_access_key" in labels
    assert "bearer_token" in labels
    assert "secret_assignment" in labels


def test_redact_text_is_idempotent(evidence: EvidenceStore) -> None:
    once = redact_text("secret = abcdefgh12345678")
    assert "<redacted:" in once
    assert redact_text(once) == once


def test_invalid_identifier_is_not_found(evidence: EvidenceStore) -> None:
    for candidate in ("../../etc/passwd", "ev_xyz", "ev_" + "0" * 64):
        with pytest.raises(ServiceError) as excinfo:
            evidence.get(candidate)
        assert excinfo.value.code is ErrorCode.NOT_FOUND
        assert evidence.exists(candidate) is False


def test_binary_payload_not_redacted(evidence: EvidenceStore) -> None:
    blob = bytes(range(256)) * 4
    record = evidence.put(blob, "application/octet-stream")
    assert record.redacted is False
    assert evidence.get(record.evidence_id) == blob


def _insert_evidence_row(
    database: Database,
    *,
    evidence_id: str,
    sha256: str,
    size_bytes: int,
    storage_path: str,
    redacted: bool,
    revision_id: str | None,
) -> None:
    database.connect().execute(
        """
        INSERT INTO evidence_objects
            (evidence_id, sha256, size_bytes, media_type, storage_path, redacted, created_at, revision_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            evidence_id,
            sha256,
            size_bytes,
            "text/plain",
            storage_path,
            int(redacted),
            iso(utcnow()),
            revision_id,
        ),
    )


def test_delete_unreferenced(evidence: EvidenceStore, db: Database) -> None:
    record = evidence.put(b"orphan evidence", "text/plain")
    _insert_evidence_row(
        db,
        evidence_id=record.evidence_id,
        sha256=record.sha256,
        size_bytes=record.size_bytes,
        storage_path=record.storage_path,
        redacted=record.redacted,
        revision_id=None,
    )
    assert evidence.delete_unreferenced(db.connect()) == 1
    assert evidence.exists(record.evidence_id) is False
    count = db.connect().execute("SELECT COUNT(*) AS n FROM evidence_objects").fetchone()["n"]
    assert count == 0


def test_delete_unreferenced_respects_ledger(evidence: EvidenceStore, db: Database) -> None:
    record = evidence.put(b"protected evidence", "text/plain")
    _insert_evidence_row(
        db,
        evidence_id=record.evidence_id,
        sha256=record.sha256,
        size_bytes=record.size_bytes,
        storage_path=record.storage_path,
        redacted=record.redacted,
        revision_id=None,
    )
    ledger = [("evidence", record.evidence_id)]
    assert evidence.delete_unreferenced(db.connect(), ledger) == 0
    assert evidence.exists(record.evidence_id) is True
    assert evidence.delete_unreferenced(db.connect(), []) == 1
    assert evidence.exists(record.evidence_id) is False
