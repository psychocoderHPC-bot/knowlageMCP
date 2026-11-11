# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Typed domain models shared by the service layer and every transport.

Request models forbid unknown fields and are frozen. Response models are also
frozen where they carry immutable facts; collection-valued fields use tuples so
that a frozen instance cannot be mutated through a list reference.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


UtcDatetime = Annotated[datetime, AfterValidator(_ensure_utc)]


class Kind(StrEnum):
    PREFERENCE = "preference"
    INSTRUCTION = "instruction"
    DECISION = "decision"
    OBSERVATION = "observation"
    HYPOTHESIS = "hypothesis"
    PROCEDURE = "procedure"
    FAILURE = "failure"
    TASK_SUMMARY = "task_summary"


class Lifecycle(StrEnum):
    CANDIDATE = "candidate"
    PUBLISHED = "published"
    DISPUTED = "disputed"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"


class EvidenceClass(StrEnum):
    UNVERIFIED = "unverified"
    SOURCE_CHECKED = "source_checked"
    REPRODUCED = "reproduced"
    OPERATOR_CONFIRMED = "operator_confirmed"


class NamespaceRole(StrEnum):
    READER = "reader"
    CONTRIBUTOR = "contributor"
    REVIEWER = "reviewer"
    MENTOR = "mentor"
    OPERATOR = "operator"


