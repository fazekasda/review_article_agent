#!/usr/bin/env python3
"""Balanced test-shard planner for the CI ``full`` job.

Stdlib-only and offline. Discovers the test modules under a tests directory
(``tests/test_*.py``), estimates each module's runtime from a recorded
``durations.json`` (modules with no recorded duration fall back to the median of
the known durations, or ``1.0`` when nothing is known), and assigns modules to
shards with a deterministic longest-processing-time (LPT) greedy heuristic so a
CI matrix can run the suite in parallel.

Commands
--------
- ``--shards N --index I``: print the dotted module names (``tests.test_x``)
  assigned to shard ``I`` (0-based), one per line, sorted by name.
- ``--plan --shards N``: print a human-readable table of all shards (module
  count and estimated seconds); ``--format json`` emits machine-readable JSON
  instead.
- ``--merge OUT IN [IN ...]``: merge per-shard timing JSON files (same schema as
  ``durations.json``) into ``OUT`` so ``durations.json`` can be refreshed from
  CI artifacts. Later files win for the same module.

The assignment is deterministic: modules are sorted by recorded duration
descending then by name, and each is placed on the currently lightest shard
(ties go to the lowest shard index). Every module appears in exactly one shard;
empty shards are allowed when there are more shards than modules.

Exit codes: ``0`` on success, ``2`` on invalid input (bad ``N``/``I``, a missing
merge input, or a malformed durations file).

Run the planner's own tests with:

    python3 -m unittest discover -s scripts -p 'test_*.py'
"""

import argparse
import json
import os
import statistics
import sys

SCHEMA = 1
UNIT = "seconds"
DEFAULT_DURATION = 1.0


class ShardError(Exception):
    """Raised for invalid inputs; the CLI maps this to exit code 2."""


