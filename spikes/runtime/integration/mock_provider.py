#!/usr/bin/env python3
"""Deterministic local *fixture* model provider for the step-00 integration spike.

This is **not** a real model provider. It is a small, loopback-only,
OpenAI-compatible HTTP server used to drive a *real* OpenCode V2 server-owned
agent turn without any paid provider call and without any network egress. The
OpenCode service, its HTTP API, admission, persistence and restart behaviour are
real; only the model transport is a fixture. That distinction is stated in the
integration README and in the recorded evidence.

Contract implemented (the subset the ai-sdk ``@ai-sdk/openai-compatible`` client
uses):

* ``GET  /v1/models``            -> ``{"object":"list","data":[{"id":...,"object":"model"}]}``
* ``POST /v1/chat/completions``  -> OpenAI chat completion, streaming (SSE) or not.

Determinism and bounded delay are controlled by the request:

* The delay before the first token is ``MOCK_DELAY_MS`` (default 0).
* A prompt whose text contains ``DELAY_MS=<n>`` uses that delay instead, so a
  single run can exercise both an in-flight (slow) and a fast turn. The delay is
  capped at ``MOCK_MAX_DELAY_MS`` (default 20000) so a runaway cannot hang a run.

Every request is appended as one JSON line to ``$MOCK_LOG`` (when set), which is
how the integration harness proves a real turn actually reached the provider.

Loopback only. No credentials are read, written or logged. Usage is reported as
zero-cost so no accounting is implied.
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
DEFAULT_DELAY_MS = int(os.environ.get("MOCK_DELAY_MS", "0"))
MAX_DELAY_MS = int(os.environ.get("MOCK_MAX_DELAY_MS", "20000"))
LOG_PATH = os.environ.get("MOCK_LOG")
REPLY_PREFIX = os.environ.get("MOCK_REPLY_PREFIX", "fixture-echo:")

_DELAY_RE = re.compile(r"DELAY_MS=(\d+)")
_log_lock = threading.Lock()


def _log(entry: dict[str, Any]) -> None:
    if not LOG_PATH:
        return
    line = json.dumps(entry, sort_keys=True)
    with _log_lock:
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def _extract_text(messages: Any) -> str:
    """Flatten the last user message's text for the deterministic reply."""
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        if message.get("role") != "user":
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


def _delay_ms(text: str) -> int:
    match = _DELAY_RE.search(text)
    requested = int(match.group(1)) if match else DEFAULT_DELAY_MS
    return max(0, min(requested, MAX_DELAY_MS))


class Handler(BaseHTTPRequestHandler):
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
            self._json(200, {"object": "list", "data": [{"id": "fixture-model", "object": "model"}]})
            return
        self._json(404, {"error": {"message": f"no route {self.path}", "type": "invalid_request_error"}})

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        if self.path.rstrip("/") not in ("/v1/chat/completions", "/chat/completions"):
            self._json(404, {"error": {"message": f"no route {self.path}", "type": "invalid_request_error"}})
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": {"message": "invalid JSON", "type": "invalid_request_error"}})
            return

        model = body.get("model", "fixture-model")
        text = _extract_text(body.get("messages"))
        delay = _delay_ms(text)
        reply = f"{REPLY_PREFIX}{text[:200]}"
        stream = bool(body.get("stream"))
        _log(
            {
                "ts": time.time(),
                "model": model,
                "stream": stream,
                "delayMs": delay,
                "prompt": text[:200],
            }
        )

        created = int(time.time())
        if stream:
            self._stream(model, reply, created, delay)
        else:
            if delay:
                time.sleep(delay / 1000.0)
            self._json(
                200,
                {
                    "id": "chatcmpl-fixture",
                    "object": "chat.completion",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": reply},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )

    def _stream(self, model: str, reply: str, created: int, delay: int) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

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
            # Bound the delay so a restart test can still observe progress later.
            time.sleep(delay / 1000.0)
        try:
            self.wfile.write(chunk({"role": "assistant"}, None))
            self.wfile.write(chunk({"content": reply}, None))
            self.wfile.write(chunk({}, "stop"))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # The service (or its process) went away mid-turn; that is exactly the
            # interruption the restart criterion wants to observe.
            _log({"ts": time.time(), "event": "client-disconnected-during-stream"})


def main() -> int:
    port = int(os.environ.get("MOCK_PORT", "0"))
    server = ThreadingHTTPServer((HOST, port), Handler)
    server.daemon_threads = True
    actual = server.server_address[1]
    # A single line the orchestrator reads to learn the bound port.
    print(json.dumps({"event": "mock-provider-listening", "host": HOST, "port": actual}), flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
