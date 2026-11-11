# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Synchronous Unix-socket client used by ``knowlageMCP-gatewayctl``."""

from __future__ import annotations

import json
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO

#: Admin error codes that mean a state conflict rather than a plain failure.
CONFLICT_CODES = frozenset({"CONFLICT", "INVALID_ARGUMENT", "REVISION_CONFLICT"})


class AdminUnavailable(RuntimeError):
    """The gateway or its admin socket is not reachable (exit code 3)."""


class AdminError(RuntimeError):
    """The admin server returned a structured failure."""

    def __init__(
        self,
        code: str,
        message: str,
        retry_after_s: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retry_after_s = retry_after_s
        self.details = details

    @property
    def exit_code(self) -> int:
        return 4 if self.code in CONFLICT_CODES else 1


class AdminClient:
    """One-shot JSON-lines client for the local admin socket."""

    def __init__(self, socket_path: str | Path, timeout: float = 10.0) -> None:
        self.socket_path = Path(socket_path)
        self.timeout = timeout
        self._next_id = 1

    def _next_request_id(self) -> int:
        value = self._next_id
        self._next_id += 1
        return value

    def _connect(self) -> socket.socket:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(self.timeout)
        try:
            client.connect(str(self.socket_path))
        except OSError as exc:
            client.close()
            raise AdminUnavailable(f"admin socket is unavailable: {self.socket_path}") from exc
        return client

    @staticmethod
    def _read_message(stream: BinaryIO) -> dict[str, Any]:
        raw = stream.readline()
        if not raw:
            raise AdminUnavailable("admin server closed the connection")
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AdminError("INTERNAL", "admin server returned malformed JSON") from exc
        if not isinstance(parsed, dict):
            raise AdminError("INTERNAL", "admin server returned a non-object response")
        return parsed

    def call(self, cmd: str, args: dict[str, Any] | None = None) -> Any:
        """Send one command and return its ``data`` value."""
        request_id = self._next_request_id()
        payload = {"id": request_id, "cmd": cmd, "args": args or {}}
        client = self._connect()
        try:
            client.sendall(json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n")
            with client.makefile("rb") as stream:
                response = self._read_message(stream)
        except OSError as exc:
            raise AdminUnavailable("admin socket connection failed") from exc
        finally:
            client.close()
        if response.get("id") != request_id:
            raise AdminError("INTERNAL", "admin response id mismatch")
        if response.get("ok"):
            return response.get("data")
        error = response.get("error") or {}
        raise AdminError(
            str(error.get("code", "INTERNAL")),
            str(error.get("message", "admin command failed")),
            retry_after_s=error.get("retry_after_s"),
            details=error.get("details"),
        )

    def watch(self, callback: Callable[[dict[str, Any]], bool | None]) -> None:
        """Stream events until the callback returns ``False`` or the server closes."""
        request = {"id": self._next_request_id(), "cmd": "watch", "args": {}}
        client = self._connect()
        try:
            # A watch stream is long-lived: do not inherit the short RPC timeout.
            # The server sends a heartbeat every 20 seconds when events are idle.
            client.settimeout(None)
            client.sendall(json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n")
            with client.makefile("rb") as stream:
                while True:
                    event = self._read_message(stream)
                    if callback(event) is False:
                        return
        except OSError as exc:
            raise AdminUnavailable("admin watch stream failed") from exc
        finally:
            client.close()


__all__ = ["CONFLICT_CODES", "AdminClient", "AdminError", "AdminUnavailable"]