# --------------------------------------------------------------------------- #
# discovery and duration loading
# --------------------------------------------------------------------------- #
def repo_root():
    """The repository root: the parent of the directory holding this script."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_tests_dir():
    return os.path.join(repo_root(), "tests")


def discover_modules(tests_dir):
    """Return the sorted basenames (without ``.py``) of ``tests/test_*.py``."""
    if not os.path.isdir(tests_dir):
        raise ShardError(f"tests directory not found: {tests_dir}")
    names = []
    for entry in os.listdir(tests_dir):
        if entry.startswith("test_") and entry.endswith(".py"):
            names.append(entry[: -len(".py")])
    return sorted(names)


def load_durations(path, missing_ok=True):
    """Load ``{module: float}`` from a ``durations.json`` file.

    A missing file yields ``{}`` when ``missing_ok`` (all modules then fall back
    to the default). A present but malformed file always raises ``ShardError``.
    """
    if not os.path.exists(path):
        if missing_ok:
            return {}
        raise ShardError(f"durations file not found: {path}")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ShardError(f"cannot read durations file {path}: {exc}")
    if not isinstance(data, dict):
        raise ShardError(f"durations file {path}: top level must be a JSON object")
    if "schema" in data and data["schema"] != SCHEMA:
        raise ShardError(
            f"durations file {path}: unsupported schema {data['schema']!r} "
            f"(expected {SCHEMA})")
    modules = data.get("modules", {})
    if not isinstance(modules, dict):
        raise ShardError(f"durations file {path}: 'modules' must be a JSON object")
    out = {}
    for key, value in modules.items():
        if not isinstance(key, str):
            raise ShardError(f"durations file {path}: module keys must be strings")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ShardError(
                f"durations file {path}: duration for {key!r} must be a number")
        if value < 0:
            raise ShardError(
                f"durations file {path}: duration for {key!r} must be non-negative")
        out[key] = float(value)
    return out


def effective_durations(modules, durations):
    """Map every discovered module to a duration, applying the default.

    Recorded durations for modules that no longer exist are ignored. The default
    is the median of the recorded durations of the discovered modules, or
    ``1.0`` when none is known.
    """
    known = {m: durations[m] for m in modules if m in durations}
    default = statistics.median(known.values()) if known else DEFAULT_DURATION
    return {m: float(durations.get(m, default)) for m in modules}


# --------------------------------------------------------------------------- #
# assignment
# --------------------------------------------------------------------------- #
def assign_shards(modules, durations, n):
    """LPT-greedy assignment of ``modules`` to ``n`` shards.

    Returns a list of ``{"index", "modules", "estimated_seconds"}`` dicts, one
    per shard, in shard order; each ``modules`` list is sorted by name.
    """
    if n <= 0:
        raise ShardError(f"--shards must be a positive integer, got {n}")
    eff = effective_durations(modules, durations)
    ordered = sorted(modules, key=lambda m: (-eff[m], m))
    buckets = [[] for _ in range(n)]
    loads = [0.0] * n
    for module in ordered:
        idx = min(range(n), key=lambda i: (loads[i], i))
        buckets[idx].append(module)
        loads[idx] += eff[module]
    shards = []
    for i in range(n):
        mods = sorted(buckets[i])
        shards.append({
            "index": i,
            "modules": mods,
            "estimated_seconds": round(sum(eff[m] for m in mods), 1),
        })
    return shards


def dotted(module):
    return "tests." + module


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def format_plan_table(shards):
    lines = [f"{'shard':>5}  {'modules':>7}  {'seconds':>9}"]
    total_modules = 0
    total_seconds = 0.0
    for shard in shards:
        total_modules += len(shard["modules"])
        total_seconds += shard["estimated_seconds"]
        lines.append(
            f"{shard['index']:>5}  {len(shard['modules']):>7}  "
            f"{shard['estimated_seconds']:>9.1f}")
    lines.append(f"{'total':>5}  {total_modules:>7}  {total_seconds:>9.1f}")
    return "\n".join(lines)


def plan_json(shards):
    payload = {
        "shards": [
            {
                "index": shard["index"],
                "estimated_seconds": shard["estimated_seconds"],
                "modules": [dotted(m) for m in shard["modules"]],
            }
            for shard in shards
        ]
    }
    return json.dumps(payload, indent=2)


# --------------------------------------------------------------------------- #
# merge / write
# --------------------------------------------------------------------------- #
def merge_durations(paths):
    """Merge ``{module: duration}`` maps; later files win for a shared module."""
    merged = {}
    for path in paths:
        for module, seconds in load_durations(path, missing_ok=False).items():
            merged[module] = seconds
    return merged


def write_durations(path, modules):
    """Write a canonical ``durations.json`` (schema 1, sorted, rounded to 0.1)."""
    payload = {
        "schema": SCHEMA,
        "unit": UNIT,
        "modules": {k: round(float(v), 1) for k, v in sorted(modules.items())},
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser():
    parser = argparse.ArgumentParser(
        description="Plan balanced test shards from recorded module durations.")
    parser.add_argument("--shards", type=int, metavar="N",
                        help="number of shards")
    parser.add_argument("--index", type=int, metavar="I",
                        help="0-based shard index to print modules for")
    parser.add_argument("--plan", action="store_true",
                        help="print a table of all shards (needs --shards)")
    parser.add_argument("--format", choices=("table", "json"), default="table",
                        help="output format for --plan (default: table)")
    parser.add_argument("--tests-dir", default=None,
                        help="tests directory (default: <repo>/tests)")
    parser.add_argument("--durations", default=None,
                        help="durations file (default: <tests-dir>/durations.json)")
    parser.add_argument("--merge", nargs="+", metavar="PATH",
                        help="merge OUT IN [IN ...] timing files into OUT")
    return parser


def run(args):
    tests_dir = args.tests_dir or default_tests_dir()
    durations_path = args.durations or os.path.join(tests_dir, "durations.json")

    if args.merge is not None:
        if len(args.merge) < 2:
            raise ShardError("--merge needs OUT and at least one IN file")
        out_path, in_paths = args.merge[0], args.merge[1:]
        merged = merge_durations(in_paths)
        write_durations(out_path, merged)
        print(f"wrote {out_path} ({len(merged)} modules)")
        return 0

    if args.plan:
        if args.shards is None:
            raise ShardError("--plan requires --shards N")
        shards = assign_shards(discover_modules(tests_dir),
                               load_durations(durations_path), args.shards)
        if args.format == "json":
            print(plan_json(shards))
        else:
            print(format_plan_table(shards))
        return 0

    if args.shards is None and args.index is None:
        raise ShardError("choose --plan, --shards N --index I, or --merge")

    if args.shards is None or args.index is None:
        raise ShardError("--shards and --index must be given together")

    n, index = args.shards, args.index
    if n <= 0:
        raise ShardError(f"--shards must be a positive integer, got {n}")
    if index < 0 or index >= n:
        raise ShardError(f"--index must be in [0, {n}), got {index}")

    shards = assign_shards(discover_modules(tests_dir),
                           load_durations(durations_path), n)
    for module in shards[index]["modules"]:
        print(dotted(module))
    return 0


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except ShardError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
