"""Bounded, authenticated, loopback-only OpenCode V2 lifecycle client.

Production packaging of the retained, tested Python spike
(``spikes/runtime/python/opencode_v2.py``) into the installable package. Only the
lifecycle surface the application owns is promoted; the spike's native typed
``generate.decisions`` route, ``session.fork``, and environment/CLI construction
are deliberately **not** included (ADR-001 retains the tested direct-OpenRouter
Jev adapter for decisions, and the app-owned service manager owns configuration).

Authority:

* ``docs/implementation/04-agent-runtime.md`` deliverables 1-2 and acceptance
  criteria 3 (fresh, different-concrete-model critic), 4 (lost event streams
  reconcile from durable state/messages), and 7 (project/session/model isolation;
  the ordinary OpenCode service is untouched).
* ``docs/specification.md`` "Product and runtime" lines 14-16 and confirmed
  workflow line 38 (different concrete model, separate-context section critic).
* ``docs/contracts/02-gates-and-runtime.md`` §8 (admission != completion;
  ambiguous remote work reconciles instead of being resent under a new identity;
  missing events do not lose a completion or approval action).
* ``docs/decisions/001-runtime-language.md`` (explicit endpoint + credential,
  fail closed; loopback only; redirects refused; only an authoritative ``idle``
  completes; a writer-history fork is not a clean critic).

Wire shapes are the observed OpenCode V2.2.0 surface (``opencode --version`` ->
``v2.2.0``; ``GET /api/info`` -> ``version`` = ``2.2.0``; published
``opencode HttpApi`` OpenAPI 3.1.0, ``info.version`` ``0.0.1``): nothing here is
invented. Operation map: ``server.info`` ``GET /api/info``; ``session.create``
``POST /api/session``; ``session.get`` ``GET /api/session/{sessionID}``;
``session.prompt`` ``POST /api/session/{sessionID}/prompt``;
``session.inbox.list`` ``GET /api/session/{sessionID}/inbox``;
``session.message.list`` ``GET /api/session/{sessionID}/message``;
``session.message.get`` ``GET /api/session/{sessionID}/message/{messageID}``;
``session.interrupt`` ``POST /api/session/{sessionID}/interrupt``;
``experimental.session.wait`` ``POST /api/experimental/session/{sessionID}/wait``.

Safety properties:

* Standard library only (``urllib``); no third-party HTTP client, no agent
  framework.
* Explicit, authenticated **loopback** endpoint; a non-loopback host, a
  non-HTTP(S) scheme, or an embedded credential fails closed. No service
  discovery, start, registration, database, or credential file is ever read.
* HTTP ``Bearer`` or the observed ``Basic`` scheme (``opencode:<per-run
  password>``); the scheme is explicit and never guessed, and the wrong scheme's
  fields are refused.
* Redirects are refused (the credential is never forwarded) and proxies are
  bypassed (``ProxyHandler({})``), so the credential cannot leave the origin.
* Response bodies are bounded (``max_response_bytes``), every request is
  time-bounded, and message paging is bounded (``max_message_pages``).
* Errors are **static and redacted**: no peer-supplied message text, raw body,
  or credential is ever included in an exception.
* Request/response identities are strictly validated (envelope, session and
  message IDs, and the pinned agent/model round-trip), so a malformed or foreign
  response fails closed instead of being trusted.

Semantics (the point of the client):

* ``admit`` uses ``session.prompt`` and returns a *durable admission receipt*
  (``Session.Inbox.User``): the input is persisted and scheduled. This is **not**
  a completion. The client-supplied admission ID is the idempotency key; a
  same-ID replay with **divergent** input is refused (:class:`AdmissionConflictError`)
  rather than silently accepted.
* ``reconcile`` reconstructs state from durable state (inbox + messages) so a
  second client recovers after missed events. A turn completes only at its
  correlated **authoritative** ``idle`` terminal (``Session.Message.Idle`` with
  ``outcome`` ``succeeded``/``failed``/``interrupted``). An ``idle`` from another
  turn never completes the target; an unread or ambiguous window returns
  ``unknown``, never success.
* ``wait`` is a bounded wait for the agent loop to become idle, so the CLI is
  never required to stay open for the task to finish.

The later app-owned service manager may call :meth:`OpenCodeV2Client.info` to
check ``server.info`` ``version`` before relying on the service.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

# --- exact patterns from the published OpenAPI schemas -----------------------
SESSION_ID_RE = re.compile(r"^ses")
MESSAGE_ID_RE = re.compile(r"^msg_")

# Response tags from the published error schemas (*ErrorEncoded).
CONFLICT_TAG = "ConflictError"

# Supported Authorization schemes. The published OpenAPI declares no
# securitySchemes, but the disposable 2.2.0 harness was observed to answer HTTP
# Basic ``opencode:<per-run-password>``. ``bearer`` stays the default for backward
# compatibility; ``basic`` is opt-in and never guessed or discovered.
AUTH_BEARER = "bearer"
AUTH_BASIC = "basic"
AUTH_SCHEMES = (AUTH_BEARER, AUTH_BASIC)
BASIC_DEFAULT_USERNAME = "opencode"

# Message types that open a new turn in the projected timeline. The published
# schemas (Session.Message.*) carry no parent/linkage field, so a turn can only be
# delimited positionally: its ``user`` message, then the next user/synthetic
# message, with the authoritative ``idle`` terminal in between.
TURN_OPENING_TYPES = ("user", "synthetic")
LOOPBACK_HOSTS = ("localhost",)

# Bounds. A response body over the limit is refused before it is trusted; paging
# stops honestly at the page bound.
DEFAULT_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_MESSAGE_PAGES = 100

# Static, redacted error details. These are the client's own strings and are the
# only detail ever attached to an error; peer-supplied text is never propagated.
_DETAIL_REDIRECT = "redirect refused; credential not forwarded"
_DETAIL_TOO_LARGE = "response body exceeds the configured size limit"
_DETAIL_NOT_JSON = "response body is not valid JSON"


class _RedirectRejectedHandler(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect so the credential is never forwarded.

    ``redirect_request`` returning ``None`` makes urllib raise the redirect as an
    ``HTTPError`` instead of following it, blocking credential leakage to a
    redirect target (cross-origin or otherwise).
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


class OpenCodeError(Exception):
    """Base class for client errors."""


class OpenCodeApiError(OpenCodeError):
    """A decoded non-2xx API response, with a **static redacted** message.

    ``tag`` is the ``_tag`` of the published error schema (for example
    ``ConflictError``) when the body carried a string tag; otherwise ``None``.
    The exception never carries the peer's ``message`` text, raw body, or any
    credential: the only free part is ``detail``, an internal constant supplied
    by this module.
    """

    def __init__(self, status: int, tag: str | None = None, detail: str | None = None):
        self.status = int(status)
        self.tag = tag or None
        self.detail = detail
        text = f"HTTP {self.status} {self.tag or 'error'}"
        if detail:
            text = f"{text}: {detail}"
        super().__init__(text)


class OpenCodeResponseTooLargeError(OpenCodeError):
    """A response body exceeded the configured ``max_response_bytes`` bound."""


class OpenCodeProtocolError(OpenCodeError, ValueError):
    """A response is malformed, misidentified, or otherwise untrustworthy.

    Distinct from :class:`OpenCodeApiError` (a well-formed error envelope) and
    from :class:`OpenCodeTransportError` (no HTTP response at all). Subclasses
    ``ValueError`` so existing ``ValueError`` handling keeps working.
    """


class AdmissionConflictError(OpenCodeProtocolError):
    """A same-ID replay would admit *different* input than the durable record.

    The idempotency key already holds other content; accepting it would silently
    drop or overwrite the admitted input, so it fails closed instead.
    """


class OpenCodeTransportError(OpenCodeError):
    """The request never produced a decodable HTTP response (network/timeout).

    The outcome is unknown; callers must reconcile instead of blindly resending
    with a new identity.
    """


class QuiescenceTimeout(OpenCodeError):
    """The bounded wait elapsed before the session became idle."""


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
      authoritative ``idle`` terminal yet, and nothing in the window shows the
      turn has died. A completed ``assistant`` message does not end the turn.
    * ``completed`` — the correlated ``idle`` terminal for *this* turn succeeded.
    * ``failed`` / ``interrupted`` — the correlated ``idle`` terminal for *this*
      turn ended without success.
    * ``unknown`` — no reliable trace, an unread/ambiguous window, **or** an
      assistant that ended in error/refusal with no authoritative ``idle`` (the
      turn cannot be assumed to still be running); ``reason`` explains why. Never
      success, and never a confident ``failed``/``interrupted`` without an
      authoritative terminal.

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
    credential, or a missing host. The client must never talk to a remote or
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


def resolve_auth(
    *,
    auth: str,
    token: str | None,
    username: str | None,
    password: str | None,
) -> tuple[str, str | None, str | None]:
    """Resolve an explicit auth scheme, failing closed on anything missing.

    Returns ``(scheme, username, secret)`` where ``secret`` is the bearer token
    for ``bearer`` or the password for ``basic``. Exactly one scheme is chosen:

    * ``bearer`` (default) uses ``token``; supplying basic fields is an error, so
      a misconfiguration cannot silently authenticate a different way.
    * ``basic`` uses ``username`` (default ``opencode``) + ``password``; a token
      is refused.

    No scheme is ever discovered, guessed, or read from OpenCode's own state. Any
    password is supplied already resolved by the caller; this client performs no
    file, keychain, or registration access.
    """
    if auth not in AUTH_SCHEMES:
        raise ValueError(f"auth must be one of {AUTH_SCHEMES}, got {auth!r}")
    if auth == AUTH_BASIC:
        if token is not None:
            raise ValueError(
                "auth='basic' does not use a bearer token; pass username/password instead"
            )
        if not isinstance(password, str) or not password:
            raise ValueError(
                "auth='basic' requires an explicit password; refusing unauthenticated access"
            )
        name = username if username is not None else BASIC_DEFAULT_USERNAME
        if not isinstance(name, str) or not name:
            raise ValueError("auth='basic' username must be a non-empty string")
        return AUTH_BASIC, name, password
    if username is not None or password is not None:
        raise ValueError(
            "auth='bearer' uses a token; pass token instead of username/password"
        )
    if not isinstance(token, str) or not token.strip():
        raise ValueError(
            "an explicit bearer token is required; refusing unauthenticated access"
        )
    return AUTH_BEARER, None, token


def validate_model_ref(model: dict[str, Any]) -> dict[str, Any]:
    """Validate a ``Model.Ref`` (``{id, providerID, variant?}``). Never guess."""
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


def _durable_text(record: dict[str, Any] | None) -> str | None:
    """The text a durable admission record holds, or ``None`` if it carries none.

    Handles both shapes the server persists: a ``Session.Inbox.User`` item
    (``payload.text``) and a projected ``user`` message (top-level ``text``).
    """
    if not isinstance(record, dict):
        return None
    payload = record.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("text"), str):
        return payload["text"]
    text = record.get("text")
    return text if isinstance(text, str) else None


class OpenCodeV2Client:
    """Small, explicit client for the OpenCode V2 lifecycle HTTP API.

    :param base_url: explicit **loopback** endpoint, e.g. ``http://127.0.0.1:4096``.
        Non-loopback hosts are refused. No discovery, start, or registration.
    :param token: explicit bearer token (``Authorization: Bearer``). Required when
        ``auth="bearer"`` (the default): the client fails closed without one.
    :param auth: explicit Authorization scheme, ``"bearer"`` (default) or
        ``"basic"``. The disposable 2.2.0 harness answers HTTP Basic
        ``opencode:<per-run-password>``, so ``auth="basic"`` is available; it is
        never discovered or guessed.
    :param username: Basic username (default ``opencode``); ``basic`` only.
    :param password: Basic password (already resolved by the caller); ``basic``
        only.
    :param timeout: per-request socket timeout in seconds.
    :param directory: absolute location directory pinned on session create.
    :param max_message_pages: hard bound on pages read while reconciling.
    :param max_response_bytes: hard bound on a response body's size.

    Redirects are refused and proxies are bypassed, so the credential cannot leave
    the origin. Every error message is static and redacted.
    """

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        *,
        auth: str = AUTH_BEARER,
        username: str | None = None,
        password: str | None = None,
        timeout: float = 30.0,
        directory: str | None = None,
        max_message_pages: int = DEFAULT_MAX_MESSAGE_PAGES,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ):
        assert_explicit_loopback_url(base_url)
        scheme, auth_username, secret = resolve_auth(
            auth=auth, token=token, username=username, password=password
        )
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise ValueError("timeout must be a positive number")
        if (
            not isinstance(max_message_pages, int)
            or isinstance(max_message_pages, bool)
            or max_message_pages < 1
        ):
            raise ValueError("max_message_pages must be a positive integer")
        if (
            not isinstance(max_response_bytes, int)
            or isinstance(max_response_bytes, bool)
            or max_response_bytes < 1
        ):
            raise ValueError("max_response_bytes must be a positive integer")
        if directory is not None and (
            not isinstance(directory, str) or not directory
        ):
            raise ValueError("directory must be a non-empty string when given")
        self.base_url = base_url.rstrip("/")
        self.auth = scheme
        self.token = token if scheme == AUTH_BEARER else None
        self.username = auth_username
        self.password = secret if scheme == AUTH_BASIC else None
        self.timeout = float(timeout)
        self.directory = directory
        self.max_message_pages = max_message_pages
        self.max_response_bytes = max_response_bytes
        # A private opener that bypasses proxies and refuses redirects. The empty
        # ProxyHandler means the request never routes through ``http_proxy`` from
        # the environment, so the credential cannot be forwarded to a proxy.
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _RedirectRejectedHandler
        )

    def __repr__(self) -> str:  # never leak the credential
        return (
            f"{type(self).__name__}(base_url={self.base_url!r}, auth={self.auth!r}, "
            f"timeout={self.timeout!r})"
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
        headers["Authorization"] = self._authorization()
        return headers

    def _read_bounded(self, stream: Any, headers: Any | None) -> bytes:
        """Read a response body no larger than ``max_response_bytes``.

        A ``Content-Length`` over the bound is refused before reading; a chunked
        body is still read one byte past the bound so an oversized body is caught
        rather than silently truncated.
        """
        limit = self.max_response_bytes
        if headers is not None:
            declared_raw = headers.get("Content-Length")
            if declared_raw is not None:
                try:
                    declared = int(declared_raw)
                except (TypeError, ValueError):
                    declared = None
                if declared is not None and declared > limit:
                    raise OpenCodeResponseTooLargeError(_DETAIL_TOO_LARGE)
        raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise OpenCodeResponseTooLargeError(_DETAIL_TOO_LARGE)
        return raw

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
        effective_timeout = self.timeout if timeout is None else timeout
        try:
            with self._opener.open(request, timeout=effective_timeout) as response:
                raw = self._read_bounded(response, response.headers)
        except urllib.error.HTTPError as exc:
            try:
                raw = self._read_bounded(exc, exc.headers)
            finally:
                exc.close()
            if 300 <= exc.code < 400:
                raise OpenCodeApiError(exc.code, None, _DETAIL_REDIRECT) from None
            raise OpenCodeApiError(exc.code, self._error_tag(raw)) from None
        except (OpenCodeResponseTooLargeError, OpenCodeProtocolError):
            raise
        except (urllib.error.URLError, OSError):
            # Covers connection refused/reset, RemoteDisconnected, and socket
            # timeouts. The outcome is unknown, so no peer text is surfaced.
            raise OpenCodeTransportError(f"{method} {path}: transport error") from None
        return self._decode(raw)

    @staticmethod
    def _decode(raw: bytes) -> Any:
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise OpenCodeProtocolError(_DETAIL_NOT_JSON) from None

    @staticmethod
    def _error_tag(raw: bytes) -> str | None:
        """The ``_tag`` of a decoded error envelope, or ``None``. Never raises."""
        if not raw:
            return None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        tag = payload.get("_tag") if isinstance(payload, dict) else None
        return tag if isinstance(tag, str) and tag else None

    # -- envelope / identity validation --------------------------------------
    @staticmethod
    def _expect_object(response: Any, context: str) -> dict[str, Any]:
        if not isinstance(response, dict):
            raise OpenCodeProtocolError(f"{context}: response envelope is not an object")
        if "data" not in response:
            raise OpenCodeProtocolError(f"{context}: response envelope has no 'data'")
        data = response["data"]
        if not isinstance(data, dict):
            raise OpenCodeProtocolError(f"{context}: 'data' is not an object")
        return data

    @staticmethod
    def _expect_list(response: Any, context: str) -> list[Any]:
        if not isinstance(response, dict):
            raise OpenCodeProtocolError(f"{context}: response envelope is not an object")
        data = response.get("data")
        if not isinstance(data, list):
            raise OpenCodeProtocolError(f"{context}: 'data' is not a list")
        return data

    @staticmethod
    def _check_session_id(value: Any, context: str) -> str:
        if not isinstance(value, str) or not SESSION_ID_RE.match(value):
            raise OpenCodeProtocolError(f"{context}: session id is not a valid Session.ID")
        return value

    @staticmethod
    def _check_message_id(value: Any, context: str) -> str:
        if not isinstance(value, str) or not MESSAGE_ID_RE.match(value):
            raise OpenCodeProtocolError(f"{context}: message id is not a valid message id")
        return value

    @staticmethod
    def _check_owner(record: dict[str, Any], session_id: str, context: str) -> None:
        """Refuse a record that names a *different* session (foreign identity).

        A missing ``sessionID`` is tolerated (the projected message schemas carry
        none); a present value must match.
        """
        owner = record.get("sessionID")
        if owner is not None and owner != session_id:
            raise OpenCodeProtocolError(f"{context}: record belongs to a different session")

    @staticmethod
    def _check_pinned(data: dict[str, Any], key: str, expected: Any, context: str) -> None:
        """Refuse a create result that reports a *different* pinned value.

        A missing field is tolerated (the server may omit it); a present,
        different value means the server did not honour the requested pin, so it
        is a silent-fallback attempt and fails closed.
        """
        if key in data and data[key] != expected:
            raise OpenCodeProtocolError(
                f"{context}: server did not honour the pinned {key}"
            )

    @staticmethod
    def _require_session_id(session_id: str) -> None:
        if not isinstance(session_id, str) or not SESSION_ID_RE.match(session_id):
            raise ValueError(f"session_id must match ^ses, got {session_id!r}")

    @staticmethod
    def _require_message_id(message_id: str) -> None:
        if not isinstance(message_id, str) or not MESSAGE_ID_RE.match(message_id):
            raise ValueError(f"message id must match ^msg_, got {message_id!r}")

    # -- server --------------------------------------------------------------
    def info(self) -> dict[str, Any]:
        """``server.info`` — prove the endpoint answers and expose its version.

        The app-owned service manager uses the returned ``version`` to check the
        pinned service version before relying on it. A response without a string
        ``version`` is malformed and fails closed.
        """
        response = self._request("GET", "/api/info")
        if not isinstance(response, dict):
            raise OpenCodeProtocolError("server.info: response is not an object")
        version = response.get("version")
        if not isinstance(version, str) or not version:
            raise OpenCodeProtocolError("server.info: missing server version")
        return response

    # -- sessions ------------------------------------------------------------
    def create_session(
        self,
        *,
        agent: str,
        model: dict[str, Any],
        title: str | None = None,
        directory: str | None = None,
    ) -> dict[str, Any]:
        """``session.create`` — create a fresh session with pinned role/model.

        ``agent`` is the role (an ``Agent.ID``) and ``model`` is a ``Model.Ref``;
        both are **required**, because the application never assumes server
        defaults are safe for scientific literature roles. The returned session is
        validated to identify itself with a ``Session.ID`` and, when present, to
        echo the pinned ``agent``/``model`` (no silent fallback).
        """
        if not isinstance(agent, str) or not agent.strip():
            raise ValueError("agent (role) is required and must be a non-empty string")
        validate_model_ref(model)
        body: dict[str, Any] = {"agent": agent, "model": model}
        if title is not None:
            if not isinstance(title, str):
                raise ValueError("title must be a string")
            body["title"] = title
        location = directory if directory is not None else self.directory
        if location is not None:
            if not isinstance(location, str) or not location:
                raise ValueError("directory must be a non-empty string when given")
            body["location"] = {"directory": location}
        data = self._expect_object(
            self._request("POST", "/api/session", body=body), "session.create"
        )
        self._check_session_id(data.get("id"), "session.create")
        self._check_pinned(data, "agent", agent, "session.create")
        self._check_pinned(data, "model", model, "session.create")
        return data

    def get_session(self, session_id: str) -> dict[str, Any]:
        """``session.get`` — retrieve a session; the response must be that session."""
        self._require_session_id(session_id)
        data = self._expect_object(
            self._request("GET", f"/api/session/{session_id}"), "session.get"
        )
        returned = self._check_session_id(data.get("id"), "session.get")
        if returned != session_id:
            raise OpenCodeProtocolError("session.get: response identifies a different session")
        return data

    def interrupt(self, session_id: str, *, resume: bool | None = None) -> bool:
        """``session.interrupt`` — returns whether an active execution stopped."""
        self._require_session_id(session_id)
        path = f"/api/session/{session_id}/interrupt"
        if resume is not None:
            path += f"?resume={'true' if resume else 'false'}"
        response = self._request("POST", path)
        if not isinstance(response, dict) or not isinstance(
            response.get("interrupted"), bool
        ):
            raise OpenCodeProtocolError(
                "session.interrupt: response has no boolean 'interrupted'"
            )
        return response["interrupted"]

    def start_critic(
        self,
        *,
        agent: str,
        model: dict[str, Any],
        writer_model: dict[str, Any],
        title: str | None = None,
        directory: str | None = None,
    ) -> dict[str, Any]:
        """Create an independent critic session with a pinned role and model.

        The critic is **always** a new session via ``session.create``; writer
        history is never forked (a fork carries writer context and is not a clean
        critic). ``writer_model`` is required, and the concrete model must differ:
        a critic that reuses the writer's ``providerID`` **and** ``id`` — including
        a variant-only change — fails rather than silently using the writer model.
        """
        if not isinstance(agent, str) or not agent.strip():
            raise ValueError("critic agent (role) is required")
        validate_model_ref(model)
        validate_model_ref(writer_model)
        if (model["providerID"], model["id"]) == (
            writer_model["providerID"],
            writer_model["id"],
        ):
            raise ValueError(
                "critic model must differ from the writer model "
                f"({model['providerID']}/{model['id']}); a variant-only change is not a "
                "different concrete model"
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
    ) -> Admission:
        """Durably admit one input using a client-supplied admission ID.

        ``admission_id`` is the idempotency key (``Session.Message.ID``, ``^msg_``).
        A retry with the same ID yields one accepted input: the server reports
        ``ConflictError`` (or, as the real 2.2.0 service was observed to do,
        returns ``200`` again) and the persisted receipt is recovered by
        reconciliation. The persisted input text is checked against what was
        submitted, so a same-ID replay with **divergent** input is refused with
        :class:`AdmissionConflictError` rather than silently accepted. A transport
        failure leaves the outcome *unknown* and is reconciled rather than resent
        under a new identity.
        """
        self._require_session_id(session_id)
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be a non-empty string")
        self._require_message_id(admission_id)
        if delivery not in ("steer", "queue"):
            raise ValueError("delivery must be 'steer' or 'queue'")
        body: dict[str, Any] = {"id": admission_id, "text": text, "delivery": delivery}
        try:
            receipt = self._expect_object(
                self._request("POST", f"/api/session/{session_id}/prompt", body=body),
                "session.prompt",
            )
        except OpenCodeApiError as exc:
            if exc.status == 409 and exc.tag == CONFLICT_TAG:
                return self._reconcile_admission(
                    session_id, text, admission_id, duplicate=True
                )
            raise
        except OpenCodeTransportError:
            return self._reconcile_admission(
                session_id, text, admission_id, duplicate=False
            )
        self._validate_receipt(receipt, session_id, admission_id)
        persisted = _durable_text(receipt)
        if persisted is not None and persisted != text:
            raise AdmissionConflictError(
                f"admission {admission_id} already holds different input"
            )
        return Admission(admission_id, receipt, duplicate=False)

    def _reconcile_admission(
        self, session_id: str, text: str, admission_id: str, *, duplicate: bool
    ) -> Admission:
        found = self.reconcile(session_id, admission_id)
        receipt = found.durable_receipt
        if receipt is not None:
            persisted = _durable_text(receipt)
            if persisted is not None and persisted != text:
                raise AdmissionConflictError(
                    f"admission {admission_id} already holds different input"
                )
        return Admission(admission_id, receipt, duplicate=duplicate, reconciled=True)

    def _validate_receipt(
        self, receipt: dict[str, Any], session_id: str, admission_id: str
    ) -> None:
        rid = self._check_message_id(receipt.get("id"), "session.prompt")
        if rid != admission_id:
            raise OpenCodeProtocolError(
                "session.prompt: receipt identifies a different admission"
            )
        if receipt.get("type") != "user":
            raise OpenCodeProtocolError("session.prompt: receipt is not a user admission")
        self._check_owner(receipt, session_id, "session.prompt")

    # -- durable reads / reconciliation --------------------------------------
    def list_inbox(self, session_id: str) -> list[dict[str, Any]]:
        """``session.inbox.list`` — pending admitted inputs (identities checked)."""
        self._require_session_id(session_id)
        items = self._expect_list(
            self._request("GET", f"/api/session/{session_id}/inbox"), "session.inbox.list"
        )
        for item in items:
            if not isinstance(item, dict):
                raise OpenCodeProtocolError(
                    "session.inbox.list: inbox item is not an object"
                )
            self._check_message_id(item.get("id"), "session.inbox.list")
            self._check_owner(item, session_id, "session.inbox.list")
        return items

    def list_messages(
        self,
        session_id: str,
        *,
        type: str | None = None,  # noqa: A002 - keep the documented query name
        limit: int | None = None,
        order: str | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """``session.message.list`` — one page of the projected timeline."""
        self._require_session_id(session_id)
        params: list[str] = []
        if type is not None:
            params.append(f"type={urllib.parse.quote(type)}")
        if limit is not None:
            if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
                raise ValueError("limit must be a positive integer")
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
        response = self._request("GET", path)
        data = self._expect_list(response, "session.message.list")
        for message in data:
            if not isinstance(message, dict):
                raise OpenCodeProtocolError(
                    "session.message.list: message is not an object"
                )
            self._check_message_id(message.get("id"), "session.message.list")
            self._check_owner(message, session_id, "session.message.list")
        cursor_obj = response.get("cursor")  # type: ignore[union-attr]
        if cursor_obj is not None and not isinstance(cursor_obj, dict):
            raise OpenCodeProtocolError("session.message.list: cursor is not an object")
        return response

    def get_message(self, session_id: str, message_id: str) -> dict[str, Any]:
        """``session.message.get`` — one message; the response must be that message."""
        self._require_session_id(session_id)
        self._require_message_id(message_id)
        data = self._expect_object(
            self._request("GET", f"/api/session/{session_id}/message/{message_id}"),
            "session.message.get",
        )
        returned = self._check_message_id(data.get("id"), "session.message.get")
        if returned != message_id:
            raise OpenCodeProtocolError(
                "session.message.get: response identifies a different message"
            )
        self._check_owner(data, session_id, "session.message.get")
        return data

    def collect_messages(
        self,
        session_id: str,
        *,
        type: str | None = None,  # noqa: A002 - documented query name
        order: str = "asc",
        max_pages: int | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Read the projected timeline, following ``cursor.next`` when needed.

        Older admissions can fall off the first page, so reconciliation pages from
        the oldest message forward. Returns ``(messages, truncated)`` where
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
                page = self.list_messages(session_id, type=type, order=order)
            else:
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

        The durable receipt (inbox item or projected user message) proves the input
        was accepted. A turn ends only at its correlated **authoritative ``idle``**
        message — a ``Session.Message.Idle`` whose ``outcome`` is ``succeeded``,
        ``failed``, or ``interrupted``. A completed ``assistant`` message is *not*
        a terminal: with ``finish=tool-calls`` the agent loop continues, and even
        ``finish=stop`` is not task success because more loop steps may follow. The
        published message schemas expose no parent/linkage field, so a turn is
        delimited positionally: the target user message, then the next
        user/synthetic message, with the idle in between. An idle from an earlier
        (or later) turn never completes the target. If the timeline cannot be read
        far enough to be certain, the state is ``unknown``, never ``completed``.

        An assistant that ended in **error/refusal** with no authoritative ``idle``
        is ``unknown``, not ``in_progress``: a live service persists such an
        assistant (``finish="error"``) when a turn is torn down, e.g. by a restart,
        and then never writes a terminal ``idle``. The outcome is genuinely
        indeterminate, so it is reported ``unknown`` rather than implying the turn
        is still running, and never a confident ``failed``/``interrupted``.
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
            return ReconcileResult(
                admission_id, "unknown", None, None, reason="no server trace"
            )

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

        # Window: (start, next user/synthetic message). An idle outside the window
        # belongs to another turn and must not complete this one.
        window_end = len(messages)
        for index in range(start + 1, len(messages)):
            if messages[index].get("type") in TURN_OPENING_TYPES:
                window_end = index
                break
        window_open_to_eof = window_end == len(messages)
        window = messages[start + 1 : window_end]

        # Only an ``idle`` message is the authoritative terminal for a turn. A
        # completed ``assistant`` is an intermediate agent-loop step and never
        # completes the target on its own.
        idle = next((m for m in window if m.get("type") == "idle"), None)

        if idle is None:
            if truncated and window_open_to_eof:
                return ReconcileResult(
                    admission_id,
                    "unknown",
                    durable,
                    None,
                    reason="turn window not fully read (message paging limit)",
                )
            if _window_has_terminal_assistant_error(window):
                return ReconcileResult(
                    admission_id,
                    "unknown",
                    durable,
                    None,
                    reason=(
                        "assistant reported an error/refusal and no authoritative idle "
                        "exists for this turn (errored turn; not completion)"
                    ),
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


def _window_has_terminal_assistant_error(window: list[dict[str, Any]]) -> bool:
    """True when a turn window holds an assistant that ended in error/refusal.

    A message-level error (``finish == "error"`` or a structured ``error`` field)
    with no authoritative ``idle`` means the turn must not be assumed to still be
    running: a live server persists such an assistant, with no terminal ``idle``,
    when a turn is torn down (for example by a service restart). Used by
    :meth:`OpenCodeV2Client._reconcile_turn` to report ``unknown`` rather than
    ``in_progress``.
    """
    for message in window:
        if message.get("type") != "assistant":
            continue
        if message.get("finish") == "error" or message.get("error"):
            return True
    return False
