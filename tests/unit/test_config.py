# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Unit tests for configuration defaults, TOML I/O, durations, and redaction."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
import tomli_w

from knowlage_mcp import config as config_module
from knowlage_mcp.config import (
    Config,
    EmbeddingIntegrationConfig,
    IdentityConfig,
    ServerConfig,
    StorageConfig,
    check_config,
    default_config,
    diff_keys,
    format_duration,
    load_config,
    parse_duration,
    redact_config,
    show_config,
    validate_config,
    write_config_atomic,
)
from knowlage_mcp.db import iso, utcnow


def test_defaults_match_spec(tmp_config: Config) -> None:
    assert tmp_config.server.host == "127.0.0.1"
    assert tmp_config.server.port == 2230
    assert tmp_config.server.max_request_bytes == 131072
    assert tmp_config.storage.evidence_quota_bytes == 1073741824
    assert tmp_config.identity.initial_namespace == "shared"
    assert tmp_config.identity.personal_namespace == "personal"
    assert tmp_config.identity.session_idle_timeout == 8 * 3600
    assert tmp_config.identity.session_absolute_lifetime == 24 * 3600
    assert tmp_config.identity.mentor_request_lifetime == 10 * 60
    assert tmp_config.identity.mentor_approved_lifetime == 2 * 3600
    assert tmp_config.identity.child_ticket_lifetime == 5 * 60
    assert tmp_config.limits.search_default_results == 5
    assert tmp_config.limits.search_max_results == 10
    assert tmp_config.review.lease_seconds == 600
    assert tmp_config.retrieval.backend == "fts5"
    assert tmp_config.integrations.enabled is False
    assert tmp_config.integrations.embedding.provider == "none"
    assert tmp_config.integrations.vector.backend == "none"
    assert tmp_config.integrations.reranker.provider == "none"
    assert tmp_config.integrations.graphiti.enabled is False
    assert tmp_config.integrations.mem0.enabled is False
    assert tmp_config.storage.database.is_absolute()
    assert validate_config(tmp_config) == []


def test_default_config_creates_no_directories(isolated_env: Path) -> None:
    before = {p for p in isolated_env.rglob("*")}
    cfg = default_config()
    after = {p for p in isolated_env.rglob("*")}
    assert before == after
    assert cfg.storage.database.is_absolute()
    assert not (isolated_env / "xdg-data" / "knowlageMCP").exists()


def test_toml_roundtrip(tmp_config: Config) -> None:
    text = tomli_w.dumps(tmp_config.to_toml_dict())
    restored = Config.from_toml_dict(config_module.tomllib.loads(text))
    assert restored == tmp_config
    assert restored.to_toml_dict() == tmp_config.to_toml_dict()


def test_duration_parsing_and_formatting() -> None:
    assert parse_duration(30) == 30
    assert parse_duration("30s") == 30
    assert parse_duration("10m") == 600
    assert parse_duration("8h") == 28800
    assert parse_duration("2h") == 7200
    assert parse_duration("24h") == 86400
    assert parse_duration("5m") == 300
    assert parse_duration("1d") == 86400
    assert format_duration(28800) == "8h"
    assert format_duration(86400) == "24h"
    assert format_duration(600) == "10m"
    assert format_duration(90) == "90s"
    for invalid in ("", "8 hours", "-1s", "1.5h", True):
        with pytest.raises(ValueError):
            parse_duration(invalid)  # type: ignore[arg-type]


def test_duration_fields_accept_strings() -> None:
    identity = IdentityConfig(session_idle_timeout="8h", child_ticket_lifetime=60)
    assert identity.session_idle_timeout == 28800
    assert identity.child_ticket_lifetime == 60
    cfg = Config.from_toml_dict({"identity": {"mentor_request_lifetime": "10m"}})
    assert cfg.identity.mentor_request_lifetime == 600