class ReviewResult(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    INCONCLUSIVE = "inconclusive"
    DUPLICATE = "duplicate"
    OUT_OF_SCOPE = "out_of_scope"


class ErrorCode(StrEnum):
    AUTH_REQUIRED = "AUTH_REQUIRED"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    APPROVAL_PENDING = "APPROVAL_PENDING"
    FORBIDDEN = "FORBIDDEN"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    REVISION_CONFLICT = "REVISION_CONFLICT"
    RATE_LIMITED = "RATE_LIMITED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    STORAGE_FULL = "STORAGE_FULL"
    REVIEW_INELIGIBLE = "REVIEW_INELIGIBLE"
    BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    INTERNAL = "INTERNAL"


class _StrictModel(BaseModel):
    """Base for request payloads: reject unknown keys, immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class _RequestModel(_StrictModel):
    """Marker base class for service request payloads."""


class _ResponseModel(BaseModel):
    """Base for service responses: reject unknown construction, frozen."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def _normalize_scope_value(value: str) -> str:
    return value.strip().casefold()


class Scope(_RequestModel):
    """Scope of a claim. Unspecified dimensions are unknown, not wildcards.

    ``matches`` is symmetric and conservative: a known value on either side
    never matches an unknown value on the other side. ``satisfies`` is the
    directional variant used by retrieval, where the instance describes a stored
    entry and ``required`` describes what a caller asked for; a requirement that
    names a dimension cannot be met by an entry whose value is unknown, while an
    unconstrained caller field imposes no restriction.
    """

    repository: str | None = None
    project: str | None = None
    version: str | None = None
    commit: str | None = None
    compiler: str | None = None
    backend: str | None = None
    host: str | None = None

    def is_empty(self) -> bool:
        return not any(
            getattr(self, field)
            for field in ("repository", "project", "version", "commit", "compiler", "backend", "host")
        )

    def matches(self, other: Scope) -> bool:
        for field in ("repository", "project", "version", "commit", "compiler", "backend", "host"):
            left = getattr(self, field)
            right = getattr(other, field)
            if left is None and right is None:
                continue
            if left is None or right is None:
                return False
            if _normalize_scope_value(left) != _normalize_scope_value(right):
                return False
        return True

    def satisfies(self, required: Scope) -> bool:
        for field in ("repository", "project", "version", "commit", "compiler", "backend", "host"):
            wanted = getattr(required, field)
            if wanted is None:
                continue
            actual = getattr(self, field)
            if actual is None:
                return False
            if _normalize_scope_value(actual) != _normalize_scope_value(wanted):
                return False
        return True


class SourceRef(_RequestModel):
    """A source attached to a revision; provenance is never inferred."""

    source_id: str | None = None
    kind: str = Field(min_length=1)
    role: str = "supporting"
    identity: str | None = None
    uri: str | None = None
    snapshot_hash: str | None = None
    trust: str = "agent_reported"
    detail: str | None = None
    created_at: UtcDatetime | None = None


class EvidenceRef(_RequestModel):
    """Reference to a bounded evidence object stored by the gateway."""

    evidence_id: str = Field(min_length=1)
    sha256: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    media_type: str | None = None
    name: str | None = None
    redacted: bool = False


class ProvenanceRef(_RequestModel):
    """A typed relation from the new revision to an existing revision."""

    to_revision_id: str = Field(min_length=1)
    relation: str = Field(min_length=1)


class EntryPayload(_RequestModel):
    """The content of one entry revision; identity and state are added later."""

    kind: Kind
    title: str = Field(min_length=1)
    claim: str = Field(min_length=1)
    conditions: str | None = None
    expected_result: str | None = None
    limitations: str | None = None
    tags: tuple[str, ...] = ()
    scope: Scope = Field(default_factory=Scope)
    observed_at: UtcDatetime = Field(default_factory=_utcnow)
    valid_from: UtcDatetime | None = None
    valid_until: UtcDatetime | None = None
    last_verified_at: UtcDatetime | None = None
    source_role: str = "agent_interpretation"
    evidence_class: EvidenceClass = EvidenceClass.UNVERIFIED
    sensitivity: str = "normal"
    lifecycle: Lifecycle | None = None
    sources: tuple[SourceRef, ...] = ()
    evidence: tuple[EvidenceRef, ...] = ()
    contributors: tuple[str, ...] = ()
    provenance: tuple[ProvenanceRef, ...] = ()

    @field_validator("tags")
    @classmethod
    def _strip_tags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned: list[str] = []
        for tag in value:
            normalized = tag.strip()
            if normalized and normalized not in cleaned:
                cleaned.append(normalized)
        return tuple(cleaned)

    @model_validator(mode="after")
    def _check_validity_window(self) -> Self:
        if (
            self.valid_from is not None
            and self.valid_until is not None
            and self.valid_from > self.valid_until
        ):
            raise ValueError("valid_from must not be after valid_until")
        return self


class RecordRequest(_RequestModel):
    namespace: str = Field(min_length=1)
    entry: EntryPayload
    idempotency_key: str | None = Field(default=None, max_length=256)


class ProposeRevisionRequest(_RequestModel):
    entry_id: str = Field(min_length=1)
    expected_revision: str = Field(min_length=1)
    entry: EntryPayload
    idempotency_key: str | None = Field(default=None, max_length=256)


class RecordOutcomeRequest(_RequestModel):
    outcome: str = Field(min_length=1)
    task_label: str | None = None
    revision_ids: tuple[str, ...] = ()
    notes: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=256)


class SearchFilters(_RequestModel):
    namespace: str | None = None
    kinds: tuple[Kind, ...] = ()
    lifecycle: tuple[Lifecycle, ...] = ()
    evidence_class: EvidenceClass | None = None
    tags: tuple[str, ...] = ()
    scope: Scope = Field(default_factory=Scope)
    include_candidates: bool | None = None
    author_agent_id: str | None = None
    exact_phrase: bool = False


class SearchRequest(_RequestModel):
    query: str
    filters: SearchFilters = Field(default_factory=SearchFilters)
    max_results: int | None = Field(default=None, ge=1)
    max_bytes: int | None = Field(default=None, ge=1)
    cursor: str | None = None


class SearchResult(_ResponseModel):
    entry_id: str
    revision_id: str
    namespace: str
    kind: Kind
    lifecycle: Lifecycle
    evidence_class: EvidenceClass
    title: str
    claim: str
    tags: tuple[str, ...] = ()
    scope: Scope = Field(default_factory=Scope)
    score: float = 0.0
    updated_at: UtcDatetime | None = None
    low_trust: bool = False
    warnings: tuple[str, ...] = ()
    contradictions: tuple[str, ...] = ()
    duplicate_of: str | None = None


