"""Out-of-band owner approval via a controlling-terminal challenge.

This module is the **application-owned owner-authority boundary** required by
``docs/implementation/04-agent-runtime.md`` deliverable 5 ("App-owned human
approvals presented through CLI") and acceptance criteria 1, 2, and 4, and by
``docs/contracts/02-gates-and-runtime.md`` §1.3 ("Authority provenance
(out-of-band)") and §2 (revision-specific approvals). It is the out-of-band
check that a later, still-unwritten CLI/approval repository can point at with an
``owner_auth_ref``: the JSON approval record only *points at* this check, and
does not prove authority by itself (C02 §11.0).

What this module **is**
-----------------------

* A function, :func:`request_owner_confirmation`, that opens the **process's
  controlling terminal** (``/dev/tty``) directly and **never** reads standard
  input, an environment variable, or a JSON/CLI token. It renders an immutable,
  exactly-bound action summary and a **random per-operation challenge**, then
  requires the owner to type that exact challenge **through that terminal**.
* An opaque, process-local **capability** (:class:`OwnerApproval`) that is
  minted **only** by a successful terminal confirmation and is bound to the
  canonical immutable action digest, the owner's effective UID, and a
  creation/expiration window.
* A trusted consumption function, :func:`consume_owner_approval`, that verifies
  and consumes the capability **once**, against the matching action, and returns
  safe-to-persist :class:`OwnerAuthEvidence` (auth id, method, UID, time, action
  digest), never the challenge or any secret.

What this module **is not**
---------------------------

* It writes **no** database rows and dispatches **no** action or model/tool
  call. It only authenticates an owner decision that the caller has already
  described; recording and execution belong to a later task.
* It is **not** protection against a malicious **same-UID native process**, a
  **root** process, or any attacker who can already run code as the owner. The
  security claim is deliberately restricted to the **application-model path**:
  within the app, the restricted scientific roles have **all tools denied**, so
  a model/tool/document/request payload cannot reach this boundary or mint a
  capability; it cannot forge authority in-band.
* It is **not** proof of a **biological human**. It proves that *a controlling
  terminal owned by the owner's effective UID produced the per-operation
  challenge*; a same-UID script could type it. It is a much stronger, explicit
  boundary than an in-band boolean, but it is not a biometrics claim.
* It does **not** accept, and cannot be granted authority by, a plain boolean
  "authenticated" flag, an actor label, a string, or any imported data record.
  No public constructor or data import can mint an :class:`OwnerApproval`.

Authority provenance rules honoured here (C02 §1.3)
---------------------------------------------------

* Authority is established **outside** the model/tool data path. Nothing a
  model, tool, retrieved document, or request payload can author enters this
  module: the only inputs are an application-supplied :class:`OwnerAction`
  (an action *description*, not a claim of authority) and bytes typed on the
  controlling terminal.
* A capability is minted **only** by :func:`request_owner_confirmation`; its
  constructor requires a private sentinel that is not part of the public API.
* A capability is **single-use** and **action-bound**; a mismatched, expired,
  replayed, or forged value fails closed with a static typed error.

LLM-annotated test file companion: ``tests/test_owner_auth.py``.
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import math
import os
import re
import select
import secrets
import threading
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..domain.errors import ReviewAgentError
from ..domain.records import (
    RecordValidationError,
    canonical_json,
    validate_identifier,
    validate_ref,
)

__all__ = [
    "OwnerAuthError",
    "OwnerActionError",
    "OwnerAuthTtyUnavailable",
    "OwnerIdentityError",
    "OwnerChallengeError",
    "InvalidCapabilityError",
    "ActionDigestMismatch",
    "ApprovalExpired",
    "ApprovalAlreadyConsumed",
    "OwnerUidMismatch",
    "OwnerAction",
    "OwnerApproval",
    "OwnerAuthEvidence",
    "action_digest",
    "request_owner_confirmation",
    "consume_owner_approval",
    "APPROVAL_GATE_KINDS",
    "APPROVAL_DECISIONS",
    "OWNER_APPROVAL_METHOD",
    "OWNER_APPROVAL_TTL_SECONDS",
    "OWNER_CONFIRM_TIMEOUT_SECONDS",
    "OWNER_CONFIRM_MAX_ATTEMPTS",
]

# --------------------------------------------------------------------------- #
# Vocabulary (C02 §2.1)
# --------------------------------------------------------------------------- #
#: The exact ``gate_kind`` enum (C02 §2.1). An unrecognized value is invalid and
#: is rejected, never coerced.
APPROVAL_GATE_KINDS = (
    "protocol",
    "pilot",
    "outline",
    "framework",
    "exclusion_set",
    "artifact_revision",
    "batch",
    "final_release",
)

#: The exact human ``decision`` enum (C02 §2.1).
APPROVAL_DECISIONS = ("approved", "rejected")

#: The persisted owner-auth method label; this is a *pointer* value, not proof.
OWNER_APPROVAL_METHOD = "controlling_tty_challenge"

#: Default capability lifetime after a successful confirmation.
OWNER_APPROVAL_TTL_SECONDS = 300.0

#: Default total deadline for the owner to complete the challenge.
OWNER_CONFIRM_TIMEOUT_SECONDS = 120.0

#: Default number of challenge attempts before the confirmation fails closed.
OWNER_CONFIRM_MAX_ATTEMPTS = 3

#: Hard bound on the number of bytes accepted for one typed challenge line.
MAX_CHALLENGE_LINE_CHARS = 64

#: The controlling terminal path. This is opened directly and is **not** a
#: parameter: the boundary must never fall back to standard input.
_CONTROLLING_TTY_PATH = "/dev/tty"

_CHALLENGE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CHALLENGE_GROUP_LENGTH = 4
_CHALLENGE_GROUPS = 3

_DIGEST_RE = re.compile(r"^(?:sha256:)?([0-9a-f]{64})$")


# --------------------------------------------------------------------------- #
# Errors (static, curated, non-reflective)
# --------------------------------------------------------------------------- #
class OwnerAuthError(ReviewAgentError):
    """Base class for every owner-authority failure.

    Every message is **static and curated**: it never echoes the challenge, the
    owner-typed text, the action rationale, or any other secret.
    """


class OwnerActionError(OwnerAuthError, ValueError):
    """An :class:`OwnerAction` is malformed or not the expected typed value."""


class OwnerAuthTtyUnavailable(OwnerAuthError):
    """No usable controlling terminal is available (absent/piped/non-tty).

    Raised when ``/dev/tty`` cannot be opened (e.g. the process has no
    controlling terminal because stdin/stdout are pipes) or when what was opened
    is not a terminal. The boundary fails closed: it never falls back to
    standard input.
    """


class OwnerIdentityError(OwnerAuthError):
    """The effective identity is elevated/mismatched, or the terminal is foreign.

    Raised when the process's effective UID differs from its real UID (a
    setuid/elevated identity mismatch) or when the controlling terminal's device
    is owned by neither the owner's effective UID nor root.
    """


class OwnerChallengeError(OwnerAuthError):
    """The owner did not type the exact challenge within the bound window.

    Covers a wrong challenge, a line exceeding the length bound, and an
    exhausted deadline/attempt count. The message never contains the typed text
    or the expected challenge.
    """


class InvalidCapabilityError(OwnerAuthError):
    """The value offered is not an owner-approval capability minted here.

    A boolean, string, mapping, ``None``, copied, or forged object never grants
    authority and fails closed.
    """


class ActionDigestMismatch(OwnerAuthError):
    """The offered action does not match the one the capability was minted for."""


class ApprovalExpired(OwnerAuthError):
    """The capability's confirmation window has elapsed."""


