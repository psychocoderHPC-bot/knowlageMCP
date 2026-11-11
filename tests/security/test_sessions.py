# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Revoked, closed, idle-expired, and absolutely expired tokens stop working."""

from __future__ import annotations

import json

import httpx2
import pytest

from conftest import CtlEnv, RunningGateway, auth, register, sqlite_exec

pytestmark = pytest.mark.security

PAST = "2000-01-01T00:00:00.000000Z"


def _replay(client: httpx2.Client, token: str) -> httpx2.Response:
    return client.post("/v1/tools/search", headers=auth(token), json={"query": "session probe"})


def test_session_close_replay_is_rejected(gateway_server: RunningGateway) -> None:
    with httpx2.Client(base_url=gateway_server.base_url) as client:
        token = register(client)["session_token"]
        closed = client.post("/v1/bridge/close", headers=auth(token))
        assert closed.status_code == 200, closed.text
        replay = _replay(client, token)
    assert replay.status_code == 401, replay.text
    assert replay.json()["error"]["code"] == "SESSION_EXPIRED"


def test_gatewayctl_revoke_replay_is_rejected(gateway_server: RunningGateway, ctl_env: CtlEnv) -> None:
    with httpx2.Client(base_url=gateway_server.base_url) as client:
        registration = register(client)
        result = ctl_env.run(["revoke", registration["session_id"], "--json"])
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["sessions"] >= 1
        replay = _replay(client, registration["session_token"])
    assert replay.status_code == 401, replay.text
    assert replay.json()["error"]["code"] == "SESSION_EXPIRED"


def test_idle_timeout_expires_a_session(gateway_server: RunningGateway) -> None:
    database = gateway_server.config.storage.database
    with httpx2.Client(base_url=gateway_server.base_url) as client:
        registration = register(client)
        sqlite_exec(
            database,
            "UPDATE sessions SET idle_expires_at = ? WHERE session_id = ?",
            (PAST, registration["session_id"]),
        )
        replay = _replay(client, registration["session_token"])
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "SESSION_EXPIRED"


def test_absolute_lifetime_expires_a_session(gateway_server: RunningGateway) -> None:
    database = gateway_server.config.storage.database
    with httpx2.Client(base_url=gateway_server.base_url) as client:
        registration = register(client)
        sqlite_exec(
            database,
            "UPDATE sessions SET absolute_expires_at = ? WHERE session_id = ?",
            (PAST, registration["session_id"]),
        )
        replay = _replay(client, registration["session_token"])
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "SESSION_EXPIRED"
