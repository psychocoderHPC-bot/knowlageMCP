# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""``knowlageMCP-gateway`` entrypoint: bootstrap or foreground service."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import PROTOCOL_VERSION, __version__
from .bootstrap import run_bootstrap
from .gateway import run_gateway_from_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="knowlageMCP-gateway",
        description="Host-side knowledge gateway for AI coding agents.",
    )
    parser.add_argument("--config", default=None, help="configuration file path")
    parser.add_argument(
        "--version",
        action="version",
        version=f"knowlageMCP-gateway {__version__} (protocol {PROTOCOL_VERSION})",
    )
    parser.add_argument("--bootstrap", action="store_true", help="write configuration and initialize storage")
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="bootstrap with defaults and explicit overrides only",
    )
    parser.add_argument("--host", default=None, help="bootstrap override: listen address")
    parser.add_argument("--port", type=int, default=None, help="bootstrap override: listen port")
    parser.add_argument("--database", default=None, help="bootstrap override: database file")
    parser.add_argument("--evidence-dir", default=None, help="bootstrap override: evidence directory")
    parser.add_argument("--backup-dir", default=None, help="bootstrap override: backup directory")
    parser.add_argument(
        "--systemd",
        dest="install_systemd",
        action="store_true",
        default=None,
        help="bootstrap: install the systemd user unit",
    )
    parser.add_argument(
        "--no-systemd",
        dest="install_systemd",
        action="store_false",
        help="bootstrap: do not install the systemd user unit",
    )
    parser.add_argument(
        "--start",
        dest="start",
        action="store_true",
        default=None,
        help="bootstrap: start the systemd user service",
    )
    parser.add_argument(
        "--no-start",
        dest="start",
        action="store_false",
        help="bootstrap: do not start a service",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="bootstrap: rewrite the configuration file (never deletes the database)",
    )
    return parser


def _overrides(args: argparse.Namespace) -> dict[str, Any]:
    values: dict[str, Any] = {"force": bool(args.force)}
    for key in ("host", "port", "database", "evidence_dir", "backup_dir", "install_systemd", "start"):
        value = getattr(args, key)
        if value is not None:
            values[key] = value
    return values


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.non_interactive and not args.bootstrap:
        parser.error("--non-interactive requires --bootstrap")
    if args.bootstrap:
        try:
            return run_bootstrap(
                Path(args.config) if args.config else None,
                non_interactive=bool(args.non_interactive),
                overrides=_overrides(args),
            )
        except KeyboardInterrupt:
            print("bootstrap interrupted", file=sys.stderr)
            return 130
        except Exception as exc:  # pragma: no cover - top-level safety
            print(f"bootstrap failed: {exc}", file=sys.stderr)
            return 1
    try:
        return run_gateway_from_path(args.config)
    except KeyboardInterrupt:
        return 130


__all__ = ["build_parser", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
