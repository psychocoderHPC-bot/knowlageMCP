# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Unix-domain admin server speaking one JSON object per line.

The socket lives in a private 0700 directory, is created 0600, and is never
bound to TCP. On Linux the peer uid is compared with the gateway uid via
``SO_PEERCRED``; a mismatch closes the connection without dispatching anything.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import queue
import socket
import stat
import struct
import time
from pathlib import Path
from typing import Any

from .config import ensure_private_dir
from .db import iso, utcnow
from .models import ErrorCode, ServiceError
from .service import KnowledgeService

logger = logging.getLogger(__name__)

#: Maximum accepted request/response line length (1 MiB).
MAX_LINE_BYTES = 1024 * 1024
#: Heartbeat cadence on a quiet ``watch`` stream.
HEARTBEAT_SECONDS = 20.0


def peer_uid(sock: Any) -> int | None:
    """Return the peer uid on Linux, or ``None`` when the platform lacks it."""
    option = getattr(socket, "SO_PEERCRED", None)
    if option is None:
        return None
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, option, struct.calcsize("3i"))
    except OSError:
        return None
    try:
        _pid, uid, _gid = struct.unpack("3i", raw)
    except struct.error:
        return None
    return int(uid)


def peer_uid_allowed(sock: Any, expected: int | None = None) -> bool:
    """True when peer credentials are unavailable or match the expected uid."""
    uid = peer_uid(sock)
    if uid is None:
        return True
    return uid == (os.geteuid() if expected is None else expected)


def _socket_active(path: Path) -> bool:
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.2)
    try:
        probe.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        probe.close()


class AdminServer:
    """Asyncio-native admin socket server with a single active instance."""

    def __init__(
        self,
        service: KnowledgeService,
        socket_path: str | Path,
        *,
        max_line_bytes: int = MAX_LINE_BYTES,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
    ) -> None:
        self.service = service
        self.socket_path = Path(socket_path)
        self.max_line_bytes = max_line_bytes
        self.heartbeat_seconds = heartbeat_seconds
        self._server: asyncio.AbstractServer | None = None

    # -- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        path = self.socket_path
        parent = path.parent
        if parent.is_symlink():
            raise RuntimeError("admin socket directory must not be a symlink")
        ensure_private_dir(parent)
        with contextlib.suppress(OSError):
            parent.chmod(0o700)
        if path.is_symlink():
            raise RuntimeError("admin socket path must not be a symlink")
        if path.exists():
            if not stat.S_ISSOCK(path.stat().st_mode):
                raise RuntimeError("admin socket path exists and is not a socket")
            if _socket_active(path):
                raise RuntimeError("another admin server is already active on this socket")
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=str(path),
            limit=self.max_line_bytes + 1,
        )
        os.chmod(path, 0o600)

    async def stop(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                # A long-lived watch client must not block process shutdown.
                await asyncio.wait_for(server.wait_closed(), timeout=2.0)
        with contextlib.suppress(FileNotFoundError):
            if stat.S_ISSOCK(self.socket_path.stat().st_mode):
                self.socket_path.unlink()

    @property
    def active(self) -> bool:
        return self._server is not None

    # -- connection handling --------------------------------------------
    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        sock = writer.get_extra_info("socket")
        if sock is not None and not peer_uid_allowed(sock):
            await self._send(
                writer,
                {"id": None, "ok": False, "error": {"code": "FORBIDDEN", "message": "peer uid mismatch"}},
            )
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return
        try:
            while True:
                try:
                    line = await reader.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    await self._send(
                        writer,
                        {
                            "id": None,
                            "ok": False,
                            "error": {
                                "code": ErrorCode.INVALID_ARGUMENT.value,
                                "message": f"request line exceeds {self.max_line_bytes} bytes",
                            },
                        },
                    )
                    return
                if not line:
                    return
                if not line.strip():
                    continue
                keep_open = await self._handle_line(line, reader, writer)
                if not keep_open:
                    return
        except (ConnectionError, BrokenPipeError, asyncio.IncompleteReadError):
            return
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _handle_line(
        self,
        line: bytes,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> bool:
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError:
            await self._send(
                writer,
                {
                    "id": None,
                    "ok": False,
                    "error": {"code": ErrorCode.INVALID_ARGUMENT.value, "message": "invalid UTF-8"},
                },
            )
            return True
        try:
            request = json.loads(text)
        except json.JSONDecodeError:
            await self._send(
                writer,
                {
                    "id": None,
                    "ok": False,
                    "error": {"code": ErrorCode.INVALID_ARGUMENT.value, "message": "invalid JSON"},
                },
            )
            return True
        if not isinstance(request, dict):
            await self._send(
                writer,
                {
                    "id": None,
                    "ok": False,
                    "error": {
                        "code": ErrorCode.INVALID_ARGUMENT.value,
                        "message": "request must be an object",
                    },
                },
            )
            return True
        request_id = request.get("id", 0)
        command = request.get("cmd")
        arguments = request.get("args") or {}
        if not isinstance(command, str) or not command.strip():
            await self._send(
                writer,
                {
                    "id": request_id,
                    "ok": False,
                    "error": {
                        "code": ErrorCode.INVALID_ARGUMENT.value,
                        "message": "cmd is required",
                    },
                },
            )
            return True
        if not isinstance(arguments, dict):
            await self._send(
                writer,
                {
                    "id": request_id,
                    "ok": False,
                    "error": {
                        "code": ErrorCode.INVALID_ARGUMENT.value,
                        "message": "args must be an object",
                    },
                },
            )
            return True
        if command.strip().lower() in {"watch", "watch.events"}:
            await self._watch(reader, writer)
            return False
        try:
            data = await asyncio.to_thread(self.service.admin_dispatch, command, arguments)
        except ServiceError as exc:
            await self._send(writer, {"id": request_id, "ok": False, "error": exc.to_envelope()["error"]})
            return True
        except Exception:
            logger.exception("admin command failed: %s", command)
            await self._send(
                writer,
                {
                    "id": request_id,
                    "ok": False,
                    "error": {"code": ErrorCode.INTERNAL.value, "message": "internal admin error"},
                },
            )
            return True
        await self._send(writer, {"id": request_id, "ok": True, "data": data})
        return True

    async def _watch(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        subscriber = self.service.subscribe()
        disconnect = asyncio.create_task(reader.read())
        last_heartbeat = time.monotonic()
        try:
            while True:
                if disconnect.done():
                    return
                try:
                    event = subscriber.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.05)
                    if time.monotonic() - last_heartbeat >= self.heartbeat_seconds:
                        last_heartbeat = time.monotonic()
                        if not await self._send(writer, {"event": "heartbeat", "ts": iso(utcnow())}):
                            return
                    continue
                if not await self._send(writer, event):
                    return
        finally:
            self.service.unsubscribe(subscriber)
            if not disconnect.done():
                disconnect.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await disconnect

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, payload: dict[str, Any]) -> bool:
        try:
            encoded = json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))
            writer.write(encoded.encode("utf-8") + b"\n")
            await writer.drain()
            return True
        except (ConnectionError, BrokenPipeError, RuntimeError, OSError):
            return False


__all__ = [
    "HEARTBEAT_SECONDS",
    "MAX_LINE_BYTES",
    "AdminServer",
    "peer_uid",
    "peer_uid_allowed",
]
