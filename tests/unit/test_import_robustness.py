# SPDX-FileCopyrightText: 2026 René Widerå
# SPDX-License-Identifier: ISC
"""Import robustness: duplicate revision ids inside a single import batch."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from conftest import build_env
from knowlage_mcp.models import ErrorCode, ServiceError
from knowlage_mcp.service import MAX_IMPORT_BYTES, MAX_IMPORT_RECORDS


def _import_header() -> dict:
    return {
        "type": "header",
        "schema_version": 1,
        "app_version": "0.1.0",
        "created_at": "2026-01-01T00:00:00.000000Z",
    }


def _import_entry(entry_id: str, revisions: list, *, namespace: str = "shared") -> dict:
    return {
        "type": "entry",
        "entry_id": entry_id,
        "namespace": namespace,
        "created_at": "2026-01-01T00:00:00.000000Z",
        "created_by_agent_id": "import",
        "revisions": revisions,
    }


def _import_revision(revision_id: str, claim: str, **overrides) -> dict:
    revision = {
        "revision_id": revision_id,
        "kind": "observation",
        "title": claim,
        "claim": claim,
        "observed_at": "2026-01-01T00:00:00.000000Z",
        "author_agent_id": "import",
        "author_root_id": "import",
        "created_at": "2026-01-01T00:00:00.000000Z",
        "sources": [],
    }
    revision.update(overrides)
    return revision


def test_import_counts_in_batch_duplicate_revision_ids_as_malformed(
    tmp_path: Path,
) -> None:
    """A revision id repeated within one import batch is counted as malformed.

    Only the database duplicate check used to run, so the second occurrence of
    the same id reached the INSERT and aborted the whole transaction on the
    UNIQUE constraint. In-batch duplicates now increment ``malformed`` (the
    counter for unimportable input) instead.
    """
    duplicate = _import_revision("rev_dup", "first occurrence of rev_dup")
    entry = _import_entry("ent_dup", [duplicate, dict(duplicate)])
    valid_entry = _import_entry("ent_valid", [_import_revision("rev_valid", "valid revision")])
    path = tmp_path / "dup.jsonl"
    path.write_text(
        json.dumps(_import_header()) + "\n" + json.dumps(entry) + "\n" + json.dumps(valid_entry) + "\n",
        encoding="utf-8",
    )
    env = build_env(tmp_path / "data")
    try:
        result = env.service.import_jsonl(path)

        # The duplicate was not inserted and did not abort the transaction.
        assert result["created_revisions"] == 2
        assert result["malformed"] == 1
        connection = env.db.connect()
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM entry_revisions WHERE revision_id = 'rev_dup'"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute("SELECT 1 FROM entry_revisions WHERE revision_id = 'rev_valid'").fetchone()
            is not None
        )
    finally:
        env.db.close()


def test_import_jsonl_rejects_an_oversized_file(tmp_path: Path) -> None:
    path = tmp_path / "oversized.jsonl"
    path.touch()
    os.truncate(path, MAX_IMPORT_BYTES + 1)
    env = build_env(tmp_path / "data")
    try:
        with pytest.raises(ServiceError) as excinfo:
            env.service.import_jsonl(path)
    finally:
        env.db.close()

    assert excinfo.value.code is ErrorCode.BUDGET_EXCEEDED
    assert excinfo.value.details == {"max_bytes": MAX_IMPORT_BYTES}


def test_import_jsonl_rejects_too_many_records(tmp_path: Path) -> None:
    path = tmp_path / "many.jsonl"
    lines = [json.dumps(_import_header())]
    lines.extend("{}" for _ in range(MAX_IMPORT_RECORDS))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    env = build_env(tmp_path / "data")
    try:
        with pytest.raises(ServiceError) as excinfo:
            env.service.import_jsonl(path)
    finally:
        env.db.close()

    assert excinfo.value.code is ErrorCode.BUDGET_EXCEEDED
    assert excinfo.value.details == {"max_records": MAX_IMPORT_RECORDS}


def test_integrations_import_memories_rejects_an_oversized_file(tmp_path: Path) -> None:
    path = tmp_path / "oversized-memories.jsonl"
    path.touch()
    os.truncate(path, MAX_IMPORT_BYTES + 1)
    env = build_env(tmp_path / "data")
    try:
        with pytest.raises(ServiceError) as excinfo:
            env.service.integrations_import_memories(path, dry_run=True)
    finally:
        env.db.close()

    assert excinfo.value.code is ErrorCode.BUDGET_EXCEEDED
    assert excinfo.value.details == {"max_bytes": MAX_IMPORT_BYTES}


def test_integrations_import_memories_rejects_too_many_records(tmp_path: Path) -> None:
    path = tmp_path / "many-memories.jsonl"
    record = json.dumps({"role": "user", "text": "memory"})
    path.write_text("\n".join([record] * (MAX_IMPORT_RECORDS + 1)) + "\n", encoding="utf-8")
    env = build_env(tmp_path / "data")
    try:
        with pytest.raises(ServiceError) as excinfo:
            env.service.integrations_import_memories(path, dry_run=True)
    finally:
        env.db.close()

    assert excinfo.value.code is ErrorCode.BUDGET_EXCEEDED
    assert excinfo.value.details == {"max_records": MAX_IMPORT_RECORDS}
