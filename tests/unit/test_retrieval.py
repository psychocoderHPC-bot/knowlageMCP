# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""ACL-first retrieval, scope, ranking, budget, cursor, and detail tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import ServiceEnv, build_env
from knowlage_mcp.models import (
    EntryPayload,
    ErrorCode,
    Kind,
    Lifecycle,
    RecordRequest,
    Scope,
    SearchFilters,
    SearchRequest,
    ServiceError,
    SessionInfo,
)


def _record(
    env: ServiceEnv,
    session: SessionInfo,
    claim: str,
    *,
    title: str,
    scope: Scope | None = None,
):
    return env.service.record(
        session,
        RecordRequest(
            namespace="shared",
            entry=EntryPayload(kind=Kind.OBSERVATION, title=title, claim=claim, scope=scope or Scope()),
        ),
    )


def test_scope_filtering_excludes_inapplicable_entries(env: ServiceEnv) -> None:
    session = env.session(env.register())
    _record(env, session, "scopeprobe alpaka", title="a", scope=Scope(repository="alpaka"))
    _record(env, session, "scopeprobe picongpu", title="b", scope=Scope(repository="picongpu"))
    _record(env, session, "scopeprobe unknown", title="c")

    response = env.service.search(
        session,
        SearchRequest(query="scopeprobe", filters=SearchFilters(scope=Scope(repository="alpaka"))),
    )
    assert len(response.results) == 1
    assert response.results[0].title == "a"


def test_exact_phrase_and_identifier_lookup(env: ServiceEnv) -> None:
    session = env.session(env.register())
    _record(env, session, "error missing include vector", title="vector")
    _record(env, session, "error missing include header", title="header")
    _record(env, session, "fatal err_XYZ_123 in linker", title="identifier")

    phrase = env.service.search(
        session,
        SearchRequest(
            query="missing include vector",
            filters=SearchFilters(exact_phrase=True),
        ),
    )
    assert [result.title for result in phrase.results] == ["vector"]

    identifier = env.service.search(session, SearchRequest(query="err_XYZ_123"))
    assert [result.title for result in identifier.results] == ["identifier"]


def test_personal_namespace_is_not_readable_without_privilege(env: ServiceEnv) -> None:
    session = env.session(env.register())
    with pytest.raises(ServiceError) as excinfo:
        env.service.search(
            session, SearchRequest(query="anything", filters=SearchFilters(namespace="personal"))
        )
    assert excinfo.value.code is ErrorCode.FORBIDDEN


def test_empty_results_are_valid(env: ServiceEnv) -> None:
    session = env.session(env.register())
    response = env.service.search(session, SearchRequest(query="zzzzz-not-present"))
    assert response.results == ()
    assert response.total == 0
    assert response.truncated is False
    assert response.approx_bytes == 0


def test_candidates_are_labelled_low_trust(env: ServiceEnv) -> None:
    session = env.session(env.register())
    write = _record(env, session, "candidate probe claim", title="candidate")
    response = env.service.search(session, SearchRequest(query="candidate probe"))
    assert len(response.results) == 1
    result = response.results[0]
    assert result.entry_id == write.entry.entry_id
    assert result.low_trust is True
    assert result.lifecycle is Lifecycle.CANDIDATE
    assert any("low trust" in warning for warning in result.warnings)


def test_published_entries_rank_before_candidates(env: ServiceEnv) -> None:
    session = env.session(env.register())
    env.service.memory_add(namespace="shared", kind="observation", text="ranker probe published")
    _record(env, session, "ranker probe candidate", title="candidate")
    response = env.service.search(session, SearchRequest(query="ranker probe"))
    assert len(response.results) == 2
    assert response.results[0].lifecycle is Lifecycle.PUBLISHED
    assert response.results[1].lifecycle is Lifecycle.CANDIDATE


def test_byte_budget_drops_whole_results(env: ServiceEnv) -> None:
    session = env.session(env.register())
    for index in range(3):
        _record(env, session, "budgetprobe " + str(index) + " " + "y" * 1000, title=f"big-{index}")
    response = env.service.search(session, SearchRequest(query="budgetprobe", max_results=10, max_bytes=2000))
    assert len(response.results) == 1
    assert response.truncated is True
    assert response.approx_bytes <= 2000
    # The claim is never clipped.
    assert response.results[0].claim == "budgetprobe 0 " + "y" * 1000