def test_validation_reports_bounds(tmp_config: Config) -> None:
    bad = tmp_config.model_copy(deep=True)
    bad.server.port = 70000
    bad.server.allowed_hosts = []
    bad.storage.evidence_quota_bytes = 0
    bad.identity.session_idle_timeout = bad.identity.session_absolute_lifetime + 1
    bad.limits.search_default_results = 11
    bad.logging.level = "LOUD"
    problems = validate_config(bad)
    joined = "\n".join(problems)
    assert "server.port" in joined
    assert "allowed_hosts" in joined
    assert "evidence_quota_bytes" in joined
    assert "session_idle_timeout" in joined
    assert "search_default_results" in joined
    assert "logging.level" in joined


def test_atomic_write_preserves_old_file_on_serialization_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_config: Config
) -> None:
    target = tmp_path / "config.toml"
    write_config_atomic(target, tmp_config)
    original = target.read_bytes()
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600

    def explode(*args: object, **kwargs: object) -> str:
        raise RuntimeError("cannot serialize")

    monkeypatch.setattr(config_module.tomli_w, "dumps", explode)
    with pytest.raises(RuntimeError):
        write_config_atomic(target, tmp_config)
    assert target.read_bytes() == original
    assert list(tmp_path.glob(".config-*")) == []


