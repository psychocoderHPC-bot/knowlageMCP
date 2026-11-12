# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Configuration schema, defaults, path resolution, and safe TOML I/O.

Durations are accepted as integer seconds or as strings such as ``30s``,
``10m``, ``8h`` or ``24h`` and are stored internally as integer seconds. Paths
are resolved to absolute paths by :func:`resolve_paths`; nothing here creates
directories at import time.
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
import tomllib
import warnings
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit

import platformdirs
import tomli_w
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, field_validator

#: Application directory name used for XDG/platformdirs lookups.
APP_NAME = "knowlageMCP"

#: Keys whose values must never appear in a rendered configuration.
SECRET_KEY_MARKERS = ("token", "secret", "password", "api_key", "apikey", "private_key")

_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhdwSMHDW])?\s*$")
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}

_IDENTITY_DURATION_FIELDS = (
    "session_idle_timeout",
    "session_absolute_lifetime",
    "mentor_request_lifetime",
    "mentor_approved_lifetime",
    "child_ticket_lifetime",
)


def parse_duration(value: str | int) -> int:
    """Parse a duration into integer seconds.

    ``int`` values are seconds. Strings may be a bare integer or an integer
    followed by one of ``s``, ``m``, ``h``, ``d``, ``w`` (case-insensitive).
    """
    if isinstance(value, bool):
        raise ValueError("duration must not be a boolean")
    if isinstance(value, int):
        if value < 0:
            raise ValueError("duration must not be negative")
        return value
    if not isinstance(value, str):
        raise ValueError(f"unsupported duration type: {type(value).__name__}")
    match = _DURATION_RE.match(value)
    if match is None:
        raise ValueError(f"invalid duration: {value!r}")
    amount = int(match.group(1))
    unit = (match.group(2) or "s").lower()
    return amount * _DURATION_UNITS[unit]


def format_duration(seconds: int) -> str:
    """Render integer seconds in the largest exact unit, otherwise ``<n>s``."""
    if seconds < 0:
        raise ValueError("duration must not be negative")
    for unit, factor in (("h", 3600), ("m", 60), ("s", 1)):
        if seconds % factor == 0:
            return f"{seconds // factor}{unit}"
    return f"{seconds}s"


def _coerce_duration(value: object) -> int:
    if isinstance(value, (int, str)) and not isinstance(value, bool):
        return parse_duration(value)
    raise ValueError("duration must be an integer number of seconds or a string such as '8h'")


DurationSeconds = Annotated[int, BeforeValidator(_coerce_duration)]


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class ServerConfig(_Section):
    host: str = "127.0.0.1"
    port: int = 2230
    allowed_hosts: list[str] = Field(
        default_factory=lambda: ["127.0.0.1", "localhost", "host.containers.internal"]
    )
    allowed_origins: list[str] = Field(default_factory=list)
    max_request_bytes: int = 131072
    #: Return the session token from a direct ``/mcp`` ``register_agent`` call.
    #: Off by default: the stdio bridge owns the credential and direct MCP
    #: clients receive a ``bridge_required`` handshake hint instead.
    direct_mcp_registration_token: bool = False


class StorageConfig(_Section):
    database: Path = Field(default_factory=lambda: data_dir() / "knowledge.sqlite3")
    evidence_dir: Path = Field(default_factory=lambda: data_dir() / "evidence")
    backup_dir: Path = Field(default_factory=lambda: data_dir() / "backups")
    #: Private Unix-domain socket used by ``gatewayctl``. It never listens on TCP.
    runtime_socket: Path = Field(default_factory=lambda: runtime_dir() / "admin.sock")
    evidence_quota_bytes: int = 1073741824
    database_soft_limit_bytes: int = 1073741824


class IdentityConfig(_Section):
    initial_namespace: str = "shared"
    personal_namespace: str = "personal"
    allow_self_registration: bool = True
    session_idle_timeout: DurationSeconds = 28800
    session_absolute_lifetime: DurationSeconds = 86400
    mentor_request_lifetime: DurationSeconds = 600
    mentor_approved_lifetime: DurationSeconds = 7200
    child_ticket_lifetime: DurationSeconds = 300
    max_sessions: int = 256


