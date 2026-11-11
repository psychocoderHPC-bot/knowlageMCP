# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Package constants for knowlageMCP.

The package exposes only stable constants here. Implementation lives in the
focused modules (``config``, ``models``, ``db``, ``protocol`` and the service
layers built on top of them).
"""

from __future__ import annotations

__version__ = "0.1.0"

#: Version of the bridge/MCP wire protocol implemented by the gateway.
PROTOCOL_VERSION = "1.0"

#: Canonical MCP tool names. Order is stable and mirrored in tests.
TOOL_NAMES: tuple[str, ...] = (
    "get_protocol_info",
    "register_agent",
    "delegate_child",
    "request_mentor",
    "enrollment_status",
    "get_context",
    "search",
    "get_entry",
    "record",
    "propose_revision",
    "record_outcome",
    "review_next",
    "submit_review",
    "session_close",
    "get_usage_skill",
)

#: Resource URI that serves the packaged agent skill.
SKILL_RESOURCE_URI = "knowlageMCP://skill"

#: Packaged skill resource file name.
SKILL_RESOURCE_NAME = "SKILL.md"

__all__ = [
    "PROTOCOL_VERSION",
    "SKILL_RESOURCE_NAME",
    "SKILL_RESOURCE_URI",
    "TOOL_NAMES",
    "__version__",
]