class ApprovalAlreadyConsumed(OwnerAuthError):
    """The capability was already consumed; it is single-use (anti-replay)."""


class OwnerUidMismatch(OwnerAuthError):
    """The effective UID at consumption differs from the confirmed owner UID."""


# --------------------------------------------------------------------------- #
# The immutable action description bound by the caller
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class OwnerAction:
    """The immutable action an owner is asked to confirm.

    The caller (a future CLI/approval repository) supplies this *description*;
    it is **not** a claim of authority. The canonical digest of these exact
    fields is what a capability binds to, so any later change to any field — or
    to the mutable ``target`` mapping — is detected as a mismatch and fails
    closed.

    Fields mirror the C02 §2.1 ``Approval`` gate identity: ``project_id`` +
    ``target`` + ``snapshot_digest`` (the dependency-snapshot digest the caller
    already computed) + ``gate_kind`` + ``gate_id`` + ``decision`` +
    ``rationale``.
    """

    project_id: str
    target: dict
    snapshot_digest: str
    gate_kind: str
    gate_id: str
    decision: str
    rationale: str

    def __post_init__(self) -> None:
        try:
            project_id = validate_identifier(self.project_id, "project_id")
            # ``validate_ref`` both normalizes and enforces that the target's
            # ``project_id`` equals ``project_id`` (a cross-project target denies
            # the approval, C02 §2.1).
            target = validate_ref(self.target, project_id)
            gate_id = validate_identifier(self.gate_id, "gate_id")
        except RecordValidationError as exc:
            raise OwnerActionError(
                f"the action description is invalid ({exc}); it cannot be "
                "confirmed."
            ) from exc
        gate_kind = self.gate_kind
        if gate_kind not in APPROVAL_GATE_KINDS:
            raise OwnerActionError(
                f"gate_kind {gate_kind!r} is not one of the approved gate kinds; "
                "an unrecognized gate kind is refused, never coerced."
            )
        decision = self.decision
        if decision not in APPROVAL_DECISIONS:
            raise OwnerActionError(
                f"decision {decision!r} is not one of {APPROVAL_DECISIONS}."
            )
        rationale = self.rationale
        if not isinstance(rationale, str) or rationale == "":
            raise OwnerActionError("rationale must be a non-empty string.")
        snapshot_digest = _normalize_digest(self.snapshot_digest, "snapshot_digest")

        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "snapshot_digest", snapshot_digest)
        object.__setattr__(self, "gate_id", gate_id)


