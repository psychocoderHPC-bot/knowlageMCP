# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Protocol version negotiation and compatibility matrix."""

from __future__ import annotations

import pytest

from knowlage_mcp import PROTOCOL_VERSION
from knowlage_mcp.protocol import version_compatible

_MAJOR = PROTOCOL_VERSION.partition(".")[0]


@pytest.mark.parametrize(
    ("client_version", "expected"),
    [
        (PROTOCOL_VERSION, True),
        (f"{_MAJOR}.0", True),
        (f"{_MAJOR}.999", True),
        (f"{int(_MAJOR) - 1}.0", False),
        (f"{int(_MAJOR) + 1}.0", False),
        ("", False),
        ("   ", False),
        ("not-a-version", False),
        ("1.x", False),
        ("1.0.x", False),
    ],
)
def test_version_compatibility_matrix(client_version: str, expected: bool) -> None:
    assert version_compatible(client_version) is expected
