"""Minimal Python HTTP lifecycle client for the official OpenCode V2 API.

Task C-python spike. Scope: ``spikes/runtime/python`` only.

Design constraints (see ``docs/implementation/00-runtime-spike.md`` lines 5, 9,
15-18, 20 and ``docs/specification.md`` "Product and runtime"):

* Python standard library only (``urllib``), no third-party HTTP client.
* Explicit, authenticated **loopback** endpoint. The base URL and an explicit
  credential are passed in by the caller and both are required: a non-loopback
  host or a missing credential fails closed, redirects are refused so the
  credential is never forwarded, and there is **no** shared-service discovery or
  start and **no** reading of private databases, registration files, or
  credentials. The Authorization scheme is explicit: ``bearer`` (default, for
  backward compatibility) or ``basic`` (the disposable 2.2.0 harness answers
  HTTP Basic ``opencode:<per-run-password>``). ``from_env`` is the only
  convenience; it refuses to guess a default and deliberately ignores
  ``OPENROUTER_API_KEY``.
* Only request/response shapes taken from the published V2 OpenAPI document
  (``https://opencode.ai/v2/openapi.json``, openapi 3.1.0, HttpApi 0.0.1) are
  used. Nothing is invented.

API map used here (operationId -> method/path):

* ``server.info``                  GET  /api/info
* ``session.create``               POST /api/session
* ``session.get``                  GET  /api/session/{sessionID}
* ``session.prompt``               POST /api/session/{sessionID}/prompt
* ``session.inbox.list``           GET  /api/session/{sessionID}/inbox
* ``session.message.list``         GET  /api/session/{sessionID}/message
* ``session.message.get``          GET  /api/session/{sessionID}/message/{messageID}
* ``session.interrupt``            POST /api/session/{sessionID}/interrupt
* ``session.fork``                 POST /api/session/{sessionID}/fork
* ``experimental.session.wait``    POST /api/experimental/session/{sessionID}/wait
* ``experimental.generate.decisions`` POST /api/experimental/generate/decisions

Admission vs completion (the point of the spike):

* ``admit`` uses ``session.prompt``. It returns a *durable admission receipt*
  (``Session.Inbox.User``): the input is persisted and scheduled. This is **not**
  a completion.
* ``reconcile`` reconstructs state from durable state (inbox + messages) so a
  second client recovers after missed events. It correlates the durable receipt
  with the terminal marker **of the same turn only** (positional windowing, since
  the schemas expose no parent link); a terminal from another turn never
  completes the target, and an uncertain read returns ``unknown``, not success.
* ``wait`` is a bounded wait for the agent loop to become idle, so the CLI is
  never required to stay open for the task to finish.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

# --- exact patterns from the OpenAPI schemas ---------------------------------
SESSION_ID_RE = re.compile(r"^ses")
MESSAGE_ID_RE = re.compile(r"^msg_")

# Response tags from the published error schemas (*ErrorEncoded).
CONFLICT_TAG = "ConflictError"
UNAUTHORIZED_TAG = "UnauthorizedError"

# Supported Authorization schemes. The published OpenAPI declares no
# securitySchemes, but the disposable 2.2.0 harness was observed to answer HTTP
# Basic ``opencode:<per-run-password>`` (its explicit app-owned descriptor holds
# the service URL and a per-run password file). ``bearer`` stays the default for
# backward compatibility; ``basic`` is opt-in and never guessed or discovered.
AUTH_BEARER = "bearer"
AUTH_BASIC = "basic"
AUTH_SCHEMES = (AUTH_BEARER, AUTH_BASIC)
BASIC_DEFAULT_USERNAME = "opencode"

# Message types that open a new turn in the projected timeline. The published
# schemas (Session.Message.*) carry no parent/linkage field, so a turn can only
# be delimited positionally: its ``user`` message, then the next user/synthetic
# message, with the authoritative ``idle`` terminal in between.
TURN_OPENING_TYPES = ("user", "synthetic")
LOOPBACK_HOSTS = ("localhost",)


class _RedirectRejectedHandler(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect so the bearer token is never forwarded.

    ``redirect_request`` returning ``None`` makes urllib raise the redirect as an
    ``HTTPError`` instead of following it. This blocks credential leakage to a
    redirect target (cross-origin or otherwise).
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


class OpenCodeError(Exception):
    """Base class for client errors."""


class OpenCodeApiError(OpenCodeError):
    """A decoded non-2xx API response.

    ``tag`` is the ``_tag`` of the published error schema (for example
    ``ConflictError`` or ``UnauthorizedError``); ``message`` is its message.
    """

    def __init__(self, status: int, tag: str | None, message: str, payload: Any = None):
        super().__init__(f"HTTP {status} {tag or 'error'}: {message}")
        self.status = status
        self.tag = tag
        self.message = message
        self.payload = payload


class OpenCodeTransportError(OpenCodeError):
    """The request never produced a decodable HTTP response (network/timeout).

    The outcome is unknown; callers must reconcile instead of blindly resending
    with a new identity.
    """


class QuiescenceTimeout(OpenCodeError):
    """The bounded wait elapsed before the session became idle."""


class DecisionProtocolError(OpenCodeError, ValueError):
    """A decisions response is malformed or missing (a protocol failure).

    Distinct from :class:`DecisionError`, which is a *valid* per-question
    ``error`` answer (for example a refusal) that the contract allows alongside
    the other answers. A protocol failure means the response cannot be trusted as
    evidence at all, so it is surfaced as a client/protocol bug instead of being
    silently treated as a refusal or a scientific exclusion. It subclasses
    ``ValueError`` so existing ``ValueError`` handling keeps working.
    """


@dataclass
class Admission:
    """Durable admission of one session input.

    ``receipt`` is the persisted ``Session.Inbox.User`` (or the persisted user
    message found by reconciliation). ``duplicate`` is true when the server
    reported a conflict for an already-admitted client ID. ``reconciled`` is true
    when the original request outcome was unknown and the state was recovered.
    """

    admission_id: str
    receipt: dict[str, Any] | None
    duplicate: bool = False
    reconciled: bool = False

    @property
    def has_durable_receipt(self) -> bool:
        return self.receipt is not None


@dataclass
class ReconcileResult:
    """State reconstructed from durable server state for one admission.

    ``state`` is one of:

    * ``queued`` — the ID is enqueued in the session inbox and not yet delivered
      (no projected user message yet).
    * ``in_progress`` — the user message is projected but its turn has no
      authoritative ``idle`` terminal yet. A completed ``assistant`` message does
      not end the turn.
    * ``completed`` — the correlated ``idle`` terminal for *this* turn succeeded.
    * ``failed`` / ``interrupted`` — the correlated ``idle`` terminal for *this*
      turn ended without success.
    * ``unknown`` — no reliable trace; ``reason`` explains why. Never success.

    ``completion`` is the correlation evidence (the ``idle`` message for *this*
    turn only), or ``None``.
    """

    admission_id: str
    state: str
    durable_receipt: dict[str, Any] | None
    completion: dict[str, Any] | None
    reason: str | None = None

    _TERMINAL_OK = ("completed", "failed", "interrupted")
    _DURABLE = ("queued", "in_progress", "completed", "failed", "interrupted")

    @property
    def is_durable(self) -> bool:
        return self.state in self._DURABLE

    @property
    def is_complete(self) -> bool:
        return self.state == "completed"

    @property
    def is_terminal(self) -> bool:
        return self.state in self._TERMINAL_OK

    @property
    def outcome(self) -> str | None:
        return (self.completion or {}).get("outcome")


def assert_explicit_loopback_url(base_url: str) -> str:
    """Return ``base_url`` only if it is an explicit loopback HTTP(S) endpoint.

    Fails closed for a non-loopback host, a non-HTTP scheme, an embedded
    credential, or a missing host. The spike must never talk to a remote or
    shared service, and must never carry a token off-host.
    """
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("base_url must be an explicit, non-empty URL")
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"base_url scheme must be http or https, got {parsed.scheme!r}")
    if parsed.username or parsed.password:
        raise ValueError("credentials must not be embedded in the endpoint URL")
    host = parsed.hostname
    if not host:
        raise ValueError("base_url must include a host")
    if host in LOOPBACK_HOSTS:
        return base_url
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError(
            f"base_url host must be a loopback address or localhost, got {host!r}"
        ) from None
    if not address.is_loopback:
        raise ValueError(
            f"base_url host must be loopback (127.0.0.0/8 or ::1), got {host}"
        )
    return base_url


def _read_password_file(path: str) -> str:
    """Read a per-run password from an explicitly named app-owned file.

    The caller supplies the path; nothing is discovered and no OpenCode state,
    registration file, database, or keychain is consulted. Only the trailing line
    endings are removed; the rest of the file is the secret verbatim.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().rstrip("\r\n")
    except OSError as exc:
        raise ValueError(f"could not read password file {path!r}: {exc}") from None


