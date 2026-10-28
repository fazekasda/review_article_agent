"""Process entry point for the standalone synthetic worker.

``python -m review_article_agent.worker --data-dir <ABS>`` starts the single
owner-authenticated control socket for ``<ABS>`` and runs the engine loop until
an owner issues ``{"version": 1, "op": "stop"}``.

The worker is a local, stdlib-only process. It makes no real model, network, or
scientific call; every task is a bounded synthetic numeric task.
"""

from __future__ import annotations

import argparse
import sys

from ..runtime.ipc import WorkerIPCServer
from .engine import WorkerEngine


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
    engine = WorkerEngine(args.data_dir)
    server = WorkerIPCServer(args.data_dir, engine.handle)
    try:
        engine.reconcile_startup()
        server.start()
    except Exception as exc:  # curated, no traceback to the caller
        print(f"worker failed to start: {exc}", file=sys.stderr)
        engine.close()
        return 1
    try:
        _serve(engine, server, args.poll_timeout)
    finally:
        server.close()
        engine.close()
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
