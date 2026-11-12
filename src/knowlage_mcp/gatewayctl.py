# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""``knowlageMCP-gatewayctl``: operator CLI over the local admin socket."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import PROTOCOL_VERSION, __version__
from .admin_client import AdminClient, AdminError, AdminUnavailable
from .config import check_config, load_config, show_config


def _add_json(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="emit one JSON object")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="knowlageMCP-gatewayctl",
        description="Administer a running knowlageMCP gateway over its Unix socket.",
    )
    parser.add_argument("--config", default=None, help="configuration file path")
    parser.add_argument(
        "--version",
        action="version",
        version=f"knowlageMCP-gatewayctl {__version__} (protocol {PROTOCOL_VERSION})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("status", "doctor", "agents", "enrollments", "reviews", "smoke-test"):
        child = sub.add_parser(name, help=f"show {name}")
        _add_json(child)

    watch = sub.add_parser("watch", help="stream audit events until interrupted")
    _add_json(watch)

    approve = sub.add_parser("approve", help="approve a mentor enrollment request")
    approve.add_argument("request")
    approve.add_argument("--role", default="mentor")
    approve.add_argument("--ttl", default=None)
    approve.add_argument("--namespace", default=None)
    _add_json(approve)

    deny = sub.add_parser("deny", help="deny a mentor enrollment request")
    deny.add_argument("request")
    _add_json(deny)

    revoke = sub.add_parser("revoke", help="revoke a session or agent")
    revoke.add_argument("target")
    _add_json(revoke)

    namespaces = sub.add_parser("namespaces", help="list or create namespaces")
    namespaces_sub = namespaces.add_subparsers(dest="namespaces_command", required=True)
    _add_json(namespaces_sub.add_parser("list", help="list namespaces"))
    create = namespaces_sub.add_parser("create", help="create a namespace")
    create.add_argument("name")
    create.add_argument("--kind", default="shared")
    create.add_argument("--description", default=None)
    _add_json(create)

    grants = sub.add_parser("grants", help="grant namespace roles")
    grants_sub = grants.add_subparsers(dest="grants_command", required=True)
    grants_add = grants_sub.add_parser("add", help="add a grant")
    grants_add.add_argument("agent")
    grants_add.add_argument("--namespace", required=True)
    grants_add.add_argument("--role", required=True)
    _add_json(grants_add)

    search = sub.add_parser("search", help="search knowledge as the operator")
    search.add_argument("query")
    search.add_argument("--namespace", default=None)
    _add_json(search)

    inspect = sub.add_parser("inspect", help="inspect one entry and its revisions")
    inspect.add_argument("entry")
    _add_json(inspect)

    memory = sub.add_parser("memory", help="operator memory operations")
    memory_sub = memory.add_subparsers(dest="memory_command", required=True)
    memory_add = memory_sub.add_parser("add", help="add operator memory from a file")
    memory_add.add_argument("--kind", required=True)
    memory_add.add_argument("--namespace", required=True)
    memory_add.add_argument("--file", required=True)
    memory_add.add_argument("--title", default=None)
    _add_json(memory_add)
    memory_correct = memory_sub.add_parser("correct", help="correct an entry from a file")
    memory_correct.add_argument("entry")
    memory_correct.add_argument("--expected-revision", required=True)
    memory_correct.add_argument("--file", required=True)
    _add_json(memory_correct)
    memory_retract = memory_sub.add_parser("retract", help="retract an entry")
    memory_retract.add_argument("entry")
    memory_retract.add_argument("--reason", required=True)
    _add_json(memory_retract)
    memory_forget = memory_sub.add_parser("forget", help="forget an entry")
    memory_forget.add_argument("entry")
    mode = memory_forget.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--yes", action="store_true")
    _add_json(memory_forget)

    config = sub.add_parser("config", help="inspect configuration without a gateway")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    _add_json(config_sub.add_parser("check", help="validate configuration"))
    show = config_sub.add_parser("show", help="print effective configuration")
    show.add_argument("--redacted", dest="redacted", action="store_true", default=True)
    show.add_argument("--no-redacted", dest="redacted", action="store_false")
    _add_json(show)

    reload_parser = sub.add_parser("reload", help="atomically reload the gateway configuration")
    _add_json(reload_parser)

    backup = sub.add_parser("backup", help="create, verify, restore, or prune snapshots")
    backup_sub = backup.add_subparsers(dest="backup_command", required=True)
    _add_json(backup_sub.add_parser("create", help="create a snapshot"))
    verify = backup_sub.add_parser("verify", help="verify a snapshot")
    verify.add_argument("snapshot")
    _add_json(verify)
    restore = backup_sub.add_parser("restore", help="restore a snapshot into a new directory")
    restore.add_argument("snapshot")
    restore.add_argument("--destination", required=True)
    _add_json(restore)
    prune = backup_sub.add_parser("prune", help="prune snapshots using the retention policy")
    prune.add_argument("--keep-daily", type=int, default=None, help="daily snapshots to keep")
    prune.add_argument("--keep-weekly", type=int, default=None, help="ISO-weekly snapshots to keep")
    prune.add_argument("--dry-run", action="store_true", help="report removals without deleting")
    _add_json(prune)

    export = sub.add_parser("export", help="export JSONL")
    export.add_argument("--namespace", default=None)
    export.add_argument("--output", required=True)
    _add_json(export)

    import_parser = sub.add_parser("import", help="import JSONL")
    import_parser.add_argument("file")
    import_parser.add_argument("--dry-run", action="store_true")
    _add_json(import_parser)

    index = sub.add_parser("index", help="index maintenance")
    index_sub = index.add_subparsers(dest="index_command", required=True)
    _add_json(index_sub.add_parser("rebuild", help="rebuild the FTS5 index"))

    return parser


def _emit(data: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2, sort_keys=True, default=str))
        return
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                print("  ".join(f"{key}={value}" for key, value in item.items() if value is not None))
            else:
                print(item)
        if not data:
            print("(none)")
        return
    if isinstance(data, dict):
        for key, value in data.items():
            print(f"{key:<20} {value}")
        return
    print(data)


