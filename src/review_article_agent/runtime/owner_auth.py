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
* It is **not** proof of a **biological human**, and it is **not** a native
  sandbox. What it proves is narrow: *the process runs with the owner's effective
  UID (equal to its real UID), has a controlling terminal, and that terminal
  produced the per-operation challenge*. It does **not** assert the terminal
  device's ownership: ``/dev/tty`` resolves to the controlling terminal whose
  node is typically root-owned (a multiplexor), so a device-owner check would be
  neither reliable nor a proof of the typing user. A same-UID script could type
  the challenge. It is a much stronger, explicit boundary than an in-band
  boolean, but it is not a biometrics claim.
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
import types
import unicodedata
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from ..domain.errors import ReviewAgentError
from ..domain.records import (
    RecordValidationError,
    canonical_json,
    validate_identifier,
    validate_ref,
)

__all__ = [
    "OwnerAuthError",
    "OwnerAuthConfigError",
    "OwnerActionError",
    "OwnerAuthTtyUnavailable",
    "OwnerIdentityError",
    "OwnerChallengeError",
    "InvalidCapabilityError",
    "CrossProcessCapabilityError",
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
    "MAX_OWNER_APPROVAL_TTL_SECONDS",
    "MAX_OWNER_CONFIRM_TIMEOUT_SECONDS",
    "MAX_OWNER_CONFIRM_ATTEMPTS",
    "MAX_ACTION_IDENTIFIER_CHARS",
    "MAX_RATIONALE_CHARS",
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

#: Hard upper bounds on the confirmation knobs. A caller-supplied value above a
#: bound (or a non-finite / non-positive one) is refused **before** any float
#: conversion, with a clean typed :class:`OwnerAuthConfigError`; a value such as
#: ``10**1000`` never reaches ``math.isfinite`` (which would raise
#: ``OverflowError``).
MAX_OWNER_APPROVAL_TTL_SECONDS = 3600.0
MAX_OWNER_CONFIRM_TIMEOUT_SECONDS = 1800.0
MAX_OWNER_CONFIRM_ATTEMPTS = 10

#: Hard bound on the number of bytes accepted for one typed challenge line.
MAX_CHALLENGE_LINE_CHARS = 64

#: Hard bounds on caller-supplied action text, so the rendered prompt and the
#: digest are bounded and cannot be used to hang the terminal write.
MAX_ACTION_IDENTIFIER_CHARS = 512
MAX_RATIONALE_CHARS = 8192

#: Hard bound on the rendered prompt bytes, **enforced** on the actual rendered
#: (escaped) text before any write. The field bounds above keep it below this.
MAX_PROMPT_BYTES = 65536

#: Bounded terminal write chunk. Writes are non-blocking and looped under the
#: write deadline, so a full terminal output queue cannot block the prompt.
WRITE_CHUNK_BYTES = 1024

#: The controlling terminal path. This is opened directly and is **not** a
#: parameter: the boundary must never fall back to standard input.
_CONTROLLING_TTY_PATH = "/dev/tty"

#: Challenge alphabet (32 symbols, 5 bits each) and shape. Four groups of four
#: characters give 16 characters over 32 symbols = **80 bits** of entropy.
_CHALLENGE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CHALLENGE_GROUP_LENGTH = 4
_CHALLENGE_GROUPS = 4

#: Matched with :meth:`re.Pattern.fullmatch`, so a trailing newline (which ``$``
#: would allow before the end) is rejected.
_DIGEST_RE = re.compile(r"(?:sha256:)?([0-9a-f]{64})")


# --------------------------------------------------------------------------- #
# Errors (static, curated, non-reflective)
# --------------------------------------------------------------------------- #
class OwnerAuthError(ReviewAgentError):
    """Base class for every owner-authority failure.

    Every message is **static and curated**: it never echoes the challenge, the
    owner-typed text, the action rationale, or any other secret.
    """


class OwnerAuthConfigError(OwnerAuthError, ValueError):
    """A caller-supplied confirmation knob is out of range or not a number.

    Raised *before* any float conversion, so an absurd integer such as
    ``10**1000`` produces this clean typed error rather than an ``OverflowError``
    from ``math.isfinite``. The message states only the bound, never caller text.
    """


class OwnerActionError(OwnerAuthError, ValueError):
    """An :class:`OwnerAction` is malformed or not the expected typed value.

    Messages are **static**: they never echo the offending identifier, gate kind,
    decision, reference, digest, or rationale (no canary/secret reflection).
    """


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


class CrossProcessCapabilityError(OwnerAuthError):
    """The capability was minted in a different process.

    ``os.fork`` duplicates the capability object into the child; the capability
    is bound to the minting process (pid **and** a per-process token refreshed
    after fork), so a forked child can neither consume nor replay it.
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
def _normalize_digest(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise OwnerActionError(f"{field} must be a string.")
    match = _DIGEST_RE.fullmatch(value)
    if match is None:
        raise OwnerActionError(
            f"{field} must be a lowercase hex SHA-256 digest "
            "(optionally prefixed with 'sha256:')."
        )
    return match.group(1)


def _require_bounded_identifier(value: object, field: str) -> str:
    """Validate one action identifier and its length, with a **static** error.

    Never echoes the offending value.
    """
    try:
        identifier = validate_identifier(value, field)
    except RecordValidationError as exc:
        raise OwnerActionError(
            f"the action's {field} is not a valid identifier; it cannot be "
            "confirmed."
        ) from exc
    if len(identifier) > MAX_ACTION_IDENTIFIER_CHARS:
        raise OwnerActionError(
            f"the action's {field} exceeds the {MAX_ACTION_IDENTIFIER_CHARS}"
            "-character bound; it cannot be confirmed."
        )
    return identifier


class _FrozenAction(NamedTuple):
    """An immutable, fully-validated capture of an :class:`OwnerAction`.

    ``target`` is a :class:`types.MappingProxyType` over a private copy, so the
    captured action cannot be mutated after capture: the same bytes feed both the
    canonical digest and the on-terminal display (no time-of-check /
    time-of-use gap).
    """

    project_id: str
    target: Mapping
    snapshot_digest: str
    gate_kind: str
    gate_id: str
    decision: str
    rationale: str


def _validate_and_normalize(
    project_id: object,
    target: object,
    snapshot_digest: object,
    gate_kind: object,
    gate_id: object,
    decision: object,
    rationale: object,
) -> _FrozenAction:
    """Validate every action field and return an immutable, deep-frozen capture.

    Every failure raises a static :class:`OwnerActionError` that never echoes the
    offending input text (identifier, reference, gate kind, decision, digest, or
    rationale).
    """
    pid = _require_bounded_identifier(project_id, "project_id")
    if not isinstance(target, Mapping):
        raise OwnerActionError(
            "the action's target reference is not a reference object; it cannot "
            "be confirmed."
        )
    try:
        # ``validate_ref`` normalizes and enforces that the target's project id
        # equals the action's (a cross-project target denies the approval,
        # C02 §2.1). Its message echoes ids, so it is never propagated.
        normalized_target = validate_ref(dict(target), pid)
    except (RecordValidationError, TypeError, ValueError) as exc:
        raise OwnerActionError(
            "the action's target reference is invalid or cross-project; it "
            "cannot be confirmed."
        ) from exc
    # Bound *every* target identifier, not just the action's project_id/gate_id:
    # a 10k-character artifact/revision id must be rejected before any rendering.
    if any(
        len(normalized_target[key]) > MAX_ACTION_IDENTIFIER_CHARS
        for key in ("project_id", "artifact_id", "revision_id")
    ):
        raise OwnerActionError(
            "the action's target reference has a field that exceeds the "
            f"{MAX_ACTION_IDENTIFIER_CHARS}-character bound; it cannot be "
            "confirmed."
        )
    frozen_target = types.MappingProxyType(dict(normalized_target))
    if gate_kind not in APPROVAL_GATE_KINDS:
        raise OwnerActionError(
            "the action's gate kind is not one of the approved gate kinds; an "
            "unrecognized gate kind is refused, never coerced or echoed."
        )
    gid = _require_bounded_identifier(gate_id, "gate_id")
    if decision not in APPROVAL_DECISIONS:
        raise OwnerActionError(
            "the action's decision is not one of the approved decisions."
        )
    if not isinstance(rationale, str) or rationale == "":
        raise OwnerActionError("the action's rationale must be a non-empty string.")
    if len(rationale) > MAX_RATIONALE_CHARS:
        raise OwnerActionError(
            f"the action's rationale exceeds the {MAX_RATIONALE_CHARS}-character "
            "bound; it cannot be confirmed."
        )
    return _FrozenAction(
        project_id=pid,
        target=frozen_target,
        snapshot_digest=_normalize_digest(snapshot_digest, "snapshot_digest"),
        gate_kind=gate_kind,
        gate_id=gid,
        decision=decision,
        rationale=rationale,
    )


@dataclass(frozen=True)
class OwnerAction:
    """The immutable action an owner is asked to confirm.

    The caller (a future CLI/approval repository) supplies this *description*;
    it is **not** a claim of authority. Fields are validated once at construction
    and the ``target`` is stored as a read-only :class:`types.MappingProxyType`,
    so the action cannot be mutated after construction (assignment through
    ``action.target[...]`` raises). The canonical digest of these exact fields is
    what a capability binds to.

    Fields mirror the C02 §2.1 ``Approval`` gate identity: ``project_id`` +
    ``target`` + ``snapshot_digest`` (the dependency-snapshot digest the caller
    already computed) + ``gate_kind`` + ``gate_id`` + ``decision`` +
    ``rationale``.
    """

    project_id: str
    target: Mapping
    snapshot_digest: str
    gate_kind: str
    gate_id: str
    decision: str
    rationale: str

    def __post_init__(self) -> None:
        frozen = _validate_and_normalize(
            self.project_id,
            self.target,
            self.snapshot_digest,
            self.gate_kind,
            self.gate_id,
            self.decision,
            self.rationale,
        )
        object.__setattr__(self, "project_id", frozen.project_id)
        object.__setattr__(self, "target", frozen.target)
        object.__setattr__(self, "snapshot_digest", frozen.snapshot_digest)
        object.__setattr__(self, "gate_id", frozen.gate_id)


def _capture_action(action: object) -> _FrozenAction:
    """Re-validate and deep-freeze an action for one digest+display use.

    Re-validating on capture means a hand-tampered ``OwnerAction`` (e.g. one whose
    ``target`` was replaced via ``object.__setattr__``) fails closed here, before
    any terminal I/O or digest comparison.
    """
    if not isinstance(action, OwnerAction):
        raise OwnerActionError(
            "an owner confirmation requires a typed OwnerAction; a mapping, "
            "string, or boolean cannot describe an approvable action."
        )
    return _validate_and_normalize(
        action.project_id,
        action.target,
        action.snapshot_digest,
        action.gate_kind,
        action.gate_id,
        action.decision,
        action.rationale,
    )


def _digest_of(frozen: _FrozenAction) -> str:
    payload = {
        "project_id": frozen.project_id,
        "target": dict(frozen.target),
        "snapshot_digest": frozen.snapshot_digest,
        "gate_kind": frozen.gate_kind,
        "gate_id": frozen.gate_id,
        "decision": frozen.decision,
        "rationale": frozen.rationale,
    }
    try:
        encoded = canonical_json(payload)
    except RecordValidationError as exc:  # a mutated/odd value fails closed
        raise OwnerActionError(
            "the action is not canonically encodable, so it cannot be confirmed."
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def action_digest(action: object) -> str:
    """Return the canonical SHA-256 digest of an exact :class:`OwnerAction`.

    Only a typed :class:`OwnerAction` is accepted; a mapping, string, or boolean
    offered as an "action" cannot produce a digest (and so cannot match a
    capability). The action is re-validated and deep-frozen on each call, so a
    tampered or mutated value fails closed with a static :class:`OwnerActionError`.
    """
    return _digest_of(_capture_action(action))


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
        "_created_monotonic",
        "_expires_monotonic",
        "_mint_pid",
        "_process_token",
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
        created_monotonic: float,
        expires_monotonic: float,
        mint_pid: int,
        process_token: str,
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
        self._created_monotonic = created_monotonic
        self._expires_monotonic = expires_monotonic
        self._mint_pid = mint_pid
        self._process_token = process_token
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
        created_monotonic: float,
        expires_monotonic: float,
        mint_pid: int,
        process_token: str,
    ) -> "OwnerApproval":
        return cls(
            _MINT,
            action_digest=action_digest,
            owner_uid=owner_uid,
            created_at=created_at,
            expires_at=expires_at,
            created_monotonic=created_monotonic,
            expires_monotonic=expires_monotonic,
            mint_pid=mint_pid,
            process_token=process_token,
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


# Per-process token, refreshed in a forked child (see ``register_at_fork``). A
# capability is bound to (minting pid, this token), so ``os.fork`` — which
# duplicates the capability object into the child — cannot yield two independent
# consumptions. The token is stronger than the pid alone (it also catches pid
# reuse within the process tree).
_PROCESS_TOKEN = [uuid.uuid4().hex]


def _refresh_process_token_after_fork() -> None:  # pragma: no cover - fork hook
    _PROCESS_TOKEN[0] = uuid.uuid4().hex


if hasattr(os, "register_at_fork"):  # pragma: no cover - platform hook
    os.register_at_fork(after_in_child=_refresh_process_token_after_fork)


def _current_process_token() -> str:
    """The current process's token. A patchable seam."""
    return _PROCESS_TOKEN[0]


def _current_pid() -> int:
    """The current process id. A patchable seam."""
    return os.getpid()


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
        # Non-blocking on our own file description (a fresh open of /dev/tty is
        # a private description, so this affects only this handle). Combined with
        # the select+deadline loops below, no single write or read can block past
        # the deadline: this flag is what makes the deadline control the I/O
        # itself, not merely a preceding select.
        try:
            os.set_blocking(self._fd, False)
        except OSError:  # pragma: no cover - defensive; keep the fd usable
            pass

    def is_tty(self) -> bool:
        return os.isatty(self._fd)

    def write(self, text: str, *, deadline: float | None = None) -> None:
        """Write ``text`` in bounded non-blocking chunks until ``deadline``.

        Because the fd is non-blocking, ``os.write`` never blocks: it either
        writes a chunk or raises ``EAGAIN``, in which case we wait for
        writability under the remaining deadline. A full terminal output queue
        with no reader therefore fails closed with a curated
        :class:`OwnerAuthTtyUnavailable` at the deadline instead of hanging.
        """
        view = memoryview(text.encode("utf-8", "strict"))
        while view:
            if deadline is not None and _monotonic() >= deadline:
                raise OwnerAuthTtyUnavailable(
                    "the controlling terminal write exceeded its deadline."
                )
            try:
                written = os.write(self._fd, view[:WRITE_CHUNK_BYTES])
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    if deadline is None:
                        # No deadline: wait for writability with a bounded poll.
                        try:
                            select.select([], [self._fd], [], 1.0)
                        except (InterruptedError, OSError, ValueError):
                            pass
                        continue
                    remaining = deadline - _monotonic()
                    if remaining <= 0:
                        raise OwnerAuthTtyUnavailable(
                            "the controlling terminal write exceeded its deadline."
                        )
                    try:
                        _, writable, _ = select.select([], [self._fd], [], remaining)
                    except InterruptedError:
                        continue
                    except (OSError, ValueError) as exc2:
                        raise OwnerAuthTtyUnavailable(
                            "the controlling terminal could not be written to."
                        ) from exc2
                    if not writable:
                        raise OwnerAuthTtyUnavailable(
                            "the controlling terminal write exceeded its deadline."
                        )
                    continue
                raise OwnerAuthTtyUnavailable(
                    "the controlling terminal could not be written to."
                ) from exc
            if written <= 0:
                raise OwnerAuthTtyUnavailable(
                    "the controlling terminal could not be written to."
                )
            view = view[written:]

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
            except (OSError, ValueError):
                return None
            if not ready:
                return None
            try:
                chunk = os.read(self._fd, 1)
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    continue  # spurious readiness; re-select under deadline
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


def _format_target(target: Mapping) -> str:
    return (
        f"{_escape_for_terminal(target['project_id'])} / "
        f"{_escape_for_terminal(target['artifact_id'])} @ "
        f"{_escape_for_terminal(target['revision_id'])}"
    )


def _render_prompt(frozen: _FrozenAction, digest: str, challenge: str) -> str:
    """Render the prompt from the **captured** action (same bytes as the digest).

    No field is re-read from a live caller object, so there is no
    time-of-check/time-of-use gap between what is hashed and what is shown.
    """
    lines = [
        "OWNER APPROVAL REQUIRED (out-of-band controlling-terminal confirmation)",
        "This authenticates an owner decision for the application only; it is not",
        "proof of a biological human and not protection against a same-UID process.",
        "Action digest: " + digest,
        "Project: " + _escape_for_terminal(frozen.project_id),
        "Gate: " + _escape_for_terminal(frozen.gate_kind) + " / " + _escape_for_terminal(frozen.gate_id),
        "Decision: " + _escape_for_terminal(frozen.decision),
        "Target: " + _format_target(frozen.target),
        "Snapshot: " + _escape_for_terminal(frozen.snapshot_digest),
        "Rationale: " + _escape_for_terminal(frozen.rationale),
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


def _enforce_prompt_cap(prompt: str) -> str:
    """Refuse a rendered prompt whose **actual UTF-8 bytes** exceed the cap.

    This measures the escaped text as it will be written (so control/surrogate
    escaping that expands a field is accounted for), and fails closed with a
    static :class:`OwnerActionError` before any terminal I/O.
    """
    try:
        size = len(prompt.encode("utf-8", "strict"))
    except UnicodeError as exc:  # a lone surrogate could not be written anyway
        raise OwnerActionError(
            "the action summary cannot be encoded for display; it cannot be "
            "confirmed."
        ) from exc
    if size > MAX_PROMPT_BYTES:
        raise OwnerActionError(
            f"the rendered action summary exceeds the {MAX_PROMPT_BYTES}-byte "
            "bound; it cannot be confirmed."
        )
    return prompt


# --------------------------------------------------------------------------- #
# Bounded confirmation knobs (checked before any float conversion)
# --------------------------------------------------------------------------- #
def _bounded_seconds(value: object, *, name: str, maximum: float) -> float:
    """Return a positive finite number of seconds ``<= maximum``.

    Rejects a bool, a non-number, a non-finite float, a non-positive value, and an
    over-maximum value **before** converting to float, so an absurd integer such
    as ``10**1000`` raises a clean typed error instead of ``OverflowError`` from
    ``math.isfinite``/``float``. Never echoes caller text.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OwnerAuthConfigError(
            f"{name} must be a positive finite number no greater than {maximum:g}."
        )
    if isinstance(value, float) and not math.isfinite(value):
        raise OwnerAuthConfigError(
            f"{name} must be a positive finite number no greater than {maximum:g}."
        )
    if value <= 0 or value > maximum:
        raise OwnerAuthConfigError(
            f"{name} must be a positive finite number no greater than {maximum:g}."
        )
    return float(value)


def _bounded_attempts(value: object, *, maximum: int) -> int:
    """Return a positive integer attempt count ``<= maximum`` (never echoes text)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise OwnerAuthConfigError(
            f"max_attempts must be a positive integer no greater than {maximum}."
        )
    if value < 1 or value > maximum:
        raise OwnerAuthConfigError(
            f"max_attempts must be a positive integer no greater than {maximum}."
        )
    return value


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
    elevated/mismatched identity), presents the **captured** immutable action
    summary plus a fresh random challenge (safely escaped against
    terminal-control injection), and returns an :class:`OwnerApproval` capability
    **only** if the owner types the exact challenge within the bounded
    length/deadline/attempts.

    The action is captured (re-validated and deep-frozen) once, and that single
    capture feeds **both** the canonical digest and the on-terminal display, so a
    concurrent mutation cannot change what is shown after the digest is fixed.
    TTL, timeout, and attempts have hard upper bounds checked before any float
    conversion. Every failure raises a static typed :class:`OwnerAuthError` and
    mints nothing. This function performs no DB write and dispatches no action.
    """
    frozen = _capture_action(action)  # rejects bad/tampered actions before any I/O
    digest = _digest_of(frozen)

    ttl = _bounded_seconds(
        expires_after_seconds,
        name="expires_after_seconds",
        maximum=MAX_OWNER_APPROVAL_TTL_SECONDS,
    )
    timeout = _bounded_seconds(
        timeout_seconds,
        name="timeout_seconds",
        maximum=MAX_OWNER_CONFIRM_TIMEOUT_SECONDS,
    )
    attempts = _bounded_attempts(max_attempts, maximum=MAX_OWNER_CONFIRM_ATTEMPTS)

    owner_uid = _current_owner_uid()
    if owner_uid != _real_uid():
        raise OwnerIdentityError(
            "the process's effective identity is elevated relative to its real "
            "identity; owner confirmation refuses an elevated/mismatched owner."
        )

    challenge = _new_challenge()
    deadline = _monotonic() + timeout

    prompt = _enforce_prompt_cap(_render_prompt(frozen, digest, challenge))
    retry = _render_retry(challenge)

    terminal = _open_controlling_tty()
    try:
        if not terminal.is_tty():
            raise OwnerAuthTtyUnavailable(
                "the opened controlling terminal is not a terminal; owner "
                "confirmation fails closed rather than reading standard input."
            )

        terminal.write(prompt, deadline=deadline)
        confirmed = False
        for _ in range(attempts):
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
            terminal.write(retry, deadline=deadline)
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
    created_monotonic = _monotonic()
    expires_at = created_at + timedelta(seconds=ttl)
    return OwnerApproval._mint(
        action_digest=digest,
        owner_uid=owner_uid,
        created_at=created_at,
        expires_at=expires_at,
        created_monotonic=created_monotonic,
        expires_monotonic=created_monotonic + ttl,
        mint_pid=_current_pid(),
        process_token=_current_process_token(),
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
    capability that has crossed a process boundary (fork), a mismatched action
    digest, a changed/elevated owner identity, a replayed (already consumed)
    capability, or an expired one. The returned :class:`OwnerAuthEvidence`
    contains no challenge and no secret; the capability is consumed atomically.
    """
    if not isinstance(capability, OwnerApproval):
        raise InvalidCapabilityError(
            "only a capability minted by a successful owner confirmation can "
            "authorize an action; a flag, string, mapping, or copied object "
            "grants nothing."
        )

    # Process binding: ``os.fork`` duplicates the capability object into the
    # child, so identity is checked before anything else. Both the pid and a
    # per-process token (refreshed in a forked child) must match the mint.
    if (
        _current_pid() != capability._mint_pid
        or _current_process_token() != capability._process_token
    ):
        raise CrossProcessCapabilityError(
            "this owner confirmation was minted in a different process; a "
            "forked or otherwise foreign process cannot consume it."
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
    # Re-check at consumption that the process has not become elevated/setuid
    # since the confirmation.
    if _current_owner_uid() != _real_uid():
        raise OwnerIdentityError(
            "the process's effective identity is elevated relative to its real "
            "identity; refusing to honor the capability."
        )

    if not capability._consume_once():
        raise ApprovalAlreadyConsumed(
            "this owner confirmation was already consumed; a capability is "
            "single-use and cannot be replayed."
        )

    now = _utcnow()
    # Two independent bounds: the wall-clock window and a monotonic window. A
    # backward wall-clock adjustment cannot prolong the capability, because the
    # monotonic window is unaffected by clock rollback. (A capability is
    # process-local and non-serializable, so it cannot outlive the process to be
    # resurrected after a restart.)
    if now >= capability._expires_at or _monotonic() >= capability._expires_monotonic:
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