class LimitsConfig(_Section):
    registration_per_minute_per_source: int = 10
    registration_per_minute_global: int = 60
    requests_per_minute: int = 60
    request_burst: int = 20
    #: Process-wide bound across all sessions. Admin socket calls do not use it.
    requests_per_minute_global: int = 6000
    request_burst_global: int = 1000
    candidate_writes_per_hour: int = 20
    candidate_writes_per_hour_global: int = 200
    search_default_results: int = 5
    search_max_results: int = 10
    search_default_bytes: int = 4000
    search_max_bytes: int = 8000
    detail_page_bytes: int = 12000
    claim_body_bytes: int = 8192
    evidence_object_bytes: int = 262144


class ReviewConfig(_Section):
    lease_seconds: int = 600
    max_attempts: int = 3
    mentor_items_per_session: int = 20


class RetrievalConfig(_Section):
    include_candidates: bool = True
    backend: str = "fts5"


class BackupConfig(_Section):
    daily: bool = True
    keep_daily: int = 7
    keep_weekly: int = 4


class LoggingConfig(_Section):
    level: str = "INFO"


#: Namespace kinds whose text must not leave the host without explicit consent.
PRIVATE_NAMESPACE_KINDS = frozenset({"personal", "private", "secret"})

#: Environment variable names are referenced, never their values.
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_EMBEDDING_PROVIDERS = ("none", "hash", "openai")
_VECTOR_BACKENDS = ("none", "qdrant")
_RERANKER_PROVIDERS = ("none", "lexical", "openai")
_DISTANCES = ("Cosine", "Euclid", "Dot")


def _normalize_distance(value: object) -> object:
    if not isinstance(value, str):
        return value
    normalized = value.strip().casefold()
    aliases = {"cosine": "Cosine", "euclid": "Euclid", "euclidean": "Euclid", "dot": "Dot"}
    return aliases.get(normalized, value)


def _is_http_url(value: str) -> bool:
    """Return whether ``value`` is an absolute http(s) URL with a host.

    Userinfo (``user:password@``) is rejected as well: credentials belong in the
    named environment variable, never in a URL that could be logged or shown.
    """
    if not value or any(character.isspace() for character in value):
        return False
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return False
    if parsed.scheme.lower() not in {"http", "https"}:
        return False
    if not parsed.netloc or not parsed.hostname:
        return False
    return parsed.username is None and parsed.password is None


