"""Deterministic local HTTP fixture for the packaged OpenCode V2 lifecycle client.

Test-only private helper (module name does not match ``test*.py``, so ``unittest
discover`` never collects it). It implements only the endpoint shapes the client
sends and reads, taken from the observed OpenCode V2.2.0 surface and the published
``opencode HttpApi`` OpenAPI document:

* ``GET  /api/info``
* ``GET  /api/redirect``                  (302, to prove redirects are refused)
* ``POST /api/session``
* ``GET  /api/session/{sid}``
* ``POST /api/session/{sid}/prompt``      (idempotent on the client-supplied id)
* ``GET  /api/session/{sid}/inbox``
* ``GET  /api/session/{sid}/message``
* ``GET  /api/session/{sid}/message/{mid}``
* ``POST /api/session/{sid}/interrupt``
* ``POST /api/experimental/session/{sid}/wait``

The fixture is a test double, not a compliant server: it validates just enough to
prove client behaviour. It accepts ``Authorization: Bearer <token>`` and/or HTTP
Basic ``<user>:<password>`` (the scheme the disposable 2.2.0 harness was observed
to use), and rejects a ``cursor`` combined with ``order`` on the message list,
matching the published operation. Tunables let a test drive malformed and foreign
responses (not present in an honest server).
"""

from __future__ import annotations

import base64
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
WAIT_RE = re.compile(r"^/api/experimental/session/(ses[^/]+)/wait$")