def _normalize_digest(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise OwnerActionError(f"{field} must be a string.")
    match = _DIGEST_RE.match(value)
    if match is None:
        raise OwnerActionError(
            f"{field} must be a lowercase hex SHA-256 digest "
            "(optionally prefixed with 'sha256:')."
        )
    return match.group(1)


def action_digest(action: OwnerAction) -> str:
    """Return the canonical SHA-256 digest of an exact :class:`OwnerAction`.

    Only a typed :class:`OwnerAction` is accepted; a mapping, string, or boolean
    offered as an "action" cannot produce a digest (and so cannot match a
    capability). The digest is recomputed from the current field values on each
    call, so a mutated ``target`` mapping yields a different digest.
    """
    if not isinstance(action, OwnerAction):
        raise OwnerActionError(
            "an owner confirmation requires a typed OwnerAction; a mapping, "
            "string, or boolean cannot describe an approvable action."
        )
    payload = {
        "project_id": action.project_id,
        "target": dict(action.target),
        "snapshot_digest": action.snapshot_digest,
        "gate_kind": action.gate_kind,
        "gate_id": action.gate_id,
        "decision": action.decision,
        "rationale": action.rationale,
    }
    try:
        encoded = canonical_json(payload)
    except RecordValidationError as exc:  # a mutated/odd value fails closed
        raise OwnerActionError(
            "the action is not canonically encodable, so it cannot be confirmed."
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


# --------------------------------------------------------------------------- #
# The opaque, single-use capability
# --------------------------------------------------------------------------- #
class _MintGuard:
    """Private sentinel type for :class:`OwnerApproval` construction."""


#: Private sentinel. Reaching it requires importing a private, underscore-prefixed
#: module attribute — deliberately outside both the public API and the
#: application-model threat model. It is **not** a security boundary against a
#: same-UID native process (see the module docstring).
_MINT = _MintGuard()


class OwnerApproval:
    """An opaque, process-local, single-use owner-confirmation capability.

    Properties:

    * **Minted only here.** Its constructor demands the private :data:`_MINT`
      sentinel, so no public boolean/string/actor constructor and no imported
      data record can create one. Only :func:`request_owner_confirmation`
      mints it, and only after a successful terminal challenge.
    * **Opaque.** It has ``__slots__`` (no ``__dict__``), no public fields, and
      no meaningful ``repr``. The bound values (action digest, UID, window) are
      private attributes.
    * **Non-serializable.** Pickling, copying, and deep-copying raise, so a
      capability cannot be replayed from bytes or a cloned reference outside this
      process.
    * **Single-use and action-bound.** Verification consumes it exactly once and
      only against the matching action digest (see
      :func:`consume_owner_approval`).
    """

    __slots__ = (
        "_action_digest",
        "_owner_uid",
        "_created_at",
        "_expires_at",
        "_consumed",
        "_lock",
    )

    def __init__(
        self,
        _guard: object,
        *,
        action_digest: str,
        owner_uid: int,
        created_at: datetime,
        expires_at: datetime,
    ) -> None:
        if _guard is not _MINT:
            raise OwnerAuthError(
                "owner approval capabilities are minted only by a successful "
                "out-of-band controlling-terminal confirmation; they cannot be "
                "constructed from a flag, string, actor, or imported record."
            )
        self._action_digest = action_digest
        self._owner_uid = owner_uid
        self._created_at = created_at
        self._expires_at = expires_at
        self._consumed = False
        self._lock = threading.Lock()

    # -- no public surface ------------------------------------------------- #
    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "<OwnerApproval opaque owner-confirmation capability>"

    @classmethod
    def _mint(
        cls,
        *,
        action_digest: str,
        owner_uid: int,
        created_at: datetime,
        expires_at: datetime,
    ) -> "OwnerApproval":
        return cls(
            _MINT,
            action_digest=action_digest,
            owner_uid=owner_uid,
            created_at=created_at,
            expires_at=expires_at,
        )

    # -- non-serializable / non-copyable ----------------------------------- #
    def __reduce__(self):
        raise TypeError(
            "OwnerApproval is a process-local capability and cannot be serialized."
        )

    def __reduce_ex__(self, protocol):
        raise TypeError(
            "OwnerApproval is a process-local capability and cannot be serialized."
        )

    def __copy__(self):
        raise TypeError("OwnerApproval cannot be copied.")

    def __deepcopy__(self, memo):
        raise TypeError("OwnerApproval cannot be copied.")

    def _consume_once(self) -> bool:
        """Atomically flip the single-use flag; ``True`` iff this call consumed it."""
        with self._lock:
            if self._consumed:
                return False
            self._consumed = True
            return True


# --------------------------------------------------------------------------- #
# Evidence returned by a trusted consumption
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class OwnerAuthEvidence:
    """Safe-to-persist evidence of a successful owner confirmation.

    A later approval repository may persist these fields and reference them with
    an ``owner_auth_ref``. It contains **no** challenge and no secret, and is
    itself only a **pointer** to the out-of-band check — a record that merely
    carries these fields does not by itself prove authority (C02 §1.3 / §11.0).
    """

    auth_id: str
    method: str
    owner_uid: int
    verified_at: datetime
    action_digest: str

    def as_record(self) -> dict:
        """Return a JSON-safe projection for durable storage."""
        return {
            "auth_id": self.auth_id,
            "method": self.method,
            "owner_uid": self.owner_uid,
            "verified_at": self.verified_at.astimezone(timezone.utc).isoformat(),
            "action_digest": self.action_digest,
        }


# --------------------------------------------------------------------------- #
# Low-level, patchable seams (tests mock only these terminal operations)
# --------------------------------------------------------------------------- #
def _utcnow() -> datetime:
    """Current time, UTC. A patchable seam (not a production parameter)."""
    return datetime.now(timezone.utc)


def _monotonic() -> float:
    """Monotonic clock for the confirmation deadline. A patchable seam."""
    import time

    return time.monotonic()


def _current_owner_uid() -> int:
    """The owner's effective UID. A patchable seam."""
    return os.geteuid()


def _real_uid() -> int:
    """The owner's real UID (for the elevated-identity mismatch check)."""
    return os.getuid()


def _new_challenge() -> str:
    """Generate a fresh random per-operation challenge. A patchable seam."""
    groups = [
        "".join(secrets.choice(_CHALLENGE_ALPHABET) for _ in range(_CHALLENGE_GROUP_LENGTH))
        for _ in range(_CHALLENGE_GROUPS)
    ]
    return "-".join(groups)


class _RealTty:
    """A controlling-terminal handle backed by a real file descriptor."""

    __slots__ = ("_fd",)

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def is_tty(self) -> bool:
        return os.isatty(self._fd)

    def device_owner_uid(self) -> int:
        return os.stat(os.ttyname(self._fd)).st_uid

    def write(self, text: str) -> None:
        data = text.encode("utf-8", "strict")
        while data:
            try:
                written = os.write(self._fd, data)
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                raise OwnerAuthTtyUnavailable(
                    "the controlling terminal could not be written to."
                ) from exc
            if written <= 0:
                raise OwnerAuthTtyUnavailable(
                    "the controlling terminal could not be written to."
                )
            data = data[written:]

    def read_line(self, *, max_chars: int, deadline: float):
        """Read one challenge line, or ``None`` on EOF/timeout/over-length."""
        buffer = bytearray()
        while True:
            remaining = deadline - _monotonic()
            if remaining <= 0:
                return None
            try:
                ready, _, _ = select.select([self._fd], [], [], remaining)
            except InterruptedError:
                continue
            except OSError:
                return None
            if not ready:
                return None
            try:
                chunk = os.read(self._fd, 1)
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                return None
            if not chunk:
                return None
            if chunk in (b"\n",):
                break
            if chunk == b"\r":
                continue
            buffer += chunk
            if len(buffer) > max_chars:
                return None
        try:
            return buffer.decode("utf-8", "strict")
        except UnicodeDecodeError:
            return None

    def close(self) -> None:
        try:
            os.close(self._fd)
        except OSError:  # pragma: no cover - defensive
            pass


def _open_controlling_tty():
    """Open the process's controlling terminal (``/dev/tty``), read/write.

    This is **not** parameterized: the owner-authority boundary must never read
    standard input. A missing/absent controlling terminal, or anything that is
    not a terminal, fails closed with :class:`OwnerAuthTtyUnavailable`. The
    function is a module-level seam so tests can substitute a fake terminal.
    """
    try:
        fd = os.open(_CONTROLLING_TTY_PATH, os.O_RDWR | os.O_NOCTTY)
    except OSError as exc:
        raise OwnerAuthTtyUnavailable(
            "no controlling terminal is available for owner confirmation "
            "(owner confirmation opens /dev/tty and never reads standard input)."
        ) from exc
    return _RealTty(fd)


# --------------------------------------------------------------------------- #
# Terminal-safe rendering (no terminal-control injection)
# --------------------------------------------------------------------------- #
def _escape_for_terminal(text: str) -> str:
    """Escape control/format characters so a value cannot drive the terminal.

    Every C0/C1 control character, DEL, surrogate, private-use, and Unicode
    format character (including bidi overrides) is replaced with a visible
    ``\\xNN``/``\\uNNNN`` escape, and newlines become ``\\x0a``. The result is a
    single line with no escape sequences, so no field value can move the cursor,
    clear the screen, or inject a fake prompt/challenge.
    """
    out = []
    for ch in text:
        code = ord(ch)
        if (
            code < 0x20
            or code == 0x7F
            or 0x80 <= code <= 0x9F
            or unicodedata.category(ch) in ("Cs", "Co", "Cf")
        ):
            if code < 0x100:
                out.append(f"\\x{code:02x}")
            else:
                out.append(f"\\u{code:04x}")
        else:
            out.append(ch)
    return "".join(out)


def _format_target(target: dict) -> str:
    return (
        f"{_escape_for_terminal(target['project_id'])} / "
        f"{_escape_for_terminal(target['artifact_id'])} @ "
        f"{_escape_for_terminal(target['revision_id'])}"
    )


def _render_prompt(action: OwnerAction, digest: str, challenge: str) -> str:
    lines = [
        "OWNER APPROVAL REQUIRED (out-of-band controlling-terminal confirmation)",
        "This authenticates an owner decision for the application only; it is not",
        "proof of a biological human and not protection against a same-UID process.",
        "Action digest: " + digest,
        "Project: " + _escape_for_terminal(action.project_id),
        "Gate: " + _escape_for_terminal(action.gate_kind) + " / " + _escape_for_terminal(action.gate_id),
        "Decision: " + _escape_for_terminal(action.decision),
        "Target: " + _format_target(action.target),
        "Snapshot: " + _escape_for_terminal(action.snapshot_digest),
        "Rationale: " + _escape_for_terminal(action.rationale),
        "---",
        "Type the challenge below exactly (case-sensitive), then press Enter.",
        "Challenge: " + challenge,
        "",
    ]
    return "\n".join(lines)


def _render_retry(challenge: str) -> str:
    return (
        "Challenge did not match. Type the challenge exactly and press Enter.\n"
        "Challenge: " + challenge + "\n"
    )


# --------------------------------------------------------------------------- #
# Minting: the only path to authority
# --------------------------------------------------------------------------- #
def request_owner_confirmation(
    action: OwnerAction,
    *,
    expires_after_seconds: float = OWNER_APPROVAL_TTL_SECONDS,
    timeout_seconds: float = OWNER_CONFIRM_TIMEOUT_SECONDS,
    max_attempts: int = OWNER_CONFIRM_MAX_ATTEMPTS,
) -> OwnerApproval:
    """Confirm an exact action on the owner's controlling terminal.

    Opens ``/dev/tty`` directly (never stdin; no ``--yes``/env/JSON token),
    verifies the controlling terminal and the owner's effective UID (refusing an
    elevated/mismatched identity and a foreign terminal), presents the immutable
    action summary plus a fresh random challenge (safely escaped against
    terminal-control injection), and returns an :class:`OwnerApproval` capability
    **only** if the owner types the exact challenge within the bounded
    length/deadline/attempts.

    Every failure raises a static typed :class:`OwnerAuthError` and mints
    nothing. This function performs no DB write and dispatches no action.
    """
    digest = action_digest(action)  # rejects a non-OwnerAction before any I/O

    if (
        isinstance(expires_after_seconds, bool)
        or not isinstance(expires_after_seconds, (int, float))
        or not math.isfinite(expires_after_seconds)
        or expires_after_seconds <= 0
    ):
        raise ValueError("expires_after_seconds must be a positive finite number.")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be a positive finite number.")
    if (
        isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or max_attempts < 1
    ):
        raise ValueError("max_attempts must be a positive integer.")

    owner_uid = _current_owner_uid()
    if owner_uid != _real_uid():
        raise OwnerIdentityError(
            "the process's effective identity is elevated relative to its real "
            "identity; owner confirmation refuses an elevated/mismatched owner."
        )

    challenge = _new_challenge()
    deadline = _monotonic() + float(timeout_seconds)

    terminal = _open_controlling_tty()
    try:
        if not terminal.is_tty():
            raise OwnerAuthTtyUnavailable(
                "the opened controlling terminal is not a terminal; owner "
                "confirmation fails closed rather than reading standard input."
            )
        device_owner = terminal.device_owner_uid()
        if device_owner not in (owner_uid, 0):
            raise OwnerIdentityError(
                "the controlling terminal is owned by neither the owner's "
                "effective UID nor root; refusing an unverified terminal."
            )

        terminal.write(_render_prompt(action, digest, challenge))
        confirmed = False
        for _ in range(max_attempts):
            typed = terminal.read_line(
                max_chars=MAX_CHALLENGE_LINE_CHARS, deadline=deadline
            )
            if typed is None:
                break
            if len(typed) > MAX_CHALLENGE_LINE_CHARS:
                # Defense in depth: never rely solely on the reader honoring the
                # bound. An over-length line is refused, not compared.
                break
            if hmac.compare_digest(
                typed.encode("utf-8"), challenge.encode("ascii")
            ):
                confirmed = True
                break
            terminal.write(_render_retry(challenge))
        if not confirmed:
            raise OwnerChallengeError(
                "the exact owner challenge was not entered within the allowed "
                "attempts and deadline; no approval was created."
            )
    finally:
        try:
            terminal.close()
        except Exception:  # pragma: no cover - defensive; never mask the failure
            pass

    created_at = _utcnow()
    expires_at = created_at + timedelta(seconds=float(expires_after_seconds))
    return OwnerApproval._mint(
        action_digest=digest,
        owner_uid=owner_uid,
        created_at=created_at,
        expires_at=expires_at,
    )


# --------------------------------------------------------------------------- #
# Trusted consumption
# --------------------------------------------------------------------------- #
def consume_owner_approval(
    capability: object,
    action: OwnerAction,
) -> OwnerAuthEvidence:
    """Verify and consume a capability once, returning safe evidence.

    Fails closed with a static typed error for a forged/absent capability, a
    mismatched action digest, a changed owner UID, a replayed (already consumed)
    capability, or an expired one. The returned :class:`OwnerAuthEvidence`
    contains no challenge and no secret; the capability is consumed atomically.
    """
    if not isinstance(capability, OwnerApproval):
        raise InvalidCapabilityError(
            "only a capability minted by a successful owner confirmation can "
            "authorize an action; a flag, string, mapping, or copied object "
            "grants nothing."
        )

    digest = action_digest(action)
    if not hmac.compare_digest(digest, capability._action_digest):
        raise ActionDigestMismatch(
            "the offered action does not match the confirmed action digest; "
            "authority is bound to one immutable action."
        )

    if _current_owner_uid() != capability._owner_uid:
        raise OwnerUidMismatch(
            "the effective owner identity no longer matches the confirmed owner "
            "UID; refusing to honor the capability."
        )

    if not capability._consume_once():
        raise ApprovalAlreadyConsumed(
            "this owner confirmation was already consumed; a capability is "
            "single-use and cannot be replayed."
        )

    now = _utcnow()
    if now >= capability._expires_at:
        raise ApprovalExpired(
            "this owner confirmation has expired; request a fresh one."
        )

    return OwnerAuthEvidence(
        auth_id="auth_" + uuid.uuid4().hex,
        method=OWNER_APPROVAL_METHOD,
        owner_uid=capability._owner_uid,
        verified_at=now,
        action_digest=capability._action_digest,
    )
