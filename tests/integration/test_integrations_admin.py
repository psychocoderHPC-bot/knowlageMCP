# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Admin and CLI integration commands over the real Unix-socket gateway."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conftest import RunningGateway
from knowlage_mcp import gatewayctl
from knowlage_mcp.admin_client import AdminClient
from knowlage_mcp.config import write_config_atomic
from knowlage_mcp.integrations.base import MemoryProposal

pytestmark = pytest.mark.integration


class StubExtractor:
    name = "stub"
    available = True

    def extract(
        self,
        text: str,
        task: str | None = None,
        sources: Any = (),
    ) -> list[MemoryProposal]:
        del sources
        return [
            MemoryProposal(
                kind="observation",
                title=task or "stub proposal",
                claim=f"proposal: {text}",
                tags=("stub",),
                source_role="unknown",
            )
        ]

    def health(self) -> dict[str, Any]:
        return {"name": self.name, "available": True}


def test_gatewayctl_integrations_status_probe_reindex(
    gateway_server: RunningGateway,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "config.toml"
    write_config_atomic(config_path, gateway_server.config)

    assert gatewayctl.main(["--config", str(config_path), "integrations", "status", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["enabled"] is False
    assert status["generations"] == []

    assert gatewayctl.main(["--config", str(config_path), "integrations", "probe", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {}

    assert gatewayctl.main(["--config", str(config_path), "integrations", "reindex", "--json"]) == 0
    reindexed = json.loads(capsys.readouterr().out)
    assert reindexed["generation"] is None
    assert reindexed["backend"] == "none"
    assert "indexed" in reindexed

    # Human-readable status must render the same data without failing.
    assert gatewayctl.main(["--config", str(config_path), "integrations", "status"]) == 0
    assert "enabled" in capsys.readouterr().out


def test_extract_stores_proposals_and_never_publishes(
    gateway_server: RunningGateway,
    admin: AdminClient,
) -> None:
    gateway_server.service.integrations.extractor = StubExtractor()
    added = admin.call(
        "memory.add",
        {
            "kind": "observation",
            "namespace": "shared",
            "text": "extract source text",
            "title": "extract source",
        },
    )
    entry_id = added["entry"]["entry_id"]
    before = admin.call("status")["entries"]

    result = admin.call("integrations.extract", {"entry_id": entry_id})
    assert result["available"] is True
    assert len(result["proposals"]) == 1
    assert result["proposals"][0]["claim"] == "proposal: extract source text"

    conn = gateway_server.service.db.connect()
    rows = conn.execute("SELECT state FROM extraction_proposals WHERE entry_id = ?", (entry_id,)).fetchall()
    assert [row["state"] for row in rows] == ["proposed"]
    assert admin.call("status")["entries"] == before
    lifecycle = conn.execute(
        "SELECT lifecycle FROM entry_revisions WHERE entry_id = ?", (entry_id,)
    ).fetchone()
    assert lifecycle["lifecycle"] == "published"
    audit = conn.execute(
        "SELECT COUNT(*) AS n FROM audit_events WHERE action = 'integrations.extract'"
    ).fetchone()
    assert audit["n"] == 1


def test_import_memories_dry_run_then_candidates_with_source_role(
    gateway_server: RunningGateway,
    admin: AdminClient,
    tmp_path: Path,
) -> None:
    gateway_server.service.integrations.extractor = StubExtractor()
    source = admin.call(
        "memory.add",
        {
            "kind": "observation",
            "namespace": "shared",
            "text": "source entry for provenance",
            "title": "provenance source",
        },
    )
    source_entry_id = source["entry"]["entry_id"]
    source_revision_id = source["entry"]["revision_id"]

    path = tmp_path / "memories.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"role": "user", "text": "first memory", "source_role": "user", "task": "first"}),
                json.dumps(
                    {
                        "role": "assistant",
                        "text": "second memory",
                        "source_role": "assistant",
                        "task": "second",
                        "source_entry_id": source_entry_id,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    dry = admin.call("integrations.import.memories", {"path": str(path), "dry_run": True})
    assert dry["dry_run"] is True
    assert dry["proposals"] == 2
    assert dry["created"] == 0
    conn = gateway_server.service.db.connect()
    assert (
        conn.execute(
            "SELECT COUNT(*) AS n FROM entries WHERE created_by_agent_id LIKE 'extractor:%'"
        ).fetchone()["n"]
        == 0
    )

    real = admin.call("integrations.import.memories", {"path": str(path), "dry_run": False})
    assert real["created"] == 2
    assert real["skipped"] == 0

    rows = conn.execute(
        "SELECT r.claim, r.source_role, r.tags, r.lifecycle, r.revision_id "
        "FROM entry_revisions r JOIN entries e ON e.entry_id = r.entry_id "
        "WHERE e.created_by_agent_id LIKE 'extractor:%' ORDER BY r.claim"
    ).fetchall()
    by_claim = {str(row["claim"]): row for row in rows}
    assert by_claim["proposal: first memory"]["source_role"] == "user"
    assert by_claim["proposal: second memory"]["source_role"] == "assistant"
    for row in rows:
        assert row["lifecycle"] == "candidate"
        assert "mem0" in json.loads(str(row["tags"]))

    second_revision = str(by_claim["proposal: second memory"]["revision_id"])
    edge = conn.execute(
        "SELECT 1 FROM provenance_edges WHERE from_revision_id = ? AND to_revision_id = ? "
        "AND relation = 'derived_from'",
        (second_revision, source_revision_id),
    ).fetchone()
    assert edge is not None

    again = admin.call("integrations.import.memories", {"path": str(path), "dry_run": False})
    assert again["created"] == 0
    assert again["skipped"] == 2
    audit = conn.execute(
        "SELECT COUNT(*) AS n FROM audit_events WHERE action = 'integrations.import.memories'"
    ).fetchone()
    assert audit["n"] == 2


def test_proposals_lists_advisory_rows_read_only(
    gateway_server: RunningGateway,
    admin: AdminClient,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    gateway_server.service.integrations.extractor = StubExtractor()
    added = admin.call(
        "memory.add",
        {
            "kind": "observation",
            "namespace": "shared",
            "text": "proposal source text",
            "title": "proposal source",
        },
    )
    entry = added["entry"]
    admin.call("integrations.extract", {"entry_id": entry["entry_id"]})

    conn = gateway_server.service.db.connect()
    audit_before = conn.execute("SELECT COUNT(*) AS n FROM audit_events").fetchone()["n"]

    listed = admin.call("integrations.proposals", {"entry_id": entry["entry_id"]})
    assert listed["entry_id"] == entry["entry_id"]
    assert listed["state"] == "proposed"
    assert len(listed["proposals"]) == 1
    first = listed["proposals"][0]
    assert first["state"] == "proposed"
    assert first["entry_id"] == entry["entry_id"]

    empty = admin.call(
        "integrations.proposals",
        {"entry_id": entry["entry_id"], "state": "accepted"},
    )
    assert empty["proposals"] == []

    with gateway_server.service.db.transaction() as tx:
        tx.executemany(
            "INSERT INTO extraction_proposals "
            "(proposal_id, entry_id, revision_id, extractor, relation, subject, object, "
            "detail, state, created_at) VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?)",
            [
                (
                    f"prp-extra-{index:02d}",
                    entry["entry_id"],
                    entry["revision_id"],
                    "stub",
                    "proposed",
                    f"2026-01-01T00:00:{index:02d}+00:00",
                )
                for index in range(55)
            ],
        )
    capped = admin.call("integrations.proposals", {"entry_id": entry["entry_id"]})
    assert len(capped["proposals"]) == 50
    timestamps = [proposal["created_at"] for proposal in capped["proposals"]]
    assert timestamps == sorted(timestamps, reverse=True)

    audit_after = conn.execute("SELECT COUNT(*) AS n FROM audit_events").fetchone()["n"]
    assert audit_after == audit_before

    config_path = tmp_path / "config.toml"
    write_config_atomic(config_path, gateway_server.config)
    assert (
        gatewayctl.main(
            [
                "--config",
                str(config_path),
                "integrations",
                "proposals",
                "--entry",
                entry["entry_id"],
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "proposed"
    assert len(payload["proposals"]) == 50

    assert (
        gatewayctl.main(
            [
                "--config",
                str(config_path),
                "integrations",
                "proposals",
                "--entry",
                entry["entry_id"],
            ]
        )
        == 0
    )
    human = capsys.readouterr().out
    assert capped["proposals"][0]["proposal_id"] in human
    assert "proposals" in human
    assert "created_at" in human
