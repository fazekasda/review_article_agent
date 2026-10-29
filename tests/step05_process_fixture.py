"""Disposable child fixture for the E1 real-process acceptance tests.

This module is **test-only**: it is never imported by product code. It is launched
as a separate process so the acceptance test can drive a *real* detached worker
(real owner control socket, real kernel peer-credential verification, real SQLite
job/attempt state, real single-slot thread executor) while the **only** external
transport is a deterministic in-process stub.

It constructs a :class:`~review_article_agent.worker.engine.WorkerEngine` with:

* a fake low-level discovery GET transport (never expected to be called here);
* a fake hosted-lead POST transport that appends one line to the file named by
  ``RAA_FIXTURE_LEAD_COUNTER`` on every POST, so a test can prove exactly one
  POST is made across a restart; and
* an explicit synthetic ``lead_api_key`` (never a real credential, never a real
  network call).

All behavior is fully synthetic: no live, paid, upload, or provider call is made.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from review_article_agent.runtime.ipc import WorkerIPCServer  # noqa: E402
from review_article_agent.runtime.lead import (  # noqa: E402
    TransportResponse as LeadResponse,
)
from review_article_agent.worker.engine import WorkerEngine  # noqa: E402


def _completion_body(model: str) -> str:
    return json.dumps(
        {
            "id": "gen-fixture-1",
            "model": model,
            "provider": "anthropic",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "synthetic proposal"},
                }
            ],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 8,
                "total_tokens": 20,
                "cost": 0.0002,
            },
        }
    )


def _build_engine(data_dir: str) -> WorkerEngine:
    model = os.environ.get("RAA_FIXTURE_LEAD_MODEL", "anthropic/claude-sonnet-5-5")
    counter = os.environ.get("RAA_FIXTURE_LEAD_COUNTER")

    def fake_discovery(url, *, timeout, max_response_bytes):  # pragma: no cover
        # No discovery provider request is expected in these acceptance cases.
        raise AssertionError("no discovery provider request was expected")

    def fake_lead(body, api_key, *, endpoint, timeout, max_response_bytes):
        if counter:
            with open(counter, "a", encoding="utf-8") as handle:
                handle.write("post\n")
                handle.flush()
                os.fsync(handle.fileno())
        return LeadResponse(status=200, body_text=_completion_body(model))

    return WorkerEngine(
        data_dir,
        discovery_transport=fake_discovery,
        lead_transport=fake_lead,
        lead_api_key="sk-or-fixture-synthetic",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="step05_process_fixture")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--poll-timeout", type=float, default=0.2)
    args = parser.parse_args(argv)

    engine = _build_engine(args.data_dir)
    server = WorkerIPCServer(args.data_dir, engine.handle)
    server.start()
    try:
        engine.reconcile_startup()
        while not engine.stopping:
            timeout = engine.tick()
            if timeout is None:
                wait: float | None = None
            elif timeout <= 0.0:
                wait = 0.0
            else:
                wait = min(timeout, args.poll_timeout)
            server.serve_once(wait)
    finally:
        try:
            server.close()
        finally:
            engine.close()
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