class EmbeddingIntegrationConfig(_Section):
    """Text embedding provider. ``provider='none'`` performs no work at all."""

    provider: Literal["none", "hash", "openai"] = "none"
    model: str = "text-embedding-3-small"
    base_url: str = ""
    api_key_env: str = "KNOWLAGEMCP_EMBEDDING_API_KEY"
    dimensions: int = 1536
    batch_size: int = 16
    timeout_s: float = 20.0
    #: Must be true before personal/private text is sent to a remote provider.
    allow_private_text: bool = False

    @field_validator("provider", mode="before")
    @classmethod
    def _normalize_provider(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    def embedding_may_send(self, namespace_kind: str) -> bool:
        """Return whether text of ``namespace_kind`` may reach this provider.

        Only a remote ``openai`` provider can export text. Local providers
        (``none`` and ``hash``) always stay on the host.
        """
        if self.provider != "openai":
            return True
        return namespace_kind.strip().casefold() not in PRIVATE_NAMESPACE_KINDS or self.allow_private_text


class VectorIntegrationConfig(_Section):
    """Derived vector index. The index is never authoritative for ACL or review."""

    backend: Literal["none", "qdrant"] = "none"
    url: str = "http://127.0.0.1:6333"
    api_key_env: str = "KNOWLAGEMCP_QDRANT_API_KEY"
    collection_prefix: str = "knowlagemcp"
    vector_size: int = 1536
    distance: Literal["Cosine", "Euclid", "Dot"] = "Cosine"
    timeout_s: float = 10.0
    top_k: int = 40
    hybrid: bool = True

    @field_validator("backend", mode="before")
    @classmethod
    def _normalize_backend(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("distance", mode="before")
    @classmethod
    def _normalize_distance_value(cls, value: object) -> object:
        return _normalize_distance(value)


class RerankerIntegrationConfig(_Section):
    """Optional bounded reranker over already ACL-filtered candidates."""

    provider: Literal["none", "lexical", "openai"] = "none"
    model: str = "rerank-1"
    base_url: str = ""
    api_key_env: str = "KNOWLAGEMCP_RERANK_API_KEY"
    top_n: int = 20
    timeout_s: float = 10.0

    @field_validator("provider", mode="before")
    @classmethod
    def _normalize_provider(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value


class GraphitiIntegrationConfig(_Section):
    """Temporal graph extraction proposals. Disabled by default."""

    enabled: bool = False
    base_url: str = "http://127.0.0.1:8000"
    api_key_env: str = "KNOWLAGEMCP_GRAPHITI_API_KEY"
    timeout_s: float = 20.0
    max_calls_per_task: int = 4


class Mem0IntegrationConfig(_Section):
    """Conversation-to-memory extraction proposals. Disabled by default."""

    enabled: bool = False
    base_url: str = "http://127.0.0.1:8001"
    api_key_env: str = "KNOWLAGEMCP_MEM0_API_KEY"
    timeout_s: float = 20.0
    max_items: int = 20


class IntegrationsConfig(_Section):
    """Optional semantic/vector layer. Every adapter is off by default."""

    enabled: bool = False
    inference: bool = False
    telemetry: bool = False
    embedding: EmbeddingIntegrationConfig = Field(default_factory=EmbeddingIntegrationConfig)
    vector: VectorIntegrationConfig = Field(default_factory=VectorIntegrationConfig)
    reranker: RerankerIntegrationConfig = Field(default_factory=RerankerIntegrationConfig)
    graphiti: GraphitiIntegrationConfig = Field(default_factory=GraphitiIntegrationConfig)
    mem0: Mem0IntegrationConfig = Field(default_factory=Mem0IntegrationConfig)

    def embedding_may_send(self, namespace_kind: str) -> bool:
        """Delegate to :meth:`EmbeddingIntegrationConfig.embedding_may_send`."""
        return self.embedding.embedding_may_send(namespace_kind)


def _normalize_legacy_integrations(data: dict[str, Any]) -> dict[str, Any]:
    """Translate the removed boolean ``integrations.embeddings`` key.

    ``false`` selects ``provider='none'`` and ``true`` selects the local
    ``hash`` provider. An explicit ``integrations.embedding.provider`` always
    wins, so a partially migrated file keeps its intent.
    """
    raw = data.get("integrations")
    if not isinstance(raw, Mapping):
        return data
    integrations = dict(raw)
    if "embeddings" not in integrations:
        return data
    legacy = integrations.pop("embeddings")
    if not isinstance(legacy, bool):
        raise ValueError("integrations.embeddings must be a boolean; use [integrations.embedding] instead")
    embedding = integrations.get("embedding")
    embedding_table = dict(embedding) if isinstance(embedding, Mapping) else {}
    if "provider" not in embedding_table:
        embedding_table["provider"] = "hash" if legacy else "none"
    integrations["embedding"] = embedding_table
    data["integrations"] = integrations
    return data


class Config(BaseModel):
    """Root configuration document. One nested model per TOML table."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    server: ServerConfig = Field(default_factory=ServerConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    identity: IdentityConfig = Field(default_factory=IdentityConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    backup: BackupConfig = Field(default_factory=BackupConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    integrations: IntegrationsConfig = Field(default_factory=IntegrationsConfig)

    def to_toml_dict(self) -> dict[str, Any]:
        """Return a TOML-serializable mapping with readable durations and paths."""
        data: dict[str, Any] = self.model_dump()
        for key in ("database", "evidence_dir", "backup_dir", "runtime_socket"):
            data["storage"][key] = str(getattr(self.storage, key))
        for key in _IDENTITY_DURATION_FIELDS:
            data["identity"][key] = format_duration(int(data["identity"][key]))
        return data

    @classmethod
    def from_toml_dict(cls, data: Mapping[str, Any]) -> Self:
        return cls.model_validate(_normalize_legacy_integrations(dict(data)))

    @classmethod
    def from_toml_text(cls, text: str) -> Self:
        parsed = tomllib.loads(text)
        return cls.from_toml_dict(parsed)


def config_dir() -> Path:
    return Path(platformdirs.user_config_dir(APP_NAME, appauthor=False))


def data_dir() -> Path:
    return Path(platformdirs.user_data_dir(APP_NAME, appauthor=False))


def runtime_dir() -> Path:
    """Return the private per-user runtime directory (never created here)."""
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / APP_NAME
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            return Path(platformdirs.user_runtime_dir(APP_NAME, appauthor=False, ensure_exists=False))
        except Exception:  # pragma: no cover - platform fallback
            return Path(tempfile.gettempdir()) / f"{APP_NAME}-{os.getuid()}"


def ensure_private_dir(path: Path) -> Path:
    """Create a directory with mode 0700 if needed and return it."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        path.chmod(0o700)
    return path


def resolve_paths(config: Config) -> Config:
    """Return a copy whose storage paths are absolute and expanded."""
    resolved = config.model_copy(deep=True)
    for key in ("database", "evidence_dir", "backup_dir", "runtime_socket"):
        raw = getattr(resolved.storage, key)
        expanded = Path(raw).expanduser()
        if not expanded.is_absolute():
            expanded = Path.cwd() / expanded
        setattr(resolved.storage, key, expanded.resolve())
    return resolved


def default_config() -> Config:
    """Return the built-in defaults with resolved paths; creates nothing."""
    return resolve_paths(Config())


def load_config(path: Path | None = None) -> Config:
    """Load a configuration file.

    With no explicit path, a missing default file yields the built-in defaults.
    An explicitly requested but missing file raises :class:`FileNotFoundError`.
    """
    if path is None:
        candidate = config_dir() / "config.toml"
        if not candidate.exists():
            return default_config()
    else:
        candidate = Path(path)
        if not candidate.exists():
            raise FileNotFoundError(candidate)
    with candidate.open("rb") as handle:
        parsed = tomllib.load(handle)
    return resolve_paths(Config.from_toml_dict(parsed))


def validate_config(config: Config) -> list[str]:
    """Return human-readable problems; an empty list means the config is valid."""
    problems: list[str] = []

    def check(condition: bool, message: str) -> None:
        if not condition:
            problems.append(message)

    server = config.server
    check(1 <= server.port <= 65535, "server.port must be between 1 and 65535")
    check(1 <= server.max_request_bytes <= 16 * 1024 * 1024, "server.max_request_bytes must be 1..16 MiB")
    check(bool(server.allowed_hosts), "server.allowed_hosts must not be empty")
    check("*" not in server.allowed_hosts, "server.allowed_hosts must not contain '*'")
    check("*" not in server.allowed_origins, "server.allowed_origins must not contain '*'")

    storage = config.storage
    for key in ("database", "evidence_dir", "backup_dir", "runtime_socket"):
        value = getattr(storage, key)
        check(value.is_absolute(), f"storage.{key} must be an absolute path")
    # The database and the admin socket are files; evidence and backups are
    # directories that legitimately exist after the first bootstrap.
    check(not storage.database.is_dir(), "storage.database must not point at an existing directory")
    check(not storage.runtime_socket.is_dir(), "storage.runtime_socket must not be a directory")
    for key in ("evidence_dir", "backup_dir"):
        value = getattr(storage, key)
        check(not value.is_file(), f"storage.{key} must not point at an existing file")
    check(storage.evidence_dir != storage.backup_dir, "storage.evidence_dir and backup_dir must differ")
    check(storage.evidence_quota_bytes > 0, "storage.evidence_quota_bytes must be positive")
    check(storage.database_soft_limit_bytes > 0, "storage.database_soft_limit_bytes must be positive")

    identity = config.identity
    check(bool(identity.initial_namespace), "identity.initial_namespace must not be empty")
    check(bool(identity.personal_namespace), "identity.personal_namespace must not be empty")
    check(
        identity.initial_namespace != identity.personal_namespace,
        "identity.initial_namespace and personal_namespace must differ",
    )
    for key in _IDENTITY_DURATION_FIELDS:
        check(getattr(identity, key) > 0, f"identity.{key} must be positive")
    check(
        identity.session_idle_timeout <= identity.session_absolute_lifetime,
        "identity.session_idle_timeout must not exceed session_absolute_lifetime",
    )
    check(identity.max_sessions > 0, "identity.max_sessions must be positive")

    limits = config.limits
    for key in (
        "registration_per_minute_per_source",
        "registration_per_minute_global",
        "requests_per_minute",
        "request_burst",
        "requests_per_minute_global",
        "request_burst_global",
        "search_default_results",
        "search_max_results",
        "search_default_bytes",
        "search_max_bytes",
        "detail_page_bytes",
        "claim_body_bytes",
        "evidence_object_bytes",
    ):
        check(getattr(limits, key) > 0, f"limits.{key} must be positive")
    check(
        limits.candidate_writes_per_hour >= 0 and limits.candidate_writes_per_hour_global >= 0,
        "limits candidate write budgets must not be negative",
    )
    check(
        limits.search_default_results <= limits.search_max_results,
        "limits.search_default_results must not exceed search_max_results",
    )
    check(
        limits.search_max_results <= 10,
        "limits.search_max_results must be at most the documented hard maximum 10",
    )
    check(
        limits.search_default_bytes <= limits.search_max_bytes,
        "limits.search_default_bytes must not exceed search_max_bytes",
    )
    check(
        limits.search_max_bytes <= 8000,
        "limits.search_max_bytes must be at most the documented hard maximum 8000",
    )
    check(
        limits.evidence_object_bytes <= 16 * 1024 * 1024,
        "limits.evidence_object_bytes must be at most 16 MiB",
    )

    review = config.review
    check(review.lease_seconds > 0, "review.lease_seconds must be positive")
    check(review.max_attempts >= 1, "review.max_attempts must be at least 1")
    check(review.mentor_items_per_session >= 0, "review.mentor_items_per_session must not be negative")

    check(config.retrieval.backend == "fts5", "retrieval.backend must be 'fts5' in this release")
    check(config.backup.keep_daily >= 0, "backup.keep_daily must not be negative")
    check(config.backup.keep_weekly >= 0, "backup.keep_weekly must not be negative")
    check(
        config.logging.level.upper() in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"},
        "logging.level must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL",
    )

    integrations = config.integrations
    embedding = integrations.embedding
    check(embedding.provider in _EMBEDDING_PROVIDERS, "integrations.embedding.provider is not supported")
    check(embedding.dimensions > 0, "integrations.embedding.dimensions must be positive")
    check(embedding.batch_size > 0, "integrations.embedding.batch_size must be positive")
    check(embedding.timeout_s > 0, "integrations.embedding.timeout_s must be positive")
    check(bool(embedding.model), "integrations.embedding.model must not be empty")
    check(
        bool(_ENV_NAME_RE.match(embedding.api_key_env)),
        "integrations.embedding.api_key_env must be an environment variable name",
    )
    if embedding.provider == "openai":
        check(
            _is_http_url(embedding.base_url),
            "integrations.embedding.base_url must be an http(s) URL for provider 'openai'",
        )

    vector = integrations.vector
    check(vector.backend in _VECTOR_BACKENDS, "integrations.vector.backend is not supported")
    check(vector.vector_size > 0, "integrations.vector.vector_size must be positive")
    check(vector.distance in _DISTANCES, "integrations.vector.distance must be Cosine, Euclid, or Dot")
    check(vector.timeout_s > 0, "integrations.vector.timeout_s must be positive")
    check(vector.top_k > 0, "integrations.vector.top_k must be positive")
    check(bool(vector.collection_prefix), "integrations.vector.collection_prefix must not be empty")
    check(
        bool(_ENV_NAME_RE.match(vector.api_key_env)),
        "integrations.vector.api_key_env must be an environment variable name",
    )
    if vector.backend == "qdrant":
        check(
            _is_http_url(vector.url),
            "integrations.vector.url must be an http(s) URL for backend 'qdrant'",
        )
        check(
            embedding.provider in {"hash", "openai"},
            "integrations.vector.backend 'qdrant' requires integrations.embedding.provider "
            "to be 'hash' or 'openai'; 'none' cannot produce vectors",
        )
        if embedding.provider in {"hash", "openai"}:
            check(
                vector.vector_size == embedding.dimensions,
                "integrations.vector.vector_size must equal integrations.embedding.dimensions",
            )

    reranker = integrations.reranker
    check(reranker.provider in _RERANKER_PROVIDERS, "integrations.reranker.provider is not supported")
    check(reranker.top_n > 0, "integrations.reranker.top_n must be positive")
    check(reranker.timeout_s > 0, "integrations.reranker.timeout_s must be positive")
    check(
        bool(_ENV_NAME_RE.match(reranker.api_key_env)),
        "integrations.reranker.api_key_env must be an environment variable name",
    )
    if reranker.provider == "openai":
        check(bool(reranker.model), "integrations.reranker.model must not be empty")
        check(
            _is_http_url(reranker.base_url),
            "integrations.reranker.base_url must be an http(s) URL for provider 'openai'",
        )

    graphiti = integrations.graphiti
    check(graphiti.timeout_s > 0, "integrations.graphiti.timeout_s must be positive")
    check(graphiti.max_calls_per_task > 0, "integrations.graphiti.max_calls_per_task must be positive")
    check(
        bool(_ENV_NAME_RE.match(graphiti.api_key_env)),
        "integrations.graphiti.api_key_env must be an environment variable name",
    )
    if graphiti.enabled:
        check(
            _is_http_url(graphiti.base_url),
            "integrations.graphiti.base_url must be an http(s) URL when enabled",
        )

    mem0 = integrations.mem0
    check(mem0.timeout_s > 0, "integrations.mem0.timeout_s must be positive")
    check(mem0.max_items > 0, "integrations.mem0.max_items must be positive")
    check(
        bool(_ENV_NAME_RE.match(mem0.api_key_env)),
        "integrations.mem0.api_key_env must be an environment variable name",
    )
    if mem0.enabled:
        check(
            _is_http_url(mem0.base_url),
            "integrations.mem0.base_url must be an http(s) URL when enabled",
        )
    check(
        not (graphiti.enabled and mem0.enabled),
        "integrations.graphiti.enabled and integrations.mem0.enabled are mutually exclusive",
    )
    return problems


def _toml_text(config: Config) -> str:
    return tomli_w.dumps(config.to_toml_dict())


def write_config_atomic(path: Path, config: Config) -> Path:
    """Write ``config`` as TOML to ``path`` atomically with mode 0600.

    Serialization happens before the target directory is touched, so a
    serialization failure leaves any existing file byte-identical.
    """
    content = _toml_text(config)
    target = Path(path)
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_name = tempfile.mkstemp(dir=parent, prefix=".config-", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_path)
        raise
    return target


_URL_USERINFO_RE = re.compile(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)[^/@\s]+@")


def _redact_url_credentials(value: str) -> str:
    """Mask ``user:password@`` userinfo in an absolute URL-like string."""
    match = _URL_USERINFO_RE.match(value)
    if match is None:
        return value
    return f"{match.group('scheme')}<redacted>@{value[match.end() :]}"


def _is_env_reference(key_text: str) -> bool:
    """Env-var *names* are safe to display; their values never are."""
    return key_text.casefold() == "env" or key_text.casefold().endswith("_env")


def _redact_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _is_env_reference(key_text):
                # ``api_key_env`` holds an environment variable name, not a key.
                redacted[key_text] = item
            elif any(marker in key_text.casefold() for marker in SECRET_KEY_MARKERS) and not isinstance(
                item, bool
            ):
                redacted[key_text] = "<redacted>"
            else:
                redacted[key_text] = _redact_value(item)
        return redacted
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return _redact_url_credentials(value)
    return value


def redact_config(config: Config | Mapping[str, Any]) -> dict[str, Any]:
    """Return a TOML-shaped dict with credential-like keys masked."""
    source = config.to_toml_dict() if isinstance(config, Config) else dict(config)
    result = _redact_value(source)
    assert isinstance(result, dict)
    return result


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, Mapping):
        flat: dict[str, Any] = {}
        for key, item in value.items():
            child = f"{prefix}.{key}" if prefix else str(key)
            flat.update(_flatten(item, child))
        return flat
    return {prefix: value}


def diff_keys(
    old: Config | Mapping[str, Any],
    new: Config | Mapping[str, Any],
) -> dict[str, tuple[Any, Any]]:
    """Return dotted-key differences as ``{key: (old_value, new_value)}``."""
    old_map = old.to_toml_dict() if isinstance(old, Config) else dict(old)
    new_map = new.to_toml_dict() if isinstance(new, Config) else dict(new)
    old_flat = _flatten(old_map)
    new_flat = _flatten(new_map)
    changes: dict[str, tuple[Any, Any]] = {}
    for key in sorted(set(old_flat) | set(new_flat)):
        before = old_flat.get(key)
        after = new_flat.get(key)
        if before != after:
            changes[key] = (before, after)
    return changes


def check_config(path: Path | None = None) -> tuple[Config, list[str]]:
    """Load and validate a config file, returning ``(config, problems)``."""
    try:
        config = load_config(path)
    except (FileNotFoundError, tomllib.TOMLDecodeError, ValidationError, ValueError) as exc:
        return default_config(), [f"config could not be loaded: {exc}"]
    return config, validate_config(config)


def show_config(config: Config, *, redacted: bool = True) -> str:
    """Serialize a config for ``gatewayctl config show``."""
    data = redact_config(config) if redacted else config.to_toml_dict()
    return tomli_w.dumps(data)


_SECTION_COMMENTS = {
    "server": (
        "Network listener. Keep the bind address on loopback unless the deployment requires otherwise. "
        "direct_mcp_registration_token stays false unless a client-side handshake adapter needs it."
    ),
    "storage": (
        "Database, evidence objects, and backup snapshots. Backups do not protect against disk loss. "
        "runtime_socket defaults to $XDG_RUNTIME_DIR/knowlageMCP/admin.sock."
    ),
    "identity": "Registration, session lifetimes (durations such as 8h), and namespace defaults.",
    "limits": "Hard request, search, and write budgets enforced by the service layer.",
    "review": "Review queue leases and per-session mentor budget.",
    "retrieval": "Search backend selection for this release.",
    "backup": "Retention for scheduled online SQLite backups.",
    "logging": "Log verbosity. Logs never contain session tokens or evidence content.",
    "integrations": (
        "Optional semantic/vector layer. Disabled by default; canonical data, ACLs, and review "
        "decisions stay in SQLite. Secrets are referenced by environment variable name only."
    ),
}


def example_config_text(config: Config | None = None) -> str:
    """Render an annotated example configuration from the live defaults."""
    cfg = config if config is not None else default_config()
    data = cfg.to_toml_dict()
    # The runtime socket normally lives in $XDG_RUNTIME_DIR and is therefore
    # host-specific. Keep the example portable by documenting a stable private
    # per-user location instead of embedding this machine's /run path.
    data["storage"]["runtime_socket"] = "~/.local/state/knowlageMCP/admin.sock"
    chunks = [
        "# SPDX-" + "FileCopyrightText: 2026 René Widera",
        "# SPDX-License-" + "Identifier: ISC",
        "#",
        "# knowlageMCP example configuration. Paths may use '~'; durations accept",
        "# integer seconds or strings such as 30s, 10m, 8h, 24h.",
        "",
    ]
    for section, values in data.items():
        chunks.append(f"# {_SECTION_COMMENTS.get(section, section)}")
        chunks.append(tomli_w.dumps({section: values}).rstrip("\n"))
        chunks.append("")
    text = "\n".join(chunks).rstrip("\n") + "\n"
    home = str(Path.home()) + os.sep
    return text.replace(home, "~/")


__all__ = [
    "APP_NAME",
    "PRIVATE_NAMESPACE_KINDS",
    "SECRET_KEY_MARKERS",
    "BackupConfig",
    "Config",
    "DurationSeconds",
    "EmbeddingIntegrationConfig",
    "GraphitiIntegrationConfig",
    "IdentityConfig",
    "IntegrationsConfig",
    "LimitsConfig",
    "LoggingConfig",
    "Mem0IntegrationConfig",
    "RerankerIntegrationConfig",
    "RetrievalConfig",
    "ReviewConfig",
    "ServerConfig",
    "StorageConfig",
    "VectorIntegrationConfig",
    "check_config",
    "config_dir",
    "data_dir",
    "default_config",
    "diff_keys",
    "ensure_private_dir",
    "example_config_text",
    "format_duration",
    "load_config",
    "parse_duration",
    "redact_config",
    "resolve_paths",
    "runtime_dir",
    "show_config",
    "validate_config",
    "write_config_atomic",
]
