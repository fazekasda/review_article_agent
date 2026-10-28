#!/usr/bin/env python3
"""Deterministic **loopback fixture model transport** for the Step 04 acceptance suite.

This is **not** a model provider. It is a small, loopback-only, OpenAI-compatible
HTTP server used by ``tests/test_step04_service_acceptance.py`` to drive a *real*
app-owned OpenCode V2.2.0 service through a real, server-owned agent turn without
any paid provider call, provider credential, or network egress.

What is real and what is a fixture (stated plainly):

* **REAL** — the ``opencode`` v2.2.0 process, its HTTP API, HTTP Basic auth,
  session creation with an explicit role/model, durable prompt admission, SQLite
  persistence, the server-owned agent turn, and owned restart.
* **FIXTURE** — *only* the model transport: this process returns a deterministic
  synthetic reply (and, on request, an adversarial tool call) after an explicit,
  bounded delay.

It is a deliberate copy of the *protocol* of the retained step-00 spike fixture
(``spikes/runtime/integration/mock_provider.py``): the SSE chat-completions wire
shape the ai-sdk ``@ai-sdk/openai-compatible`` client uses. This module has **no
dependency** on the spike and no production dependency; it is a test child
process.

Contract implemented (the subset the OpenAI-compatible client uses):

* ``GET  /v1/models``           -> ``{"object":"list","data":[{"id":..,"object":"model"}]}``
* ``POST /v1/chat/completions`` -> OpenAI chat completion (SSE stream, or not).

Determinism / control (all from the request, never from a secret):

* The last user text may carry ``DELAY_MS=<n>`` (bounded by
  ``RAA_FIXTURE_MAX_DELAY_MS``) to delay the first token, so one server can drive
  both a slow (in-flight) and a fast turn.
* The last user text may carry ``TOOLCALL=<name>`` to make the fixture emit a
  single adversarial ``tool_calls`` delta naming ``<name>`` (the arguments embed
  ``RAA_FIXTURE_CANARY`` when set), so a test can prove an adversarial tool call
  is never executed by the service.

Every request is appended as one bounded JSON line to ``RAA_FIXTURE_LOG`` (when
set), which is how a test proves a real turn reached the transport and inspects
the **tool set the real service offered the model**. No credential is read,
written, or logged; the synthetic reply contains no secret.

Arrival is observable *before* the synthetic reply: the log line (with
``"kind": "chat.request"``) is written and flushed **before** any
``DELAY_MS`` sleep, so a test can boundedly wait for the exact prompt to reach
the model transport and then act while the turn is still live.

A readiness failure is reported on stdout as ``{"event": "fixture-error", ...}``
and exits non-zero, so a parent that bounds its startup read gets a typed
failure instead of blocking forever on a silent child.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

HOST = "127.0.0.1"
DEFAULT_MODELS = ("writer-model", "critic-model")
DEFAULT_DELAY_MS = int(os.environ.get("RAA_FIXTURE_DELAY_MS", "0"))
MAX_DELAY_MS = int(os.environ.get("RAA_FIXTURE_MAX_DELAY_MS", "30000"))
LOG_PATH = os.environ.get("RAA_FIXTURE_LOG")
CANARY = os.environ.get("RAA_FIXTURE_CANARY", "")
MODELS = tuple(
    m.strip()
    for m in os.environ.get("RAA_FIXTURE_MODELS", ",".join(DEFAULT_MODELS)).split(",")
    if m.strip()
)
REPLY_PREFIX = os.environ.get("RAA_FIXTURE_REPLY_PREFIX", "fixture-synthetic:")
PROMPT_LOG_LIMIT = 400
#: Upper bound (bytes) on one request body; a larger body is refused, so the
#: fixture can never be made to buffer an unbounded payload.
MAX_REQUEST_BYTES = 1_048_576

_DELAY_RE = re.compile(r"DELAY_MS=(\d+)")
_TOOLCALL_RE = re.compile(r"TOOLCALL=([A-Za-z0-9_.:-]+)")
_log_lock = threading.Lock()


def _log(entry: dict[str, Any]) -> None:
    if not LOG_PATH:
        return
    line = json.dumps(entry, sort_keys=True)
    with _log_lock:
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def _last_user_text(messages: Any) -> str:
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ]
            return "\n".join(parts)
    return ""


def _tool_names(body: dict[str, Any]) -> list[str]:
    """The function names the service offered the model in this request."""
    tools = body.get("tools")
    names: list[str] = []
    if isinstance(tools, list):
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            function = tool.get("function")
            if isinstance(function, dict) and isinstance(function.get("name"), str):
                names.append(function["name"])
    return names


def _delay_ms(text: str) -> int:
    match = _DELAY_RE.search(text)
    requested = int(match.group(1)) if match else DEFAULT_DELAY_MS
    return max(0, min(requested, MAX_DELAY_MS))


def _toolcall_name(text: str) -> str | None:
    match = _TOOLCALL_RE.search(text)
    return match.group(1) if match else None


def _toolcall_arguments(name: str) -> str:
    if name in ("shell", "bash"):
        return json.dumps({"command": f"touch {CANARY}" if CANARY else "true"})
    return json.dumps({"path": CANARY or "probe"})


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: Any) -> None:  # keep the fixture quiet
        return

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.path.rstrip("/") in ("/v1/models", "/models"):
            self._json(
                200,
                {"object": "list", "data": [{"id": m, "object": "model"} for m in MODELS]},
            )
            return
        self._json(404, {"error": {"message": "no route", "type": "invalid_request_error"}})

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        if self.path.rstrip("/") not in ("/v1/chat/completions", "/chat/completions"):
            self._json(404, {"error": {"message": "no route", "type": "invalid_request_error"}})
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length < 0 or length > MAX_REQUEST_BYTES:
            self._json(
                413,
                {"error": {"message": "request body too large", "type": "invalid_request_error"}},
            )
            return
        try:
            body = json.loads(self.rfile.read(length) if length else b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": {"message": "invalid JSON", "type": "invalid_request_error"}})
            return

        model = body.get("model", MODELS[0] if MODELS else "fixture-model")
        text = _last_user_text(body.get("messages"))
        delay = _delay_ms(text)
        toolcall = _toolcall_name(text)
        # Logged (and flushed, one line per file open) *before* any delay, so the
        # arrival is observable while the turn is still live.
        _log(
            {
                "kind": "chat.request",
                "ts": time.time(),
                "model": model,
                "stream": bool(body.get("stream")),
                "delayMs": delay,
                "toolNames": _tool_names(body),
                "messageRoles": [
                    m.get("role")
                    for m in body.get("messages", [])
                    if isinstance(m, dict)
                ],
                "toolCallRequested": toolcall,
                "prompt": text[:PROMPT_LOG_LIMIT],
            }
        )

        reply = f"{REPLY_PREFIX}{text[:200]}"
        if body.get("stream"):
            self._stream(model, reply, toolcall, delay)
        else:
            if delay:
                time.sleep(delay / 1000.0)
            message: dict[str, Any] = {"role": "assistant", "content": reply}
            finish = "stop"
            if toolcall:
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_fixture",
                            "type": "function",
                            "function": {"name": toolcall, "arguments": _toolcall_arguments(toolcall)},
                        }
                    ],
                }
                finish = "tool_calls"
            self._json(
                200,
                {
                    "id": "chatcmpl-fixture",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )

    def _stream(self, model: str, reply: str, toolcall: str | None, delay: int) -> None:
        created = int(time.time())

        def chunk(delta: dict[str, Any], finish: str | None) -> bytes:
            payload = {
                "id": "chatcmpl-fixture",
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            return f"data: {json.dumps(payload)}\n\n".encode("utf-8")

        if delay:
            time.sleep(delay / 1000.0)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self.wfile.write(chunk({"role": "assistant"}, None))
            if toolcall:
                self.wfile.write(
                    chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_fixture",
                                    "type": "function",
                                    "function": {
                                        "name": toolcall,
                                        "arguments": _toolcall_arguments(toolcall),
                                    },
                                }
                            ]
                        },
                        None,
                    )
                )
                self.wfile.write(chunk({}, "tool_calls"))
            else:
                self.wfile.write(chunk({"content": reply}, None))
                self.wfile.write(chunk({}, "stop"))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # The service (or its process) went away mid-stream; that is exactly
            # the interruption an owned-restart test wants to observe.
            _log({"ts": time.time(), "event": "client-disconnected-during-stream"})


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):  # noqa: ANN001 - keep the child quiet
        return


def main() -> int:
    port = int(os.environ.get("RAA_FIXTURE_PORT", "0"))
    try:
        server = _Server((HOST, port), _Handler)
    except OSError as exc:
        # A typed readiness failure on stdout (never a silent child), so the
        # parent's bounded startup read fails fast with a typed error.
        print(
            json.dumps(
                {"event": "fixture-error", "error": "bind", "detail": exc.__class__.__name__}
            ),
            flush=True,
        )
        return 2
    actual = server.server_address[1]
    print(
        json.dumps({"event": "fixture-listening", "host": HOST, "port": actual, "models": list(MODELS)}),
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
