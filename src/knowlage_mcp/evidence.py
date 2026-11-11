# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Bounded evidence object store with hashing, quota, and secret redaction."""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import re
import sqlite3
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .db import utcnow
from .models import ErrorCode, ServiceError

#: Evidence identifiers are derived from the stored SHA-256 digest.
_EVIDENCE_ID_RE = re.compile(r"^ev_[0-9a-f]{64}$")

_TEXT_MEDIA_TYPES = {
    "application/json",
    "application/x-ndjson",
    "application/yaml",
    "application/x-yaml",
    "application/xml",
    "application/toml",
    "application/sql",
    "application/x-sh",
}
_BINARY_MEDIA_TYPES = {
    "application/octet-stream",
    "application/pdf",
    "application/zip",
    "application/gzip",
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
}

# Ordered from specific to generic. The label becomes ``<redacted:label>``.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    ("private_key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("github_fine_grained_token", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("openai_key", re.compile(r"(?<![A-Za-z0-9])sk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}")),
    ("aws_access_key", re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}")),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("bearer_token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")),
    (
        "secret_assignment",
        re.compile(r"(?i)\b(?:password|passwd|secret|token|api[_-]?key)\b\s*[:=]\s*[\"']?[^\s\"',;]{8,}"),
    ),
    ("long_base64", re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/=])")),
)


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    evidence_id: str
    sha256: str
    size_bytes: int
    media_type: str
    storage_path: str
    redacted: bool = False
    name: str | None = None
    created_at: datetime = field(default_factory=utcnow)


def _is_text(media_type: str, data: bytes) -> bool:
    normalized = (media_type or "").split(";", 1)[0].strip().lower()
    if normalized.startswith("text/"):
        return True
    if normalized in _TEXT_MEDIA_TYPES or normalized.endswith(("+json", "+xml")):
        return True
    if normalized in _BINARY_MEDIA_TYPES:
        return False
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def redact_text(text: str) -> str:
    """Replace recognized credential patterns with ``<redacted:label>``."""
    redacted = text
    for label, pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(f"<redacted:{label}>", redacted)
    return redacted


def scan_for_secrets(text: str) -> list[str]:
    """Return the labels of credential patterns found in ``text``, once each."""
    found: list[str] = []
    for label, pattern in _SECRET_PATTERNS:
        if pattern.search(text) and label not in found:
            found.append(label)
    return found


def _storage_error(exc: OSError) -> ServiceError:
    """Map filesystem failures to structured storage errors."""
    if exc.errno in {errno.ENOSPC, errno.EDQUOT}:
        return ServiceError(ErrorCode.STORAGE_FULL, "evidence storage is full")
    if exc.errno in {errno.EROFS, errno.EACCES, errno.EPERM}:
        return ServiceError(ErrorCode.STORAGE_FULL, "evidence storage is read-only")
    return ServiceError(ErrorCode.BACKEND_UNAVAILABLE, "evidence storage is unavailable")


