"""Minimal Python HTTP lifecycle client for the official OpenCode V2 API.

Task C-python spike. Scope: ``spikes/runtime/python`` only.

Design constraints (see ``docs/implementation/00-runtime-spike.md`` lines 5, 9,
15-18, 20 and ``docs/specification.md`` "Product and runtime"):

* Python standard library only (``urllib``), no third-party HTTP client.
* Explicit, authenticated loopback endpoint. The base URL and bearer token are
  passed in by the caller; there is **no** shared-service discovery or start and
  **no** reading of private databases, registration files, or credentials.
  ``from_env`` is the only convenience and it refuses to guess a default.
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
  second client recovers after missed events. It distinguishes the durable
  receipt from the completion (idle/assistant message).
* ``wait`` is a bounded wait for the agent loop to become idle, so the CLI is
  never required to stay open for the task to finish.
"""

from __future__ import annotations

import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

# --- exact patterns from the OpenAPI schemas ---------------------------------
SESSION_ID_RE = re.compile(r"^ses")
MESSAGE_ID_RE = re.compile(r"^msg_")

# Response tags from the published error schemas (*ErrorEncoded).
CONFLICT_TAG = "ConflictError"
UNAUTHORIZED_TAG = "UnauthorizedError"


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
    """State reconstructed from durable server state."""

    admission_id: str
    state: str  # "admitted" | "delivered" | "completed" | "unknown"
    durable_receipt: dict[str, Any] | None
    completion: dict[str, Any] | None

    @property
    def is_durable(self) -> bool:
        return self.state in ("admitted", "delivered", "completed")

    @property
    def is_complete(self) -> bool:
        return self.state == "completed"


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

    :param base_url: explicit endpoint, e.g. ``http://127.0.0.1:4096``. No
        discovery is performed.
    :param token: explicit bearer token (sent as ``Authorization: Bearer``).
        Optional because a disposable loopback server may run unauthenticated.
    :param directory: absolute location directory pinned on session create.
    """

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        *,
        timeout: float = 30.0,
        directory: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ):
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url must be an explicit, non-empty URL")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = float(timeout)
        self.directory = directory
        self.extra_headers = dict(extra_headers or {})

    # -- construction helpers ------------------------------------------------
    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "OpenCodeV2Client":
        """Build a client from explicit environment variables only.

        Required: ``OPENCODE_SPIKE_BASE_URL``. Optional: ``OPENCODE_SPIKE_TOKEN``,
        ``OPENCODE_SPIKE_DIRECTORY``. Refuses to fall back to any shared-service
        discovery or default port.
        """
        env = os.environ if env is None else env
        base_url = env.get("OPENCODE_SPIKE_BASE_URL")
        if not base_url:
            raise ValueError(
                "OPENCODE_SPIKE_BASE_URL is required; no default endpoint is used"
            )
        return cls(
            base_url,
            token=env.get("OPENCODE_SPIKE_TOKEN"),
            directory=env.get("OPENCODE_SPIKE_DIRECTORY"),
        )

    # -- transport -----------------------------------------------------------
    def _headers(self, has_body: bool) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if has_body:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        headers.update(self.extra_headers)
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
            with urllib.request.urlopen(
                request, timeout=self.timeout if timeout is None else timeout
            ) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
            finally:
                exc.close()
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

    def reconcile(self, session_id: str, admission_id: str) -> ReconcileResult:
        """Reconstruct admission/completion state from durable server state.

        The durable receipt (an inbox item or a persisted user message) proves the
        input was accepted. A separate completion (idle or completed assistant
        message) proves the turn ended. ``unknown`` means the server holds no
        trace of the ID — it is never reported as complete.
        """
        self._require_message_id(admission_id)
        inbox = self.list_inbox(session_id)
        receipt = next((i for i in inbox if i.get("id") == admission_id), None)

        messages = self.list_messages(session_id)["data"]
        user_message = next(
            (
                m
                for m in messages
                if m.get("type") == "user" and m.get("id") == admission_id
            ),
            None,
        )
        durable = receipt if receipt is not None else user_message

        completion: dict[str, Any] | None = None
        idle = next((m for m in messages if m.get("type") == "idle"), None)
        assistant = next(
            (
                m
                for m in messages
                if m.get("type") == "assistant"
                and m.get("time", {}).get("completed") is not None
            ),
            None,
        )
        if idle is not None:
            completion = {"outcome": idle.get("outcome"), "message": idle}
        elif assistant is not None:
            outcome = "failed" if assistant.get("error") else "succeeded"
            completion = {"outcome": outcome, "message": assistant}

        if durable is None:
            state = "unknown"
        elif completion is not None:
            state = "completed"
        elif receipt is not None:
            state = "admitted"
        else:
            state = "delivered"
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

        Stateless: nothing is stored. Per-question failures come back as
        ``{"type": "error", ...}`` answers while the other answers still return;
        that is *not* a scientific exclusion.
        """
        validate_model_ref(model)
        if not input_parts:
            raise ValueError("input_parts must contain at least one part")
        if not questions:
            raise ValueError("questions must contain at least one question")
        names = [q.get("name") for q in questions]
        if len(set(names)) != len(names):
            raise ValueError("question names must be unique")
        for question in questions:
            if question.get("type") not in ("predicate", "choice", "score"):
                raise ValueError(f"unknown question type: {question.get('type')!r}")
        body = {"model": model, "input": input_parts, "questions": questions}
        return self._request(
            "POST", "/api/experimental/generate/decisions", body=body
        )["data"]

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
_ANSWER_REQUIRED = {
    "predicate": ("name", "probability"),
    "choice": ("name", "choice", "probabilities", "confidence"),
    "score": ("name", "score", "probabilities", "confidence"),
    "error": ("name", "reason"),
}


def validate_decision_result(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate a ``Generate.DecisionResult`` answer list (fail closed).

    Returns the per-question error answers (which are valid shapes). Raises
    ``ValueError`` only for malformed evidence, so a refusal is distinguished
    from a client/protocol bug.
    """
    answers = result.get("answers")
    if not isinstance(answers, list):
        raise ValueError("decision result must have an 'answers' list")
    errors: list[dict[str, Any]] = []
    for index, answer in enumerate(answers):
        if not isinstance(answer, dict):
            raise ValueError(f"answer {index} is not an object")
        kind = answer.get("type")
        required = _ANSWER_REQUIRED.get(kind)
        if required is None:
            raise ValueError(f"answer {index} has unknown type {kind!r}")
        missing = [key for key in required if key not in answer]
        if missing:
            raise ValueError(f"answer {index} ({kind}) missing {missing}")
        if kind == "predicate":
            probability = answer["probability"]
            if not isinstance(probability, (int, float)) or not 0 <= probability <= 1:
                raise ValueError(f"answer {index} probability out of [0,1]")
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
