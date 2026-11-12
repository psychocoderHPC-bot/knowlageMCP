# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Socket-level fake HTTP backends for the over-the-wire integration tests.

Every fake is a small :class:`http.server.ThreadingHTTPServer` bound to
``127.0.0.1:0``. The gateway talks to them through its real HTTP adapters, so a
test covers configuration, registry construction, the adapter, a real socket,
and the service without injecting a transport.
"""

from __future__ import annotations

import json
import math
import threading
from collections import deque
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar
from urllib.parse import SplitResult, urlsplit

from knowlage_mcp.integrations.hash_embed import HashEmbeddingProvider

DIMENSIONS = 16


class _FakeHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = False
    block_on_close = True


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, _format: str, *args: Any) -> None:
        pass

    def _dispatch(self) -> None:
        fake = self.server.fake
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        try:
            status, payload = fake.handle(self.command, self.path, body, self.headers)
        except Exception as exc:  # pragma: no cover - defensive handler
            status, payload = 500, {"status": {"error": str(exc)}}
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        with suppress(BrokenPipeError, ConnectionResetError):  # pragma: no cover - client went away
            self.wfile.write(data)

    do_GET = _dispatch
    do_POST = _dispatch
    do_PUT = _dispatch
    do_DELETE = _dispatch


class _FakeHttpServer:
    """Lifecycle, request bookkeeping, and failure injection shared by fakes."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._failures: deque[int] = deque()
        self._requests: list[tuple[str, str]] = []
        self._server = _FakeHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.fake = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=False)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def request_count(self) -> int:
        with self._lock:
            return len(self._requests)

    def fail_next(self, status: int = 500) -> None:
        with self._lock:
            self._failures.append(status)

    def _record(self, method: str, path: str) -> None:
        with self._lock:
            self._requests.append((method, path))

    def _injected_failure(self) -> int | None:
        with self._lock:
            return self._failures.popleft() if self._failures else None

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        assert not self._thread.is_alive(), "fake backend thread did not stop"

    @staticmethod
    def _split(path: str) -> SplitResult:
        return urlsplit(path)

    @staticmethod
    def _json(body: bytes) -> Any:
        return json.loads(body) if body else {}


def _match(payload: dict[str, Any], condition: dict[str, Any]) -> bool:
    key = condition.get("key")
    match = condition.get("match")
    if not isinstance(match, dict):
        return True
    if "any" in match:
        values = match["any"] if isinstance(match["any"], list) else [match["any"]]
        return payload.get(str(key)) in values
    if "value" in match:
        return payload.get(str(key)) == match["value"]
    return True


def _matches(payload: dict[str, Any], raw_filter: Any) -> bool:
    if not isinstance(raw_filter, dict):
        return True
    must = raw_filter.get("must") or []
    must_not = raw_filter.get("must_not") or []
    if not all(_match(payload, condition) for condition in must):
        return False
    return not any(_match(payload, condition) for condition in must_not)


def _cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    norm_left = math.sqrt(sum(value * value for value in left))
    norm_right = math.sqrt(sum(value * value for value in right))
    if not norm_left or not norm_right:
        return 0.0
    return dot / (norm_left * norm_right)


class FakeQdrantServer(_FakeHttpServer):
    """In-memory Qdrant subset with cosine scoring and payload filters."""

    def __init__(self) -> None:
        self.collections: dict[str, dict[str, Any]] = {}
        self.collection_creations = 0
        self.upserts = 0
        self.upserted_points = 0
        self.deletes = 0
        self.searches = 0
        super().__init__()

    def handle(self, method: str, path: str, body_raw: bytes, _headers: Any) -> tuple[int, Any]:
        self._record(method, path)
        injected = self._injected_failure()
        if injected is not None:
            return injected, {"status": {"error": "injected failure"}}
        parsed = self._split(path)
        parts = [part for part in parsed.path.split("/") if part]
        body = self._json(body_raw)
        with self._lock:
            if method == "PUT" and len(parts) == 2 and parts[0] == "collections":
                name = parts[1]
                vectors = body.get("vectors") if isinstance(body, dict) else None
                size = int((vectors or {}).get("size", 0))
                self.collection_creations += 1
                self.collections.setdefault(
                    name,
                    {"size": size, "distance": (vectors or {}).get("distance", "Cosine"), "points": {}},
                )
                return 200, {"result": True}
            if method == "GET" and len(parts) == 2 and parts[0] == "collections":
                collection = self.collections.get(parts[1])
                if collection is None:
                    return 404, {"status": {"error": "not found"}}
                return 200, {
                    "result": {
                        "status": "green",
                        "config": {
                            "params": {
                                "vectors": {"size": collection["size"], "distance": collection["distance"]}
                            }
                        },
                    }
                }
            if len(parts) < 3 or parts[0] != "collections" or parts[2] != "points":
                return 404, {"status": {"error": "unknown"}}
            collection = self.collections.get(parts[1])
            if collection is None:
                return 404, {"status": {"error": "not found"}}
            points: dict[str, dict[str, Any]] = collection["points"]
            if method == "PUT" and len(parts) == 3:
                self.upserts += 1
                incoming = body.get("points") or []
                for point in incoming:
                    points[str(point["id"])] = {
                        "vector": [float(value) for value in point["vector"]],
                        "payload": dict(point.get("payload") or {}),
                    }
                self.upserted_points += len(incoming)
                return 200, {"result": {"status": "completed"}}
            if method == "POST" and len(parts) == 4 and parts[3] == "delete":
                self.deletes += 1
                raw_filter = body.get("filter") if isinstance(body, dict) else None
                for point_id in [
                    key for key, point in points.items() if _matches(point["payload"], raw_filter)
                ]:
                    del points[point_id]
                return 200, {"result": {"status": "completed"}}
            if method == "POST" and len(parts) == 4 and parts[3] == "payload":
                payload = body.get("payload") or {}
                raw_filter = body.get("filter") if isinstance(body, dict) else None
                for point in points.values():
                    if _matches(point["payload"], raw_filter):
                        point["payload"].update(payload)
                return 200, {"result": {"status": "completed"}}
            if method == "POST" and len(parts) == 4 and parts[3] == "search":
                self.searches += 1
                vector = [float(value) for value in body["vector"]]
                raw_filter = body.get("filter") if isinstance(body, dict) else None
                scored = [
                    {
                        "id": point_id,
                        "score": _cosine(vector, point["vector"]),
                        "payload": dict(point["payload"]),
                    }
                    for point_id, point in points.items()
                    if _matches(point["payload"], raw_filter)
                ]
                scored.sort(key=lambda item: item["score"], reverse=True)
                limit = int(body.get("limit", 10))
                return 200, {"result": scored[:limit]}
            return 404, {"status": {"error": "unknown"}}

    def total_points(self) -> int:
        with self._lock:
            return sum(len(collection["points"]) for collection in self.collections.values())

    def points_for(self, entry_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                point
                for collection in self.collections.values()
                for point in collection["points"].values()
                if point["payload"].get("entry_id") == entry_id
            ]


