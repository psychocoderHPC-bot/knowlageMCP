# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""HTTP bridge wire protocol shared by the gateway and the stdio bridge.

These models are deliberately transport-shaped: they describe JSON bodies and
headers only. Business rules and authorization stay in the service layer.
"""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import PROTOCOL_VERSION, __version__
from .models import ErrorCode, Grant, ServiceError

#: URL prefix of the agent-facing HTTP API.
API_VERSION = "v1"
HTTP_PREFIX = f"/{API_VERSION}"

ROUTE_HEALTH = f"{HTTP_PREFIX}/health"
ROUTE_REGISTER = f"{HTTP_PREFIX}/bridge/register"
ROUTE_MENTOR = f"{HTTP_PREFIX}/bridge/mentor"
ROUTE_ENROLLMENT = f"{HTTP_PREFIX}/bridge/enrollment/{{request_id}}"
ROUTE_EVIDENCE = f"{HTTP_PREFIX}/bridge/evidence"
ROUTE_EVIDENCE_ITEM = f"{HTTP_PREFIX}/bridge/evidence/{{evidence_id}}"
ROUTE_CLOSE = f"{HTTP_PREFIX}/bridge/close"
ROUTE_TOOLS = f"{HTTP_PREFIX}/tools/{{tool}}"

#: Header carrying a connection-local actor handle for multiplexed bridges.
HEADER_ACTOR_HANDLE = "X-Actor-Handle"
#: Header naming an uploaded evidence object.
HEADER_EVIDENCE_NAME = "X-Evidence-Name"

AUTH_SCHEME = "Bearer"


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterRequest(_WireModel):
    mode: Literal["root", "child"] = "root"
    ticket: str | None = None
    label: str | None = Field(default=None, max_length=200)
    model: str | None = Field(default=None, max_length=200)
    provider: str | None = Field(default=None, max_length=200)
    task: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _ticket_required_for_child(self) -> Self:
        if self.mode == "child" and not self.ticket:
            raise ValueError("mode=child requires a delegation ticket")
        return self


class RegisterResponse(_WireModel):
    session_token: str = Field(repr=False)
    actor_handle: str
    agent_id: str
    root_agent_id: str
    session_id: str
    expires_at: str
    grants: tuple[Grant, ...] = ()


class MentorHttpRequest(_WireModel):
    namespace: str = Field(min_length=1)
    scopes: tuple[str, ...] = ()


class EnrollmentResponse(_WireModel):
    request_id: str
    state: str
    approved_at: str | None = None


class EvidenceUploadResponse(_WireModel):
    evidence_id: str
    sha256: str
    size_bytes: int
    redacted: bool = False


class CloseResponse(_WireModel):
    closed: bool = True


class ErrorBody(_WireModel):
    code: str
    message: str
    retry_after_s: int | None = None
    details: dict[str, Any] | None = None


class ToolEnvelope(_WireModel):
    """Uniform tool result: exactly one of ``data`` or ``error`` is set."""

    ok: bool
    data: Any | None = None
    error: ErrorBody | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.ok and self.error is not None:
            raise ValueError("ok envelope must not carry an error")
        if not self.ok and self.error is None:
            raise ValueError("failed envelope must carry an error")
        if not self.ok and self.data is not None:
            raise ValueError("failed envelope must not carry data")
        return self

    @classmethod
    def success(cls, data: Any) -> ToolEnvelope:
        return cls(ok=True, data=data)

    @classmethod
    def failure(
        cls,
        code: ErrorCode | str,
        message: str,
        retry_after_s: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> ToolEnvelope:
        value = code.value if isinstance(code, ErrorCode) else str(code)
        return cls(
            ok=False,
            error=ErrorBody(code=value, message=message, retry_after_s=retry_after_s, details=details),
        )

    @classmethod
    def from_error(cls, exc: ServiceError) -> ToolEnvelope:
        return cls(
            ok=False,
            error=ErrorBody(
                code=exc.code.value,
                message=exc.message,
                retry_after_s=exc.retry_after_s,
                details=exc.details,
            ),
        )


class HealthResponse(_WireModel):
    status: str = "ok"
    version: str = __version__
    protocol_version: str = PROTOCOL_VERSION
    schema_version: int
    uptime_s: float = 0.0


def error_envelope(exc: ServiceError) -> ToolEnvelope:
    """Map a service failure to the uniform bridge envelope."""
    return ToolEnvelope.from_error(exc)


def version_compatible(client_version: str) -> bool:
    """Return whether a client protocol version is understood by this gateway.

    Compatibility is same major and client minor not newer than the server
    minor. Unknown or malformed values are rejected rather than guessed.
    """
    server_major, _, server_minor_text = PROTOCOL_VERSION.partition(".")
    parts = client_version.split(".")
    try:
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        return False
    if major != int(server_major):
        return False
    return minor <= int(server_minor_text)


def parse_bearer(header_value: str | None) -> str | None:
    """Extract a bearer token without ever logging the value."""
    if not header_value:
        return None
    scheme, _, token = header_value.partition(" ")
    if scheme.lower() != AUTH_SCHEME.lower():
        return None
    token = token.strip()
    return token or None


__all__ = [
    "API_VERSION",
    "AUTH_SCHEME",
    "HEADER_ACTOR_HANDLE",
    "HEADER_EVIDENCE_NAME",
    "HTTP_PREFIX",
    "ROUTE_CLOSE",
    "ROUTE_ENROLLMENT",
    "ROUTE_EVIDENCE",
    "ROUTE_EVIDENCE_ITEM",
    "ROUTE_HEALTH",
    "ROUTE_MENTOR",
    "ROUTE_REGISTER",
    "ROUTE_TOOLS",
    "CloseResponse",
    "EnrollmentResponse",
    "ErrorBody",
    "EvidenceUploadResponse",
    "HealthResponse",
    "MentorHttpRequest",
    "RegisterRequest",
    "RegisterResponse",
    "ToolEnvelope",
    "error_envelope",
    "parse_bearer",
    "version_compatible",
]