def resolve_auth(
    *,
    auth: str,
    token: str | None,
    username: str | None,
    password: str | None,
    password_file: str | None,
) -> tuple[str, str | None, str | None]:
    """Resolve an explicit auth scheme, failing closed on anything missing.

    Returns ``(scheme, username, secret)`` where ``secret`` is the bearer token
    for ``bearer`` or the password for ``basic``. Exactly one scheme is chosen:

    * ``bearer`` (default) uses ``token``; supplying basic fields is an error, so
      a misconfiguration cannot silently authenticate a different way.
    * ``basic`` uses ``username`` (default ``opencode``) + ``password``; the
      password may come from ``password_file`` instead. A token is refused.

    No scheme is ever discovered, guessed, or read from OpenCode's own state.
    """
    if auth not in AUTH_SCHEMES:
        raise ValueError(f"auth must be one of {AUTH_SCHEMES}, got {auth!r}")
    if auth == AUTH_BASIC:
        if token is not None:
            raise ValueError(
                "auth='basic' does not use a bearer token; pass username/password instead"
            )
        if password is not None and password_file is not None:
            raise ValueError("pass either password or password_file, not both")
        secret = password
        if secret is None and password_file is not None:
            secret = _read_password_file(password_file)
        if not isinstance(secret, str) or not secret:
            raise ValueError(
                "auth='basic' requires an explicit password (or password_file); "
                "refusing unauthenticated access"
            )
        name = username if username is not None else BASIC_DEFAULT_USERNAME
        if not isinstance(name, str) or not name:
            raise ValueError("auth='basic' username must be a non-empty string")
        return AUTH_BASIC, name, secret
    if username is not None or password is not None or password_file is not None:
        raise ValueError(
            "auth='bearer' uses a token; pass token instead of username/password"
        )
    if not isinstance(token, str) or not token.strip():
        raise ValueError(
            "an explicit bearer token is required; refusing unauthenticated access"
        )
    return AUTH_BEARER, None, token


def validate_model_ref(model: dict[str, Any]) -> dict[str, Any]:
    """Validate a ``Model.Ref`` ({id, providerID, variant?}). Never guess."""
    if not isinstance(model, dict):
        raise ValueError("model must be a dict (Model.Ref)")
    for key in ("id", "providerID"):
        value = model.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"model.{key} must be a non-empty string")
    allowed = {"id", "providerID", "variant"}
    extra = set(model) - allowed
    if extra:
        raise ValueError(f"model has unknown fields: {sorted(extra)}")
    if "variant" in model and not isinstance(model["variant"], str):
        raise ValueError("model.variant must be a string")
    return model


