# SPDX-FileCopyrightText: 2026 René Widera
# SPDX-License-Identifier: ISC
"""Deterministic synthetic JSONL generator for the retrieval benchmark.

The generator is seeded, offline, and independent of wall-clock time. Every
compiler, version, host, component, and token is fictional. ``run_bench.py``
loads the output through the real service layer.
"""

from __future__ import annotations

import argparse
import json
import random
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

KINDS = (
    "failure",
    "observation",
    "procedure",
    "decision",
    "preference",
    "instruction",
    "hypothesis",
    "task_summary",
)
NAMESPACES = (
    ("shared", 0.68),
    ("project-atlas", 0.14),
    ("project-borealis", 0.10),
    ("personal", 0.08),
)
COMPONENTS = ("aurora-rt", "nova-kern", "helix-fft", "atlas-build")
REPOSITORIES = {
    "aurora-rt": "fictive/aurora",
    "nova-kern": "fictive/nova",
    "helix-fft": "fictive/helix",
    "atlas-build": "fictive/atlas",
}
VERSIONS = (
    "toolkit 2.2-fictive",
    "toolkit 3.0-fictive",
    "nova-kern 3.0-fictive",
    "helix 4.1-fictive",
)
COMPILERS = ("nvcc 12.4-fictive", "hipcc 6.2-fictive", "g++ 14.3-fictive")
BACKENDS = ("cuda", "hip", "sycl", "openmp")
HOSTS = ("node-orion-07", "node-vega-02", "node-lyra-11", "node-castor-04")
FEATURES = (
    "kernel cache",
    "full-text index",
    "async copy",
    "collective reduce",
    "fft plan",
    "build cache",
    "offload registry",
)
PROFILES = ("release", "debug", "sanitize")
ERRORS = (
    "nvcc fatal : Unsupported gpu architecture 'compute_90'",
    "hipcc: error: unknown argument: '-fno-gpu-rdc'",
    "g++: error: unrecognized command-line option '-march=native+avx512'",
    "ld: undefined reference to `__cudaRegisterFatBinaryEnd'",
    "CMake Error: No CMAKE_CUDA_COMPILER could be found",
    "ModuleNotFoundError: No module named 'fictive_rt'",
)


def _namespace(rng: random.Random) -> str:
    names = [name for name, _ in NAMESPACES]
    weights = [weight for _, weight in NAMESPACES]
    return rng.choices(names, weights=weights, k=1)[0]


def _scope(rng: random.Random, component: str) -> dict[str, str]:
    scope: dict[str, str] = {"repository": REPOSITORIES[component]}
    optional = (
        ("version", VERSIONS),
        ("compiler", COMPILERS),
        ("backend", BACKENDS),
        ("host", HOSTS),
    )
    for field, values in optional:
        if rng.random() < 0.55:
            scope[field] = rng.choice(values)
    if rng.random() < 0.20:
        scope["project"] = "atlas-build"
    if rng.random() < 0.10:
        scope["commit"] = f"fictive-commit-{rng.randrange(10_000):04d}"
    return scope


def _claim(rng: random.Random, index: int, component: str, kind: str, probe: str) -> tuple[str, str]:
    feature = rng.choice(FEATURES)
    host = rng.choice(HOSTS)
    profile = rng.choice(PROFILES)
    error = rng.choice(ERRORS)
    if kind == "failure":
        claim = (
            f"{error} while building {component}; row {probe} reproduced it on {host} with profile {profile}."
        )
        title = f"{component} failure {error.split(':')[0][:28]} {probe}"
    elif kind == "procedure":
        claim = (
            f"Run `{component}ctl rebuild --profile {profile}` to regenerate the {feature} "
            f"for {component}; row {probe} documents the command."
        )
        title = f"{component} rebuild procedure {probe}"
    elif kind == "observation":
        claim = (
            f"On {host}, {component} reports a stale {feature} after an interrupted copy; "
            f"row {probe} reproduces the observation."
        )
        title = f"{component} {feature} observation {probe}"
    elif kind == "decision":
        claim = (
            f"Pin {component} for the {profile} build until the {feature} migration lands; "
            f"row {probe} records the decision."
        )
        title = f"{component} version pin decision {probe}"
    elif kind == "preference":
        claim = (
            f"Prefer dedicated hosts when comparing {component} {feature} timings; "
            f"row {probe} notes the preference."
        )
        title = f"{component} timing preference {probe}"
    elif kind == "instruction":
        claim = (
            f"Never enable the {feature} on {host} during {profile} runs of {component}; "
            f"row {probe} records the instruction."
        )
        title = f"{component} host instruction {probe}"
    elif kind == "hypothesis":
        claim = (
            f"{component} latency grows when the {feature} is enabled with "
            f"{rng.choice(BACKENDS)}; row {probe} is an untested hypothesis."
        )
        title = f"{component} latency hypothesis {probe}"
    else:
        claim = (
            f"Migrated {component} to the {profile} profile; the {feature} now uses "
            f"{rng.choice(BACKENDS)} and row {probe} summarizes the task."
        )
        title = f"{component} migration summary {probe}"
    return title, claim


def generate_entries(count: int, seed: int = 7) -> Iterator[dict[str, object]]:
    """Yield ``count`` reproducible synthetic entries."""
    if count < 1:
        raise ValueError("count must be positive")
    rng = random.Random(seed)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for index in range(count):
        component = rng.choice(COMPONENTS)
        kind = KINDS[index % len(KINDS)]
        probe = f"probe{index:08d}"
        title, claim = _claim(rng, index, component, kind, probe)
        observed = base + timedelta(seconds=index)
        yield {
            "id": f"bench{index:08d}",
            "namespace": _namespace(rng),
            "kind": kind,
            "title": title,
            "claim": claim,
            "conditions": f"Synthetic benchmark row {index}; use only with the stated scope.",
            "scope": _scope(rng, component),
            "tags": [component, kind, rng.choice(BACKENDS)],
            "lifecycle": "published",
            "evidence_class": "operator_confirmed",
            "observed_at": observed.isoformat().replace("+00:00", "Z"),
            "valid_from": None,
            "valid_until": None,
            "supersedes": [],
            "contradicts": [],
            "sensitivity": "normal",
            "probe": probe,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entries", type=int, default=10_000, help="number of entries (default 10000)")
    parser.add_argument("--seed", type=int, default=7, help="deterministic seed (default 7)")
    parser.add_argument("--out", required=True, type=Path, help="output JSONL file")
    args = parser.parse_args(argv)

    if args.entries < 1:
        parser.error("--entries must be positive")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8", newline="\n") as handle:
        for entry in generate_entries(args.entries, args.seed):
            handle.write(json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n")
    print(f"wrote {args.entries} entries (seed={args.seed}) to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
