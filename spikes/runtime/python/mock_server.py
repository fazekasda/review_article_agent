"""Deterministic local mock of the OpenCode V2 HTTP endpoints used by the spike.

This exists so ``test_opencode_v2.py`` and ``demo_lifecycle.py`` can exercise the
client without any live service, paid calls, service discovery, or private
credential/database access. It implements only the shapes the client sends and
reads, taken from the published V2 OpenAPI document:

* ``GET  /api/info``
* ``POST /api/session``
* ``GET  /api/session/{sid}``
* ``POST /api/session/{sid}/prompt``   (idempotent on the client-supplied id)
* ``GET  /api/session/{sid}/inbox``
* ``GET  /api/session/{sid}/message``
* ``GET  /api/session/{sid}/message/{mid}``
* ``POST /api/session/{sid}/interrupt``
* ``POST /api/session/{sid}/fork``
* ``POST /api/experimental/session/{sid}/wait``
* ``POST /api/experimental/generate/decisions``

The mock is a test double, not a compliant server: it validates just enough to
prove client behaviour.
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

SESSION_RE = re.compile(r"^/api/session/(ses[^/]+)$")
PROMPT_RE = re.compile(r"^/api/session/(ses[^/]+)/prompt$")
INBOX_RE = re.compile(r"^/api/session/(ses[^/]+)/inbox$")
MESSAGES_RE = re.compile(r"^/api/session/(ses[^/]+)/message$")
MESSAGE_RE = re.compile(r"^/api/session/(ses[^/]+)/message/(msg_[^/]+)$")
INTERRUPT_RE = re.compile(r"^/api/session/(ses[^/]+)/interrupt$")
FORK_RE = re.compile(r"^/api/session/(ses[^/]+)/fork$")
WAIT_RE = re.compile(r"^/api/experimental/session/(ses[^/]+)/wait$")


class MockState:
    """Mutable server state plus tunable failure modes for tests."""

    def __init__(self, *, token: str | None = None):
        self.token = token
        self.sessions: dict[str, dict[str, Any]] = {}
        self.inbox: dict[str, list[dict[str, Any]]] = {}
        self.messages: dict[str, list[dict[str, Any]]] = {}
        self.seen_admission: dict[str, str] = {}  # admission id -> session id
        self._counter = 0
        self._tick = 1_700_000_000_000
        # failure tunables
        self.prompt_drops = 0  # drop the connection N times, after accepting
        self.wait_status = 204  # 204 = idle; 503 = service unavailable
        self.wait_503_times = 0  # return 503 this many times before idle
        self.interrupt_result = True
        self.rename: dict[str, str] = {}
        self.fail_conflict_instead = False  # retry returns 409 even if unseen
        self.decision_answers: list[dict[str, Any]] | None = None
        self.message_page_size: int | None = None  # None = one unpaged page

    # -- helpers -------------------------------------------------------------
    def new_session_id(self) -> str:
        self._counter += 1
        return f"ses_mock{self._counter:04d}"

    def new_message_id(self) -> str:
        self._counter += 1
        return f"msg_mock{self._counter:04d}"

    def _next_time(self) -> int:
        self._tick += 1_000
        return self._tick

    def create_session(self, body: dict[str, Any]) -> dict[str, Any]:
        sid = body.get("id") or self.new_session_id()
        info = {
            "id": sid,
            "projectID": "prj_mock",
            "cost": 0,
            "tokens": {"input": 0, "output": 0},
            "time": {"created": 1_700_000_000_000, "updated": 1_700_000_000_000},
            "location": {"directory": (body.get("location") or {}).get("directory", "/tmp/mock")},
        }
        if body.get("agent") is not None:
            info["agent"] = body["agent"]
        if body.get("model") is not None:
            info["model"] = body["model"]
        if body.get("parentID") is not None:
            info["parentID"] = body["parentID"]
        if body.get("title") is not None:
            info["title"] = body["title"]
        if body.get("metadata") is not None:
            info["metadata"] = body["metadata"]
        self.sessions[sid] = info
        self.inbox.setdefault(sid, [])
        self.messages.setdefault(sid, [])
        return info

    def complete(self, sid: str, *, outcome: str = "succeeded") -> dict[str, Any]:
        """Mark a session complete and append the terminating idle message."""
        return self.append_idle(sid, outcome)

    def add_assistant(self, sid: str, text: str = "done", *, error: Any = None) -> dict[str, Any]:
        return self.append_assistant(sid, text, error=error)
    # -- ordered turn builders (newer repair tests) --------------------------
    def append_user(self, sid: str, mid: str, text: str = "") -> dict[str, Any]:
        """Project a delivered user message with the given admission ID."""
        message = {
            "id": mid,
            "time": {"created": self._next_time()},
            "text": text,
            "type": "user",
        }
        self.messages[sid].append(message)
        return message

    def enqueue(self, sid: str, mid: str, text: str = "") -> dict[str, Any]:
        """Enqueue work durably in the inbox without delivering it."""
        item = {
            "id": mid,
            "sessionID": sid,
            "time": {"created": self._next_time()},
            "type": "user",
            "payload": {"text": text},
            "delivery": "queue",
        }
        self.seen_admission[mid] = sid
        self.inbox[sid].append(item)
        return item

    def append_assistant(
        self,
        sid: str,
        text: str = "done",
        *,
        error: Any = None,
        finish: str | None = None,
        content: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        created = self._next_time()
        message = {
            "id": self.new_message_id(),
            "time": {"created": created, "completed": created + 1},
            "type": "assistant",
            "agent": self.sessions[sid].get("agent", "build"),
            "model": self.sessions[sid].get("model", {"id": "m", "providerID": "p"}),
            "content": content if content is not None else [{"type": "text", "text": text}],
        }
        if finish is not None:
            message["finish"] = finish
            message["rawFinish"] = finish
        if error is not None:
            message["error"] = error
        self.messages[sid].append(message)
        return message

    def append_idle(self, sid: str, outcome: str = "succeeded") -> dict[str, Any]:
        created = self._next_time()
        message = {
            "id": self.new_message_id(),
            "time": {"created": created},
            "type": "idle",
            "outcome": outcome,
        }
        self.messages[sid].append(message)
        self.sessions[sid]["outcome"] = outcome
        self.sessions[sid]["time"]["idle"] = created
        return message

    def append_system(self, sid: str, text: str = "") -> dict[str, Any]:
        """Append a projected ``system`` message (neither turn-opening nor terminal)."""
        message = {
            "id": self.new_message_id(),
            "time": {"created": self._next_time()},
            "type": "system",
            "text": text,
        }
        self.messages[sid].append(message)
        return message


class MockHandler(BaseHTTPRequestHandler):
    server_version = "MockOpenCodeV2/0.0.1"

    @property
    def state(self) -> MockState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, *args: Any) -> None:  # keep test output quiet
        return

    # -- plumbing ------------------------------------------------------------
    def _json(self, status: int, payload: Any) -> None:
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        if body:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if not length:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8")) if raw else {}

    def _authorized(self) -> bool:
        if not self.state.token:
            return True
        return self.headers.get("Authorization") == f"Bearer {self.state.token}"

    def _unauthorized(self) -> None:
        self._json(401, {"_tag": "UnauthorizedError", "message": "missing or bad token"})

    def _not_found(self, sid: str) -> None:
        self._json(404, {"_tag": "SessionNotFoundError", "sessionID": sid, "message": "no session"})

    def _drop_connection(self) -> None:
        self.close_connection = True
        try:
            self.connection.shutdown(2)  # SHUT_RDWR
        except OSError:
            pass
        try:
            self.connection.close()
        except OSError:
            pass

    # -- verb routing --------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        self._route("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._route("POST")

    def _route(self, method: str) -> None:  # noqa: C901 - explicit routing table
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if not self._authorized():
            return self._unauthorized()

        if method == "GET" and path == "/api/info":
            return self._json(
                200,
                {"version": "mock-0.0.1", "pid": 1, "urls": ["http://127.0.0.1"], "paths": {"tmp": "/tmp"}},
            )

        if method == "GET" and path == "/api/redirect":
            # used to prove the client refuses redirects (no token forwarding)
            self.send_response(302)
            self.send_header("Location", "http://example.com/stolen-token")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if method == "POST" and path == "/api/session":
            return self._json(200, {"data": self.state.create_session(self._read_body())})

        match = PROMPT_RE.match(path)
        if method == "POST" and match:
            return self._prompt(match.group(1))

        match = INBOX_RE.match(path)
        if method == "GET" and match:
            sid = match.group(1)
            if sid not in self.state.sessions:
                return self._not_found(sid)
            return self._json(200, {"data": self.state.inbox[sid]})

        match = MESSAGES_RE.match(path)
        if method == "GET" and match:
            sid = match.group(1)
            if sid not in self.state.sessions:
                return self._not_found(sid)
            return self._json(200, self._message_page(sid, query))

        match = MESSAGE_RE.match(path)
        if method == "GET" and match:
            sid, mid = match.group(1), match.group(2)
            for message in self.state.messages.get(sid, []):
                if message.get("id") == mid:
                    return self._json(200, {"data": message})
            return self._json(404, {"_tag": "MessageNotFoundError", "message": "no message"})

        match = SESSION_RE.match(path)
        if method == "GET" and match:
            sid = match.group(1)
            info = self.state.sessions.get(sid)
            if info is None:
                return self._not_found(sid)
            return self._json(200, {"data": info})

        match = WAIT_RE.match(path)
        if method == "POST" and match:
            sid = match.group(1)
            if sid not in self.state.sessions:
                return self._not_found(sid)
            if self.state.wait_503_times > 0:
                self.state.wait_503_times -= 1
                return self._json(503, {"_tag": "ServiceUnavailableError", "message": "warming"})
            if self.state.wait_status != 204:
                return self._json(self.state.wait_status, {"_tag": "ServiceUnavailableError", "message": "down"})
            return self._json(204, None)

        match = INTERRUPT_RE.match(path)
        if method == "POST" and match:
            sid = match.group(1)
            if sid not in self.state.sessions:
                return self._not_found(sid)
            return self._json(200, {"interrupted": self.state.interrupt_result})

        match = FORK_RE.match(path)
        if method == "POST" and match:
            sid = match.group(1)
            if sid not in self.state.sessions:
                return self._not_found(sid)
            body = self._read_body()
            child = self.state.create_session({"parentID": sid, "metadata": {"forkOf": sid}})
            child["fork"] = {"sessionID": sid, "boundary": {"before": body.get("before")}}
            return self._json(200, {"data": child})

        if method == "POST" and path == "/api/experimental/generate/decisions":
            body = self._read_body()
            answers = self.state.decision_answers
            if answers is None:
                answers = [
                    {"type": "predicate", "name": "is_review", "probability": 0.73},
                    {
                        "type": "choice",
                        "name": "study_design",
                        "choice": "rct",
                        "probabilities": [
                            {"value": "rct", "probability": 0.6},
                            {"value": "cohort", "probability": 0.4},
                        ],
                        "confidence": 0.6,
                    },
                    {
                        "type": "score",
                        "name": "quality",
                        "score": 1.6,
                        "probabilities": [
                            {"value": 1, "label": "low", "probability": 0.4},
                            {"value": 2, "label": "medium", "probability": 0.6},
                        ],
                        "confidence": 0.55,
                    },
                ]
            return self._json(200, {"data": {"answers": answers, "cost": 0.001, "tokens": {"input": 12, "output": 3}}})

        return self._json(404, {"_tag": "InvalidRequestError", "message": f"no route {method} {path}"})

    # -- admission -----------------------------------------------------------
    def _message_page(self, sid: str, query: dict[str, list[str]]) -> dict[str, Any]:
        """Cursor-paged message list honouring order/limit/cursor."""
        items = list(self.state.messages[sid])
        wanted = query.get("type", [None])[0]
        if wanted:
            items = [m for m in items if m.get("type") == wanted]
        if query.get("order", ["asc"])[0] == "desc":
            items = list(reversed(items))
        raw_limit = query.get("limit", [None])[0]
        limit = int(raw_limit) if raw_limit else self.state.message_page_size
        raw_cursor = query.get("cursor", [None])[0]
        start = int(raw_cursor) if raw_cursor else 0
        total = len(items)
        end = min(total, start + limit) if limit else total
        page = items[start:end]
        next_cursor = str(end) if limit and end > start and end < total else None
        previous_cursor = str(max(0, start - limit)) if limit and start > 0 else None
        return {"data": page, "cursor": {"previous": previous_cursor, "next": next_cursor}}

    def _prompt(self, sid: str) -> None:
        if sid not in self.state.sessions:
            return self._not_found(sid)
        body = self._read_body()
        admission_id = body.get("id")
        if isinstance(admission_id, str) and admission_id in self.state.seen_admission:
            return self._json(
                409,
                {
                    "_tag": "ConflictError",
                    "message": "input already admitted",
                    "resource": admission_id,
                },
            )
        if self.state.fail_conflict_instead and admission_id:
            return self._json(409, {"_tag": "ConflictError", "message": "forced", "resource": admission_id})

        created = self.state._next_time()
        message = {
            "id": admission_id or self.state.new_message_id(),
            "time": {"created": created},
            "text": body.get("text", ""),
            "type": "user",
        }
        item = {
            "id": message["id"],
            "sessionID": sid,
            "time": {"created": created},
            "type": "user",
            "payload": {"text": body.get("text", "")},
            "delivery": body.get("delivery") or "queue",
        }
        # admit durably first so a dropped connection is recoverable
        if isinstance(message["id"], str):
            self.state.seen_admission[message["id"]] = sid
        self.state.inbox[sid].append(item)
        self.state.messages[sid].append(message)

        if self.state.prompt_drops > 0:
            self.state.prompt_drops -= 1
            return self._drop_connection()
        return self._json(200, {"data": item})


class MockServer:
    """Context manager running the mock on an ephemeral loopback port."""

    def __init__(self, *, token: str | None = None):
        self.state = MockState(token=token)
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
        self._httpd.state = self.state  # type: ignore[attr-defined]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> "MockServer":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Run the mock OpenCode V2 endpoint.")
    parser.add_argument("--port", type=int, default=0, help="0 picks a free port")
    parser.add_argument("--token", default=None, help="require this bearer token")
    args = parser.parse_args()

    state = MockState(token=args.token)
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), MockHandler)
    httpd.state = state  # type: ignore[attr-defined]
    host, port = httpd.server_address[:2]
    print(f"mock opencode v2 at http://{host}:{port} (token={'set' if args.token else 'none'})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
