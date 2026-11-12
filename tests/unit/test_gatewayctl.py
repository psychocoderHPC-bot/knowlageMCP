# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Failure rendering and exit codes of the operator CLI."""

from __future__ import annotations

import json
from typing import Any

import pytest

from knowlage_mcp import gatewayctl
from knowlage_mcp.admin_client import AdminError, AdminUnavailable
from knowlage_mcp.config import default_config
from knowlage_mcp.models import ErrorCode, ServiceError


@pytest.fixture
def failing_call(monkeypatch: pytest.MonkeyPatch):
    def install(error: Exception) -> None:
        monkeypatch.setattr(gatewayctl, "load_config", lambda path: default_config())

        def call(self: Any, command: str, arguments: dict[str, Any] | None = None) -> Any:
            del self, command, arguments
            raise error

        monkeypatch.setattr(gatewayctl.AdminClient, "call", call)

    return install


@pytest.mark.parametrize("as_json", [False, True])
def test_status_admin_error_renders_per_mode(
    failing_call: Any,
    capsys: pytest.CaptureFixture[str],
    as_json: bool,
) -> None:
    failing_call(AdminError("CONFLICT", "state changed"))
    argv = ["status", "--json"] if as_json else ["status"]
    assert gatewayctl.main(argv) == 4
    captured = capsys.readouterr()
    if as_json:
        assert captured.err == ""
        assert json.loads(captured.out) == {
            "ok": False,
            "error": {"code": "CONFLICT", "message": "state changed", "retry_after_s": None},
        }
    else:
        assert captured.out == ""
        assert "CONFLICT: state changed" in captured.err


@pytest.mark.parametrize(
    ("error", "code", "exit_code"),
    [
        (AdminUnavailable("admin socket is unavailable"), "BACKEND_UNAVAILABLE", 3),
        (ServiceError(ErrorCode.INVALID_ARGUMENT, "bad value"), "INVALID_ARGUMENT", 4),
        (ServiceError(ErrorCode.BACKEND_UNAVAILABLE, "backend down"), "BACKEND_UNAVAILABLE", 3),
        (ServiceError(ErrorCode.INTERNAL, "boom"), "INTERNAL", 1),
    ],
)
def test_json_failure_codes_and_exit_status(
    failing_call: Any,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
    code: str,
    exit_code: int,
) -> None:
    failing_call(error)
    assert gatewayctl.main(["status", "--json"]) == exit_code
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == code
