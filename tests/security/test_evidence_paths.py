# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Evidence ids and on-disk symlinks cannot escape the evidence root."""

from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import quote

import httpx2
import pytest

from conftest import RunningGateway, auth, register

pytestmark = pytest.mark.security

SENTINEL = b"EXTERNAL-SYMLINK-SECRET\n"


def test_traversal_and_absolute_evidence_ids_are_rejected(
    gateway_server: RunningGateway, tmp_path: Path
) -> None:
    sentinel = tmp_path / "outside-secret.txt"
    sentinel.write_bytes(SENTINEL)
    with httpx2.Client(base_url=gateway_server.base_url) as client:
        token = register(client)["session_token"]
        headers = auth(token)
        uploaded = client.post(
            "/v1/bridge/evidence",
            headers={**headers, "Content-Type": "text/plain"},
            content=b"ordinary evidence",
        )
        assert uploaded.status_code == 200, uploaded.text
        for bad in (
            "../../etc/passwd",
            "/etc/passwd",
            "ev_../../etc/passwd",
            "../" + str(sentinel),
            str(sentinel),
            "ev_" + "0" * 64,
        ):
            response = client.get(f"/v1/bridge/evidence/{quote(bad, safe='')}", headers=headers)
            assert response.status_code in {400, 404}, (bad, response.text)
            assert b"root:x:" not in response.content
            assert SENTINEL not in response.content


def test_symlink_inside_evidence_root_is_rejected(gateway_server: RunningGateway, tmp_path: Path) -> None:
    external = tmp_path / "external-target.txt"
    external.write_bytes(SENTINEL)
    payload = b"planted payload for symlink test"
    sha = hashlib.sha256(payload).hexdigest()
    evidence_id = f"ev_{sha}"
    root = Path(gateway_server.config.storage.evidence_dir)
    target = root / sha[:2] / sha[2:4] / sha
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(external)

    with httpx2.Client(base_url=gateway_server.base_url) as client:
        token = register(client)["session_token"]
        headers = auth(token)
        download = client.get(f"/v1/bridge/evidence/{evidence_id}", headers=headers)
        assert download.status_code == 404, download.text
        assert SENTINEL not in download.content

        upload = client.post(
            "/v1/bridge/evidence",
            headers={**headers, "Content-Type": "text/plain"},
            content=payload,
        )
        assert upload.status_code == 404, upload.text

    assert external.read_bytes() == SENTINEL
    assert target.is_symlink()