class MockState:
    """Mutable fixture state plus tunable failure/protocol modes for tests."""

    def __init__(
        self,
        *,
        token: str | None = None,
        basic_username: str = "opencode",
        basic_password: str | None = None,
    ):
        self.token = token
        self.basic_username = basic_username
        self.basic_password = basic_password
        self.sessions: dict[str, dict[str, Any]] = {}
        self.inbox: dict[str, list[dict[str, Any]]] = {}
        self.messages: dict[str, list[dict[str, Any]]] = {}
        self.seen_admission: dict[str, str] = {}  # admission id -> session id
        self.seen_receipts: dict[str, dict[str, Any]] = {}  # admission id -> receipt
        # Every accepted request is recorded so tests can prove the exact wire
        # query and credential scheme.
        self.requests: list[dict[str, Any]] = []
        self._counter = 0
        self._tick = 1_700_000_000_000
        # -- failure / protocol tunables --
        self.info_payload: Any = None  # whole /api/info body override
        self.info_padding: int = 0  # extra bytes appended to the info body
        self.prompt_drops = 0  # drop the connection N times, after accepting
        self.prompt_repeat_409 = True  # 409 on a repeat id (else replay 200)
        self.session_get_id_override: str | None = None
        self.inbox_owner_override: str | None = None
        self.receipt_id_override: str | None = None
        self.message_list_payload: Any = None  # whole message-list body override
        self.message_get_id_override: str | None = None
        self.create_model_override: Any = None
        self.create_agent_override: Any = None
        self.omit_pinned = False  # omit agent/model from Session.Info entirely
        self.receipt_omit_text = False  # admit receipt carries no payload text
        self.wait_status = 204  # 204 = idle; 503 = service unavailable
        self.wait_503_times = 0
        self.interrupt_result = True
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
        info: dict[str, Any] = {
            "id": sid,
            "projectID": "prj_mock",
            "time": {"created": 1_700_000_000_000, "updated": 1_700_000_000_000},
            "location": {
                "directory": (body.get("location") or {}).get("directory", "/tmp/mock")
            },
        }
        agent = body.get("agent")
        if self.create_agent_override is not None:
            agent = self.create_agent_override
        model = body.get("model")
        if self.create_model_override is not None:
            model = self.create_model_override
        if agent is not None and not self.omit_pinned:
            info["agent"] = agent
        if model is not None and not self.omit_pinned:
            info["model"] = model
        if body.get("title") is not None:
            info["title"] = body["title"]
        self.sessions[sid] = info
        self.inbox.setdefault(sid, [])
        self.messages.setdefault(sid, [])
        return info

    # -- ordered turn builders ----------------------------------------------
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
    ) -> dict[str, Any]:
        created = self._next_time()
        message: dict[str, Any] = {
            "id": self.new_message_id(),
            "time": {"created": created, "completed": created + 1},
            "type": "assistant",
            "agent": self.sessions[sid].get("agent", "build"),
            "model": self.sessions[sid].get("model", {"id": "m", "providerID": "p"}),
            "content": [{"type": "text", "text": text}],
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

    def _json_bytes(self, status: int, body: bytes) -> None:
        """Send a raw body (used to exceed the client's size bound)."""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if not length:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8")) if raw else {}

    def _authorized(self) -> bool:
        if not self.state.token and not self.state.basic_password:
            return True
        auth = self.headers.get("Authorization")
        if self.state.token and auth == f"Bearer {self.state.token}":
            return True
        if self.state.basic_password:
            raw = f"{self.state.basic_username}:{self.state.basic_password}".encode("utf-8")
            expected = "Basic " + base64.b64encode(raw).decode("ascii")
            if auth == expected:
                return True
        return False

    def _unauthorized(self) -> None:
        # A deliberately leaky body: the client must never surface this text.
        self._json(401, {"_tag": "UnauthorizedError", "message": "peer-secret-leak-42"})

    def _not_found(self, sid: str) -> None:
        self._json(
            404, {"_tag": "SessionNotFoundError", "sessionID": sid, "message": "no session"}
        )

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
        self.state.requests.append(
            {
                "method": method,
                "path": path,
                "query": query,
                "authorization": self.headers.get("Authorization"),
            }
        )
        if not self._authorized():
            return self._unauthorized()

        if method == "GET" and path == "/api/info":
            if self.state.info_payload is not None:
                return self._json(200, self.state.info_payload)
            if self.state.info_padding:
                body = json.dumps(
                    {"version": "mock-0.0.1", "padding": "x" * self.state.info_padding}
                ).encode("utf-8")
                return self._json_bytes(200, body)
            return self._json(
                200,
                {
                    "version": "mock-0.0.1",
                    "pid": 1,
                    "urls": ["http://127.0.0.1"],
                    "paths": {"tmp": "/tmp"},
                },
            )

        if method == "GET" and path == "/api/redirect":
            # Prove the client refuses redirects (no credential forwarding).
            self.send_response(302)
            self.send_header("Location", "http://example.invalid/stolen")
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
            items = list(self.state.inbox[sid])
            if self.state.inbox_owner_override is not None and items:
                items = [dict(items[0], sessionID=self.state.inbox_owner_override)] + items[1:]
            return self._json(200, {"data": items})

        match = MESSAGES_RE.match(path)
        if method == "GET" and match:
            sid = match.group(1)
            if sid not in self.state.sessions:
                return self._not_found(sid)
            if self.state.message_list_payload is not None:
                return self._json(200, self.state.message_list_payload)
            if query.get("cursor") and query.get("order"):
                return self._json(
                    400,
                    {
                        "_tag": "InvalidRequestError",
                        "message": "cursor must not be combined with order",
                    },
                )
            return self._json(200, self._message_page(sid, query))

        match = MESSAGE_RE.match(path)
        if method == "GET" and match:
            sid, mid = match.group(1), match.group(2)
            for message in self.state.messages.get(sid, []):
                if message.get("id") == mid:
                    found = dict(message)
                    if self.state.message_get_id_override is not None:
                        found["id"] = self.state.message_get_id_override
                    return self._json(200, {"data": found})
            return self._json(404, {"_tag": "MessageNotFoundError", "message": "no message"})

        match = SESSION_RE.match(path)
        if method == "GET" and match:
            sid = match.group(1)
            info = self.state.sessions.get(sid)
            if info is None:
                return self._not_found(sid)
            if self.state.session_get_id_override is not None:
                info = dict(info, id=self.state.session_get_id_override)
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
                return self._json(
                    self.state.wait_status,
                    {"_tag": "ServiceUnavailableError", "message": "down"},
                )
            return self._json(204, None)

        match = INTERRUPT_RE.match(path)
        if method == "POST" and match:
            sid = match.group(1)
            if sid not in self.state.sessions:
                return self._not_found(sid)
            return self._json(200, {"interrupted": self.state.interrupt_result})

        return self._json(404, {"_tag": "InvalidRequestError", "message": "no route"})

    # -- paging --------------------------------------------------------------
    def _message_page(self, sid: str, query: dict[str, list[str]]) -> dict[str, Any]:
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

    # -- admission -----------------------------------------------------------
    def _prompt(self, sid: str) -> None:
        if sid not in self.state.sessions:
            return self._not_found(sid)
        body = self._read_body()
        admission_id = body.get("id")
        if isinstance(admission_id, str) and admission_id in self.state.seen_admission:
            if self.state.prompt_repeat_409:
                return self._json(
                    409,
                    {
                        "_tag": "ConflictError",
                        "message": "input already admitted",
                        "resource": admission_id,
                    },
                )
            # Observed real 2.2.0 behaviour: a same-ID retry returns 200 again with
            # the originally persisted receipt (exactly one accepted input).
            return self._json(200, {"data": self.state.seen_receipts[admission_id]})

        created = self.state._next_time()
        message_id = admission_id or self.state.new_message_id()
        if self.state.receipt_id_override is not None:
            message_id = self.state.receipt_id_override
        text = body.get("text", "")
        payload: dict[str, Any] = {} if self.state.receipt_omit_text else {"text": text}
        item: dict[str, Any] = {
            "id": message_id,
            "sessionID": sid,
            "time": {"created": created},
            "type": "user",
            "payload": payload,
            "delivery": body.get("delivery") or "queue",
        }
        message: dict[str, Any] = {
            "id": message_id,
            "time": {"created": created},
            "type": "user",
        }
        if not self.state.receipt_omit_text:
            message["text"] = text
        # Admit durably first so a dropped connection is recoverable.
        if isinstance(admission_id, str):
            self.state.seen_admission[admission_id] = sid
            self.state.seen_receipts[admission_id] = item
        self.state.inbox[sid].append(item)
        self.state.messages[sid].append(message)

        if self.state.prompt_drops > 0:
            self.state.prompt_drops -= 1
            return self._drop_connection()
        return self._json(200, {"data": item})


class MockServer:
    """Context manager running the fixture on an ephemeral loopback port."""

    def __init__(
        self,
        *,
        token: str | None = None,
        basic_username: str = "opencode",
        basic_password: str | None = None,
    ):
        self.state = MockState(
            token=token, basic_username=basic_username, basic_password=basic_password
        )
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