def _humanize(config: dict[str, Any]) -> None:
    print(f"{'version':<20} {config.get('version', '-')}")
    print(f"{'protocol':<20} {config.get('protocol_version', '-')}")
    print(f"{'schema':<20} {config.get('schema_version', '-')}")
    print(f"{'listener':<20} {config.get('listener', '-')}")
    print(f"{'sessions':<20} {config.get('sessions_active', '-')}")
    print(f"{'agents':<20} {config.get('agents_active', '-')}")
    print(f"{'entries':<20} {config.get('entries', '-')}")
    print(f"{'database bytes':<20} {config.get('database_bytes', '-')}")


def _print_smoke(status: dict[str, Any], smoke: dict[str, Any]) -> None:
    print(
        f"{'health':<16} ok (protocol {status.get('protocol_version', '-')}, "
        f"schema {status.get('schema_version', '-')})"
    )
    print(f"{'register':<16} ok (service-managed)")
    print(f"{'record':<16} {'ok' if smoke.get('recorded') else 'failed'}")
    print(f"{'search':<16} {'ok' if smoke.get('searchable') else 'failed'}")
    print(f"{'close':<16} ok")
    print("smoke test passed" if smoke.get("ok") else "smoke test failed")


def _preview(value: Any, limit: int = 160) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _print_write_result(data: dict[str, Any]) -> None:
    entry = data.get("entry", {})
    print(f"{'entry_id':<18} {entry.get('entry_id', '-')}")
    print(f"{'namespace':<18} {entry.get('namespace', '-')}")
    print(f"{'lifecycle':<18} {entry.get('lifecycle', '-')}")
    print(f"{'title':<18} {entry.get('title', '-')}")
    print(f"{'claim':<18} {_preview(entry.get('claim', ''))}")


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "config":
        return _dispatch_config(args)

    try:
        config = load_config(Path(args.config) if args.config else None)
    except (FileNotFoundError, ValueError) as exc:
        print(f"configuration could not be loaded: {exc}", file=sys.stderr)
        return 1

    client = AdminClient(config.storage.runtime_socket)
    command = args.command

    if command == "watch":

        def on_event(event: dict[str, Any]) -> None:
            if args.json:
                print(json.dumps(event, sort_keys=True, default=str))
            elif event.get("event") == "heartbeat":
                print(f"{event.get('ts', '-')} heartbeat")
            else:
                print(
                    f"{event.get('ts', '-')} {event.get('action', event.get('event', '-'))} "
                    f"{event.get('object_type') or '-'}:{event.get('object_id') or '-'}"
                )

        try:
            client.watch(on_event)
        except KeyboardInterrupt:
            return 130
        return 0

    if command == "status":
        data = client.call("status")
        data = {**data, "listener": f"{config.server.host}:{config.server.port}"}
        if args.json:
            _emit(data, True)
        else:
            _humanize(data)
        return 0

    if command == "doctor":
        data = client.call("doctor")
        if args.json:
            _emit(data, True)
        else:
            for check in data.get("checks", []):
                mark = "ok" if check.get("ok") else "FAIL"
                print(f"{check.get('name', '-'):<20} {mark:<5} {check.get('detail', '')}")
            print("doctor passed" if data.get("ok") else "doctor found problems")
        return 0

    if command in {"agents", "enrollments", "reviews"}:
        data = client.call(command)
        _emit(data, args.json)
        return 0

    if command == "smoke-test":
        status = client.call("status")
        smoke = client.call("smoke.test")
        if args.json:
            _emit({"status": status, "smoke": smoke}, True)
        else:
            _print_smoke(status, smoke)
        return 0 if smoke.get("ok") else 1

    if command == "approve":
        data = client.call(
            "approve",
            {
                "request_id": args.request,
                "role": args.role,
                "ttl": args.ttl,
                "namespace": args.namespace,
            },
        )
        _emit(data, args.json)
        return 0

    if command == "deny":
        _emit(client.call("deny", {"request_id": args.request}), args.json)
        return 0

    if command == "revoke":
        _emit(client.call("revoke", {"target": args.target}), args.json)
        return 0

    if command == "namespaces":
        if args.namespaces_command == "list":
            _emit(client.call("namespaces.list"), args.json)
        else:
            _emit(
                client.call(
                    "namespaces.create",
                    {
                        "name": args.name,
                        "kind": args.kind,
                        "description": args.description,
                    },
                ),
                args.json,
            )
        return 0

    if command == "grants":
        _emit(
            client.call(
                "grants.add",
                {"agent_id": args.agent, "namespace": args.namespace, "role": args.role},
            ),
            args.json,
        )
        return 0

    if command == "search":
        data = client.call("search", {"query": args.query, "namespace": args.namespace})
        if args.json:
            _emit(data, True)
        else:
            results = data.get("results", [])
            print(f"{data.get('total', len(results))} result(s)")
            for result in results:
                print(f"{result.get('entry_id')}  {result.get('title')}")
                print(f"  {result.get('claim', '')[:160]}")
        return 0

    if command == "inspect":
        data = client.call("inspect", {"entry_id": args.entry})
        if args.json:
            _emit(data, True)
        else:
            entry = data.get("entry", {})
            print(f"{'entry_id':<18} {entry.get('entry_id', '-')}")
            print(f"{'namespace':<18} {entry.get('namespace', '-')}")
            print(f"{'title':<18} {entry.get('title', '-')}")
            print(f"{'lifecycle':<18} {entry.get('lifecycle', '-')}")
            print(f"{'revisions':<18} {len(data.get('revisions', []))}")
            print(f"{'claim':<18} {entry.get('claim', '')}")
        return 0

    if command == "memory":
        return _dispatch_memory(args, client)

    if command == "reload":
        arguments: dict[str, Any] = {}
        if args.config:
            arguments["path"] = args.config
        _emit(client.call("reload", arguments), args.json)
        return 0

    if command == "backup":
        if args.backup_command == "create":
            _emit(client.call("backup.create"), args.json)
        elif args.backup_command == "verify":
            _emit(client.call("backup.verify", {"path": args.snapshot}), args.json)
        elif args.backup_command == "prune":
            data = client.call(
                "backup.prune",
                {
                    "keep_daily": args.keep_daily,
                    "keep_weekly": args.keep_weekly,
                    "dry_run": bool(args.dry_run),
                },
            )
            if args.json:
                _emit(data, True)
            else:
                print(f"{'dry_run':<18} {data.get('dry_run', '-')}")
                print(f"{'keep_daily':<18} {data.get('keep_daily', '-')}")
                print(f"{'keep_weekly':<18} {data.get('keep_weekly', '-')}")
                print(f"{'kept':<18} {len(data.get('kept', []))}")
                print(f"{'removed':<18} {len(data.get('removed', []))}")
                for removed in data.get("removed", []):
                    print(f"  removed {removed}")
        else:
            data = client.call(
                "backup.restore",
                {"path": args.snapshot, "destination": args.destination},
            )
            if args.json:
                _emit(data, True)
            else:
                print(f"{'restored':<18} {data.get('restored', '-')}")
                print(f"{'destination':<18} {data.get('destination', args.destination)}")
        return 0

    if command == "export":
        _emit(
            client.call("export", {"namespace": args.namespace, "path": args.output}),
            args.json,
        )
        return 0

    if command == "import":
        data = client.call("import", {"path": args.file, "dry_run": bool(args.dry_run)})
        _emit(data, args.json)
        return 0

    if command == "index":
        _emit(client.call("index.rebuild"), args.json)
        return 0

    print(f"unknown command: {command}", file=sys.stderr)
    return 2


