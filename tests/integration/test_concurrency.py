# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Concurrency acceptance tests: 50 real HTTP clients, bounded writes, lock refusal.

The reference brief (13.3) asks for 50 simultaneous bounded read/write clients,
busy-timeout handling, and bounded rejection. These tests use the real uvicorn
gateway, real bearer tokens, and the real tool routes; nothing bypasses policy.
Timing is reported for information only, never asserted.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import math
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2
import pytest
import tomli_w

from conftest import RunningGateway, free_port, wait_until
from knowlage_mcp.admin_client import AdminClient
from knowlage_mcp.config import (
    Config,
    IdentityConfig,
    LimitsConfig,
    LoggingConfig,
    ServerConfig,
    StorageConfig,
    resolve_paths,
)

pytestmark = pytest.mark.integration

CLIENTS = 50
WRITE_ITERATIONS = 20
RETRIEVE_ITERATIONS = 10


def _config(
    tmp_path: Path,
    runtime_dir: Path,
    *,
    requests_per_minute: int = 10000,
    request_burst: int = 1000,
) -> Config:
    """A gateway config whose limits make 50 identities a policy test."""
    data = tmp_path / "data"
    config = Config(
        server=ServerConfig(
            host="127.0.0.1",
            port=free_port(),
            allowed_hosts=["127.0.0.1", "localhost"],
            max_request_bytes=131072,
        ),
        storage=StorageConfig(
            database=data / "knowledge.sqlite3",
            evidence_dir=data / "evidence",
            backup_dir=data / "backups",
            runtime_socket=runtime_dir / "admin.sock",
        ),
        identity=IdentityConfig(max_sessions=256),
        limits=LimitsConfig(
            registration_per_minute_per_source=200,
            registration_per_minute_global=200,
            requests_per_minute=requests_per_minute,
            request_burst=request_burst,
            requests_per_minute_global=100000,
            request_burst_global=10000,
            candidate_writes_per_hour=1000,
            candidate_writes_per_hour_global=10000,
            evidence_object_bytes=1024,
        ),
        logging=LoggingConfig(level="WARNING"),
    )
    return resolve_paths(config)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _register(client: httpx2.Client, label: str) -> dict[str, Any]:
    response = client.post(
        "/v1/bridge/register",
        json={"mode": "root", "label": label, "model": "pytest", "provider": "local"},
    )
    assert response.status_code == 200, response.text
    registration = response.json()
    assert registration["session_token"]
    assert registration["agent_id"]
    return registration


def _register_many(base_url: str) -> list[dict[str, Any]]:
    with httpx2.Client(base_url=base_url, timeout=30.0) as client:
        return [_register(client, f"concurrency-{index:02d}") for index in range(CLIENTS)]


def _private_namespaces(admin: AdminClient, registrations: list[dict[str, Any]]) -> list[str]:
    """Create one private namespace per root and grant only that root access."""
    namespaces: list[str] = []
    for index, registration in enumerate(registrations):
        namespace = f"concurrency-{index:02d}"
        admin.call(
            "namespaces.create",
            {"name": namespace, "kind": "project", "description": "concurrency probe"},
        )
        admin.call(
            "grants.add",
            {"agent_id": registration["agent_id"], "namespace": namespace, "role": "contributor"},
        )
        namespaces.append(namespace)
    return namespaces


def _ok(response: httpx2.Response, *, label: str) -> dict[str, Any]:
    assert response.status_code == 200, f"{label}: HTTP {response.status_code}: {response.text[:300]}"
    body = response.json()
    assert body.get("ok") is True, f"{label}: {body}"
    return body["data"]


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(0, math.ceil(fraction * len(ordered)) - 1)
    return ordered[min(rank, len(ordered) - 1)]


