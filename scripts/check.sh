#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
#
# One-command clean-environment validation for knowlageMCP.
#
# Usage:
#   ./scripts/check.sh
#   PYTHON=python3.14 ./scripts/check.sh
#   SKIP_BUILD=1 ./scripts/check.sh
#
# SKIP_BUILD=1 skips `python -m build` and the wheel-install smoke test.
# Every other step still runs. The script stops at the first failing command
# and prints the commands it executed at the end.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
PYTHON="${PYTHON:-python3}"
VENV="$ROOT/.venv"
SKIP_BUILD="${SKIP_BUILD:-0}"
COMMANDS=()

run() {
    local label="$1"
    shift
    printf '\n==> %s\n' "$label"
    COMMANDS+=("$*")
    "$@"
}

run_in() {
    local directory="$1"
    local label="$2"
    shift 2
    printf '\n==> %s\n' "$label"
    COMMANDS+=("(cd $directory && $*)")
    ( cd "$directory" && "$@" )
}

cd "$ROOT"

printf 'knowlageMCP validation (root=%s, python=%s)\n' "$ROOT" "$PYTHON"

if [[ ! -x "$VENV/bin/python" ]]; then
    run "Create virtual environment (.venv)" "$PYTHON" -m venv "$VENV"
fi
run "Upgrade pip" "$VENV/bin/python" -m pip install --upgrade pip
run "Install package with dev extras" "$VENV/bin/python" -m pip install -e '.[dev]'

run "ruff check" "$VENV/bin/ruff" check .
run "ruff format --check" "$VENV/bin/ruff" format --check .
run "mypy" "$VENV/bin/mypy" src/knowlage_mcp
run "pytest (full suite)" "$VENV/bin/pytest" -q
run "pytest (quality corpus)" "$VENV/bin/pytest" tests/quality -q -s
run "REUSE lint" "$VENV/bin/reuse" lint

if [[ "$SKIP_BUILD" == "1" ]]; then
    printf '\n==> Build and wheel smoke test skipped (SKIP_BUILD=1)\n'
else
    run "Build sdist and wheel" "$VENV/bin/python" -m build

    WHEEL="$(find "$ROOT/dist" -maxdepth 1 -name '*.whl' -print -quit)"
    if [[ -z "$WHEEL" ]]; then
        printf 'error: no wheel found in %s/dist\n' "$ROOT" >&2
        exit 1
    fi

    SMOKE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/knowlagemcp-smoke-XXXXXX")"
    cleanup() { rm -rf "$SMOKE_DIR"; }
    trap cleanup EXIT

    run "Create smoke-test virtual environment (outside the source tree)" \
        "$PYTHON" -m venv "$SMOKE_DIR/venv"
    run "Upgrade pip in the smoke environment" "$SMOKE_DIR/venv/bin/python" -m pip install --upgrade pip
    run "Install built wheel into the smoke environment" \
        "$SMOKE_DIR/venv/bin/python" -m pip install "$WHEEL"

    run_in "$SMOKE_DIR" "Verify wheel imports and packaged SKILL.md" \
        "$SMOKE_DIR/venv/bin/python" -c 'import importlib.resources, knowlage_mcp, knowlage_mcp.integrations.qdrant, knowlage_mcp.integrations.embeddings, knowlage_mcp.integrations.rerank, knowlage_mcp.integrations.graphiti, knowlage_mcp.integrations.mem0, knowlage_mcp.integrations.registry; assert knowlage_mcp.__version__ == "0.1.0"; skill = importlib.resources.files(knowlage_mcp).joinpath("resources", "SKILL.md"); assert skill.is_file() and skill.read_text(encoding="utf-8").strip()'

    run_in "$SMOKE_DIR" "knowlageMCP-gateway --version" \
        "$SMOKE_DIR/venv/bin/knowlageMCP-gateway" --version
    run_in "$SMOKE_DIR" "knowlageMCP-gatewayctl --version" \
        "$SMOKE_DIR/venv/bin/knowlageMCP-gatewayctl" --version
    run_in "$SMOKE_DIR" "knowlageMCP-mcp --version" \
        "$SMOKE_DIR/venv/bin/knowlageMCP-mcp" --version

    cleanup
    trap - EXIT
fi

printf '\n==> Summary: all requested checks passed\n'
printf 'Commands executed:\n'
for command in "${COMMANDS[@]}"; do
    printf '  %s\n' "$command"
done
