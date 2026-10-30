#!/usr/bin/env python3
"""Parallel, stdlib-only local test runner for ``tests/test_*.py``.

Runs one subprocess per test *module* in parallel (module-level parallelism was
verified safe for this suite), prints a progress line as each module finishes,
the full captured output of every failure, a summary, and the slowest modules.

Usage
-----
    python3 scripts/run_tests.py [MODULE ...] [options]

Positional modules are optional. Each may be given as ``tests.test_x``,
``test_x`` or ``tests/test_x.py``. With no modules, every ``test_*.py`` under
``--tests-dir`` (default ``<repo>/tests``) is run.

Each module runs as::

    python3 -m unittest tests.<module>

with ``cwd`` set to the repo root and an environment that copies ``os.environ``,
prepends ``src:tests`` to ``PYTHONPATH`` and sets ``PYTHONDONTWRITEBYTECODE=1``.
``--slow`` additionally sets ``RAA_SLOW_TESTS=1`` for the child (the slow tier
introduced separately); without it the variable is removed from the child env.
By default the child also gets ``RAA_TEST_SQLITE_SYNCHRONOUS=OFF``, the
test-only hook that lowers SQLite durability for throwaway temp stores; pass
``--durable-sqlite`` to keep the production ``PRAGMA synchronous = FULL``.

Exit codes: ``0`` all passed and within budget, ``1`` any failure/timeout or a
budget overrun, ``2`` usage error.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import threading
import time

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_REPO = os.path.dirname(SCRIPTS_DIR)

# unittest summary line, e.g. "OK", "OK (skipped=3)",
# "FAILED (failures=1, errors=2, skipped=3)".
_RAN_RE = re.compile(r"^Ran (\d+) tests? in ", re.M)
_COUNT_RE = re.compile(r"([A-Za-z][A-Za-z ]*?)=(\d+)")

# Live child processes, so Ctrl-C can terminate them (best effort).
_procs: set[subprocess.Popen] = set()
_procs_lock = threading.Lock()


def _kill_children() -> None:
    with _procs_lock:
        procs = list(_procs)
    for proc in procs:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001 - best effort
            pass


def normalize_module(raw: str) -> str:
    """Accept ``tests.test_x``, ``test_x`` or ``tests/test_x.py`` -> ``test_x``."""
    name = raw.strip()
    if name.endswith(".py"):
        name = name[:-3]
    name = name.replace("\\", "/")
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    if name.startswith("tests."):
        name = name[len("tests."):]
    if "." in name:
        name = name.rsplit(".", 1)[-1]
    return name


def discover_modules(tests_dir: str) -> list[str]:
    """Return the sorted module stems of ``test_*.py`` under *tests_dir*."""
    try:
        entries = os.listdir(tests_dir)
    except OSError:
        return []
    names = [
        e[:-3]
        for e in entries
        if e.startswith("test_") and e.endswith(".py")
    ]
    return sorted(names)


def resolve_modules(requested: list[str], tests_dir: str) -> list[str]:
    """Normalize and validate requested modules against *tests_dir*.

    Raises ``SystemExit(2)`` for an unknown module. With no request, returns the
    discovered modules.
    """
    if not requested:
        return discover_modules(tests_dir)
    resolved = []
    for raw in requested:
        name = normalize_module(raw)
        if not name or not os.path.isfile(os.path.join(tests_dir, name + ".py")):
            sys.stderr.write(
                f"error: unknown module {raw!r} "
                f"(no {name or raw}.py under {tests_dir})\n"
            )
            raise SystemExit(2)
        if name not in resolved:
            resolved.append(name)
    return resolved


def parse_output(text: str) -> dict:
    """Parse a unittest run's combined output into a result summary.

    Returns ``{tests, ok, failed, failures, errors, skipped}``. ``ok`` is True
    only when the final summary line is an ``OK`` line.
    """
    tests = 0
    m = _RAN_RE.search(text)
    if m:
        tests = int(m.group(1))
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    last = lines[-1] if lines else ""
    ok = last.startswith("OK")
    failed = last.startswith("FAILED")
    counts = {"failures": 0, "errors": 0, "skipped": 0}
    if ok or failed:
        for key, value in _COUNT_RE.findall(last):
            key = key.strip()
            if key in counts:
                counts[key] = int(value)
    return {
        "tests": tests,
        "ok": ok,
        "failed": failed,
        "failures": counts["failures"],
        "errors": counts["errors"],
        "skipped": counts["skipped"],
    }


def build_env(base_env: dict, slow: bool, durable_sqlite: bool = False) -> dict:
    """Return the child environment for a module run.

    By default the child gets ``RAA_TEST_SQLITE_SYNCHRONOUS=OFF``, the test-only
    hook that lowers SQLite durability for throwaway temp stores (see
    ``storage.schema.resolve_synchronous``). ``durable_sqlite`` restores the
    production ``FULL`` by removing the variable from the child environment.
    """
    env = dict(base_env)
    prefix = "src:tests"
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = prefix + (os.pathsep + existing if existing else "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if slow:
        env["RAA_SLOW_TESTS"] = "1"
    else:
        env.pop("RAA_SLOW_TESTS", None)
    if durable_sqlite:
        env.pop("RAA_TEST_SQLITE_SYNCHRONOUS", None)
    else:
        env["RAA_TEST_SQLITE_SYNCHRONOUS"] = "OFF"
    return env


def run_module(
    name: str,
    repo: str,
    slow: bool,
    timeout: float | None,
    durable_sqlite: bool = False,
) -> dict:
    """Run one module in a subprocess and return its result dict."""
    env = build_env(os.environ, slow, durable_sqlite)
    cmd = [sys.executable, "-m", "unittest", f"tests.{name}"]
    start = time.monotonic()
    timed_out = False
    proc = subprocess.Popen(
        cmd,
        cwd=repo,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    with _procs_lock:
        _procs.add(proc)
    try:
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
            out, _ = proc.communicate()
            out = (out or "") + f"\nTIMEOUT: exceeded {timeout}s\n"
    finally:
        with _procs_lock:
            _procs.discard(proc)
    elapsed = time.monotonic() - start
    parsed = parse_output(out)
    passed = (not timed_out) and proc.returncode == 0 and parsed["ok"]
    return {
        "name": name,
        "seconds": elapsed,
        "output": out,
        "returncode": proc.returncode,
        "timed_out": timed_out,
        "passed": passed,
        **parsed,
    }


def write_durations(path: str, results: list[dict]) -> None:
    """Write the shard-balancer durations file (schema 1, seconds, 0.1s)."""
    modules = {r["name"]: round(r["seconds"], 1) for r in results}
    payload = {"schema": 1, "unit": "seconds", "modules": dict(sorted(modules.items()))}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")


def _run_all(modules, repo, jobs, slow, timeout, durable_sqlite=False):
    """Run every module in parallel; yield results as they complete."""
    results = []
    if not modules:
        return results
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=jobs)
    futures = {
        executor.submit(
            run_module, name, repo, slow, timeout, durable_sqlite
        ): name
        for name in modules
    }
    try:
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            results.append(result)
            status = "PASS" if result["passed"] else "FAIL"
            sys.stdout.write(
                f"{status}  {result['seconds']:.1f}s  {result['name']}  "
                f"({result['tests']} tests)\n"
            )
            sys.stdout.flush()
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_tests.py",
        description="Run tests/test_*.py modules in parallel (stdlib only).",
    )
    parser.add_argument("modules", nargs="*", help="modules to run (default: all)")
    parser.add_argument(
        "-j", "--jobs", type=int, default=os.cpu_count() or 1,
        help="parallel worker count (default: os.cpu_count())",
    )
    parser.add_argument(
        "--slow", action="store_true",
        help="set RAA_SLOW_TESTS=1 for the child processes (slow tier)",
    )
    parser.add_argument(
        "--durable-sqlite", action="store_true",
        help="keep production PRAGMA synchronous=FULL in children "
        "(default: test-only RAA_TEST_SQLITE_SYNCHRONOUS=OFF)",
    )
    parser.add_argument(
        "--top", type=int, default=10,
        help="print the N slowest modules (0 disables; default: 10)",
    )
    parser.add_argument(
        "--durations-out", default=None, metavar="PATH",
        help="write per-module seconds as JSON for the shard balancer",
    )
    parser.add_argument(
        "--budget", type=float, default=None, metavar="SECONDS",
        help="fail (non-zero) if total wall time exceeds SECONDS",
    )
    parser.add_argument(
        "--timeout", type=float, default=None, metavar="SECONDS",
        help="per-module timeout; a timed-out module is marked FAIL",
    )
    parser.add_argument(
        "--repo", default=DEFAULT_REPO,
        help="repo root the modules run from (default: parent of this script)",
    )
    parser.add_argument(
        "--tests-dir", default=None,
        help="directory holding test modules (default: <repo>/tests)",
    )
    args = parser.parse_args(argv)

    if args.jobs < 1:
        parser.error("--jobs must be >= 1")
    if args.top < 0:
        parser.error("--top must be >= 0")
    if args.budget is not None and args.budget < 0:
        parser.error("--budget must be >= 0")
    if args.timeout is not None and args.timeout <= 0:
        parser.error("--timeout must be > 0")

    repo = os.path.abspath(args.repo)
    tests_dir = args.tests_dir or os.path.join(repo, "tests")
    if not os.path.isabs(tests_dir):
        tests_dir = os.path.abspath(tests_dir)
    if not os.path.isdir(tests_dir):
        parser.error(f"--tests-dir is not a directory: {tests_dir}")

    modules = resolve_modules(args.modules, tests_dir)

    wall_start = time.monotonic()
    try:
        results = _run_all(
            modules, repo, args.jobs, args.slow, args.timeout,
            args.durable_sqlite,
        )
    except KeyboardInterrupt:
        _kill_children()
        sys.stdout.write("\ninterrupted; terminating child processes\n")
        return 130
    wall = time.monotonic() - wall_start

    failed = [r for r in results if not r["passed"]]
    for result in failed:
        sys.stdout.write(f"\n===== FAIL {result['name']} =====\n")
        sys.stdout.write(result["output"])
        if not result["output"].endswith("\n"):
            sys.stdout.write("\n")

    totals = {
        "tests": sum(r["tests"] for r in results),
        "failures": sum(r["failures"] for r in results),
        "errors": sum(r["errors"] for r in results),
        "skipped": sum(r["skipped"] for r in results),
    }
    total_module_seconds = sum(r["seconds"] for r in results)

    sys.stdout.write(
        f"\nmodules: {len(results)}  tests: {totals['tests']}  "
        f"failures: {totals['failures']}  errors: {totals['errors']}  "
        f"skipped: {totals['skipped']}\n"
    )
    sys.stdout.write(
        f"wall: {wall:.1f}s  sum: {total_module_seconds:.1f}s\n"
    )

    if args.top > 0 and results:
        slowest = sorted(results, key=lambda r: r["seconds"], reverse=True)
        sys.stdout.write(f"\nslowest {min(args.top, len(slowest))}:\n")
        for result in slowest[: args.top]:
            sys.stdout.write(f"  {result['seconds']:.1f}s  {result['name']}\n")

    if args.durations_out:
        write_durations(args.durations_out, results)

    over_budget = args.budget is not None and wall > args.budget
    if over_budget:
        sys.stdout.write(
            f"\nbudget exceeded: {wall:.1f}s > {args.budget:.1f}s\n"
        )

    if failed or over_budget:
        sys.stdout.write(
            f"\nFAILED: {len(failed)} module(s) failed"
            + (" and budget exceeded" if over_budget else "")
            + "\n"
        )
        return 1
    sys.stdout.write("\nOK: all modules passed\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        _kill_children()
        sys.exit(130)
