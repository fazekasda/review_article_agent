"""Process entry point for the standalone synthetic worker.

``python -m review_article_agent.worker --data-dir <ABS>`` starts the single
owner-authenticated control socket for ``<ABS>`` and runs the engine loop until
an owner issues ``{"version": 1, "op": "stop"}``.

**Ordering is safety-critical.** The engine object may be *constructed* (it only
reads the registry/backend paths), but **no recovery or scheduling state is
mutated until this process owns the IPC singleton lock** — otherwise a second
worker whose lock acquisition fails could still interrupt the first worker's
live attempts.

The worker is a local, stdlib-only process. It makes no real model, network, or
scientific call; every task is a bounded synthetic numeric task. All failures
are reported as a static, curated line on stderr (never a raw exception or any
request payload).
"""

from __future__ import annotations

import argparse
import math
import sys

from ..runtime.ipc import WorkerIPCServer
from .engine import WorkerEngine

#: Static, curated process-failure messages (never reflect an exception/payload).
_START_FAILED = "worker failed to start"
_RUNTIME_FAILED = "worker stopped unexpectedly"
_BAD_OPTION = "worker: invalid --poll-timeout (must be a finite number > 0)"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review_article_agent.worker",
        description="Standalone durable synthetic worker (Step 03).",
    )
    parser.add_argument(
        "--data-dir",
        required=True,
        help="Absolute application data directory (holds the registry and the "
        "owner-private control socket under <data-dir>/runtime).",
    )
    parser.add_argument(
        "--poll-timeout",
        type=float,
        default=0.5,
        help="Bounded poll interval, in seconds, for in-flight work.",
    )
    return parser


def _serve(engine: WorkerEngine, server: WorkerIPCServer, poll_timeout: float) -> None:
    """Run the control loop until the engine is asked to stop.

    Between control requests the loop advances one unit of scheduling; when
    there is no pending work it blocks indefinitely on ``serve_once(None)`` so it
    never busy-loops while idle or waiting for a human gate.
    """
    while not engine.stopping:
        timeout = engine.tick()
        if timeout is None:
            wait: float | None = None
        elif timeout <= 0.0:
            wait = 0.0
        else:
            wait = min(timeout, poll_timeout)
        server.serve_once(wait)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    # A non-finite / non-positive poll interval would busy-loop or block.
    if not math.isfinite(args.poll_timeout) or args.poll_timeout <= 0:
        print(_BAD_OPTION, file=sys.stderr)
        return 2

    try:
        engine = WorkerEngine(args.data_dir)
        server = WorkerIPCServer(args.data_dir, engine.handle)
    except Exception:
        print(_START_FAILED, file=sys.stderr)
        return 1

    # 1) Own the singleton lock BEFORE any recovery/scheduling mutation.
    try:
        server.start()
    except Exception:
        print(_START_FAILED, file=sys.stderr)
        engine.close()
        return 1

    try:
        # 2) Only now reconcile recorded attempts and begin serving.
        engine.reconcile_startup()
        _serve(engine, server, args.poll_timeout)
    except Exception:
        print(_RUNTIME_FAILED, file=sys.stderr)
        return 1
    finally:
        try:
            server.close()
        finally:
            engine.close()
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