def _dispatch_memory(args: argparse.Namespace, client: AdminClient) -> int:
    if args.memory_command == "add":
        text = _read_file(args.file)
        data = client.call(
            "memory.add",
            {
                "kind": args.kind,
                "namespace": args.namespace,
                "text": text,
                "title": args.title,
            },
        )
        if args.json:
            _emit(data, True)
        else:
            _print_write_result(data)
        return 0
    if args.memory_command == "correct":
        text = _read_file(args.file)
        data = client.call(
            "memory.correct",
            {
                "entry_id": args.entry,
                "expected_revision": args.expected_revision,
                "text": text,
            },
        )
        if args.json:
            _emit(data, True)
        else:
            _print_write_result(data)
        return 0
    if args.memory_command == "retract":
        _emit(
            client.call("memory.retract", {"entry_id": args.entry, "reason": args.reason}),
            args.json,
        )
        return 0
    _emit(
        client.call(
            "memory.forget",
            {"entry_id": args.entry, "dry_run": bool(args.dry_run)},
        ),
        args.json,
    )
    return 0


def _read_file(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _dispatch_config(args: argparse.Namespace) -> int:
    path = Path(args.config) if args.config else None
    if args.config_command == "check":
        if path is not None and not path.exists():
            print(f"configuration file not found: {path}", file=sys.stderr)
            return 1
        _config, problems = check_config(path)
        if args.json:
            _emit({"ok": not problems, "problems": problems}, True)
        else:
            if problems:
                for problem in problems:
                    print(problem)
            else:
                print("configuration is valid")
        return 0 if not problems else 4
    try:
        config = load_config(path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"configuration could not be loaded: {exc}", file=sys.stderr)
        return 1
    text = show_config(config, redacted=bool(args.redacted))
    if args.json:
        _emit({"config": text}, True)
    else:
        print(text, end="")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return _dispatch(args)
    except AdminUnavailable as exc:
        print(f"gateway unavailable: {exc}", file=sys.stderr)
        return 3
    except AdminError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return exc.exit_code
    except FileNotFoundError as exc:
        print(f"file not found: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


__all__ = ["build_parser", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
