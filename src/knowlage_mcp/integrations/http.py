# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Small JSON-over-HTTP helper for optional adapters.

The helper never logs or embeds request headers or response bodies in an
error. API keys are read from the environment at request time by name; the
key value is never stored on the instance.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx2

from ..models import ErrorCode, ServiceError

#: Attempts for idempotent methods. Non-idempotent requests are never retried.
DEFAULT_ATTEMPTS = 3
#: Exponential backoff between retries, capped to keep a probe bounded.
DEFAULT_BACKOFF_BASE_S = 0.1
DEFAULT_BACKOFF_MAX_S = 1.0
#: Hard ceiling for a backend response body; larger bodies are rejected unread.
MAX_RESPONSE_BYTES = 4 * 1024 * 1024

#: Methods safe to retry. ``POST`` is deliberately absent.
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD"})


def _map_status(status_code: int) -> ErrorCode:
    if status_code in {401, 403}:
        return ErrorCode.FORBIDDEN
    if status_code in {408, 429}:
        return ErrorCode.BACKEND_UNAVAILABLE
    return ErrorCode.INVALID_ARGUMENT


def _validate_base_url(base_url: str) -> None:
    """Reject unusable base URLs without echoing a credential-bearing value."""
    if not base_url:
        raise ServiceError(ErrorCode.INVALID_ARGUMENT, "base_url must not be empty")
    if any(character.isspace() for character in base_url):
        raise ServiceError(
            ErrorCode.INVALID_ARGUMENT,
            "base_url must be an absolute http(s) URL",
        )
    try:
        parsed = urlsplit(base_url)
        _ = parsed.port
    except ValueError as exc:
        raise ServiceError(
            ErrorCode.INVALID_ARGUMENT,
            "base_url must be an absolute http(s) URL",
        ) from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ServiceError(
            ErrorCode.INVALID_ARGUMENT,
            "base_url must be an absolute http(s) URL",
        )
    if parsed.username is not None or parsed.password is not None:
        raise ServiceError(
            ErrorCode.INVALID_ARGUMENT,
            "base_url must not contain userinfo",
        )


class JsonHttpClient:
    """Bounded sync JSON client.

    ``transport`` accepts an injected :class:`httpx2.BaseTransport`, which lets
    tests use :class:`httpx2.MockTransport` without touching the network.
    """

    def __init__(
        self,
        base_url: str,
        timeout_s: float,
        api_key_env: str | None = None,
        transport: httpx2.BaseTransport | None = None,
        *,
        attempts: int = DEFAULT_ATTEMPTS,
        backoff_base_s: float = DEFAULT_BACKOFF_BASE_S,
        backoff_max_s: float = DEFAULT_BACKOFF_MAX_S,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if attempts < 1:
            raise ValueError("attempts must be at least 1")
        _validate_base_url(base_url)
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.api_key_env = api_key_env
        # Retries are capped so a probe can never amplify into a long loop.
        self.attempts = min(attempts, DEFAULT_ATTEMPTS)
        self.backoff_base_s = backoff_base_s
        self.backoff_max_s = backoff_max_s
        self._sleep = sleep
        self._client = httpx2.Client(
            base_url=self.base_url,
            timeout=timeout_s,
            transport=transport,
            follow_redirects=False,
        )

    def _headers(self, *, has_body: bool) -> dict[str, str]:
        headers: dict[str, str] = {"Accept": "application/json"}
        if has_body:
            headers["Content-Type"] = "application/json"
        if self.api_key_env:
            api_key = os.environ.get(self.api_key_env)
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def _backoff(self, attempt: int) -> float:
        return float(min(self.backoff_base_s * (2 ** (attempt - 1)), self.backoff_max_s))

    def request(
        self,
        method: str,
        path: str,
        json: Any | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        """Perform one JSON request and return the decoded body or ``None``."""
        normalized = method.upper()
        max_attempts = self.attempts if normalized in _IDEMPOTENT_METHODS else 1
        for attempt in range(1, max_attempts + 1):
            try:
                response = self._client.request(
                    normalized,
                    path,
                    json=json,
                    params=params,
                    headers=self._headers(has_body=json is not None),
                )
            except httpx2.TransportError as exc:
                if attempt < max_attempts:
                    self._sleep(self._backoff(attempt))
                    continue
                raise ServiceError(
                    ErrorCode.BACKEND_UNAVAILABLE,
                    f"{normalized} {path} failed: backend unreachable",
                ) from exc

            status = response.status_code
            if status >= 500:
                if attempt < max_attempts:
                    self._sleep(self._backoff(attempt))
                    continue
                raise ServiceError(
                    ErrorCode.BACKEND_UNAVAILABLE,
                    f"{normalized} {path} failed with status {status}",
                )
            if 300 <= status < 400:
                raise ServiceError(
                    ErrorCode.BACKEND_UNAVAILABLE,
                    f"{normalized} {path} returned an unexpected redirect ({status})",
                )
            if status >= 400:
                raise ServiceError(
                    _map_status(status),
                    f"{normalized} {path} failed with status {status}",
                )
            if len(response.content) > MAX_RESPONSE_BYTES:
                raise ServiceError(
                    ErrorCode.BACKEND_UNAVAILABLE,
                    "backend response exceeded the 4 MiB bound",
                )
            if status == 204 or not response.content:
                return None
            try:
                return response.json()
            except ValueError as exc:
                raise ServiceError(
                    ErrorCode.BACKEND_UNAVAILABLE,
                    f"{normalized} {path} returned invalid JSON",
                ) from exc
        raise ServiceError(  # pragma: no cover - loop always returns or raises
            ErrorCode.BACKEND_UNAVAILABLE,
            f"{normalized} {path} exhausted retries",
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> JsonHttpClient:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


__all__ = ["MAX_RESPONSE_BYTES", "JsonHttpClient"]
