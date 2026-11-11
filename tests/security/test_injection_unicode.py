# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Search operators are neutralised and hostile Unicode is rejected inertly."""

from __future__ import annotations

import httpx2
import pytest
from secutil import RunningGateway, auth, count_rows, register, table_names

from knowlage_mcp.retrieval import fts_query

pytestmark = pytest.mark.security

INJECTIONS = [
    'alpha" OR 1=1 --',
    "*",
    "NEAR(alpha beta, 5)",
    '" ; DROP TABLE entry_fts; --',
    "ＯＲ alpha",  # noqa: RUF001 - deliberate fullwidth NFKC trick
    "a" * 4096,
    "alpha	beta",
]


def test_fts_injection_is_inert(gateway_server: RunningGateway) -> None:
    database = gateway_server.config.storage.database
    with httpx2.Client(base_url=gateway_server.base_url) as client:
        token = register(client)["session_token"]
        headers = auth(token)
        record = client.post(
            "/v1/tools/record",
            headers=headers,
            json={
                "namespace": "shared",
                "entry": {"kind": "observation", "title": "inject", "claim": "INJECTCANARY alpha"},
            },
        )
        assert record.status_code == 200, record.text
        before_tables = table_names(database)
        before_entries = count_rows(database, "entries")
        for query in INJECTIONS:
            response = client.post("/v1/tools/search", headers=headers, json={"query": query})
            assert response.status_code == 200, (query, response.text)
            envelope = response.json()
            assert envelope["ok"] is True, (query, envelope)
            assert "syntax" not in response.text.lower()
            assert "drop table" not in response.text.lower()
        assert table_names(database) == before_tables
        assert count_rows(database, "entries") == before_entries


def test_fts_query_builder_never_emits_raw_syntax() -> None:
    for query in INJECTIONS:
        built = fts_query(query)
        if built is None:
            continue
        assert "NEAR(" not in built
        assert "--" not in built
        assert ";" not in built


def test_hostile_unicode_and_bounds_are_structured(gateway_server: RunningGateway) -> None:
    database = gateway_server.config.storage.database
    with httpx2.Client(base_url=gateway_server.base_url) as client:
        token = register(client)["session_token"]
        headers = auth(token)

        def send(entry: dict[str, object]) -> httpx2.Response:
            return client.post(
                "/v1/tools/record",
                headers=headers,
                json={"namespace": "shared", "entry": entry},
            )

        before = count_rows(database, "entries")
        rejects = [
            {"kind": "observation", "title": "bidi", "claim": "safe \u202e evil"},
            {"kind": "observation", "title": "zero", "claim": "zero\u200bwidth"},
            {"kind": "observation", "title": "nul", "claim": "nul\u0000byte"},
            {"kind": "observation", "title": "t" * 3000, "claim": "title too long"},
            {
                "kind": "observation",
                "title": "tags",
                "claim": "too many",
                "tags": [f"tag{i}" for i in range(33)],
            },
            {"kind": "observation", "title": "tag", "claim": "tag too long", "tags": ["y" * 80]},
        ]
        for entry in rejects:
            response = send(entry)
            assert response.status_code == 400, (entry["title"], response.text)
            assert response.json()["error"]["code"] == "INVALID_ARGUMENT"
        assert count_rows(database, "entries") == before

        invalid_utf8 = client.post(
            "/v1/tools/record",
            headers={**headers, "Content-Type": "application/json"},
            content=(
                b'{"namespace":"shared","entry":{"kind":"observation","title":"utf8","claim":"\xff\xfe"}}'
            ),
        )
        assert invalid_utf8.status_code == 400, invalid_utf8.text
        assert invalid_utf8.json()["error"]["code"] == "INVALID_ARGUMENT"
        assert count_rows(database, "entries") == before

        exact = send({"kind": "observation", "title": "boundary", "claim": "a" * 8192})
        assert exact.status_code == 200, exact.text
        assert len(exact.json()["data"]["entry"]["claim"]) == 8192
        assert count_rows(database, "entries") == before + 1

        over = send({"kind": "observation", "title": "boundary", "claim": "a" * 8193})
        assert over.status_code == 413, over.text
        assert over.json()["error"]["code"] == "BUDGET_EXCEEDED"
        assert count_rows(database, "entries") == before + 1
