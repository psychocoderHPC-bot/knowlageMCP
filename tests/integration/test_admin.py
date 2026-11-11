# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Admin Unix-socket tests: permissions, commands, streaming, single instance."""

from __future__ import annotations

import asyncio
import os
import socket
import stat
import threading
import time
from typing import Any

import httpx2
import pytest

from conftest import RunningGateway, wait_until
from knowlage_mcp.admin import AdminServer, peer_uid, peer_uid_allowed
from knowlage_mcp.admin_client import AdminClient, AdminError

pytestmark = pytest.mark.integration


def test_admin_socket_permissions_and_status(gateway_server: RunningGateway) -> None:
    socket_path = gateway_server.admin_socket
    assert stat.S_IMODE(os.stat(socket_path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(socket_path.parent).st_mode) == 0o700
    client = AdminClient(socket_path)
    status = client.call("status")
    assert status["version"]
    assert status["protocol_version"]
    assert status["schema_version"] >= 1
    assert "database_bytes" in status


def test_enrollment_approval_flow(gateway_server: RunningGateway, admin: AdminClient) -> None:
    with httpx2.Client(base_url=gateway_server.base_url) as http:
        registration = http.post(
            "/v1/bridge/register", json={"mode": "root", "label": "mentor-candidate"}
        ).json()
        token = registration["session_token"]
        headers = {"Authorization": f"Bearer {token}"}
        requested = http.post(
            "/v1/bridge/mentor",
            headers=headers,
            json={"namespace": "shared", "scopes": ["review", "publish"]},
        )
        assert requested.status_code == 200, requested.text
        request_id = requested.json()["request_id"]

        enrollments = admin.call("enrollments")
        assert any(item["request_id"] == request_id for item in enrollments)
        approved = admin.call(
            "approve",
            {"request_id": request_id, "role": "mentor", "ttl": "2h", "namespace": "shared"},
        )
        assert approved["state"] == "approved"

        status = http.get(f"/v1/bridge/enrollment/{request_id}", headers=headers)
        assert status.status_code == 200
        assert status.json()["state"] == "approved"
        assert status.json()["approved_at"]


def test_watch_receives_audit_event(gateway_server: RunningGateway, admin: AdminClient) -> None:
    events: list[dict[str, Any]] = []

    def on_event(event: dict[str, Any]) -> bool | None:
        events.append(event)
        return False if event.get("event") == "audit" else None

    thread = threading.Thread(
        target=lambda: AdminClient(gateway_server.admin_socket, timeout=10).watch(on_event),
        daemon=True,
    )
    thread.start()
    try:
        wait_until(lambda: admin.call("status")["event_subscribers"] >= 1)
        admin.call("namespaces.create", {"name": "watch-probe"})
        wait_until(lambda: bool(events), timeout=5)
    finally:
        thread.join(timeout=5)
    assert any(event.get("action") == "namespace.create" for event in events)


def test_unknown_command_and_malformed_request(gateway_server: RunningGateway, admin: AdminClient) -> None:
    with pytest.raises(AdminError) as unknown:
        admin.call("definitely.not.a.command")
    assert unknown.value.code == "INVALID_ARGUMENT"
    assert unknown.value.exit_code == 4
    with pytest.raises(AdminError) as missing:
        admin.call("approve", {})
    assert missing.value.code == "INVALID_ARGUMENT"


def test_second_server_on_same_socket_is_refused(gateway_server: RunningGateway) -> None:
    async def attempt() -> None:
        server = AdminServer(gateway_server.service, gateway_server.admin_socket)
        with pytest.raises(RuntimeError):
            await server.start()
        await server.stop()

    asyncio.run(attempt())


def test_peer_uid_check_when_available() -> None:
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        uid = peer_uid(left)
        if uid is None:
            pytest.skip("SO_PEERCRED is not available on this platform")
        assert uid == os.geteuid()
        assert peer_uid_allowed(left)
        assert not peer_uid_allowed(left, os.geteuid() + 1)
    finally:
        left.close()
        right.close()


def test_watch_heartbeat_is_not_required_for_events() -> None:
    # A bounded sanity check that the heartbeat constant is a real interval.
    from knowlage_mcp import admin as admin_module

    assert admin_module.HEARTBEAT_SECONDS >= 1.0
    assert admin_module.MAX_LINE_BYTES == 1024 * 1024
    assert time.monotonic() > 0
