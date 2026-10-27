#!/usr/bin/env python3
"""Python-side lifecycle worker for the step-00 integration spike.

One command per invocation so the orchestrator can run each step in a **separate
process**. That is what proves the *disconnect* criterion against the real
app-owned service: the submitting process exits after printing its durable
admission receipt, the server-owned turn keeps running, and a *different*
process recovers the outcome from persisted state after missing every event.

It uses the untouched Python prototype client,
``spikes/runtime/python/opencode_v2.py`` (imported from this directory), against
a real, explicitly supplied loopback endpoint with HTTP Basic auth. No service
discovery, no private database/registration, no provider credentials.

Usage (all endpoints/credentials are explicit; the password is read from a
file, never passed on the command line):

    py_lifecycle.py info       --base-url URL --password-file FILE
    py_lifecycle.py create     --base-url URL --password-file FILE --directory DIR
                               --agent build --model provider/id [--title T]
    py_lifecycle.py submit     --base-url URL --password-file FILE --session SID
                               --id msg_x --text TEXT [--delivery queue]
    py_lifecycle.py reconcile  --base-url URL --password-file FILE --session SID --id msg_x
    py_lifecycle.py messages   --base-url URL --password-file FILE --session SID [--admission-id msg_x]
    py_lifecycle.py critic     --base-url URL --password-file FILE --directory DIR
                               --agent R --model provider/id --writer-model provider/id

All output is a single JSON object on stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
PYTHON_PROTO = os.path.abspath(os.path.join(HERE, "..", "python"))
sys.path.insert(0, PYTHON_PROTO)

from opencode_v2 import (  # noqa: E402  (path is set above)
    OpenCodeV2Client,
    QuiescenceTimeout,
)

DEFAULT_USERNAME = "opencode"


def _model(value: str) -> dict[str, str]:
    provider, _, ident = value.partition("/")
    if not provider or not ident:
        raise SystemExit(f"--model must be provider/id, got {value!r}")
    return {"providerID": provider, "id": ident}


def _client(args: argparse.Namespace) -> OpenCodeV2Client:
    return OpenCodeV2Client(
        args.base_url,
        auth="basic",
        username=args.username or DEFAULT_USERNAME,
        password_file=args.password_file,
        directory=getattr(args, "directory", None),
    )


def _emit(payload: dict[str, Any]) -> None:
    json.dump(payload, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")


def cmd_info(args: argparse.Namespace) -> int:
    info = _client(args).info()
    _emit({"prototype": "python", "version": info.get("version"), "pid": info.get("pid")})
    return 0


def cmd_create(args: argparse.Namespace) -> int:
    session = _client(args).create_session(
        agent=args.agent, model=_model(args.model), title=args.title
    )
    _emit(
        {
            "prototype": "python",
            "session": session,
            "fresh": "parentID" not in session and "fork" not in session,
        }
    )
    return 0


def cmd_submit(args: argparse.Namespace) -> int:
    client = _client(args)
    admission = client.admit(
        args.session, args.text, args.id, delivery=args.delivery
    )
    _emit(
        {
            "prototype": "python",
            "admissionId": admission.admission_id,
            "durableReceipt": admission.has_durable_receipt,
            "duplicate": admission.duplicate,
            "reconciled": admission.reconciled,
            "receipt": admission.receipt,
            # Explicitly show the receipt is not a completion.
            "receiptHasOutcome": bool(admission.receipt and "outcome" in admission.receipt),
        }
    )
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    found = _client(args).reconcile(args.session, args.id)
    _emit(
        {
            "prototype": "python",
            "admissionId": found.admission_id,
            "state": found.state,
            "isComplete": found.is_complete,
            "isTerminal": found.is_terminal,
            "outcome": found.outcome,
            "reason": found.reason,
            "hasCompletion": found.completion is not None,
        }
    )
    return 0


def cmd_wait(args: argparse.Namespace) -> int:
    client = _client(args)
    try:
        idle = client.wait(args.session, timeout=args.timeout)
        timed_out = False
    except QuiescenceTimeout:
        idle = False
        timed_out = True
    _emit({"prototype": "python", "idle": idle, "timedOut": timed_out})
    return 0


def cmd_messages(args: argparse.Namespace) -> int:
    client = _client(args)
    messages, truncated = client.collect_messages(args.session, order="asc")
    types = [m.get("type") for m in messages]
    payload: dict[str, Any] = {
        "prototype": "python",
        "count": len(messages),
        "truncated": truncated,
        "types": types,
        "ids": [m.get("id") for m in messages],
        "idleOutcomes": [m.get("outcome") for m in messages if m.get("type") == "idle"],
    }
    if args.admission_id is not None:
        payload["userInputsForId"] = sum(
            1 for m in messages if m.get("type") == "user" and m.get("id") == args.admission_id
        )
    _emit(payload)
    return 0


def cmd_critic(args: argparse.Namespace) -> int:
    client = _client(args)
    writer = _model(args.writer_model)
    session = client.start_critic(
        agent=args.agent, model=_model(args.model), writer_model=writer, title=args.title
    )
    messages, _ = client.collect_messages(session["id"], order="asc")
    _emit(
        {
            "prototype": "python",
            "session": session,
            "fresh": "parentID" not in session and "fork" not in session,
            "historyLength": len(messages),
        }
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Python prototype lifecycle worker")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser, directory: bool = False) -> None:
        p.add_argument("--base-url", required=True)
        p.add_argument("--password-file", required=True)
        p.add_argument("--username", default=None)
        if directory:
            p.add_argument("--directory")

    p = sub.add_parser("info")
    common(p)
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("create")
    common(p, directory=True)
    p.add_argument("--agent", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--title", default=None)
    p.set_defaults(func=cmd_create)

    p = sub.add_parser("submit")
    common(p)
    p.add_argument("--session", required=True)
    p.add_argument("--id", required=True)
    p.add_argument("--text", required=True)
    p.add_argument("--delivery", default="queue", choices=["queue", "steer"])
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("reconcile")
    common(p)
    p.add_argument("--session", required=True)
    p.add_argument("--id", required=True)
    p.set_defaults(func=cmd_reconcile)

    p = sub.add_parser("wait")
    common(p)
    p.add_argument("--session", required=True)
    p.add_argument("--timeout", type=float, default=15.0)
    p.set_defaults(func=cmd_wait)

    p = sub.add_parser("messages")
    common(p)
    p.add_argument("--session", required=True)
    p.add_argument("--admission-id", default=None)
    p.set_defaults(func=cmd_messages)

    p = sub.add_parser("critic")
    common(p, directory=True)
    p.add_argument("--agent", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--writer-model", required=True)
    p.add_argument("--title", default=None)
    p.set_defaults(func=cmd_critic)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
