# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""ASGI gateway: HTTP bridge API, MCP mount, lifespan, and server runner.

Routes defined here are registered before the ``/mcp`` mount so ``/v1/health``
and the bridge API are never swallowed by the MCP Starlette application. Every
response is JSON except evidence upload/download, which move raw bytes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import signal
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import uvicorn
from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import ValidationError
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import PROTOCOL_VERSION, __version__
from .admin import AdminServer
from .backup import prune_backups
from .config import Config, load_config, validate_config
from .db import Database, DatabaseLockedError, lock_file
from .evidence import EvidenceStore
from .mcp_server import (
    ALL_TOOL_NAMES,
    TOOL_ARG_MODELS,
    build_mcp_server,
    call_tool,
    failure,
    sanitize_evidence_name,
)
from .models import ErrorCode, MentorRequest, ServiceError, SessionInfo
from .policy import readable_namespaces
from .protocol import (
    HEADER_ACTOR_HANDLE,
    HEADER_EVIDENCE_NAME,
    HEADER_PROTOCOL,
    ROUTE_CLOSE,
    ROUTE_ENROLLMENT,
    ROUTE_EVIDENCE,
    ROUTE_EVIDENCE_ITEM,
    ROUTE_HEALTH,
    ROUTE_MENTOR,
    ROUTE_REGISTER,
    ROUTE_TOOLS,
    CloseResponse,
    EnrollmentResponse,
    EvidenceUploadResponse,
    MentorHttpRequest,
    RegisterRequest,
    parse_bearer,
    version_compatible,
)
from .service import KnowledgeService

logger = logging.getLogger(__name__)

#: Upper bound for the daily backup sleep loop.
BACKUP_INTERVAL_SECONDS = 24 * 60 * 60

_JSON_HEADERS = [(b"content-type", b"application/json; charset=utf-8")]

_HTTP_STATUS: dict[str, int] = {
    ErrorCode.AUTH_REQUIRED.value: 401,
    ErrorCode.SESSION_EXPIRED.value: 401,
    ErrorCode.APPROVAL_PENDING.value: 403,
    ErrorCode.FORBIDDEN.value: 403,
    ErrorCode.INVALID_ARGUMENT.value: 400,
    ErrorCode.REVISION_CONFLICT.value: 409,
    ErrorCode.RATE_LIMITED.value: 429,
    ErrorCode.BUDGET_EXCEEDED.value: 413,
    ErrorCode.STORAGE_FULL.value: 507,
    ErrorCode.REVIEW_INELIGIBLE.value: 403,
    ErrorCode.BACKEND_UNAVAILABLE.value: 503,
    ErrorCode.NOT_FOUND.value: 404,
    ErrorCode.CONFLICT.value: 409,
    ErrorCode.INTERNAL.value: 500,
}

#: Actions that must never be exposed over the TCP listener.
FORBIDDEN_PATHS = ("/admin", "/sql", "/v1/admin")


def http_status_for(code: str) -> int:
    return _HTTP_STATUS.get(code, 500)