class SearchResponse(_ResponseModel):
    results: tuple[SearchResult, ...] = ()
    total: int = 0
    truncated: bool = False
    next_cursor: str | None = None
    approx_bytes: int = 0


class ReviewView(_ResponseModel):
    review_id: str
    reviewer_agent_id: str
    result: ReviewResult
    method: str
    created_at: UtcDatetime | None = None


class RevisionSummary(_ResponseModel):
    revision_id: str
    lifecycle: Lifecycle
    evidence_class: EvidenceClass
    title: str
    author_agent_id: str
    created_at: UtcDatetime
    is_current: bool = False


class EntryDetail(_ResponseModel):
    entry_id: str
    namespace: str
    current_revision_id: str | None = None
    revision_id: str
    prev_revision_id: str | None = None
    kind: Kind
    lifecycle: Lifecycle
    evidence_class: EvidenceClass
    title: str
    claim: str
    conditions: str | None = None
    expected_result: str | None = None
    limitations: str | None = None
    tags: tuple[str, ...] = ()
    scope: Scope = Field(default_factory=Scope)
    observed_at: UtcDatetime
    valid_from: UtcDatetime | None = None
    valid_until: UtcDatetime | None = None
    last_verified_at: UtcDatetime | None = None
    sensitivity: str = "normal"
    author_agent_id: str
    author_root_id: str
    contributors: tuple[str, ...] = ()
    source_role: str = "agent_interpretation"
    content_hash: str
    created_at: UtcDatetime
    sources: tuple[SourceRef, ...] = ()
    evidence: tuple[EvidenceRef, ...] = ()
    reviews: tuple[ReviewView, ...] = ()
    history: tuple[RevisionSummary, ...] = ()
    warnings: tuple[str, ...] = ()
    contradictions: tuple[str, ...] = ()
    duplicate_of: str | None = None
    next_cursor: str | None = None
    approx_bytes: int = 0


class Grant(_ResponseModel):
    namespace: str
    role: NamespaceRole
    grant_id: str | None = None
    agent_id: str | None = None
    created_at: UtcDatetime | None = None
    expires_at: UtcDatetime | None = None
    revoked_at: UtcDatetime | None = None


class Registration(_ResponseModel):
    agent_id: str
    root_agent_id: str
    session_id: str
    actor_handle: str
    session_token: str = Field(repr=False)
    expires_at: UtcDatetime
    grants: tuple[Grant, ...] = ()


class Delegation(_ResponseModel):
    ticket: str
    parent_agent_id: str
    root_agent_id: str
    expires_at: UtcDatetime


class MentorRequest(_ResponseModel):
    request_id: str
    code: str
    state: str
    namespace: str
    scopes: tuple[str, ...] = ()
    expires_at: UtcDatetime
    approved_at: UtcDatetime | None = None


class ReviewJob(_ResponseModel):
    job_id: str
    entry_id: str
    revision_id: str
    state: str
    priority: float = 0.0
    created_at: UtcDatetime
    attempts: int = 0
    lease_owner: str | None = None
    lease_id: str | None = None
    lease_expires_at: UtcDatetime | None = None
    namespace: str | None = None
    kind: Kind | None = None
    title: str | None = None
    claim: str | None = None
    scope: Scope = Field(default_factory=Scope)


class ReviewFilters(_RequestModel):
    namespace: str | None = None
    kinds: tuple[Kind, ...] = ()
    tags: tuple[str, ...] = ()
    scope: Scope = Field(default_factory=Scope)


class SubmitReviewRequest(_RequestModel):
    entry_id: str = Field(min_length=1)
    revision_id: str = Field(min_length=1)
    result: ReviewResult
    method: str = Field(min_length=1)
    job_id: str | None = None
    lease_id: str | None = None
    evidence: str | None = None
    conditions: str | None = None
    limitations: str | None = None
    evidence_class: EvidenceClass | None = None
    idempotency_key: str | None = Field(default=None, max_length=256)