def test_dedupe_shared_lineage_is_merged(env: ServiceEnv) -> None:
    session = env.session(env.register())
    _record(env, session, "lineageprobe identical claim", title="one")
    _record(env, session, "lineageprobe identical claim", title="two")
    response = env.service.search(session, SearchRequest(query="lineageprobe"))
    assert len(response.results) == 1
    assert any("duplicate lineage" in warning for warning in response.results[0].warnings)


def test_cursor_pagination(env: ServiceEnv) -> None:
    session = env.session(env.register())
    for index in range(3):
        env.service.memory_add(namespace="shared", kind="observation", text=f"pageprobe entry {index}")
    first = env.service.search(session, SearchRequest(query="pageprobe", max_results=2))
    assert len(first.results) == 2
    assert first.next_cursor is not None
    second = env.service.search(
        session, SearchRequest(query="pageprobe", max_results=2, cursor=first.next_cursor)
    )
    assert len(second.results) == 1
    assert second.next_cursor is None
    combined = {result.entry_id for result in first.results} | {result.entry_id for result in second.results}
    assert len(combined) == 3

    with pytest.raises(ServiceError):
        env.service.search(
            session,
            SearchRequest(query="different query", max_results=2, cursor=first.next_cursor),
        )


def test_selection_reasons_and_token_estimate(env: ServiceEnv) -> None:
    session = env.session(env.register())
    env.service.memory_add(namespace="shared", kind="observation", text="reasonprobe published hint")
    response = env.service.search(session, SearchRequest(query="reasonprobe hint"))
    assert len(response.results) == 1
    result = response.results[0]
    assert "published" in result.selection_reasons
    assert len(result.selection_reasons) <= 5
    assert "estimate" in response.token_estimate_method
    assert response.estimated_tokens > 0
    payload = json.dumps(response.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    assert response.estimated_tokens == (len(payload) + 3) // 4


def test_selection_reasons_include_scope_and_low_trust(env: ServiceEnv) -> None:
    session = env.session(env.register())
    _record(
        env,
        session,
        "scopereason probe",
        title="scoped",
        scope=Scope(repository="alpaka"),
    )
    response = env.service.search(
        session,
        SearchRequest(query="scopereason", filters=SearchFilters(scope=Scope(repository="alpaka"))),
    )
    assert len(response.results) == 1
    reasons = response.results[0].selection_reasons
    assert "scope matches query" in reasons
    assert "candidate low trust" in reasons
    assert len(reasons) <= 5


def test_get_context_returns_published_bundle(env: ServiceEnv) -> None:
    session = env.session(env.register())
    published = env.service.memory_add(
        namespace="shared", kind="observation", text="contextbundle published hint"
    )
    _record(env, session, "contextbundle candidate hint", title="candidate")
    bundle = env.service.get_context(session, "contextbundle hint", Scope())
    assert "shared" in bundle.namespaces
    assert [result.entry_id for result in bundle.results] == [published.entry.entry_id]
    assert bundle.approx_bytes > 0
    assert bundle.estimated_tokens > 0
    assert "estimate" in bundle.token_estimate_method
    payload = json.dumps(bundle.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    assert bundle.estimated_tokens == (len(payload) + 3) // 4


def test_get_entry_history_and_paging(tmp_path: Path) -> None:
    env = build_env(tmp_path, limits={"detail_page_bytes": 2000})
    try:
        session = env.session(env.register())
        write = env.service.memory_add(
            namespace="shared", kind="observation", text="detailprobe " + "z" * 300
        )
        entry_id = write.entry.entry_id
        revision = write.entry.revision_id
        for index in range(3):
            correction = env.service.memory_correct(
                entry_id, revision, f"detailprobe correction {index} " + "z" * 300
            )
            revision = correction.entry.revision_id
        detail = env.service.get_entry(session, entry_id)
        assert detail.entry_id == entry_id
        assert len(detail.history) < 4
        assert detail.approx_bytes <= 2000
        assert detail.next_cursor is not None

        page = env.service.get_entry(session, entry_id, cursor=detail.next_cursor)
        assert page.approx_bytes <= 2000
    finally:
        env.db.close()
