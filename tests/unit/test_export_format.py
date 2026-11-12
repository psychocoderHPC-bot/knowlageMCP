# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Golden version 1 JSONL export fixture and idempotent re-import."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from conftest import ServiceEnv

ENTRY_ID = "ent_fixture0000000000000001"
REVISION_ID = "rev_fixture0000000000000001"
SOURCE_ID = "src_fixture0000000000000001"


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_golden_fixture_import_export_and_reimport(env: ServiceEnv, repo_root: Path, tmp_path: Path) -> None:
    fixture = repo_root / "tests" / "fixtures" / "export_v1.jsonl"
    connection = env.db.connect()

    dry_run = env.service.import_jsonl(fixture, dry_run=True)
    assert dry_run["created_revisions"] == 1
    assert connection.execute("SELECT 1 FROM entries WHERE entry_id = ?", (ENTRY_ID,)).fetchone() is None

    imported = env.service.import_jsonl(fixture)
    assert imported["created_entries"] == 1
    assert imported["created_revisions"] == 1
    entry = connection.execute("SELECT * FROM entries WHERE entry_id = ?", (ENTRY_ID,)).fetchone()
    revision = connection.execute(
        "SELECT * FROM entry_revisions WHERE revision_id = ?", (REVISION_ID,)
    ).fetchone()
    source = connection.execute("SELECT * FROM sources WHERE source_id = ?", (SOURCE_ID,)).fetchone()
    assert entry is not None and entry["current_revision_id"] == REVISION_ID
    assert revision is not None
    assert revision["lifecycle"] == "candidate"
    assert revision["evidence_class"] == "unverified"
    assert source is not None
    assert source["revision_id"] == REVISION_ID
    assert source["identity"] == "https://example.invalid/fictional/orbit-lib"

    output = tmp_path / "exported.jsonl"
    exported = env.service.export_jsonl(None, output)
    assert exported["entries"] == 1
    records = _jsonl(output)
    header = next(record for record in records if record.get("type") == "header")
    assert header["schema_version"] == 1
    entry_record = next(record for record in records if record.get("type") == "entry")
    assert entry_record["entry_id"] == ENTRY_ID
    assert entry_record["current_revision_id"] == REVISION_ID
    revision_record = entry_record["revisions"][0]
    assert revision_record["revision_id"] == REVISION_ID
    assert revision_record["lifecycle"] == "candidate"
    assert revision_record["evidence_class"] == "unverified"
    assert revision_record["sources"][0]["source_id"] == SOURCE_ID
    assert revision_record["sources"][0]["identity"] == source["identity"]

    reimported = env.service.import_jsonl(output)
    assert reimported["created_entries"] == 0
    assert reimported["created_revisions"] == 0
    assert reimported["skipped"] >= 1
    for table, key, object_id in (
        ("entries", "entry_id", ENTRY_ID),
        ("entry_revisions", "revision_id", REVISION_ID),
        ("sources", "source_id", SOURCE_ID),
    ):
        count = connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {key} = ?", (object_id,)).fetchone()[
            0
        ]
        assert count == 1