class SubmitReviewResult(_ResponseModel):
    review_id: str
    entry_id: str
    revision_id: str
    lifecycle: Lifecycle
    evidence_class: EvidenceClass
    published: bool = False
    job_state: str = "done"
    entry_current_revision_id: str | None = None


class WriteResult(_ResponseModel):
    entry: EntryDetail
    duplicate: bool = False
    idempotent: bool = False
    created: bool = True


class ContextBundle(_ResponseModel):
    results: tuple[SearchResult, ...] = ()
    namespaces: tuple[str, ...] = ()
    approx_bytes: int = 0
    truncated: bool = False


class TaskOutcome(_ResponseModel):
    outcome_id: str
    agent_id: str
    session_id: str
    task_label: str | None = None
    outcome: str
    revision_ids: tuple[str, ...] = ()
    notes: str | None = None
    created_at: UtcDatetime


class ProtocolInfo(_ResponseModel):
    name: str = "knowlageMCP"
    version: str
    protocol_version: str
    schema_version: int
    tool_names: tuple[str, ...] = ()
    resource_uri: str | None = None
    features: tuple[str, ...] = ()


class SessionInfo(_ResponseModel):
    session_id: str
    agent_id: str
    root_agent_id: str
    actor_handle: str
    created_at: UtcDatetime
    last_seen_at: UtcDatetime
    idle_expires_at: UtcDatetime
    absolute_expires_at: UtcDatetime
    revoked_at: UtcDatetime | None = None
    grants: tuple[Grant, ...] = ()


class Namespace(_ResponseModel):
    name: str = Field(min_length=1)
    kind: str = "shared"
    description: str | None = None
    created_at: UtcDatetime | None = None


class AdminEntryView(_ResponseModel):
    entry_id: str
    namespace: str
    current_revision_id: str | None = None
    lifecycle: Lifecycle
    evidence_class: EvidenceClass
    title: str
    claim: str
    scope: Scope = Field(default_factory=Scope)
    author_agent_id: str
    created_at: UtcDatetime
    updated_at: UtcDatetime | None = None
    sources: tuple[SourceRef, ...] = ()
    evidence: tuple[EvidenceRef, ...] = ()
    reviews: tuple[ReviewView, ...] = ()


class ServiceError(Exception):
    """Transport-independent service failure.

    Every service method raises this; transports map it to an envelope. The
    message is safe to show to a client and must never contain a token or a
    host path.
    """

    def __init__(
        self,
        code: ErrorCode | str,
        message: str,
        retry_after_s: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if isinstance(code, ErrorCode):
            normalized = code
        else:
            try:
                normalized = ErrorCode(code)
            except ValueError:
                normalized = ErrorCode.INTERNAL
        super().__init__(message)
        self.code: ErrorCode = normalized
        self.message = message
        self.retry_after_s = retry_after_s
        self.details = details

    def __str__(self) -> str:
        return f"{self.code.value}: {self.message}"

    def to_envelope(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code.value, "message": self.message}
        if self.retry_after_s is not None:
            error["retry_after_s"] = self.retry_after_s
        if self.details:
            error["details"] = self.details
        return {"ok": False, "error": error}


__all__ = [
    "AdminEntryView",
    "ContextBundle",
    "Delegation",
    "EntryDetail",
    "EntryPayload",
    "ErrorCode",
    "EvidenceClass",
    "EvidenceRef",
    "Grant",
    "Kind",
    "Lifecycle",
    "MentorRequest",
    "Namespace",
    "NamespaceRole",
    "ProposeRevisionRequest",
    "ProtocolInfo",
    "ProvenanceRef",
    "RecordOutcomeRequest",
    "RecordRequest",
    "Registration",
    "ReviewFilters",
    "ReviewJob",
    "ReviewResult",
    "ReviewView",
    "RevisionSummary",
    "Scope",
    "SearchFilters",
    "SearchRequest",
    "SearchResponse",
    "SearchResult",
    "ServiceError",
    "SessionInfo",
    "SourceRef",
    "SubmitReviewRequest",
    "SubmitReviewResult",
    "TaskOutcome",
    "UtcDatetime",
    "WriteResult",
]