class OpenCodeV2Client:
    """Small, explicit client for the OpenCode V2 HTTP API.

    :param base_url: explicit **loopback** endpoint, e.g.
        ``http://127.0.0.1:4096``. Non-loopback hosts are refused. No discovery.
    :param token: explicit bearer token (``Authorization: Bearer``). Required when
        ``auth="bearer"`` (the default): the client fails closed without one.
    :param auth: explicit Authorization scheme, ``"bearer"`` (default) or
        ``"basic"``. The disposable 2.2.0 harness answers HTTP Basic
        ``opencode:<per-run-password>``, so ``auth="basic"`` is available; it is
        never discovered or guessed.
    :param username: Basic username (default ``opencode``); ``basic`` only.
    :param password: Basic password; ``basic`` only.
    :param password_file: path to a file holding the Basic password (an explicit
        app-owned path; the file is read, never discovered).
    :param directory: absolute location directory pinned on session create.

    Redirects are refused so the credential is never forwarded off-origin.
    """

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        *,
        auth: str = AUTH_BEARER,
        username: str | None = None,
        password: str | None = None,
        password_file: str | None = None,
        timeout: float = 30.0,
        directory: str | None = None,
        extra_headers: dict[str, str] | None = None,
        max_message_pages: int = 100,
    ):
        assert_explicit_loopback_url(base_url)
        scheme, auth_username, secret = resolve_auth(
            auth=auth,
            token=token,
            username=username,
            password=password,
            password_file=password_file,
        )
        if not isinstance(max_message_pages, int) or max_message_pages < 1:
            raise ValueError("max_message_pages must be a positive integer")
        self.base_url = base_url.rstrip("/")
        self.auth = scheme
        self.token = token if scheme == AUTH_BEARER else None
        self.username = auth_username
        self.password = secret if scheme == AUTH_BASIC else None
        self.timeout = float(timeout)
        self.directory = directory
        self.extra_headers = dict(extra_headers or {})
        self.max_message_pages = max_message_pages
        # A private opener that refuses redirects (defends the credential).
        self._opener = urllib.request.build_opener(_RedirectRejectedHandler)

    # -- construction helpers ------------------------------------------------
    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "OpenCodeV2Client":
        """Build a client from explicit environment variables only.

        Always required: ``OPENCODE_SPIKE_BASE_URL`` (no default endpoint).
        ``OPENCODE_SPIKE_AUTH`` selects ``bearer`` (default) or ``basic``:

        * ``bearer`` — ``OPENCODE_SPIKE_TOKEN`` is required.
        * ``basic`` — ``OPENCODE_SPIKE_PASSWORD`` or ``OPENCODE_SPIKE_PASSWORD_FILE``
          is required; ``OPENCODE_SPIKE_USERNAME`` defaults to ``opencode``.

        Optional: ``OPENCODE_SPIKE_DIRECTORY``. Refuses a default endpoint and
        never reads provider keys: ``OPENROUTER_API_KEY`` is deliberately ignored.
        """
        env = os.environ if env is None else env
        base_url = env.get("OPENCODE_SPIKE_BASE_URL")
        if not base_url:
            raise ValueError(
                "OPENCODE_SPIKE_BASE_URL is required; no default endpoint is used"
            )
        auth = env.get("OPENCODE_SPIKE_AUTH", AUTH_BEARER)
        if auth not in AUTH_SCHEMES:
            raise ValueError(f"OPENCODE_SPIKE_AUTH must be one of {AUTH_SCHEMES}, got {auth!r}")
        if auth == AUTH_BASIC:
            return cls(
                base_url,
                auth=AUTH_BASIC,
                username=env.get("OPENCODE_SPIKE_USERNAME"),
                password=env.get("OPENCODE_SPIKE_PASSWORD"),
                password_file=env.get("OPENCODE_SPIKE_PASSWORD_FILE"),
                directory=env.get("OPENCODE_SPIKE_DIRECTORY"),
            )
        token = env.get("OPENCODE_SPIKE_TOKEN")
        if not token:
            raise ValueError(
                "OPENCODE_SPIKE_TOKEN is required; refusing unauthenticated access"
            )
        return cls(
            base_url,
            token=token,
            directory=env.get("OPENCODE_SPIKE_DIRECTORY"),
        )

    # -- transport -----------------------------------------------------------
    def _authorization(self) -> str:
        """The explicit Authorization header value for the configured scheme."""
        if self.auth == AUTH_BASIC:
            raw = f"{self.username}:{self.password}".encode("utf-8")
            return "Basic " + base64.b64encode(raw).decode("ascii")
        return f"Bearer {self.token}"

    def _headers(self, has_body: bool) -> dict[str, str]:
        headers: dict[str, str] = {"Accept": "application/json"}
        if has_body:
            headers["Content-Type"] = "application/json"
        # Caller extras first, then the explicit scheme's Authorization, so an
        # extra header can never silently replace the configured credential.
        headers.update(self.extra_headers)
        headers["Authorization"] = self._authorization()
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers=self._headers(data is not None),
            method=method,
        )
        try:
            with self._opener.open(
                request, timeout=self.timeout if timeout is None else timeout
            ) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
            finally:
                exc.close()
            if 300 <= exc.code < 400:
                location = exc.headers.get("Location") if exc.headers else None
                raise OpenCodeApiError(
                    exc.code,
                    None,
                    f"redirect blocked (Location={location}); token not forwarded",
                    None,
                ) from None
            payload = self._decode(raw)
            tag = payload.get("_tag") if isinstance(payload, dict) else None
            message = (
                payload.get("message") if isinstance(payload, dict) else None
            ) or f"HTTP {exc.code}"
            raise OpenCodeApiError(exc.code, tag, str(message), payload) from None
        except (urllib.error.URLError, OSError) as exc:
            # URLError and OSError both cover connection refused, reset,
            # RemoteDisconnected, and socket timeouts; the outcome is unknown.
            raise OpenCodeTransportError(f"{method} {path}: {exc}") from None
        return self._decode(raw)

    @staticmethod
    def _decode(raw: bytes) -> Any:
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {"_raw": raw.decode("utf-8", "replace")}

    # -- server --------------------------------------------------------------
    def info(self) -> dict[str, Any]:
        """``server.info`` — prove the explicit endpoint answers before use."""
        return self._request("GET", "/api/info")

    # -- sessions ------------------------------------------------------------
    def create_session(
        self,
        *,
        agent: str | None = None,
        model: dict[str, Any] | None = None,
        title: str | None = None,
        parent_id: str | None = None,
        directory: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """``session.create`` — create a fresh session with pinned role/model.

        ``agent`` is the role; ``model`` is a ``Model.Ref``. Omitting them leaves
        server defaults, which this client never assumes are safe.
        """
        if model is not None:
            validate_model_ref(model)
        if parent_id is not None and not SESSION_ID_RE.match(parent_id):
            raise ValueError("parent_id must match ^ses")
        body: dict[str, Any] = {}
        if agent is not None:
            body["agent"] = agent
        if model is not None:
            body["model"] = model
        if title is not None:
            body["title"] = title
        if parent_id is not None:
            body["parentID"] = parent_id
        if metadata is not None:
            body["metadata"] = metadata
        location = directory if directory is not None else self.directory
        if location is not None:
            body["location"] = {"directory": location}
        data = self._request("POST", "/api/session", body=body)["data"]
        self._require_session_id(data.get("id"))
        return data

    def get_session(self, session_id: str) -> dict[str, Any]:
        self._require_session_id(session_id)
        return self._request("GET", f"/api/session/{session_id}")["data"]

    def interrupt(self, session_id: str, *, resume: bool | None = None) -> bool:
        """``session.interrupt`` — returns whether an active execution stopped."""
        self._require_session_id(session_id)
        path = f"/api/session/{session_id}/interrupt"
        if resume is not None:
            path += f"?resume={'true' if resume else 'false'}"
        return bool(self._request("POST", path)["interrupted"])

    def fork(self, session_id: str, *, before: str | None = None) -> dict[str, Any]:
        """``session.fork`` — child session copying history.

        Note: a fork carries writer history and is **not** a clean critic
        context. Use :meth:`start_critic` for a fresh critic session.
        """
        self._require_session_id(session_id)
        if before is not None and not MESSAGE_ID_RE.match(before):
            raise ValueError("before must match ^msg_")
        body = {"before": before}
        data = self._request("POST", f"/api/session/{session_id}/fork", body=body)["data"]
        self._require_session_id(data.get("id"))
        return data

    def start_critic(
        self,
        *,
        agent: str,
        model: dict[str, Any],
        writer_model: dict[str, Any] | None = None,
        title: str | None = None,
        directory: str | None = None,
    ) -> dict[str, Any]:
        """Create an independent critic session with a pinned role and model.

        This always creates a *new* session via ``session.create``; it never
        forks writer history. When ``writer_model`` is supplied the concrete
        model must differ, so a misconfiguration fails instead of silently using
        the writer model.
        """
        if not agent:
            raise ValueError("critic agent (role) is required")
        validate_model_ref(model)
        if writer_model is not None:
            validate_model_ref(writer_model)
            if (model["providerID"], model["id"]) == (
                writer_model["providerID"],
                writer_model["id"],
            ):
                raise ValueError(
                    "critic model must differ from the writer model "
                    f"({model['providerID']}/{model['id']})"
                )
        return self.create_session(
            agent=agent, model=model, title=title, directory=directory
        )

    # -- admission -----------------------------------------------------------
    def admit(
        self,
        session_id: str,
        text: str,
        admission_id: str,
        *,
        delivery: str = "queue",
        metadata: dict[str, Any] | None = None,
    ) -> Admission:
        """Durably admit one input using a client-supplied admission ID.

        ``admission_id`` is the idempotency key (``Session.Message.ID``, ``^msg_``).
        A retry with the same ID yields one accepted input: the server reports
        ``ConflictError`` and the persisted receipt is recovered by
        reconciliation. A transport failure leaves the outcome *unknown* and is
        reconciled rather than resent under a new identity.
        """
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be a non-empty string")
        self._require_message_id(admission_id)
        if delivery not in ("steer", "queue"):
            raise ValueError("delivery must be 'steer' or 'queue'")
        body: dict[str, Any] = {
            "id": admission_id,
            "text": text,
            "delivery": delivery,
        }
        if metadata is not None:
            body["metadata"] = metadata
        try:
            receipt = self._request(
                "POST", f"/api/session/{session_id}/prompt", body=body
            )["data"]
            return Admission(admission_id, receipt, duplicate=False)
        except OpenCodeApiError as exc:
            if exc.status == 409 and exc.tag == CONFLICT_TAG:
                found = self.reconcile(session_id, admission_id)
                return Admission(
                    admission_id,
                    found.durable_receipt,
                    duplicate=True,
                    reconciled=True,
                )
            raise
        except OpenCodeTransportError:
            found = self.reconcile(session_id, admission_id)
            return Admission(
                admission_id,
                found.durable_receipt,
                duplicate=False,
                reconciled=True,
            )

    # -- durable reads / reconciliation --------------------------------------
    def list_inbox(self, session_id: str) -> list[dict[str, Any]]:
        self._require_session_id(session_id)
        return self._request("GET", f"/api/session/{session_id}/inbox")["data"]

    def list_messages(
        self,
        session_id: str,
        *,
        type: str | None = None,  # noqa: A002 - keep the documented query name
        limit: int | None = None,
        order: str | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        self._require_session_id(session_id)
        params: list[str] = []
        if type is not None:
            params.append(f"type={urllib.parse.quote(type)}")
        if limit is not None:
            params.append(f"limit={int(limit)}")
        if order is not None:
            if order not in ("asc", "desc"):
                raise ValueError("order must be 'asc' or 'desc'")
            params.append(f"order={order}")
        if cursor is not None:
            params.append(f"cursor={urllib.parse.quote(cursor)}")
        path = f"/api/session/{session_id}/message"
        if params:
            path += "?" + "&".join(params)
        return self._request("GET", path)

    def get_message(self, session_id: str, message_id: str) -> dict[str, Any]:
        self._require_session_id(session_id)
        self._require_message_id(message_id)
        return self._request(
            "GET", f"/api/session/{session_id}/message/{message_id}"
        )["data"]

    def collect_messages(
        self,
        session_id: str,
        *,
        type: str | None = None,  # noqa: A002 - documented query name
        order: str = "asc",
        max_pages: int | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Read the projected timeline, following ``cursor.next`` when needed.

        Older admissions can fall off the first page, so reconciliation pages
        from the oldest message forward. Returns ``(messages, truncated)`` where
        ``truncated`` is true when the page bound was reached before the server
        exhausted its cursor; a truncated read is never treated as complete.

        Query conformance (published ``session.message.list``): ``order`` applies
        to the **first page** only and must not be combined with ``cursor``, while
        the ``type`` filter must be repeated on every page. Only the first page
        therefore sends ``order``; later pages send the same ``type`` (when set)
        plus the opaque ``cursor`` and nothing else that could conflict.
        """
        self._require_session_id(session_id)
        bound = self.max_message_pages if max_pages is None else int(max_pages)
        if bound < 1:
            raise ValueError("max_pages must be a positive integer")
        messages: list[dict[str, Any]] = []
        cursor: str | None = None
        pages = 0
        seen_cursors: set[str] = set()
        while True:
            if cursor is None:
                # First page: order is permitted (and only here).
                page = self.list_messages(session_id, type=type, order=order)
            else:
                # Later pages: repeat the type filter, send the cursor, never order.
                page = self.list_messages(session_id, type=type, cursor=cursor)
            messages.extend(page.get("data", []))
            pages += 1
            next_cursor = (page.get("cursor") or {}).get("next")
            if not next_cursor:
                return messages, False
            if next_cursor in seen_cursors:
                # a repeating cursor cannot make progress; stop honestly
                return messages, True
            seen_cursors.add(next_cursor)
            if pages >= bound:
                return messages, True
            cursor = next_cursor

    def reconcile(self, session_id: str, admission_id: str) -> ReconcileResult:
        """Reconstruct per-turn state for one admission from durable server data.

        The durable receipt (inbox item or projected user message) proves the
        input was accepted. A turn ends only at its correlated **authoritative
        ``idle``** message — a ``Session.Message.Idle`` whose ``outcome`` is
        ``succeeded``, ``failed``, or ``interrupted``. A completed ``assistant``
        message is *not* a terminal: with ``finish=tool-calls`` the agent loop
        continues, and even ``finish=stop`` is not task success because more loop
        steps may follow. The published message schemas expose no parent/linkage
        field, so a turn is delimited positionally: the target user message, then
        the next user/synthetic message, with the idle in between. An idle from an
        earlier (or later) turn never completes the target. If the timeline cannot
        be read far enough to be certain, the state is ``unknown``, never
        ``completed``.
        """
        self._require_message_id(admission_id)
        inbox = self.list_inbox(session_id)
        receipt = next((i for i in inbox if i.get("id") == admission_id), None)

        messages, truncated = self.collect_messages(session_id, order="asc")
        start = next(
            (
                index
                for index, message in enumerate(messages)
                if message.get("type") == "user" and message.get("id") == admission_id
            ),
            None,
        )

        if start is None:
            # No projected user message: either still enqueued, or the timeline
            # could not be read far enough to be sure.
            if receipt is not None:
                return ReconcileResult(
                    admission_id, "queued", receipt, None, reason="enqueued, not delivered"
                )
            if truncated:
                return ReconcileResult(
                    admission_id,
                    "unknown",
                    None,
                    None,
                    reason=f"message paging limit ({self.max_message_pages} pages) reached",
                )
            return ReconcileResult(admission_id, "unknown", None, None, reason="no server trace")

        return self._reconcile_turn(messages, start, admission_id, receipt, truncated)

    @staticmethod
    def _reconcile_turn(
        messages: list[dict[str, Any]],
        start: int,
        admission_id: str,
        receipt: dict[str, Any] | None,
        truncated: bool,
    ) -> ReconcileResult:
        user_message = messages[start]
        durable = receipt if receipt is not None else user_message

        # Window: (start, next user/synthetic message). An idle outside the
        # window belongs to another turn and must not complete this one.
        window_end = len(messages)
        for index in range(start + 1, len(messages)):
            if messages[index].get("type") in TURN_OPENING_TYPES:
                window_end = index
                break
        window_open_to_eof = window_end == len(messages)
        window = messages[start + 1 : window_end]

        # Only an ``idle`` message is the authoritative terminal for a turn. A
        # completed ``assistant`` is an intermediate agent-loop step and never
        # completes the target on its own:
        #   * finish=tool-calls -> the loop continues with tool execution;
        #   * finish=stop        -> still not task success (more steps may follow);
        #   * finish=length/content-filter/error -> truncated or failed step.
        # A terminal from another turn, or an assistant with no correlated idle,
        # must not be read as success.
        idle = next((m for m in window if m.get("type") == "idle"), None)

        if idle is None:
            if truncated and window_open_to_eof:
                # the idle could be on an unread page: do not guess
                return ReconcileResult(
                    admission_id,
                    "unknown",
                    durable,
                    None,
                    reason="turn window not fully read (message paging limit)",
                )
            return ReconcileResult(
                admission_id,
                "in_progress",
                durable,
                None,
                reason="no authoritative idle for this turn (assistant completion is not success)",
            )

        outcome = idle.get("outcome")
        state = {
            "succeeded": "completed",
            "failed": "failed",
            "interrupted": "interrupted",
        }.get(outcome, "unknown")
        completion = {"outcome": outcome, "message": idle}
        if state == "unknown":
            return ReconcileResult(
                admission_id,
                "unknown",
                durable,
                completion,
                reason=f"unrecognized idle outcome {outcome!r}",
            )
        return ReconcileResult(admission_id, state, durable, completion)

    # -- bounded wait --------------------------------------------------------
    def wait(
        self,
        session_id: str,
        *,
        timeout: float = 30.0,
        retry_interval: float = 0.25,
    ) -> bool:
        """Bounded wait for the session agent loop to become idle.

        Uses ``experimental.session.wait`` (204 when idle / 503 when a service is
        unavailable). Retries on 503 and transport timeouts until the deadline,
        then raises :class:`QuiescenceTimeout`. Never blocks forever.
        """
        self._require_session_id(session_id)
        deadline = time.monotonic() + float(timeout)
        last: OpenCodeError | None = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise QuiescenceTimeout(
                    f"session {session_id} not idle within {timeout}s (last={last})"
                )
            try:
                self._request(
                    "POST",
                    f"/api/experimental/session/{session_id}/wait",
                    timeout=remaining,
                )
                return True
            except OpenCodeApiError as exc:
                if exc.status == 503:
                    last = exc
                else:
                    raise
            except OpenCodeTransportError as exc:
                last = exc
            time.sleep(min(retry_interval, max(0.0, deadline - time.monotonic())))
        # unreachable

    # -- typed decisions (Jev predicate/choice/score) ------------------------
    def generate_decisions(
        self,
        *,
        model: dict[str, Any],
        input_parts: list[dict[str, Any]],
        questions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """``experimental.generate.decisions`` — stateless typed decisions.

        Stateless: nothing is stored. The response is validated **at this
        boundary** against the published answer schemas and against the exact
        ``questions`` sent (cardinality, names, order, types, choice/score
        distributions), so an unchecked answer never leaves the client.

        Per-question failures come back as ``{"type": "error", ...}`` answers
        while the other answers still return; that is *not* a scientific
        exclusion and is preserved. A malformed or missing response is a
        :class:`DecisionProtocolError` (a protocol failure, not a refusal).
        """
        validate_model_ref(model)
        if not isinstance(input_parts, list) or not input_parts:
            raise ValueError("input_parts must contain at least one part")
        normalized_questions = validate_decision_questions(questions)
        body = {
            "model": model,
            "input": input_parts,
            "questions": normalized_questions,
        }
        response = self._request(
            "POST", "/api/experimental/generate/decisions", body=body
        )
        if not isinstance(response, dict) or "data" not in response:
            raise DecisionProtocolError(
                "decisions response is missing the required 'data' object"
            )
        result = response["data"]
        if not isinstance(result, dict):
            raise DecisionProtocolError("decisions response 'data' must be an object")
        validate_decision_result(result, normalized_questions)
        return result

    # -- validation ----------------------------------------------------------
    @staticmethod
    def _require_session_id(session_id: str) -> None:
        if not isinstance(session_id, str) or not SESSION_ID_RE.match(session_id):
            raise ValueError(f"session_id must match ^ses, got {session_id!r}")

    @staticmethod
    def _require_message_id(message_id: str) -> None:
        if not isinstance(message_id, str) or not MESSAGE_ID_RE.match(message_id):
            raise ValueError(f"message id must match ^msg_, got {message_id!r}")


# --- typed decision helpers ---------------------------------------------------
# Exact field sets from the published V2 OpenAPI (all schemas set
# ``additionalProperties: false``), so an unknown field is a protocol failure.
_ANSWER_KEYS = {
    "predicate": frozenset({"type", "name", "probability"}),
    "choice": frozenset({"type", "name", "choice", "probabilities", "confidence"}),
    "score": frozenset({"type", "name", "score", "probabilities", "confidence"}),
    "error": frozenset({"type", "name", "reason", "message"}),
}
_ANSWER_REQUIRED = {
    "predicate": ("name", "probability"),
    "choice": ("name", "choice", "probabilities", "confidence"),
    "score": ("name", "score", "probabilities", "confidence"),
    "error": ("name", "reason"),
}
_CHOICE_PROB_KEYS = frozenset({"value", "probability"})
_SCORE_PROB_KEYS = frozenset({"value", "label", "probability"})
_RESULT_KEYS = frozenset({"answers", "cost", "tokens", "native"})
_USAGE_KEYS = frozenset({"input", "output"})
_QUESTION_KEYS = {
    "predicate": frozenset({"type", "name", "instructions"}),
    "choice": frozenset({"type", "name", "instructions", "choices"}),
    "score": frozenset({"type", "name", "instructions", "levels"}),
}
_QUESTION_REQUIRED = {
    "predicate": ("name", "instructions"),
    "choice": ("name", "instructions", "choices"),
    "score": ("name", "instructions", "levels"),
}
_CHOICE_OPTION_KEYS = frozenset({"value", "description"})
_SCORE_LEVEL_KEYS = frozenset({"label", "description"})


def _is_number(value: Any) -> bool:
    """A JSON number: a finite ``int``/``float``, never a ``bool``.

    JSON booleans are not numbers (the schema ``type: number``), and ``NaN`` /
    ``Infinity`` are not valid JSON numbers.
    """
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _is_probability(value: Any) -> bool:
    return _is_number(value) and 0 <= value <= 1


def _is_choice_value(value: Any) -> bool:
    """A ``Generate.ChoiceValue``: a string or a boolean (typed, not coerced)."""
    return isinstance(value, str) or isinstance(value, bool)


def _typed_key(value: Any) -> tuple[str, Any]:
    """Typed identity so ``true`` and ``"true"`` are different choice values."""
    return (type(value).__name__, value)


def _reject_extra_fields(
    obj: dict[str, Any], allowed: frozenset[str], label: str, error: type[Exception]
) -> None:
    extra = set(obj) - allowed
    if extra:
        raise error(f"{label} has unknown fields: {sorted(extra)}")


def validate_decision_questions(
    questions: Any,
) -> list[dict[str, Any]]:
    """Validate ``Generate.DecisionQuestion`` request shapes (fail closed).

    Checks the published per-type required fields, rejects unknown fields, and
    requires non-empty ``choices`` / ``levels`` and unique names. Returns the
    questions unchanged for use as the expected answer contract.
    """
    if not isinstance(questions, list) or not questions:
        raise ValueError("questions must be a non-empty list")
    names: list[str] = []
    for index, question in enumerate(questions):
        if not isinstance(question, dict):
            raise ValueError(f"question {index} is not an object")
        kind = question.get("type")
        allowed = _QUESTION_KEYS.get(kind)
        if allowed is None:
            raise ValueError(f"question {index} has unknown type {kind!r}")
        missing = [key for key in _QUESTION_REQUIRED[kind] if key not in question]
        if missing:
            raise ValueError(f"question {index} ({kind}) missing {missing}")
        _reject_extra_fields(question, allowed, f"question {index} ({kind})", ValueError)
        name = question["name"]
        if not isinstance(name, str) or not name:
            raise ValueError(f"question {index} name must be a non-empty string")
        if not isinstance(question["instructions"], str):
            raise ValueError(f"question {index} instructions must be a string")
        if kind == "choice":
            choices = question["choices"]
            if not isinstance(choices, list) or not choices:
                raise ValueError(f"question {index} choices must be a non-empty list")
            seen: set[tuple[str, Any]] = set()
            for option in choices:
                if not isinstance(option, dict) or "value" not in option:
                    raise ValueError(f"question {index} choice option needs a value")
                _reject_extra_fields(option, _CHOICE_OPTION_KEYS, "choice option", ValueError)
                if not _is_choice_value(option["value"]):
                    raise ValueError(
                        f"question {index} choice value must be a string or boolean"
                    )
                key = _typed_key(option["value"])
                if key in seen:
                    raise ValueError(f"question {index} has a duplicate choice value")
                seen.add(key)
        if kind == "score":
            levels = question["levels"]
            if not isinstance(levels, list) or not levels:
                raise ValueError(f"question {index} levels must be a non-empty list")
            labels: list[str] = []
            for level in levels:
                if not isinstance(level, dict) or not isinstance(level.get("label"), str):
                    raise ValueError(f"question {index} level needs a string label")
                _reject_extra_fields(level, _SCORE_LEVEL_KEYS, "score level", ValueError)
                labels.append(level["label"])
            if len(set(labels)) != len(labels):
                raise ValueError(f"question {index} level labels must be unique")
        names.append(name)
    if len(set(names)) != len(names):
        raise ValueError("question names must be unique")
    return questions


def _validate_choice_probability(index: int, position: int, entry: Any) -> None:
    if not isinstance(entry, dict):
        raise DecisionProtocolError(f"answer {index} probability {position} is not an object")
    _reject_extra_fields(
        entry, _CHOICE_PROB_KEYS, f"answer {index} probability {position}", DecisionProtocolError
    )
    missing = [key for key in ("value", "probability") if key not in entry]
    if missing:
        raise DecisionProtocolError(
            f"answer {index} probability {position} missing {missing}"
        )
    if not _is_choice_value(entry["value"]):
        raise DecisionProtocolError(
            f"answer {index} probability {position} value must be a string or boolean"
        )
    if not _is_probability(entry["probability"]):
        raise DecisionProtocolError(
            f"answer {index} probability {position} must be a finite number in [0,1]"
        )


def _validate_score_probability(index: int, position: int, entry: Any) -> None:
    if not isinstance(entry, dict):
        raise DecisionProtocolError(f"answer {index} probability {position} is not an object")
    _reject_extra_fields(
        entry, _SCORE_PROB_KEYS, f"answer {index} probability {position}", DecisionProtocolError
    )
    missing = [key for key in ("value", "label", "probability") if key not in entry]
    if missing:
        raise DecisionProtocolError(
            f"answer {index} probability {position} missing {missing}"
        )
    if not isinstance(entry["value"], int) or isinstance(entry["value"], bool):
        raise DecisionProtocolError(
            f"answer {index} probability {position} value must be an integer level index"
        )
    if not isinstance(entry["label"], str):
        raise DecisionProtocolError(
            f"answer {index} probability {position} label must be a string"
        )
    if not _is_probability(entry["probability"]):
        raise DecisionProtocolError(
            f"answer {index} probability {position} must be a finite number in [0,1]"
        )


def _validate_choice_against_question(
    index: int, answer: dict[str, Any], question: dict[str, Any]
) -> None:
    declared = {_typed_key(option["value"]) for option in question["choices"]}
    if _typed_key(answer["choice"]) not in declared:
        raise DecisionProtocolError(
            f"answer {index} choice {answer['choice']!r} is not one of the question's choices"
        )
    seen: set[tuple[str, Any]] = set()
    for position, entry in enumerate(answer["probabilities"]):
        key = _typed_key(entry["value"])
        if key not in declared:
            raise DecisionProtocolError(
                f"answer {index} probability {position} value is not one of the question's choices"
            )
        if key in seen:
            raise DecisionProtocolError(
                f"answer {index} probability {position} duplicates a choice value"
            )
        seen.add(key)
    if seen != declared:
        raise DecisionProtocolError(
            f"answer {index} probabilities do not cover the question's choices exactly once"
        )


def _validate_score_against_question(
    index: int, answer: dict[str, Any], question: dict[str, Any]
) -> None:
    """The score distribution must cover the question's declared levels once each.

    The published ``Generate.ScoreAnswer`` probability entry is
    ``{value: integer, label: string, probability: number}``; the schema does not
    state the integer's base, so this checks the documented bindings (labels are
    the question's levels, each once, with distinct integer values) rather than
    inventing an index convention.
    """
    levels = question["levels"]
    declared = [level["label"] for level in levels]
    if len(answer["probabilities"]) != len(levels):
        raise DecisionProtocolError(
            f"answer {index} probabilities must have one entry per level "
            f"({len(levels)}), got {len(answer['probabilities'])}"
        )
    seen_labels: set[str] = set()
    seen_values: set[int] = set()
    for position, entry in enumerate(answer["probabilities"]):
        label = entry["label"]
        if label not in declared:
            raise DecisionProtocolError(
                f"answer {index} probability {position} label {label!r} is not one of the "
                f"question's level labels"
            )
        if label in seen_labels:
            raise DecisionProtocolError(
                f"answer {index} probability {position} duplicates level label {label!r}"
            )
        seen_labels.add(label)
        value = entry["value"]
        if value in seen_values:
            raise DecisionProtocolError(
                f"answer {index} probability {position} duplicates level value {value!r}"
            )
        seen_values.add(value)
    if seen_labels != set(declared):
        raise DecisionProtocolError(
            f"answer {index} probabilities do not cover the question's levels exactly once"
        )


def _validate_one_answer(index: int, answer: Any) -> str:
    """Validate one ``Generate.DecisionAnswer`` shape; return its ``type``."""
    if not isinstance(answer, dict):
        raise DecisionProtocolError(f"answer {index} is not an object")
    kind = answer.get("type")
    allowed = _ANSWER_KEYS.get(kind)
    if allowed is None:
        raise DecisionProtocolError(f"answer {index} has unknown type {kind!r}")
    missing = [key for key in _ANSWER_REQUIRED[kind] if key not in answer]
    if missing:
        raise DecisionProtocolError(f"answer {index} ({kind}) missing {missing}")
    _reject_extra_fields(answer, allowed, f"answer {index} ({kind})", DecisionProtocolError)
    name = answer["name"]
    if not isinstance(name, str) or not name:
        raise DecisionProtocolError(f"answer {index} name must be a non-empty string")
    if kind == "predicate":
        if not _is_probability(answer["probability"]):
            raise DecisionProtocolError(
                f"answer {index} probability must be a finite number in [0,1]"
            )
    elif kind == "choice":
        if not _is_choice_value(answer["choice"]):
            raise DecisionProtocolError(
                f"answer {index} choice must be a string or boolean"
            )
        if not _is_probability(answer["confidence"]):
            raise DecisionProtocolError(
                f"answer {index} confidence must be a finite number in [0,1]"
            )
        if not isinstance(answer["probabilities"], list):
            raise DecisionProtocolError(f"answer {index} probabilities must be a list")
        for position, entry in enumerate(answer["probabilities"]):
            _validate_choice_probability(index, position, entry)
    elif kind == "score":
        if not _is_number(answer["score"]):
            raise DecisionProtocolError(f"answer {index} score must be a finite number")
        if not _is_probability(answer["confidence"]):
            raise DecisionProtocolError(
                f"answer {index} confidence must be a finite number in [0,1]"
            )
        if not isinstance(answer["probabilities"], list):
            raise DecisionProtocolError(f"answer {index} probabilities must be a list")
        for position, entry in enumerate(answer["probabilities"]):
            _validate_score_probability(index, position, entry)
    elif kind == "error":
        if not isinstance(answer["reason"], str) or not answer["reason"]:
            raise DecisionProtocolError(
                f"answer {index} error reason must be a non-empty string"
            )
        if "message" in answer and not isinstance(answer["message"], str):
            raise DecisionProtocolError(f"answer {index} error message must be a string")
    return kind


def validate_decision_result(
    result: Any, questions: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Validate a ``Generate.DecisionResult`` (fail closed).

    Enforces the published answer schemas: required fields, no unknown fields,
    finite numeric bounds excluding booleans, and typed choice values. When the
    original ``questions`` are supplied (as ``generate_decisions`` does), it also
    enforces cardinality, names, order, types, and the choice/score distributions
    the contract describes (``answers`` are one per question, in question order).

    Returns the per-question ``error`` answers, which are valid shapes and are
    preserved: a refusal is **not** a protocol failure and is not an exclusion.
    Raises :class:`DecisionProtocolError` (a ``ValueError``) for malformed
    evidence.
    """
    if not isinstance(result, dict):
        raise DecisionProtocolError("decision result must be an object")
    _reject_extra_fields(result, _RESULT_KEYS, "decision result", DecisionProtocolError)
    if "answers" not in result:
        raise DecisionProtocolError("decision result is missing the required 'answers' list")
    answers = result["answers"]
    if not isinstance(answers, list):
        raise DecisionProtocolError("decision result 'answers' must be a list")
    if "cost" in result and not _is_number(result["cost"]):
        raise DecisionProtocolError("decision result cost must be a finite number")
    if "tokens" in result:
        tokens = result["tokens"]
        if not isinstance(tokens, dict):
            raise DecisionProtocolError("decision result tokens must be an object")
        _reject_extra_fields(tokens, _USAGE_KEYS, "tokens", DecisionProtocolError)
        for field in ("input", "output"):
            if field in tokens and not _is_number(tokens[field]):
                raise DecisionProtocolError(f"token count {field} must be a finite number")
    if "native" in result and not isinstance(result["native"], dict):
        raise DecisionProtocolError("decision result native must be an object")

    expected = None if questions is None else validate_decision_questions(questions)
    if expected is not None and len(answers) != len(expected):
        raise DecisionProtocolError(
            f"expected one answer per question ({len(expected)}), got {len(answers)}"
        )

    errors: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for index, answer in enumerate(answers):
        kind = _validate_one_answer(index, answer)
        name = answer["name"]
        if name in seen_names:
            raise DecisionProtocolError(f"answer {index} duplicates name {name!r}")
        seen_names.add(name)
        if expected is not None:
            question = expected[index]
            if name != question["name"]:
                raise DecisionProtocolError(
                    f"answer {index} name {name!r} does not match question {index} "
                    f"name {question['name']!r}"
                )
            if kind not in (question["type"], "error"):
                raise DecisionProtocolError(
                    f"answer {index} type {kind!r} does not answer the "
                    f"{question['type']!r} question {name!r}"
                )
            if kind == "choice":
                _validate_choice_against_question(index, answer, question)
            elif kind == "score":
                _validate_score_against_question(index, answer, question)
        if kind == "error":
            errors.append(answer)
    return errors


def first_decision_error(result: dict[str, Any]) -> "DecisionError | None":
    """Return a :class:`DecisionError` for the first failed question, if any."""
    for answer in result.get("answers", []):
        if answer.get("type") == "error":
            return DecisionError(answer)
    return None


def raise_on_decision_errors(result: dict[str, Any]) -> None:
    """Raise the first per-question :class:`DecisionError`, if any."""
    error = first_decision_error(result)
    if error is not None:
        raise error
    return None


class DecisionError(OpenCodeError):
    """One question had no answer (for example a refusal).

    The other answers are still available; callers must not treat this as a
    scientific exclusion.
    """

    def __init__(self, answer: dict[str, Any]):
        super().__init__(f"decision question {answer.get('name')!r} failed: {answer.get('reason')}")
        self.answer = answer
        self.question_name = answer.get("name")
        self.reason = answer.get("reason")


def summarize_decisions(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Return one summary row per answer, flagging per-question failures."""
    rows = []
    for answer in result.get("answers", []):
        kind = answer.get("type")
        if kind == "predicate":
            detail = answer.get("probability")
        elif kind == "choice":
            detail = answer.get("choice")
        elif kind == "score":
            detail = answer.get("score")
        else:
            detail = answer.get("reason")
        rows.append(
            {
                "name": answer.get("name"),
                "type": kind,
                "detail": detail,
                "failed": kind == "error",
            }
        )
    return rows
