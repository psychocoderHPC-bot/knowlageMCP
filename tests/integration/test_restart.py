# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Restart semantics: durable knowledge, revoked sessions, requeued leases."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx2
import pytest

from conftest import RunningGateway, stop_gateway
from knowlage_mcp.admin_client import AdminClient
from knowlage_mcp.config import Config
from knowlage_mcp.db import Database

pytestmark = pytest.mark.integration


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _register(client: httpx2.Client) -> dict[str, Any]:
    response = client.post("/v1/bridge/register", json={"mode": "root", "label": "restart"})
    assert response.status_code == 200, response.text
    return response.json()


def test_restart_is_durable_and_invalidates_live_state(
    gateway_config: Config,
    gateway_factory: Callable[[Config], RunningGateway],
) -> None:
    claim = "restart durability probe 44aa"
    first = gateway_factory(gateway_config)
    with httpx2.Client(base_url=first.base_url) as client:
        registration = _register(client)
        token = registration["session_token"]
        recorded = client.post(
            "/v1/tools/record",
            headers=_headers(token),
            json={
                "namespace": "shared",
                "entry": {"kind": "observation", "title": "restart probe", "claim": claim},
            },
        )
        assert recorded.status_code == 200, recorded.text
        entry_id = recorded.json()["data"]["entry"]["entry_id"]
    stop_gateway(first)

    database = Database(gateway_config.storage.database)
    try:
        with database.transaction() as conn:
            updated = conn.execute(
                "UPDATE review_jobs SET state = 'leased', lease_owner = 'stale-session', "
                "lease_id = 'stale-lease', lease_expires_at = '2999-01-01T00:00:00Z'"
            ).rowcount
        assert updated >= 1
    finally:
        database.close()

    second = gateway_factory(gateway_config)
    try:
        with httpx2.Client(base_url=second.base_url) as client:
            stale = client.post("/v1/tools/search", headers=_headers(token), json={"query": "restart"})
            assert stale.status_code == 401
            assert stale.json()["error"]["code"] == "SESSION_EXPIRED"

            client.cookies.clear()
            replacement = _register(client)
            fresh = _headers(replacement["session_token"])
            found = client.post("/v1/tools/search", headers=fresh, json={"query": "restart durability probe"})
            assert found.status_code == 200
            assert entry_id in [item["entry_id"] for item in found.json()["data"]["results"]]

            status = AdminClient(second.admin_socket).call("status")
            assert status["sessions_active"] == 1
    finally:
        stop_gateway(second)

    database = Database(gateway_config.storage.database)
    try:
        row = database.connect().execute("SELECT state, lease_owner, lease_id FROM review_jobs").fetchone()
        assert row is not None
        assert row["state"] == "queued"
        assert row["lease_owner"] is None
        assert row["lease_id"] is None
    finally:
        database.close()
