# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Interactive and non-interactive host bootstrap.

The wizard asks a short list of questions with usable defaults, shows a
summary, validates the listen port by binding it, creates private directories,
writes the config atomically with mode 0600, initializes the schema, and prints
the next commands. Re-running never truncates an existing database.
"""

from __future__ import annotations

import contextlib
import importlib.resources
import os
import shutil
import socket
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import (
    Config,
    config_dir,
    default_config,
    ensure_private_dir,
    format_duration,
    load_config,
    parse_duration,
    resolve_paths,
    validate_config,
    write_config_atomic,
)
from .db import Database

UNIT_NAME = "knowlage-mcp-gateway.service"
SYSTEMD_TARGET = Path("systemd/user") / UNIT_NAME


def _ask_text(
    input_fn: Callable[[str], str],
    print_fn: Callable[[str], None],
    label: str,
    default: str,
) -> str:
    raw = input_fn(f"{label} [{default}]: ").strip()
    return raw or default


def _ask_int(
    input_fn: Callable[[str], str],
    print_fn: Callable[[str], None],
    label: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    while True:
        raw = _ask_text(input_fn, print_fn, label, str(default))
        try:
            value = int(raw)
        except ValueError:
            print_fn(f"  enter a whole number between {minimum} and {maximum}")
            continue
        if minimum <= value <= maximum:
            return value
        print_fn(f"  enter a whole number between {minimum} and {maximum}")


def _ask_bool(
    input_fn: Callable[[str], str],
    print_fn: Callable[[str], None],
    label: str,
    default: bool,
) -> bool:
    default_text = "yes" if default else "no"
    while True:
        raw = input_fn(f"{label} (yes/no) [{default_text}]: ").strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes", "true", "1"}:
            return True
        if raw in {"n", "no", "false", "0"}:
            return False
        print_fn("  answer yes or no")


def _port_available(host: str, port: int) -> tuple[bool, str]:
    family = socket.AF_INET6 if ":" in host and host not in {"0.0.0.0"} else socket.AF_INET
    probe = socket.socket(family, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
        return True, ""
    except OSError as exc:
        return False, str(exc)
    finally:
        probe.close()


def _prepare_directory(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeError(f"refusing to use a symlink: {path}")
    ensure_private_dir(path)


def _systemctl() -> str | None:
    return shutil.which("systemctl")


def install_systemd_unit(print_fn: Callable[[str], None]) -> Path | None:
    """Install the packaged user unit and reload systemd when available."""
    try:
        resource = importlib.resources.files("knowlage_mcp.resources").joinpath(UNIT_NAME)
        content = resource.read_text(encoding="utf-8")
    except (OSError, ModuleNotFoundError):
        print_fn("systemd unit is not packaged in this build; skipping installation")
        return None
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    target = base / SYSTEMD_TARGET
    try:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        target.write_text(content, encoding="utf-8")
        os.chmod(target, 0o644)
    except OSError as exc:
        print_fn(f"could not install the systemd unit: {exc}")
        return None
    print_fn(f"systemd unit written to {target}")
    executable = _systemctl()
    if executable is None:
        print_fn("systemctl is not available; the unit was written but not reloaded")
        return target
    try:
        result = subprocess.run(
            [executable, "--user", "daemon-reload"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print_fn(f"systemctl --user daemon-reload failed: {exc}")
        return target
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        print_fn(f"systemctl --user daemon-reload failed: {detail or result.returncode}")
    return target


def start_systemd_unit(print_fn: Callable[[str], None]) -> bool:
    executable = _systemctl()
    if executable is None:
        print_fn("systemctl is not available; start the gateway manually")
        return False
    try:
        result = subprocess.run(
            [executable, "--user", "start", "knowlage-mcp-gateway"],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print_fn(f"systemctl --user start failed: {exc}")
        return False
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        print_fn(f"systemctl --user start failed: {detail or result.returncode}")
        return False
    print_fn("systemd user service started")
    return True


def _summary(config: Config) -> str:
    return "\n".join(
        [
            "Summary:",
            f"  listen address      http://{config.server.host}:{config.server.port}",
            f"  database            {config.storage.database}",
            f"  evidence directory  {config.storage.evidence_dir}",
            f"  backup directory    {config.storage.backup_dir}",
            f"  admin socket        {config.storage.runtime_socket}",
            f"  self-registration   {'enabled' if config.identity.allow_self_registration else 'disabled'}",
            f"  session idle/life   {format_duration(int(config.identity.session_idle_timeout))} / "
            f"{format_duration(int(config.identity.session_absolute_lifetime))}",
            "  backups stay on this disk and do not protect against disk loss.",
        ]
    )


def _interactive_config(
    base: Config,
    *,
    input_fn: Callable[[str], str],
    print_fn: Callable[[str], None],
) -> tuple[Config, bool, bool, bool]:
    print_fn("knowlageMCP bootstrap")
    print_fn("Press Enter to accept each default.")
    config = base.model_copy(deep=True)
    config.server.host = _ask_text(input_fn, print_fn, "1. Listen address", base.server.host)
    config.server.port = _ask_int(input_fn, print_fn, "2. Listen port", base.server.port, 1, 65535)
    config.storage.database = Path(
        _ask_text(input_fn, print_fn, "3. Database file", str(base.storage.database))
    ).expanduser()
    config.storage.evidence_dir = Path(
        _ask_text(input_fn, print_fn, "4. Evidence directory", str(base.storage.evidence_dir))
    ).expanduser()
    config.storage.backup_dir = Path(
        _ask_text(input_fn, print_fn, "5. Backup directory", str(base.storage.backup_dir))
    ).expanduser()
    config.identity.allow_self_registration = _ask_bool(
        input_fn, print_fn, "6. Allow standard agent self-registration", base.identity.allow_self_registration
    )
    install_unit = _ask_bool(input_fn, print_fn, "7. Install a systemd user unit", True)
    if _ask_bool(input_fn, print_fn, "8. Show advanced settings", False):
        config.server.max_request_bytes = _ask_int(
            input_fn,
            print_fn,
            "   Maximum JSON request bytes",
            int(base.server.max_request_bytes),
            1024,
            16 * 1024 * 1024,
        )
        config.identity.session_idle_timeout = parse_duration(
            _ask_text(
                input_fn,
                print_fn,
                "   Session idle timeout (e.g. 8h)",
                format_duration(int(base.identity.session_idle_timeout)),
            )
        )
        config.identity.session_absolute_lifetime = parse_duration(
            _ask_text(
                input_fn,
                print_fn,
                "   Session absolute lifetime (e.g. 24h)",
                format_duration(int(base.identity.session_absolute_lifetime)),
            )
        )
        config.storage.runtime_socket = Path(
            _ask_text(input_fn, print_fn, "   Admin socket path", str(base.storage.runtime_socket))
        ).expanduser()
    config = resolve_paths(config)
    print_fn(_summary(config))
    if not _ask_bool(input_fn, print_fn, "Write this configuration", True):
        return config, install_unit, False, False
    start_now = False
    if install_unit:
        start_now = _ask_bool(input_fn, print_fn, "Start the service now", False)
    return config, install_unit, start_now, True


def _apply_overrides(config: Config, overrides: dict[str, Any]) -> tuple[Config, bool]:
    updated = config.model_copy(deep=True)
    changed = False
    if overrides.get("host"):
        updated.server.host = str(overrides["host"])
        changed = True
    if overrides.get("port") is not None:
        updated.server.port = int(overrides["port"])
        changed = True
    for key in ("database", "evidence_dir", "backup_dir"):
        value = overrides.get(key)
        if value:
            setattr(updated.storage, key, Path(str(value)).expanduser())
            changed = True
    return resolve_paths(updated), changed


def run_bootstrap(
    config_path: Path | None = None,
    *,
    non_interactive: bool = False,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[[str], None] = print,
    overrides: dict[str, Any] | None = None,
) -> int:
    """Create or update a host installation; returns a process exit code."""
    options = dict(overrides or {})
    target = Path(config_path) if config_path is not None else config_dir() / "config.toml"
    existing = target.exists()
    force = bool(options.get("force", False))
    try:
        base = load_config(target) if existing else default_config()
    except (FileNotFoundError, ValueError) as exc:
        print_fn(f"existing configuration could not be read: {exc}")
        return 1

    if non_interactive:
        config, override_changed = _apply_overrides(base, options)
        install_unit = bool(options.get("install_systemd", True))
        start_now = bool(options.get("start", False))
        write_config = (not existing) or force or override_changed
    else:
        config, install_unit, start_now, confirmed = _interactive_config(
            base, input_fn=input_fn, print_fn=print_fn
        )
        if not confirmed:
            print_fn("bootstrap aborted; nothing was changed")
            return 1
        write_config = True

    problems = validate_config(config)
    if problems:
        print_fn("configuration is invalid:")
        for problem in problems:
            print_fn(f"  {problem}")
        return 1
    available, detail = _port_available(config.server.host, config.server.port)
    if not available:
        print_fn(f"port {config.server.port} on {config.server.host} is not available: {detail}")
        print_fn("choose another port explicitly; bootstrap never picks one silently")
        return 1

    try:
        _prepare_directory(target.parent)
        _prepare_directory(Path(config.storage.database).parent)
        _prepare_directory(Path(config.storage.evidence_dir))
        _prepare_directory(Path(config.storage.backup_dir))
        _prepare_directory(Path(config.storage.runtime_socket).parent)
    except (OSError, RuntimeError) as exc:
        print_fn(f"could not create private directories: {exc}")
        return 1

    if write_config:
        try:
            write_config_atomic(target, config)
        except OSError as exc:
            print_fn(f"could not write configuration: {exc}")
            return 1
        print_fn(f"configuration written to {target} (mode 0600)")
    else:
        print_fn(f"configuration preserved at {target}")

    try:
        database = Database(config.storage.database)
        version = database.migrate()
        database.close()
        with contextlib.suppress(OSError):
            os.chmod(config.storage.database, 0o600)
    except Exception as exc:
        print_fn(f"database initialization failed: {exc}")
        return 1
    print_fn(f"database ready at {config.storage.database} (schema {version})")

    if install_unit:
        install_systemd_unit(print_fn)
    if start_now:
        start_systemd_unit(print_fn)

    print_fn("Next commands:")
    print_fn(f"  knowlageMCP-gateway --config {target}")
    print_fn("  knowlageMCP-gatewayctl status")
    return 0


__all__ = [
    "SYSTEMD_TARGET",
    "UNIT_NAME",
    "install_systemd_unit",
    "run_bootstrap",
    "start_systemd_unit",
]