class FakeEmbeddingServer(_FakeHttpServer):
    """OpenAI-compatible embeddings with deterministic hashed vectors."""

    def __init__(self, expected_key: str | None = None, dimensions: int = DIMENSIONS) -> None:
        self.expected_key = expected_key
        self.dimensions = dimensions
        self.requests = 0
        self.authorization_headers: list[str | None] = []
        self.inputs: list[list[str]] = []
        self._embedder = HashEmbeddingProvider(dimensions=dimensions)
        super().__init__()

    def handle(self, method: str, path: str, body_raw: bytes, headers: Any) -> tuple[int, Any]:
        self._record(method, path)
        injected = self._injected_failure()
        if injected is not None:
            return injected, {"error": {"message": "injected failure"}}
        if method != "POST" or self._split(path).path != "/embeddings":
            return 404, {"error": {"message": "unknown"}}
        with self._lock:
            self.requests += 1
            self.authorization_headers.append(headers.get("Authorization"))
        if self.expected_key is not None and headers.get("Authorization") != f"Bearer {self.expected_key}":
            return 401, {"error": {"message": "invalid api key"}}
        body = self._json(body_raw)
        raw_input = body.get("input", []) if isinstance(body, dict) else []
        texts = [raw_input] if isinstance(raw_input, str) else [str(item) for item in raw_input]
        vectors = self._embedder.embed(texts)
        with self._lock:
            self.inputs.append(texts)
        return 200, {
            "object": "list",
            "model": body.get("model", "fake") if isinstance(body, dict) else "fake",
            "data": [
                {"object": "embedding", "index": index, "embedding": vector}
                for index, vector in enumerate(vectors)
            ],
        }


class FakeGraphitiServer(_FakeHttpServer):
    """Graphiti edge extraction returning one fixed, well-formed edge."""

    EDGES: ClassVar[list[dict[str, str]]] = [
        {"subject": "alpha", "relation": "causes", "object": "beta", "detail": "observed in a test"}
    ]

    def __init__(self) -> None:
        self.requests = 0
        super().__init__()

    def handle(self, method: str, path: str, _body_raw: bytes, _headers: Any) -> tuple[int, Any]:
        self._record(method, path)
        injected = self._injected_failure()
        if injected is not None:
            return injected, {"error": {"message": "injected failure"}}
        if method != "POST" or self._split(path).path != "/extract":
            return 404, {"error": {"message": "unknown"}}
        with self._lock:
            self.requests += 1
        return 200, {"edges": [dict(edge) for edge in self.EDGES]}


class FakeMem0Server(_FakeHttpServer):
    """Mem0 extraction returning one fixed preference proposal."""

    MEMORIES: ClassVar[list[dict[str, str]]] = [
        {
            "kind": "preference",
            "title": "Concise logs",
            "claim": "Prefer concise logs",
            "source_role": "user_message",
        }
    ]

    def __init__(self) -> None:
        self.requests = 0
        self.message_roles: list[str] = []
        super().__init__()

    def handle(self, method: str, path: str, body_raw: bytes, _headers: Any) -> tuple[int, Any]:
        self._record(method, path)
        injected = self._injected_failure()
        if injected is not None:
            return injected, {"error": {"message": "injected failure"}}
        if method != "POST" or self._split(path).path != "/memories/extract":
            return 404, {"error": {"message": "unknown"}}
        body = self._json(body_raw)
        messages = body.get("messages") if isinstance(body, dict) else None
        role = str(messages[0].get("role")) if isinstance(messages, list) and messages else "unknown"
        with self._lock:
            self.requests += 1
            self.message_roles.append(role)
        return 200, {"memories": [dict(memory) for memory in self.MEMORIES]}


__all__ = [
    "DIMENSIONS",
    "FakeEmbeddingServer",
    "FakeGraphitiServer",
    "FakeMem0Server",
    "FakeQdrantServer",
]