class EvidenceStore:
    """Content-addressed evidence storage under a private root directory."""

    def __init__(self, root: str | Path, quota_bytes: int, max_object_bytes: int) -> None:
        if quota_bytes <= 0:
            raise ValueError("quota_bytes must be positive")
        if max_object_bytes <= 0:
            raise ValueError("max_object_bytes must be positive")
        self.root = Path(root)
        self.quota_bytes = quota_bytes
        self.max_object_bytes = max_object_bytes

    @staticmethod
    def evidence_id_for_sha256(sha256_hex: str) -> str:
        return f"ev_{sha256_hex}"

    @staticmethod
    def _relpath(sha256_hex: str) -> str:
        return f"{sha256_hex[:2]}/{sha256_hex[2:4]}/{sha256_hex}"

    @staticmethod
    def _validate_evidence_id(evidence_id: str) -> str:
        if not _EVIDENCE_ID_RE.match(evidence_id):
            raise ServiceError(ErrorCode.NOT_FOUND, "evidence object not found")
        return evidence_id[3:]

    def _path_for_sha(self, sha256_hex: str) -> Path:
        return self.root / self._relpath(sha256_hex)

    def _safe_path(self, sha256_hex: str) -> Path:
        """Return the content path, refusing symlinks and root escapes.

        Evidence is content-addressed, so a crafted symlink inside the root
        must not turn an evidence id into an arbitrary host file read or write.
        """
        path = self._path_for_sha(sha256_hex)
        root = self.root.resolve()
        if path.is_symlink():
            raise ServiceError(ErrorCode.NOT_FOUND, "evidence object not found")
        try:
            parent = path.parent
            if parent.exists() and not parent.resolve().is_relative_to(root):
                raise ServiceError(ErrorCode.NOT_FOUND, "evidence object not found")
        except OSError as exc:
            raise ServiceError(ErrorCode.NOT_FOUND, "evidence object not found") from exc
        return path

    def put(
        self,
        data: bytes,
        media_type: str,
        name: str | None = None,
        revision_id: str | None = None,
    ) -> EvidenceRecord:
        """Store bytes, deduplicating by content hash.

        Text-like objects are scanned and redacted before hashing and storage.
        The size cap applies to the supplied bytes; the quota applies to the
        stored bytes.
        """
        if not isinstance(data, bytes):
            raise ServiceError(ErrorCode.INVALID_ARGUMENT, "evidence data must be bytes")
        original_size = len(data)
        if original_size > self.max_object_bytes:
            raise ServiceError(
                ErrorCode.BUDGET_EXCEEDED,
                f"evidence object exceeds the {self.max_object_bytes} byte limit",
                details={"size_bytes": original_size, "max_object_bytes": self.max_object_bytes},
            )

        normalized_media = media_type or "application/octet-stream"
        payload = data
        redacted = False
        if _is_text(normalized_media, data):
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = None
            if text is not None:
                cleaned = redact_text(text)
                if cleaned != text:
                    payload = cleaned.encode("utf-8")
                    redacted = True

        sha256_hex = hashlib.sha256(payload).hexdigest()
        evidence_id = self.evidence_id_for_sha256(sha256_hex)
        relative = self._relpath(sha256_hex)
        target = self._safe_path(sha256_hex)

        if target.exists() and target.stat().st_size == len(payload):
            return EvidenceRecord(
                evidence_id=evidence_id,
                sha256=sha256_hex,
                size_bytes=len(payload),
                media_type=normalized_media,
                storage_path=relative,
                redacted=redacted,
                name=name,
            )

        if not target.exists() and self.total_bytes() + len(payload) > self.quota_bytes:
            raise ServiceError(
                ErrorCode.STORAGE_FULL,
                "evidence storage quota exhausted",
                details={"quota_bytes": self.quota_bytes, "used_bytes": self.total_bytes()},
            )

        try:
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".upload-", suffix=".part")
            tmp_path = Path(tmp_name)
            try:
                os.fchmod(fd, 0o644)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_path, target)
            except BaseException:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(tmp_path)
                raise
        except OSError as exc:
            raise _storage_error(exc) from exc
        # ``revision_id`` is persisted by the knowledge layer in evidence_objects;
        # the file store keeps only content so that it stays content-addressed.
        return EvidenceRecord(
            evidence_id=evidence_id,
            sha256=sha256_hex,
            size_bytes=len(payload),
            media_type=normalized_media,
            storage_path=relative,
            redacted=redacted,
            name=name,
        )

    def get(self, evidence_id: str) -> bytes:
        """Read one stored object. Invalid or missing identifiers are NOT_FOUND."""
        sha256_hex = self._validate_evidence_id(evidence_id)
        path = self._safe_path(sha256_hex)
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise ServiceError(ErrorCode.NOT_FOUND, "evidence object not found") from exc
        except OSError as exc:
            raise ServiceError(ErrorCode.INTERNAL, "evidence object could not be read") from exc

    def exists(self, evidence_id: str) -> bool:
        try:
            sha256_hex = self._validate_evidence_id(evidence_id)
            return self._safe_path(sha256_hex).is_file()
        except ServiceError:
            return False

    def delete_unreferenced(
        self,
        conn: sqlite3.Connection,
        ledger: Iterable[tuple[str, str]] | None = None,
    ) -> int:
        """Delete evidence rows and files no revision references.

        ``ledger`` carries ``(object_type, object_id)`` tombstones; evidence
        listed there is preserved even when currently unreferenced. The caller
        owns the transaction.
        """
        protected = {object_id for object_type, object_id in (ledger or ()) if object_type == "evidence"}
        rows = conn.execute(
            """
            SELECT evidence_id, storage_path
            FROM evidence_objects
            WHERE revision_id IS NULL
               OR revision_id NOT IN (SELECT revision_id FROM entry_revisions)
            """
        ).fetchall()
        deleted = 0
        for row in rows:
            evidence_id = str(row[0])
            if evidence_id in protected:
                continue
            storage_path = str(row[1])
            file_path = self.root / storage_path
            with contextlib.suppress(FileNotFoundError):
                file_path.unlink()
            conn.execute("DELETE FROM evidence_objects WHERE evidence_id = ?", (evidence_id,))
            deleted += 1
        return deleted

    def total_bytes(self) -> int:
        if not self.root.exists():
            return 0
        total = 0
        for current, _dirs, files in os.walk(self.root, followlinks=False):
            for filename in files:
                if filename.endswith(".part"):
                    continue
                try:
                    total += os.stat(Path(current) / filename).st_size
                except OSError:
                    continue
        return total


__all__ = [
    "EvidenceRecord",
    "EvidenceStore",
    "redact_text",
    "scan_for_secrets",
]
