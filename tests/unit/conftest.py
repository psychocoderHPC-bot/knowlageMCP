# SPDX-FileCopyrightText: 2026 René Widerå
# SPDX-License-Identifier: ISC
"""Shared fixtures and builders for the service-layer unit tests."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from knowlage_mcp.config import (
    Config,
    IdentityConfig,
    LimitsConfig,
    RetrievalConfig,
    ReviewConfig,
    StorageConfig,
    resolve_paths,
)
from knowlage_mcp.db import Database
from knowlage_mcp.evidence import EvidenceStore
from knowlage_mcp.models import Registration, ReviewResult, SessionInfo, SubmitReviewRequest
from knowlage_mcp.service import KnowledgeService


class ManualClock:
    """Deterministic clock implementing both ``time`` and ``monotonic``."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class ServiceEnv:
    service: KnowledgeService
    db: Database
    evidence: EvidenceStore
    config: Config
    clock: ManualClock
    monotonic: ManualClock
    root: Path
    registrations: list[Registration] = field(default_factory=list)

    def register(
        self,
        *,
        mode: str = "root",
        ticket: str | None = None,
        label: str | None = None,
        source: str = "test",
    ) -> Registration:
        registration = self.service.register(mode=mode, ticket=ticket, label=label, source=source)
        self.registrations.append(registration)
        return registration

    def session(self, registration: Registration) -> SessionInfo:
        return self.service.authenticate(registration.session_token)


def build_env(
    tmp_path: Path,
    *,
    identity: dict[str, Any] | None = None,
    limits: dict[str, Any] | None = None,
    review: dict[str, Any] | None = None,
    retrieval: dict[str, Any] | None = None,
    bootstrap: bool = True,
) -> ServiceEnv:
    base = tmp_path / "data"
    config = Config(
        storage=StorageConfig(
            database=base / "knowledge.sqlite3",
            evidence_dir=base / "evidence",
            backup_dir=base / "backups",
        ),
        identity=IdentityConfig(**(identity or {})),
        limits=LimitsConfig(**(limits or {})),
        review=ReviewConfig(**(review or {})),
        retrieval=RetrievalConfig(**(retrieval or {})),
    )
    config = resolve_paths(config)
    database = Database(config.storage.database)
    database.migrate()
    evidence_store = EvidenceStore(
        config.storage.evidence_dir,
        quota_bytes=8 * 1024 * 1024,
        max_object_bytes=1024 * 1024,
    )
    clock = ManualClock()
    monotonic = ManualClock(start=1000.0)
    service = KnowledgeService(config, database, evidence_store, clock=clock, monotonic=monotonic)
    if bootstrap:
        service.bootstrap()
    return ServiceEnv(
        service=service,
        db=database,
        evidence=evidence_store,
        config=config,
        clock=clock,
        monotonic=monotonic,
        root=tmp_path,
    )


@pytest.fixture
def env(tmp_path: Path) -> Iterator[ServiceEnv]:
    built = build_env(tmp_path)
    try:
        yield built
    finally:
        built.db.close()


@pytest.fixture
def service(env: ServiceEnv) -> KnowledgeService:
    return env.service


def make_mentor(env: ServiceEnv, namespace: str = "shared") -> tuple[SessionInfo, Registration]:
    """Register an agent and approve a mentor grant bound to its session."""
    registration = env.register(label="mentor", source="mentor")
    session = env.session(registration)
    request = env.service.request_mentor(session, namespace, ["review", "publish"])
    env.service.approve(request.request_id, role="mentor", ttl=7200, namespace=namespace)
    refreshed = env.service.authenticate(registration.session_token)
    return refreshed, registration


def publish_with_mentor(env: ServiceEnv, entry_id: str, revision_id: str) -> None:
    """Review and publish one candidate revision as an eligible mentor."""
    mentor_session, _ = make_mentor(env)
    job = env.service.review_next(mentor_session)
    assert job is not None
    assert job.entry_id == entry_id
    env.service.submit_review(
        mentor_session,
        SubmitReviewRequest(
            entry_id=entry_id,
            revision_id=revision_id,
            result=ReviewResult.SUPPORTS,
            method="manual verification",
            evidence="ran the documented command",
            job_id=job.job_id,
            lease_id=job.lease_id,
        ),
    )