def test_atomic_write_cleans_temp_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_config: Config
) -> None:
    target = tmp_path / "config.toml"
    write_config_atomic(target, tmp_config)
    original = target.read_bytes()

    def explode(src: object, dst: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(config_module.os, "replace", explode)
    with pytest.raises(OSError):
        write_config_atomic(target, tmp_config)
    assert target.read_bytes() == original
    assert list(tmp_path.glob(".config-*")) == []


def test_redaction_masks_credential_keys() -> None:
    redacted = redact_config(
        {
            "server": {"host": "127.0.0.1", "api_token": "secret-value"},
            "outer": [{"password": "p", "keep": 1}],
        }
    )
    assert redacted["server"]["host"] == "127.0.0.1"
    assert redacted["server"]["api_token"] == "<redacted>"
    assert redacted["outer"][0]["password"] == "<redacted>"
    assert redacted["outer"][0]["keep"] == 1


def test_redacted_config_keeps_shape(tmp_config: Config) -> None:
    data = redact_config(tmp_config)
    assert data["server"]["port"] == 2230
    assert data["identity"]["session_idle_timeout"] == "8h"


def test_check_example_configuration(repo_root: Path) -> None:
    cfg, problems = check_config(repo_root / "config.example.toml")
    assert problems == []
    assert cfg.server.port == 2230
    assert cfg.identity.session_idle_timeout == 28800
    assert cfg.storage.database.is_absolute()
    assert cfg.integrations.enabled is False
    assert cfg.integrations.embedding.provider == "none"
    assert cfg.integrations.vector.vector_size == cfg.integrations.embedding.dimensions


def test_load_config_missing_default_returns_defaults(isolated_env: Path) -> None:
    cfg = load_config()
    assert cfg.server.port == 2230
    assert cfg.storage.database.is_absolute()


def test_load_config_explicit_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.toml")


def test_diff_keys(tmp_config: Config) -> None:
    changed = tmp_config.model_copy(deep=True)
    changed.server.port = 9999
    changes = diff_keys(tmp_config, changed)
    assert changes == {"server.port": (2230, 9999)}
    assert diff_keys(tmp_config, tmp_config) == {}


def test_config_mutation_is_validated(tmp_config: Config) -> None:
    with pytest.raises(ValueError):
        StorageConfig(evidence_quota_bytes="many")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ServerConfig(unknown_field=True)  # type: ignore[call-arg]


def test_utc_helpers() -> None:
    stamp = utcnow()
    encoded = iso(stamp)
    assert encoded.endswith("Z")
    assert "T" in encoded


def test_integration_master_switch_defaults_off(tmp_config: Config) -> None:
    integrations = tmp_config.integrations
    assert integrations.enabled is False
    assert integrations.inference is False
    assert integrations.telemetry is False
    assert integrations.embedding.dimensions == 1536
    assert integrations.vector.distance == "Cosine"
    assert integrations.vector.top_k == 40
    assert integrations.reranker.top_n == 20
    assert integrations.graphiti.max_calls_per_task == 4
    assert integrations.mem0.max_items == 20


def test_legacy_boolean_embeddings_key_is_migrated() -> None:
    disabled = Config.from_toml_dict({"integrations": {"embeddings": False}})
    assert disabled.integrations.embedding.provider == "none"
    enabled = Config.from_toml_dict({"integrations": {"embeddings": True}})
    assert enabled.integrations.embedding.provider == "hash"
    explicit = Config.from_toml_dict(
        {
            "integrations": {
                "embeddings": True,
                "embedding": {"provider": "openai", "base_url": "https://example.invalid/v1"},
            }
        }
    )
    assert explicit.integrations.embedding.provider == "openai"
    with pytest.raises(ValueError):
        Config.from_toml_dict({"integrations": {"embeddings": "yes"}})


def test_integration_enums_reject_unknown_values() -> None:
    for payload in (
        {"embedding": {"provider": "bogus"}},
        {"vector": {"backend": "bogus"}},
        {"vector": {"distance": "Manhattan"}},
        {"reranker": {"provider": "bogus"}},
    ):
        with pytest.raises(ValueError):
            Config.from_toml_dict({"integrations": payload})
    distance = Config.from_toml_dict({"integrations": {"vector": {"distance": "cosine"}}})
    assert distance.integrations.vector.distance == "Cosine"


def test_integration_validation_reports_bad_values(tmp_config: Config) -> None:
    bad = tmp_config.model_copy(deep=True)
    bad.integrations.enabled = True
    bad.integrations.embedding.provider = "openai"
    bad.integrations.embedding.base_url = "ftp://example.invalid"
    bad.integrations.embedding.dimensions = 1536
    bad.integrations.embedding.batch_size = 0
    bad.integrations.vector.backend = "qdrant"
    bad.integrations.vector.url = "ftp://example.invalid"
    bad.integrations.vector.vector_size = 768
    bad.integrations.vector.top_k = 0
    bad.integrations.reranker.provider = "openai"
    bad.integrations.reranker.base_url = "not-a-url"
    bad.integrations.reranker.top_n = 0
    bad.integrations.graphiti.enabled = True
    bad.integrations.graphiti.base_url = "ftp://example.invalid"
    bad.integrations.mem0.enabled = True
    bad.integrations.mem0.max_items = 0
    joined = "\n".join(validate_config(bad))
    assert "embedding.base_url" in joined
    assert "embedding.batch_size" in joined
    assert "vector.url" in joined
    assert "vector.vector_size" in joined
    assert "vector.top_k" in joined
    assert "reranker.base_url" in joined
    assert "reranker.top_n" in joined
    assert "graphiti.base_url" in joined
    assert "mem0.max_items" in joined


def test_qdrant_backend_requires_a_real_embedding_provider(tmp_config: Config) -> None:
    cfg = tmp_config.model_copy(deep=True)
    cfg.integrations.enabled = True
    cfg.integrations.vector.backend = "qdrant"
    problems = validate_config(cfg)
    assert any("requires integrations.embedding.provider" in problem for problem in problems)

    cfg.integrations.embedding.provider = "hash"
    assert validate_config(cfg) == []

    cfg.integrations.embedding.provider = "openai"
    cfg.integrations.embedding.base_url = "https://example.invalid/v1"
    assert validate_config(cfg) == []


def test_graphiti_and_mem0_extractors_are_mutually_exclusive(tmp_config: Config) -> None:
    cfg = tmp_config.model_copy(deep=True)
    cfg.integrations.enabled = True
    cfg.integrations.graphiti.enabled = True
    cfg.integrations.mem0.enabled = True
    problems = validate_config(cfg)
    assert any("mutually exclusive" in problem for problem in problems)

    cfg.integrations.mem0.enabled = False
    assert validate_config(cfg) == []
    cfg.integrations.graphiti.enabled = False
    cfg.integrations.mem0.enabled = True
    assert validate_config(cfg) == []


def test_master_switch_off_accepts_nested_adapter_configuration(tmp_config: Config) -> None:
    cfg = tmp_config.model_copy(deep=True)
    cfg.integrations.enabled = False
    cfg.integrations.embedding.provider = "hash"
    cfg.integrations.vector.backend = "qdrant"
    cfg.integrations.graphiti.enabled = True
    assert validate_config(cfg) == []


def test_embedding_may_send_guards_private_text() -> None:
    remote = EmbeddingIntegrationConfig(provider="openai", base_url="https://example.invalid/v1")
    assert remote.embedding_may_send("shared") is True
    assert remote.embedding_may_send("personal") is False
    remote.allow_private_text = True
    assert remote.embedding_may_send("personal") is True
    local = EmbeddingIntegrationConfig(provider="hash")
    assert local.embedding_may_send("personal") is True
    config = Config.from_toml_dict(
        {"integrations": {"embedding": {"provider": "openai", "base_url": "https://example.invalid/v1"}}}
    )
    assert config.integrations.embedding_may_send("personal") is False


def test_redaction_hides_url_credentials_and_keeps_env_names(tmp_config: Config) -> None:
    cfg = Config.from_toml_dict(
        {
            "integrations": {
                "vector": {
                    "url": "http://user:secret@127.0.0.1:6333",
                    "api_key_env": "MY_QDRANT_KEY",
                }
            }
        }
    )
    data = redact_config(cfg)
    assert data["integrations"]["vector"]["url"] == "http://<redacted>@127.0.0.1:6333"
    assert data["integrations"]["vector"]["api_key_env"] == "MY_QDRANT_KEY"
    rendered = show_config(cfg)
    assert "secret" not in rendered
    assert "MY_QDRANT_KEY" in rendered


def test_api_key_values_are_never_stored() -> None:
    cfg = Config.from_toml_dict(
        {
            "integrations": {
                "embedding": {"provider": "openai", "base_url": "https://example.invalid/v1"},
                "vector": {"backend": "qdrant"},
            }
        }
    )
    data = str(cfg.to_toml_dict())
    for key_name in (
        "KNOWLAGEMCP_EMBEDDING_API_KEY",
        "KNOWLAGEMCP_QDRANT_API_KEY",
        "KNOWLAGEMCP_RERANK_API_KEY",
        "KNOWLAGEMCP_GRAPHITI_API_KEY",
        "KNOWLAGEMCP_MEM0_API_KEY",
    ):
        assert key_name in data
    assert "api_key" not in data.replace("api_key_env", "")


@pytest.mark.parametrize(
    "base_url",
    [
        "",
        "not-a-url",
        "ftp://example.invalid",
        "http://exa mple.invalid",
        "http://user:secret@example.invalid",
    ],
)
def test_integration_urls_must_be_usable(tmp_config: Config, base_url: str) -> None:
    cfg = tmp_config.model_copy(deep=True)
    cfg.integrations.enabled = True
    cfg.integrations.embedding.provider = "openai"
    cfg.integrations.embedding.base_url = base_url
    joined = "\n".join(validate_config(cfg))
    assert "integrations.embedding.base_url" in joined

    cfg = tmp_config.model_copy(deep=True)
    cfg.integrations.enabled = True
    cfg.integrations.vector.backend = "qdrant"
    cfg.integrations.vector.url = base_url
    joined = "\n".join(validate_config(cfg))
    assert "integrations.vector.url" in joined

    cfg = tmp_config.model_copy(deep=True)
    cfg.integrations.enabled = True
    cfg.integrations.graphiti.enabled = True
    cfg.integrations.graphiti.base_url = base_url
    joined = "\n".join(validate_config(cfg))
    assert "integrations.graphiti.base_url" in joined

    cfg = tmp_config.model_copy(deep=True)
    cfg.integrations.enabled = True
    cfg.integrations.mem0.enabled = True
    cfg.integrations.mem0.base_url = base_url
    joined = "\n".join(validate_config(cfg))
    assert "integrations.mem0.base_url" in joined