@dataclass
class WorkerResult:
    latencies: list[float] = field(default_factory=list)
    entry_ids: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _read_or_write_client(
    index: int,
    base_url: str,
    token: str,
    namespace: str,
    barrier: threading.Barrier,
) -> WorkerResult:
    """Run the deterministic 60/30/10 mix for one independent identity."""
    result = WorkerResult()
    headers = _auth(token)
    written: list[str] = []

    def send(client: httpx2.Client, tool: str, payload: dict[str, Any], label: str) -> dict[str, Any]:
        started = time.perf_counter()
        response = client.post(f"/v1/tools/{tool}", headers=headers, json=payload)
        result.latencies.append((time.perf_counter() - started) * 1000.0)
        return _ok(response, label=label)

    try:
        with httpx2.Client(base_url=base_url, timeout=30.0) as client:
            barrier.wait(timeout=15.0)
            for iteration in range(WRITE_ITERATIONS):
                slot = iteration % 10
                if slot < 6:  # 60% bounded reads
                    if iteration % 2 == 0:
                        data = send(
                            client,
                            "search",
                            {
                                # No namespace filter: the ACL must keep every
                                # other client's private namespace invisible.
                                "query": "concurrency acceptance probe",
                                "max_bytes": 8000,
                            },
                            f"client {index} search {iteration}",
                        )
                        results = data["results"]
                        assert all(item["namespace"] == namespace for item in results), results
                        if iteration >= 10:
                            assert results, f"client {index} search {iteration} was empty"
                    else:
                        send(
                            client,
                            "get_context",
                            {"task": "concurrency acceptance probe"},
                            f"client {index} get_context {iteration}",
                        )
                elif slot < 9:  # 30% candidate writes, unique per client/iteration
                    claim = (
                        "concurrency acceptance probe "
                        f"client {index:02d} iteration {iteration:02d} serial {index * 100 + iteration}"
                    )
                    data = send(
                        client,
                        "record",
                        {
                            "namespace": namespace,
                            "entry": {
                                "kind": "observation",
                                "title": f"concurrency probe {index:02d} {iteration:02d}",
                                "claim": claim,
                                "tags": ["concurrency"],
                            },
                        },
                        f"client {index} record {iteration}",
                    )
                    entry_id = data["entry"]["entry_id"]
                    written.append(entry_id)
                    result.entry_ids.append(entry_id)
                else:  # 10% read back an id this client wrote
                    entry_id = written[(iteration // 10) * 3]
                    data = send(
                        client,
                        "get_entry",
                        {"entry_id": entry_id},
                        f"client {index} get_entry {iteration}",
                    )
                    assert data["entry_id"] == entry_id, data
                    assert data["namespace"] == namespace, data
    except BaseException as exc:
        result.errors.append(f"client {index}: {type(exc).__name__}: {exc}")
    return result


def _retrieve_own_writes(
    index: int,
    base_url: str,
    token: str,
    entry_ids: list[str],
) -> list[str]:
    """Read back every id the client wrote, over real HTTP with its own token."""
    errors: list[str] = []
    try:
        with httpx2.Client(base_url=base_url, timeout=30.0) as client:
            for entry_id in entry_ids:
                response = client.post(
                    "/v1/tools/get_entry",
                    headers=_auth(token),
                    json={"entry_id": entry_id},
                )
                if response.status_code != 200:
                    errors.append(f"client {index} get_entry {entry_id}: HTTP {response.status_code}")
                    continue
                data = response.json()["data"]
                if data["entry_id"] != entry_id:
                    errors.append(f"client {index} get_entry {entry_id} returned {data['entry_id']}")
    except BaseException as exc:
        errors.append(f"client {index}: {type(exc).__name__}: {exc}")
    return errors


def _refused_second_gateway(config: Config, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Start a second gateway process on the same database and require exit 3."""
    second = config.model_copy(deep=True)
    second.server.port = free_port()
    second.storage.runtime_socket = tmp_path / "second-admin.sock"
    config_path = tmp_path / "second-gateway.toml"
    config_path.write_text(tomli_w.dumps(second.to_toml_dict()), encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "knowlage_mcp.cli", "--config", str(config_path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@contextlib.contextmanager
def _process_gateway(config: Config, tmp_path: Path) -> Any:
    """Run the real CLI gateway process so it owns the database writer lock."""
    config_path = tmp_path / "gateway.toml"
    config_path.write_text(tomli_w.dumps(config.to_toml_dict()), encoding="utf-8")
    log_path = tmp_path / "gateway.log"
    log = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-m", "knowlage_mcp.cli", "--config", str(config_path)],
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=str(tmp_path),
    )
    base_url = f"http://{config.server.host}:{config.server.port}"

    def healthy() -> bool:
        if process.poll() is not None:
            return True
        try:
            return httpx2.get(f"{base_url}/v1/health", timeout=1.0).status_code == 200
        except httpx2.HTTPError:
            return False

    try:
        wait_until(healthy, timeout=20.0)
        assert process.poll() is None, f"gateway exited early:\n{log_path.read_text(encoding='utf-8')}"
        yield process
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        log.close()


def _read_only_database(config: Config) -> sqlite3.Connection:
    uri = f"file:{config.storage.database}?mode=ro"
    return sqlite3.connect(uri, uri=True)


@pytest.mark.slow
def test_fifty_concurrent_read_write_clients(
    tmp_path: Path,
    short_runtime_dir: Path,
) -> None:
    config = _config(tmp_path, short_runtime_dir)
    base_url = f"http://{config.server.host}:{config.server.port}"
    with _process_gateway(config, tmp_path):
        registrations = _register_many(base_url)
        assert len(registrations) == CLIENTS
        assert len({item["agent_id"] for item in registrations}) == CLIENTS
        tokens = [item["session_token"] for item in registrations]
        namespaces = _private_namespaces(
            AdminClient(config.storage.runtime_socket, timeout=30.0), registrations
        )

        barrier = threading.Barrier(CLIENTS + 1)
        with concurrent.futures.ThreadPoolExecutor(max_workers=CLIENTS) as pool:
            futures = [
                pool.submit(
                    _read_or_write_client,
                    index,
                    base_url,
                    tokens[index],
                    namespaces[index],
                    barrier,
                )
                for index in range(CLIENTS)
            ]
            with contextlib.suppress(threading.BrokenBarrierError):
                barrier.wait(timeout=15.0)
            # The first gateway process holds the writer lock while clients run.
            second = _refused_second_gateway(config, tmp_path)
            outcomes = [future.result(timeout=90.0) for future in futures]

        errors = [error for outcome in outcomes for error in outcome.errors]
        assert errors == []
        assert second.returncode == 3, (second.returncode, second.stdout, second.stderr)
        assert "database writer lock is already held" in second.stderr.lower(), second.stderr

        health = httpx2.get(f"{base_url}/v1/health", timeout=5.0)
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        with concurrent.futures.ThreadPoolExecutor(max_workers=CLIENTS) as pool:
            retrieve_futures = [
                pool.submit(
                    _retrieve_own_writes,
                    index,
                    base_url,
                    tokens[index],
                    outcomes[index].entry_ids,
                )
                for index in range(CLIENTS)
            ]
            retrieve_errors = [error for future in retrieve_futures for error in future.result(timeout=90.0)]
        assert retrieve_errors == []

        written_ids = [entry_id for outcome in outcomes for entry_id in outcome.entry_ids]
        assert len(written_ids) == CLIENTS * 6
        assert len(set(written_ids)) == len(written_ids)

        latencies = [latency for outcome in outcomes for latency in outcome.latencies]
        print(
            f"\n50-client read/write: calls={len(latencies)} "
            f"p50={_percentile(latencies, 0.50):.1f}ms p95={_percentile(latencies, 0.95):.1f}ms"
        )

    connection = _read_only_database(config)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        stored = {str(row[0]) for row in connection.execute("SELECT entry_id FROM entries").fetchall()}
    finally:
        connection.close()
    assert stored == set(written_ids)


def test_fifty_concurrent_readers_respect_byte_budget(
    gateway_factory: Any,
    tmp_path: Path,
    short_runtime_dir: Path,
) -> None:
    config = _config(tmp_path, short_runtime_dir)
    running: RunningGateway = gateway_factory(config)
    registrations = _register_many(running.base_url)
    tokens = [item["session_token"] for item in registrations]
    namespaces = _private_namespaces(AdminClient(running.admin_socket, timeout=30.0), registrations)

    # Seed published-looking candidate content, then measure reads only.
    with httpx2.Client(base_url=running.base_url, timeout=30.0) as client:
        for index, token in enumerate(tokens):
            for seed in range(3):
                _ok(
                    client.post(
                        "/v1/tools/record",
                        headers=_auth(token),
                        json={
                            "namespace": namespaces[index],
                            "entry": {
                                "kind": "observation",
                                "title": f"reader corpus {index:02d} {seed}",
                                "claim": (
                                    "concurrency reader corpus "
                                    f"client {index:02d} seed {seed} " + "bounded payload " * 80
                                ),
                                "tags": ["concurrency", "reader"],
                            },
                        },
                    ),
                    label=f"seed {index}/{seed}",
                )

    budget = 512
    barrier = threading.Barrier(CLIENTS + 1)
    errors: list[str] = []
    latencies: list[float] = []

    def reader(index: int) -> WorkerResult:
        result = WorkerResult()
        headers = _auth(tokens[index])
        try:
            with httpx2.Client(base_url=running.base_url, timeout=30.0) as client:
                barrier.wait(timeout=15.0)
                for iteration in range(RETRIEVE_ITERATIONS):
                    if iteration % 3 == 0:
                        payload = {"task": "concurrency reader corpus"}
                        tool = "get_context"
                    else:
                        payload = {
                            "query": "concurrency reader corpus",
                            "filters": {"namespace": namespaces[index]},
                            "max_bytes": budget,
                        }
                        tool = "search"
                    started = time.perf_counter()
                    response = client.post(f"/v1/tools/{tool}", headers=headers, json=payload)
                    result.latencies.append((time.perf_counter() - started) * 1000.0)
                    data = _ok(response, label=f"reader {index} {tool} {iteration}")
                    if tool == "search":
                        assert data["approx_bytes"] <= budget, data
                        assert data["approx_bytes"] <= config.limits.search_max_bytes
                        assert all(item["namespace"] == namespaces[index] for item in data["results"])
        except BaseException as exc:
            result.errors.append(f"reader {index}: {type(exc).__name__}: {exc}")
        return result

    with concurrent.futures.ThreadPoolExecutor(max_workers=CLIENTS) as pool:
        futures = [pool.submit(reader, index) for index in range(CLIENTS)]
        with contextlib.suppress(threading.BrokenBarrierError):
            barrier.wait(timeout=15.0)
        outcomes = [future.result(timeout=90.0) for future in futures]

    for outcome in outcomes:
        errors.extend(outcome.errors)
        latencies.extend(outcome.latencies)
    assert errors == []
    assert len(latencies) == CLIENTS * RETRIEVE_ITERATIONS
    print(
        f"\n50 concurrent readers: calls={len(latencies)} "
        f"p50={_percentile(latencies, 0.50):.1f}ms p95={_percentile(latencies, 0.95):.1f}ms"
    )


def test_request_rate_limit_rejects_excess_with_retry_after(
    gateway_factory: Any,
    tmp_path: Path,
    short_runtime_dir: Path,
) -> None:
    config = _config(tmp_path, short_runtime_dir, requests_per_minute=5, request_burst=5)
    running: RunningGateway = gateway_factory(config)
    registrations = _register_many(running.base_url)[:2]
    first, second = (item["session_token"] for item in registrations)

    with httpx2.Client(base_url=running.base_url, timeout=30.0) as client:
        responses = [
            client.post("/v1/tools/search", headers=_auth(first), json={"query": f"rate probe {index}"})
            for index in range(40)
        ]
        spare = client.post("/v1/tools/search", headers=_auth(second), json={"query": "spare capacity"})

    allowed = [response for response in responses if response.status_code == 200]
    rejected = [response for response in responses if response.status_code == 429]
    assert 1 <= len(allowed) <= 10, [response.status_code for response in responses]
    assert len(rejected) >= 20, [response.status_code for response in responses]
    assert len(allowed) + len(rejected) == len(responses)

    for response in responses:
        assert response.status_code < 500, response.text
        if response.status_code == 200:
            assert response.json()["ok"] is True
            continue
        assert response.status_code == 429, response.text
        body = response.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "RATE_LIMITED", body
        assert int(response.headers["Retry-After"]) >= 1
        assert int(body["error"]["retry_after_s"]) >= 1

    # Rate limiting is per session: the second identity still has its own budget.
    assert spare.status_code == 200, spare.text
    assert spare.json()["ok"] is True