# -- low-level response helpers ------------------------------------------
async def _send_json(
    send: Send,
    status: int,
    payload: dict[str, Any],
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = list(_JSON_HEADERS)
    if extra_headers:
        headers.extend(extra_headers)
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def _envelope_error(
    code: ErrorCode | str,
    message: str,
    retry_after_s: int | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return failure(code, message, retry_after_s=retry_after_s, details=details)


def _error_response(exc: ServiceError) -> JSONResponse:
    envelope = exc.to_envelope()
    headers: dict[str, str] = {}
    if exc.code is ErrorCode.RATE_LIMITED and exc.retry_after_s is not None:
        headers["Retry-After"] = str(max(1, int(exc.retry_after_s)))
    if exc.code in {ErrorCode.AUTH_REQUIRED, ErrorCode.SESSION_EXPIRED}:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(envelope, status_code=http_status_for(exc.code.value), headers=headers)


def _validation_message(context: str, exc: ValidationError) -> str:
    parts: list[str] = []
    for item in exc.errors(include_url=False, include_input=False):
        location = ".".join(str(part) for part in item.get("loc", ())) or "(body)"
        parts.append(f"{location}: {item.get('msg', 'invalid value')}")
    return f"invalid {context}: {'; '.join(parts)}"[:500]  # no values echoed


# -- host and origin allowlists ------------------------------------------
def _base_host(host_header: str) -> str:
    value = host_header.strip()
    if value.startswith("["):
        end = value.find("]")
        return value[: end + 1] if end != -1 else value
    if value.count(":") == 1:
        name, _, port = value.rpartition(":")
        if port.isdigit():
            return name
    return value


def host_allowed(host_header: str | None, allowed: list[str] | tuple[str, ...]) -> bool:
    """Accept exact hosts and ``host:port`` forms against the allowlist."""
    if not host_header:
        return False
    host = host_header.strip().lower()
    base = _base_host(host)
    for candidate in allowed:
        entry = candidate.strip().lower()
        if not entry:
            continue
        if host == entry or base == entry:
            return True
        if entry.endswith(":*"):
            prefix = entry[:-2]
            if host == prefix or host.startswith(prefix + ":"):
                return True
        if entry == "*":
            return True
    return False


def origin_allowed(origin: str | None, allowed: list[str] | tuple[str, ...]) -> bool:
    """An absent Origin is same-origin; anything else must be explicit."""
    if not origin:
        return True
    value = origin.strip()
    for candidate in allowed:
        entry = candidate.strip()
        if not entry:
            continue
        if value == entry:
            return True
        if entry.endswith(":*") and value.startswith(entry[:-2] + ":"):
            return True
        if entry == "*":
            return True
    return False


# -- middleware -----------------------------------------------------------
class RequestContextMiddleware:
    """Assign a request id, log one structured line, and never leak traces."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid.uuid4().hex[:16]
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        started = time.perf_counter()
        status = 500
        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status = int(message["status"])
                headers = list(message.get("headers", []))
                if not any(key.lower() == b"x-request-id" for key, _ in headers):
                    headers.append((b"x-request-id", request_id.encode("ascii")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            logger.exception(
                "unhandled request failure request_id=%s method=%s path=%s",
                request_id,
                scope.get("method"),
                scope.get("path"),
            )
            if not response_started:
                with contextlib.suppress(Exception):
                    await _send_json(
                        send,
                        500,
                        _envelope_error(ErrorCode.INTERNAL, "internal server error"),
                        [(b"x-request-id", request_id.encode("ascii"))],
                    )
        finally:
            duration_ms = (time.perf_counter() - started) * 1000.0
            logger.info(
                "http request request_id=%s method=%s path=%s status=%s duration_ms=%.1f",
                request_id,
                scope.get("method"),
                scope.get("path"),
                status,
                duration_ms,
            )


#: JSON-RPC framing overhead allowed above the evidence object budget on /mcp.
#: The upload_evidence handler still enforces the exact object limit; this only
#: keeps the envelope from winning the race against the tool-level check.
MCP_ENVELOPE_HEADROOM_BYTES = 64 * 1024


def _mcp_request_limit(config: Config) -> int:
    return max(
        config.server.max_request_bytes,
        config.limits.evidence_object_bytes + MCP_ENVELOPE_HEADROOM_BYTES,
    )


class BodyLimitMiddleware:
    """Enforce request byte caps before buffering the whole body.

    The JSON API and raw evidence upload have different budgets. Evidence is
    capped by ``evidence_object_bytes`` (the store's own hard limit), so a
    256 KiB object is accepted even when the generic JSON cap is 128 KiB.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_bytes: int,
        evidence_max_bytes: int | None = None,
        evidence_path: str = ROUTE_EVIDENCE,
        mcp_path: str = "/mcp",
        mcp_max_bytes: int | None = None,
    ) -> None:
        self.app = app
        self.max_bytes = max(1, int(max_bytes))
        self.evidence_max_bytes = max(1, int(evidence_max_bytes)) if evidence_max_bytes else None
        self.evidence_path = evidence_path
        self.mcp_path = mcp_path
        self.mcp_max_bytes = max(1, int(mcp_max_bytes)) if mcp_max_bytes else None

    def _limit_for(self, scope: Scope) -> int:
        if self.evidence_max_bytes is None or scope.get("method") != "POST":
            return self.max_bytes
        path = scope.get("path")
        if path == self.evidence_path:
            # Raw evidence has its own object budget, independent of the JSON cap.
            return self.evidence_max_bytes
        if path == self.mcp_path:
            # The MCP JSON-RPC envelope can carry an upload_evidence payload.
            # The tool itself enforces evidence_object_bytes, so the transport
            # allows bounded framing overhead and reports the tool's own error.
            return self.mcp_max_bytes or max(self.max_bytes, self.evidence_max_bytes)
        return self.max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self._limit_for(scope)
        headers = Headers(scope=scope)
        declared = headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > limit:
                    await _send_json(
                        send,
                        413,
                        _envelope_error(
                            ErrorCode.BUDGET_EXCEEDED,
                            f"request body exceeds the {limit} byte limit",
                        ),
                    )
                    return
            except ValueError:
                pass

        chunks: list[bytes] = []
        total = 0
        # Stream until the peer stops sending; a declared length is only a hint.
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                break
            body = message.get("body", b"")
            total += len(body)
            if total > limit:
                await _send_json(
                    send,
                    413,
                    _envelope_error(
                        ErrorCode.BUDGET_EXCEEDED,
                        f"request body exceeds the {limit} byte limit",
                    ),
                )
                return
            chunks.append(body)
            more = bool(message.get("more_body", False))

        body = b"".join(chunks)
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


class HostOriginMiddleware:
    """Reject foreign Host and browser Origin headers before routing."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        allowed_hosts: list[str] | tuple[str, ...],
        allowed_origins: list[str] | tuple[str, ...],
    ) -> None:
        self.app = app
        self.allowed_hosts = list(allowed_hosts)
        self.allowed_origins = list(allowed_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        host = headers.get("host")
        if not host_allowed(host, self.allowed_hosts):
            await _send_json(
                send,
                400,
                _envelope_error(ErrorCode.INVALID_ARGUMENT, "Host header is not allowed"),
            )
            return
        origin = headers.get("origin")
        if not origin_allowed(origin, self.allowed_origins):
            await _send_json(
                send,
                403,
                _envelope_error(ErrorCode.FORBIDDEN, "Origin header is not allowed"),
            )
            return
        await self.app(scope, receive, send)


# -- authentication -------------------------------------------------------
@dataclass(frozen=True, slots=True)
class AuthContext:
    session: SessionInfo
    token: str
    actor_handle: str | None


def _service_of(request: Request) -> KnowledgeService:
    service: KnowledgeService = request.app.state.service
    return service


def require_auth(request: Request) -> AuthContext:
    """Resolve a bearer token and optional actor handle, else raise 401."""
    token = parse_bearer(request.headers.get("authorization"))
    actor = request.headers.get(HEADER_ACTOR_HANDLE)
    service = _service_of(request)
    session = service.resolve_actor(token, actor)
    return AuthContext(session=session, token=token or "", actor_handle=actor)


#: FastAPI dependency alias; ``Depends`` is evaluated at import, not per call.
AuthDep = Annotated[AuthContext, Depends(require_auth)]


def require_protocol_header(request: Request) -> None:
    """Validate an explicitly supplied protocol version; absence is allowed.

    A missing header means an older client that predates negotiation, so it
    continues to work. A present but incompatible or malformed value fails
    before any request state changes.
    """
    value = request.headers.get(HEADER_PROTOCOL)
    if value is None:
        return
    if not version_compatible(value):
        shown = value.strip() or "<empty>"
        raise ServiceError(
            ErrorCode.INVALID_ARGUMENT,
            f"client protocol {shown} is not compatible with server protocol {PROTOCOL_VERSION}",
            details={"client_protocol": value, "server_protocol": PROTOCOL_VERSION},
        )


ProtocolDep = Annotated[None, Depends(require_protocol_header)]


# -- backup scheduler ------------------------------------------------------
async def _backup_loop(service: KnowledgeService) -> None:
    """Create one backup per day while the gateway runs. Never crashes."""
    config = service.config
    while True:
        await asyncio.sleep(BACKUP_INTERVAL_SECONDS)
        if not config.backup.daily:
            continue
        try:
            result = await asyncio.to_thread(service.backup.create)
            pruned = await asyncio.to_thread(
                prune_backups,
                Path(config.storage.backup_dir),
                config.backup.keep_daily,
                config.backup.keep_weekly,
            )
            logger.info(
                "scheduled backup created path=%s pruned=%s",
                result.get("path"),
                len(pruned["removed"]),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("scheduled backup failed; gateway continues")


# -- app factory -----------------------------------------------------------
def _mcp_transport_security(config: Config) -> TransportSecuritySettings:
    hosts: list[str] = []
    for entry in config.server.allowed_hosts:
        entry = entry.strip()
        if not entry:
            continue
        hosts.append(entry)
        if ":" not in entry and not entry.endswith(":*"):
            hosts.append(f"{entry}:*")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=sorted(set(hosts)),
        allowed_origins=list(config.server.allowed_origins),
    )


def create_app(config: Config, service: KnowledgeService) -> FastAPI:
    """Build the ASGI app. No sockets are opened; the lifespan does the work."""
    mcp = build_mcp_server(config, service)
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        # upload_evidence travels inside the JSON-RPC envelope; allow the
        # evidence object budget through the MCP transport as well. The tool
        # handler still enforces evidence_object_bytes on the decoded text.
        max_request_body_size=_mcp_request_limit(config),
        transport_security=_mcp_transport_security(config),
        host=config.server.host,
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        service.bootstrap()
        scheduler = asyncio.create_task(_backup_loop(service), name="knowlageMCP-backup")
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            scheduler.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await scheduler
            service.close()

    app = FastAPI(
        title="knowlageMCP gateway",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.config = config
    app.state.service = service
    app.state.mcp = mcp

    @app.exception_handler(ServiceError)
    async def service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
        return _error_response(exc)

    @app.exception_handler(Exception)
    async def internal_error_handler(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", "-")
        logger.exception("unhandled route failure request_id=%s", request_id)
        return JSONResponse(_envelope_error(ErrorCode.INTERNAL, "internal server error"), status_code=500)

    @app.get(ROUTE_HEALTH)
    async def health(request: Request) -> JSONResponse:
        status = _service_of(request).status()
        return JSONResponse(
            {
                "status": "ok",
                "version": status["version"],
                "protocol_version": status["protocol_version"],
                "schema_version": status["schema_version"],
                "uptime_s": status["uptime_s"],
            }
        )

    @app.post(ROUTE_REGISTER)
    async def bridge_register(request: Request, _: ProtocolDep) -> JSONResponse:
        try:
            body = RegisterRequest.model_validate(await request.json())
        except ValidationError as exc:
            raise ServiceError(ErrorCode.INVALID_ARGUMENT, _validation_message("registration", exc)) from exc
        except (json.JSONDecodeError, ValueError) as exc:
            raise ServiceError(ErrorCode.INVALID_ARGUMENT, "request body must be a JSON object") from exc
        source = request.client.host if request.client is not None else "unknown"
        registration = _service_of(request).register(
            mode=body.mode,
            ticket=body.ticket,
            label=body.label,
            model=body.model,
            provider=body.provider,
            task=body.task,
            source=source,
        )
        return JSONResponse(
            {
                "session_token": registration.session_token,
                "actor_handle": registration.actor_handle,
                "agent_id": registration.agent_id,
                "root_agent_id": registration.root_agent_id,
                "session_id": registration.session_id,
                "expires_at": registration.expires_at.isoformat(),
                "grants": [
                    {"namespace": grant.namespace, "role": grant.role.value} for grant in registration.grants
                ],
            }
        )

    @app.post(ROUTE_MENTOR)
    async def bridge_mentor(request: Request, auth: AuthDep) -> JSONResponse:
        try:
            body = MentorHttpRequest.model_validate(await request.json())
        except ValidationError as exc:
            raise ServiceError(
                ErrorCode.INVALID_ARGUMENT, _validation_message("mentor request", exc)
            ) from exc
        except (json.JSONDecodeError, ValueError) as exc:
            raise ServiceError(ErrorCode.INVALID_ARGUMENT, "request body must be a JSON object") from exc
        mentor: MentorRequest = _service_of(request).request_mentor(auth.session, body.namespace, body.scopes)
        return JSONResponse(mentor.model_dump(mode="json"))

    @app.get(ROUTE_ENROLLMENT)
    async def bridge_enrollment(request_id: str, request: Request, auth: AuthDep) -> JSONResponse:
        status = _service_of(request).enrollment_status(auth.session, request_id)
        return JSONResponse(
            EnrollmentResponse(
                request_id=status.request_id,
                state=status.state,
                approved_at=status.approved_at.isoformat() if status.approved_at else None,
            ).model_dump(mode="json")
        )

    @app.post(ROUTE_CLOSE)
    async def bridge_close(request: Request, auth: AuthDep) -> JSONResponse:
        result = _service_of(request).session_close(auth.session)
        return JSONResponse(CloseResponse(closed=bool(result.get("closed", True))).model_dump(mode="json"))

    @app.post(ROUTE_TOOLS)
    async def bridge_tool(tool: str, request: Request, _: ProtocolDep, auth: AuthDep) -> JSONResponse:
        if tool not in ALL_TOOL_NAMES or tool not in TOOL_ARG_MODELS:
            return JSONResponse(
                _envelope_error(ErrorCode.INVALID_ARGUMENT, f"unknown tool: {tool}"),
                status_code=404,
            )
        try:
            raw = await request.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise ServiceError(ErrorCode.INVALID_ARGUMENT, "request body must be a JSON object") from exc
        if not isinstance(raw, dict):
            raise ServiceError(ErrorCode.INVALID_ARGUMENT, "tool arguments must be a JSON object")
        args = dict(raw)
        if auth.actor_handle and "actor_handle" not in args:
            args["actor_handle"] = auth.actor_handle
        envelope = await call_tool(_service_of(request), tool, args, auth.token)
        headers: dict[str, str] = {}
        if envelope["ok"]:
            return JSONResponse(envelope)
        error = envelope["error"]
        status = http_status_for(str(error.get("code", ErrorCode.INTERNAL.value)))
        retry = error.get("retry_after_s")
        if status == 429 and retry is not None:
            headers["Retry-After"] = str(max(1, int(retry)))
        if status == 401:
            headers["WWW-Authenticate"] = "Bearer"
        return JSONResponse(envelope, status_code=status, headers=headers)

    @app.post(ROUTE_EVIDENCE)
    async def evidence_upload(request: Request, auth: AuthDep) -> JSONResponse:
        media_type = request.headers.get("content-type") or "application/octet-stream"
        name = sanitize_evidence_name(request.headers.get(HEADER_EVIDENCE_NAME))
        payload = await request.body()
        record = _service_of(request).evidence.put(payload, media_type, name=name)
        return JSONResponse(
            EvidenceUploadResponse(
                evidence_id=record.evidence_id,
                sha256=record.sha256,
                size_bytes=record.size_bytes,
                redacted=record.redacted,
            ).model_dump(mode="json")
        )

    @app.get(ROUTE_EVIDENCE_ITEM)
    async def evidence_download(evidence_id: str, request: Request, auth: AuthDep) -> Response:
        service = _service_of(request)
        media_type = _evidence_media_type(service, auth.session, evidence_id)
        payload = service.evidence.get(evidence_id)
        return Response(content=payload, media_type=media_type)

    # The MCP Starlette app is mounted last so registered routes win.
    app.mount("/", mcp_app)

    app.add_middleware(
        HostOriginMiddleware,
        allowed_hosts=list(config.server.allowed_hosts),
        allowed_origins=list(config.server.allowed_origins),
    )
    app.add_middleware(
        BodyLimitMiddleware,
        max_bytes=config.server.max_request_bytes,
        evidence_max_bytes=config.limits.evidence_object_bytes,
        mcp_max_bytes=_mcp_request_limit(config),
    )
    app.add_middleware(RequestContextMiddleware)
    return app


def _evidence_media_type(service: KnowledgeService, session: SessionInfo, evidence_id: str) -> str:
    """Return the stored media type, or hide referenced evidence from outsiders."""
    conn = service.db.connect()
    row = conn.execute(
        "SELECT media_type, revision_id FROM evidence_objects WHERE evidence_id = ?",
        (evidence_id,),
    ).fetchone()
    if row is None:
        return "application/octet-stream"
    revision_id = row["revision_id"]
    if revision_id is not None:
        namespace_row = conn.execute(
            "SELECT e.namespace FROM entry_revisions r JOIN entries e ON e.entry_id = r.entry_id "
            "WHERE r.revision_id = ?",
            (str(revision_id),),
        ).fetchone()
        if namespace_row is not None:
            readable = readable_namespaces(conn, session.agent_id, datetime.now(UTC))
            if str(namespace_row["namespace"]) not in readable:
                raise ServiceError(ErrorCode.NOT_FOUND, "evidence object not found")
    media = row["media_type"]
    return str(media) if media else "application/octet-stream"


# -- process runner --------------------------------------------------------
def _configure_logging(config: Config) -> None:
    level = getattr(logging, config.logging.level.upper(), logging.INFO)
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
    else:
        logging.getLogger("knowlage_mcp").setLevel(level)


def _lock_path(config: Config) -> Path:
    database = config.storage.database
    return database.parent / f"{database.name}.lock"


async def _serve(config: Config, config_path: Path | None = None) -> int:
    db = Database(config.storage.database)
    evidence = EvidenceStore(
        config.storage.evidence_dir,
        quota_bytes=config.storage.evidence_quota_bytes,
        max_object_bytes=config.limits.evidence_object_bytes,
    )
    service = KnowledgeService(config, db, evidence, config_path=config_path)
    try:
        service.bootstrap()
    except Exception:
        logger.exception("gateway startup failed during schema bootstrap")
        db.close()
        return 1
    admin = AdminServer(service, config.storage.runtime_socket)
    app = create_app(config, service)
    uv_config = uvicorn.Config(
        app,
        host=config.server.host,
        port=config.server.port,
        log_level=config.logging.level.lower(),
        access_log=False,
        lifespan="on",
    )
    server = uvicorn.Server(uv_config)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(sig, server.handle_exit, sig, None)
    try:
        await admin.start()
    except (OSError, RuntimeError) as exc:
        logger.error("admin socket could not be started: %s", exc)
        db.close()
        return 3
    try:
        logger.info(
            "knowlageMCP gateway %s listening on http://%s:%s (admin socket %s)",
            __version__,
            config.server.host,
            config.server.port,
            config.storage.runtime_socket,
        )
        await server.serve()
        return 0
    except SystemExit:
        logger.error("gateway startup aborted")
        return 1
    except OSError as exc:
        logger.error("gateway listener could not start: %s", exc)
        return 3
    finally:
        await admin.stop()
        # The uvicorn thread closed its own connection in the lifespan; close
        # this thread's bootstrap connection as well before releasing the lock.
        with contextlib.suppress(Exception):
            db.close()


def run_gateway(config: Config, config_path: Path | None = None) -> int:
    """Run the gateway in the foreground; returns a process exit code."""
    _configure_logging(config)
    problems = validate_config(config)
    if problems:
        for problem in problems:
            logger.error("config: %s", problem)
        return 1
    try:
        with lock_file(_lock_path(config)):
            return asyncio.run(_serve(config, config_path))
    except DatabaseLockedError as exc:
        logger.error("%s", exc)
        return 3
    except OSError as exc:
        logger.error("gateway filesystem setup failed: %s", exc)
        return 3


def run_gateway_from_path(path: str | None = None) -> int:
    try:
        config = load_config(Path(path) if path else None)
    except (FileNotFoundError, ValueError) as exc:
        logger.error("config could not be loaded: %s", exc)
        return 1
    return run_gateway(config, Path(path) if path else None)


__all__ = [
    "AuthContext",
    "BodyLimitMiddleware",
    "HostOriginMiddleware",
    "RequestContextMiddleware",
    "create_app",
    "host_allowed",
    "http_status_for",
    "origin_allowed",
    "run_gateway",
    "run_gateway_from_path",
]
