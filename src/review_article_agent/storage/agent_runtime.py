"""Durable, immutable per-project runtime task/session/proposal mappings.

This module is the Step 04 storage-side core that the later worker facade builds
on. It sits *on top of* an already-verified
:class:`~review_article_agent.storage.store.ProjectStore` (the caller resolves
registry paths with the path-repair layer and passes a live store in) and reuses
the pure role/task/proposal boundary in
:mod:`review_article_agent.runtime.roles`. It performs **no** model, network,
dispatch, approval, or budget work.

What it guarantees
------------------

* **Per-project store identity, never a caller project.** Every row is written
  with ``store.project_id`` (``runtime/*`` migrations 5); no method accepts a
  caller-supplied ``project_id``, so a mapping/task/proposal can never be
  recorded into another project (:meth:`RuntimeRepository.project_id`).
* **Immutable rows + always-on integrity revalidation.** ``schema`` migration 5
  pins every runtime row immutable at the database (never ``UPDATE``d/``DELETE``d),
  and — critically — **every read, replay, and write path structurally
  revalidates** the row it returns: the recomputed SHA-256 of the exact canonical
  bytes, the re-parsed configuration/task/proposal identity, the projected
  columns, and every linked child row. A corrupt canonical blob, a tampered
  projection, a broken child-input ordinal, or a re-derived provenance mismatch
  fails closed with a typed :class:`RuntimeCorruptionError`; it is never handed to
  a caller (and therefore never to dispatch) unchecked. The same validation is
  exported (:func:`store_integrity_problems`) so a later backup verifier can reuse
  it instead of duplicating the logic.
* **Exact, idempotent bounded tasks keyed by ``request_id``.** A repeated request
  key with the *same* task returns the historical row; a *divergent* task under
  the same key raises :class:`RuntimeConflictError`. The full trusted
  ``BoundedTask`` fingerprint (role config, exact target, input pins + digests,
  scope, prompt version, size/step limits) is persisted as canonical bytes.
* **Fail-closed reference verification on write.** Recording a **new** task
  requires every target/input revision to exist in this project, its **actual
  content bytes** to recompute to the stored ``content_sha256``/``byte_size``
  *and* the pinned digest, its pinned rights record to be current and
  storage-appropriate (**never** a hosted-transfer grant), and the target **and
  every live input** to be the artifact's **current** revision; a missing,
  foreign, digest-mismatched, superseded-rights, or stale reference is refused
  before any row is written. A historical task stays readable/auditable after a
  legitimate head change (integrity is not an authorization to execute).
* **Task-scoped, non-authorizing proposals.** :meth:`RuntimeRepository.record_proposal`
  accepts a **trusted, already-persisted** task plus a payload and calls the
  integrated :func:`review_article_agent.runtime.roles.submit_proposal`, so the
  model provenance and every digest are **derived anew** — an in-band payload
  ``actor``/``id``/``digest`` is never trusted. Each proposal is keyed by an
  immutable **task-scoped record id** (``record_id``) separate from the machine
  ``proposal_id``, so two *identical* task rows each keep their own proposal while
  a same-task replay stays idempotent. A proposal is stored in its own table and
  is **never** written into the artifact/approval/scientific state.
* **No secret material.** No endpoint, password, provider key, or challenge
  value is ever stored (spec lines 62-63; ``docs/storage.md``).

What this deliberately is **not**
---------------------------------

* It is **not** dispatch, admission, budget, or approval logic; no model call,
  no network access, no money, and no authorization is performed here.
* It does **not** expose the store to agent tools: the repository is a Python API
  on a verified store, never a tool/MCP surface.
* A persisted proposal is **not** an artifact, approval, or scientific claim.

Consumers (the later worker facade) use the documented return types: every value
object exposes a ``to_mapping()`` JSON-safe view.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from ..domain.errors import StoreError
from ..domain.records import (
    RecordValidationError,
    canonical_json,
    require_storage_allowed,
    validate_identifier,
    validate_rights_record,
)
from ..runtime.roles import (
    SCIENTIFIC_ROLES,
    BoundedTask,
    ModelRef,
    RoleConfiguration,
    submit_proposal,
)
from .artifacts import ArtifactRepository

__all__ = [
    "CONTEXT_KINDS",
    "VERSION_ROUTES",
    "VERSION_SLOT_NAMES",
    "VERSION_SLOT_STATES",
    "AgentRuntimeRepository",
    "PureJsonVersionSet",
    "RuntimeConfigSnapshot",
    "RuntimeConflictError",
    "RuntimeCorruptionError",
    "RuntimeProposalRecord",
    "RuntimeRepository",
    "RuntimeRepositoryError",
    "RuntimeSessionMapping",
    "RuntimeTaskRecord",
    "RuntimeValidationError",
    "store_integrity_problems",
    "validate_version_set",
    "verify_store_integrity",
]

#: A lowercase hex SHA-256 digest (the contract's single digest algorithm).
_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

#: A URL-safe opaque id: unreserved URL characters only.
_URLSAFE_RE = re.compile(r"^[A-Za-z0-9._~-]+$")

#: The observed OpenCode session-id shape (``^ses`` + a URL-safe opaque suffix),
#: mirrored so a pathlike session id cannot be recorded.
_SESSION_ID_RE = re.compile(r"ses[A-Za-z0-9_-]+")

#: Upper bound on any recorded opaque URL-safe identifier.
MAX_URL_ID_LENGTH = 128


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_hex(data: bytes) -> str:
    """Return the lowercase hex SHA-256 of ``data`` (bare, no ``sha256:`` prefix)."""
    return hashlib.sha256(data).hexdigest()


def _bare_hex(value: object, field: str) -> str:
    """Normalize a digest to bare lowercase hex, rejecting anything malformed."""
    if not isinstance(value, str):
        raise RuntimeValidationError(f"{field}: a SHA-256 digest must be a string")
    text = value[7:] if value.startswith("sha256:") else value
    if _HEX_RE.fullmatch(text) is None:
        raise RuntimeValidationError(f"{field}: must be a lowercase hex SHA-256 digest")
    return text


def _require_nonempty_str(value: object, field: str) -> str:
    if not isinstance(value, str) or value == "" or value != value.strip():
        raise RuntimeValidationError(
            f"{field}: must be non-empty with no leading/trailing whitespace"
        )
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise RuntimeValidationError(f"{field}: must not contain control characters")
    return value


def _require_utc_timestamp(value: object, field: str) -> str:
    """Validate an exact RFC 3339 **UTC** timestamp (never a bare non-empty string)."""
    text = _require_nonempty_str(value, field)
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise RuntimeValidationError(
            f"{field}: must be an RFC 3339 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise RuntimeValidationError(f"{field}: must carry an explicit UTC offset")
    return text


def _require_url_safe(
    value: object, field: str, *, pattern: re.Pattern[str] = _URLSAFE_RE
) -> str:
    """Validate one URL-safe, length-bounded opaque identifier (no input echo)."""
    if not isinstance(value, str) or value == "":
        raise RuntimeValidationError(f"{field}: must be a non-empty string")
    if len(value) > MAX_URL_ID_LENGTH:
        raise RuntimeValidationError(f"{field}: is too long")
    if any(ch.isspace() for ch in value) or pattern.fullmatch(value) is None:
        raise RuntimeValidationError(f"{field}: must be a URL-safe opaque identifier")
    return value


def _require_session_id(value: object) -> str:
    """Validate a recorded OpenCode session id (URL-safe, ``ses`` shape)."""
    if (
        not isinstance(value, str)
        or len(value) > MAX_URL_ID_LENGTH
        or _SESSION_ID_RE.fullmatch(value) is None
    ):
        raise RuntimeValidationError(
            "session_id: must be a URL-safe OpenCode session id"
        )
    return value


def _require_clean_location(value: object) -> str:
    """Validate a normalized absolute app-owned location (a cleaned directory)."""
    text = _require_nonempty_str(value, "clean_location")
    if not os.path.isabs(text) or os.path.normpath(text) != text:
        raise RuntimeValidationError(
            "clean_location: must be a normalized absolute location"
        )
    return text


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class RuntimeRepositoryError(StoreError):
    """A durable runtime mapping operation failed."""


class RuntimeValidationError(RuntimeRepositoryError, ValueError):
    """A runtime record is malformed, foreign, missing, superseded, or stale."""


class RuntimeConflictError(RuntimeRepositoryError):
    """A request/identity conflicts with already-recorded immutable state."""


class RuntimeCorruptionError(RuntimeRepositoryError):
    """A stored runtime row fails structural/hash/link revalidation on read."""


# --------------------------------------------------------------------------- #
# Identifier derivation
# --------------------------------------------------------------------------- #
def _scoped_id(prefix: str, project_id: str, key: str) -> str:
    digest = _sha256_hex((project_id + "\x00" + key).encode("utf-8"))
    return f"{prefix}_{digest[:32]}"


# --------------------------------------------------------------------------- #
# Value objects (documented return types for the later worker facade)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RuntimeConfigSnapshot:
    """One immutable configuration-snapshot row (as stored, revalidated).

    ``config_version`` is the monotonic per-project ordinal and ``config_digest``
    is the recomputed SHA-256 of the exact canonical bytes in ``canonical`` —
    never a caller-supplied or fabricated value.
    """

    project_id: str
    snapshot_id: str
    config_version: int
    config_digest: str
    created_at_utc: str
    canonical: bytes

    def role_configuration(self) -> RoleConfiguration:
        """Reconstruct the validated :class:`RoleConfiguration` from stored bytes."""
        return RoleConfiguration.from_mapping(json.loads(self.canonical.decode("utf-8")))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "snapshot_id": self.snapshot_id,
            "config_version": self.config_version,
            "config_digest": self.config_digest,
            "created_at_utc": self.created_at_utc,
            "config": json.loads(self.canonical.decode("utf-8")),
        }


@dataclass(frozen=True)
class RuntimeSessionMapping:
    """One immutable app-owned service/project/session mapping (as stored)."""

    project_id: str
    mapping_id: str
    service_instance_id: str
    opencode_project_id: str | None
    clean_location: str
    session_id: str
    role: str
    provider_id: str
    model_id: str
    variant: str | None
    config_digest: str
    config_snapshot_id: str
    created_at_utc: str

    @property
    def model_ref(self) -> str:
        base = f"{self.provider_id}/{self.model_id}"
        return base if self.variant is None else f"{base}#{self.variant}"

    def to_mapping(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "mapping_id": self.mapping_id,
            "service_instance_id": self.service_instance_id,
            "opencode_project_id": self.opencode_project_id,
            "clean_location": self.clean_location,
            "session_id": self.session_id,
            "role": self.role,
            "model": self.model_ref,
            "config_digest": self.config_digest,
            "config_snapshot_id": self.config_snapshot_id,
            "created_at_utc": self.created_at_utc,
        }


@dataclass(frozen=True)
class RuntimeTaskRecord:
    """One immutable persisted bounded task (as stored, revalidated)."""

    project_id: str
    task_id: str
    request_id: str
    fingerprint: str
    role: str
    config_digest: str
    config_snapshot_id: str
    target: Mapping[str, str]
    input_refs: tuple[Mapping[str, str], ...]
    prompt_version: str
    size_limit: int
    step_limit: int
    created_at_utc: str
    canonical: bytes

    def to_mapping(self) -> dict[str, Any]:
        """A JSON-safe view (target/input pins + role/prompt/limit identity)."""
        return {
            "project_id": self.project_id,
            "task_id": self.task_id,
            "request_id": self.request_id,
            "fingerprint": self.fingerprint,
            "role": self.role,
            "config_digest": self.config_digest,
            "config_snapshot_id": self.config_snapshot_id,
            "target": dict(self.target),
            "input_refs": [dict(entry) for entry in self.input_refs],
            "prompt_version": self.prompt_version,
            "size_limit": self.size_limit,
            "step_limit": self.step_limit,
            "created_at_utc": self.created_at_utc,
        }


@dataclass(frozen=True)
class RuntimeProposalRecord:
    """One immutable, non-authorizing persisted proposal (as stored, revalidated).

    ``record_id`` is the immutable **task-scoped** record identity; ``proposal_id``
    is the machine identity derived by ``submit_proposal``. Two identical task
    rows therefore each keep their own proposal under a distinct ``record_id``.
    """

    project_id: str
    record_id: str
    proposal_id: str
    task_id: str
    role: str
    provider_id: str
    model_id: str
    variant: str | None
    target: Mapping[str, str]
    task_digest: str
    input_digest: str
    payload_digest: str
    stored_digest: str
    created_at_utc: str
    canonical: bytes

    @property
    def model_ref(self) -> str:
        base = f"{self.provider_id}/{self.model_id}"
        return base if self.variant is None else f"{base}#{self.variant}"

    def to_mapping(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "record_id": self.record_id,
            "proposal_id": self.proposal_id,
            "task_id": self.task_id,
            "kind": "proposal",
            "role": self.role,
            "model": self.model_ref,
            "target": dict(self.target),
            "task_digest": self.task_digest,
            "input_digest": self.input_digest,
            "payload_digest": self.payload_digest,
            "stored_digest": self.stored_digest,
            "created_at_utc": self.created_at_utc,
        }


# --------------------------------------------------------------------------- #
# VersionSet validation helper (C02 §6.1)
# --------------------------------------------------------------------------- #
#: ``context_kind`` values (C02 §6.1).
CONTEXT_KINDS = ("model_derived", "human_authored", "mixed")

#: ``VersionSlot.state`` values (C02 §6.1).
VERSION_SLOT_STATES = ("known", "unknown", "not_applicable")

#: ``route`` values (C02 §6.1).
VERSION_ROUTES = ("opencode_session", "direct_call", "not_applicable")

#: The named version slots (C02 §6.1).
VERSION_SLOT_NAMES = ("model", "prompt", "rubric", "parser", "adapter")

_VERSION_SET_KEYS = frozenset(
    {"context_kind", "input_digest", "route", *VERSION_SLOT_NAMES}
)

# Static, value-free messages: an error from untrusted input never echoes it.
_MSG_VS_OBJECT = "versions: must be an object"
_MSG_VS_FIELD = "versions: contains a field that is not permitted"
_MSG_VS_MISSING = "versions: is missing a required field"
_MSG_VS_CONTEXT = "versions.context_kind: is not a supported context kind"
_MSG_VS_DIGEST = "versions.input_digest: must be a lowercase hex SHA-256 digest"
_MSG_VS_ROUTE = "versions.route: is not a supported route"
_MSG_VS_SLOT_OBJECT = "versions: a version slot must be an object"
_MSG_VS_SLOT_FIELD = "versions: a version slot has a field that is not permitted"
_MSG_VS_SLOT_STATE = "versions: a version slot state is not supported"
_MSG_VS_SLOT_REF = "versions: a known version slot requires a versioned ref"
_MSG_VS_SLOT_NO_REF = "versions: a ref is permitted only on a known version slot"
_MSG_VS_REF_OBJECT = "versions: a versioned ref must be an object"
_MSG_VS_REF_FIELD = "versions: a versioned ref has a field that is not permitted"
_MSG_VS_REF_MISSING = "versions: a versioned ref is missing version or digest"
_MSG_VS_REF_VERSION = "versions: a versioned ref version must be a non-empty string"
_MSG_VS_REF_DIGEST = "versions: a versioned ref digest must be a lowercase hex digest"
_MSG_VS_HUMAN_SLOT = (
    "versions: a human_authored set must mark model and prompt not_applicable"
)
_MSG_VS_MODEL_SLOT = (
    "versions: a model-derived set must mark model and prompt known"
)
_MSG_VS_HUMAN_CONSUMED = "versions: a human_authored set consumes no versioned slot"
_MSG_VS_CONSUMED_NAME = "versions: is not a named version slot"
_MSG_VS_CONSUMED_STATE = "versions: a consumed version slot must be known"


def _vs_text(value: object) -> str:
    if (
        not isinstance(value, str)
        or value == ""
        or value != value.strip()
        or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)
    ):
        raise RecordValidationError(_MSG_VS_REF_VERSION)
    return value


def _vs_digest(value: object) -> str:
    if not isinstance(value, str):
        raise RecordValidationError(_MSG_VS_REF_DIGEST)
    text = value[7:] if value.startswith("sha256:") else value
    if _HEX_RE.fullmatch(text) is None:
        raise RecordValidationError(_MSG_VS_REF_DIGEST)
    return text


def _normalize_version_slot(value: object, name: str) -> Mapping[str, Any]:
    """Validate one canonical ``VersionSlot`` (``{state, ref?}``) — never invents.

    The **canonical nested shape is preserved** (``{state, ref: {version,
    digest}}``) so the normalized result is itself a valid ``VersionSet`` and the
    validator is idempotent (``validate_version_set(validate_version_set(v)) ==
    validate_version_set(v)``).
    """
    if not isinstance(value, Mapping):
        raise RecordValidationError(_MSG_VS_SLOT_OBJECT)
    for key in value:
        if not isinstance(key, str) or key not in {"state", "ref"}:
            raise RecordValidationError(_MSG_VS_SLOT_FIELD)
    state = value.get("state")
    if state not in VERSION_SLOT_STATES:
        raise RecordValidationError(_MSG_VS_SLOT_STATE)
    if state == "known":
        ref = value.get("ref")
        if not isinstance(ref, Mapping):
            raise RecordValidationError(_MSG_VS_SLOT_REF)
        for key in ref:
            if not isinstance(key, str) or key not in {"version", "digest"}:
                raise RecordValidationError(_MSG_VS_REF_FIELD)
        if "version" not in ref or "digest" not in ref:
            raise RecordValidationError(_MSG_VS_REF_MISSING)
        normalized = {
            "state": "known",
            "ref": {"version": _vs_text(ref["version"]), "digest": _vs_digest(ref["digest"])},
        }
        return MappingProxyType(
            {"state": "known", "ref": MappingProxyType(normalized["ref"])}
        )
    if "ref" in value:
        raise RecordValidationError(_MSG_VS_SLOT_NO_REF)
    return MappingProxyType({"state": state})


def validate_version_set(
    value: object, *, consumed: Iterable[str] = ()
) -> dict[str, Any]:
    """Validate a persisted ``VersionSet`` (C02 §6.1) and return its canonical form.

    The returned mapping keeps the **canonical nested shape** (``{state, ref:
    {version, digest}}``), so it validates again unchanged (idempotent) and
    round-trips through :class:`PureJsonVersionSet`.

    ``consumed`` names the slots the stage actually consumed (e.g. ``("model",
    "prompt", "adapter")`` for a Jev/typed-decision stage); each named slot MUST be
    ``known`` (an unknown/missing applied version is refused rather than
    fabricated). For a ``human_authored`` set the ``model``/``prompt`` slots MUST
    be ``not_applicable`` (a human-only approval never invents a model id), and no
    slot may be named as consumed.

    Never fabricates a version or digest. Every failure is a static, value-free
    :class:`~review_article_agent.domain.records.RecordValidationError`.
    """
    if not isinstance(value, Mapping):
        raise RecordValidationError(_MSG_VS_OBJECT)
    for key in value:
        if not isinstance(key, str) or key not in _VERSION_SET_KEYS:
            raise RecordValidationError(_MSG_VS_FIELD)
    if _VERSION_SET_KEYS - set(value):
        raise RecordValidationError(_MSG_VS_MISSING)
    context_kind = value["context_kind"]
    if context_kind not in CONTEXT_KINDS:
        raise RecordValidationError(_MSG_VS_CONTEXT)
    try:
        input_digest = _vs_digest(value["input_digest"])
    except RecordValidationError:
        raise RecordValidationError(_MSG_VS_DIGEST) from None
    route = value["route"]
    if route not in VERSION_ROUTES:
        raise RecordValidationError(_MSG_VS_ROUTE)
    slots = {
        name: _normalize_version_slot(value[name], name) for name in VERSION_SLOT_NAMES
    }
    if context_kind == "human_authored":
        for name in ("model", "prompt"):
            if slots[name]["state"] != "not_applicable":
                raise RecordValidationError(_MSG_VS_HUMAN_SLOT)
    else:
        for name in ("model", "prompt"):
            if slots[name]["state"] != "known":
                raise RecordValidationError(_MSG_VS_MODEL_SLOT)
    consumed_names = list(consumed)
    if context_kind == "human_authored" and consumed_names:
        raise RecordValidationError(_MSG_VS_HUMAN_CONSUMED)
    for name in consumed_names:
        if name not in VERSION_SLOT_NAMES:
            raise RecordValidationError(_MSG_VS_CONSUMED_NAME)
        if slots[name]["state"] != "known":
            raise RecordValidationError(_MSG_VS_CONSUMED_STATE)
    normalized: dict[str, Any] = {
        "context_kind": context_kind,
        "input_digest": input_digest,
        "route": route,
    }
    for name in VERSION_SLOT_NAMES:
        slot = slots[name]
        normalized[name] = (
            {"state": slot["state"], "ref": dict(slot["ref"])}
            if "ref" in slot
            else {"state": slot["state"]}
        )
    return normalized


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


class PureJsonVersionSet:
    """A validated, deeply-immutable ``VersionSet`` (C02 §6.1) for decision records.

    Pure: no I/O, no model call. The **direct constructor validates** (a caller
    cannot build an unchecked instance), the value is deep-frozen so no post-hash
    mutation is possible, and :meth:`as_mapping` returns a fresh plain copy.
    """

    __slots__ = ("_mapping",)

    def __init__(self, value: object, *, consumed: Iterable[str] = ()) -> None:
        self._mapping = _deep_freeze(validate_version_set(value, consumed=consumed))

    @classmethod
    def from_mapping(
        cls, value: object, *, consumed: Iterable[str] = ()
    ) -> "PureJsonVersionSet":
        return cls(value, consumed=consumed)

    @property
    def context_kind(self) -> str:
        return self._mapping["context_kind"]

    @property
    def input_digest(self) -> str:
        return self._mapping["input_digest"]

    @property
    def route(self) -> str:
        return self._mapping["route"]

    def slot(self, name: str) -> dict[str, Any]:
        if name not in VERSION_SLOT_NAMES:
            raise RecordValidationError(_MSG_VS_CONSUMED_NAME)
        return _thaw(self._mapping[name])

    def as_mapping(self) -> dict[str, Any]:
        return _thaw(self._mapping)


# --------------------------------------------------------------------------- #
# A minimal read-only store view (for the shared integrity validator)
# --------------------------------------------------------------------------- #
class _ReadOnlyStoreView:
    """The exact surface the repositories/validators read from a store."""

    __slots__ = ("connection", "project_id", "schema_version")

    def __init__(self, conn: sqlite3.Connection, project_id: str) -> None:
        self.connection = conn
        self.project_id = project_id
        self.schema_version = 0


# --------------------------------------------------------------------------- #
# Repository
# --------------------------------------------------------------------------- #
class RuntimeRepository:
    """Immutable runtime mappings over an already-verified :class:`ProjectStore`.

    Every read/replay/write path revalidates the stored row (canonical bytes,
    recomputed hashes, re-parsed identity, projected columns, and linked child
    rows) and fails closed with :class:`RuntimeCorruptionError` on any mismatch.

    Typical use::

        store = ProjectStore.open_at(app_dir, expected_project_id=pid)  # path layer
        runtime = RuntimeRepository(store)
        snapshot = runtime.record_configuration(configuration)      # immutable bytes
        mapping = runtime.record_session_mapping(
            service_instance_id="svc_1", opencode_project_id="prj_oc_1",
            clean_location="/tmp/opencode/clean", session_id="ses_abc",
            role="writer", model="openrouter/anthropic/claude-sonnet-5-5#high",
            configuration=configuration,
        )
        task = runtime.record_task(bounded_task, request_id="t-1",
                                   configuration=configuration)
        proposal = runtime.record_proposal(task, {"content": {...}},
                                           configuration=configuration)
    """

    def __init__(self, store, *, artifacts: ArtifactRepository | None = None) -> None:
        self._store = store
        self._artifacts = artifacts if artifacts is not None else ArtifactRepository(store)

    @property
    def project_id(self) -> str:
        """The store's own project id; the repository never uses a caller project."""
        return self._store.project_id

    def _conn(self) -> sqlite3.Connection:
        return self._store.connection

    # -- configuration snapshots --------------------------------------------- #
    def record_configuration(
        self,
        configuration: RoleConfiguration,
        *,
        snapshot_id: str | None = None,
        created_at_utc: str | None = None,
    ) -> RuntimeConfigSnapshot:
        """Persist (or return) an immutable configuration snapshot.

        The snapshot's digest is the recomputed SHA-256 of the exact canonical
        bytes of ``configuration.as_mapping()``; an identical configuration is
        idempotent (returns the validated existing row). A corrupt prior row with
        the same digest fails closed rather than being reused.
        """
        with self._store.transaction():
            row = self._ensure_configuration(
                configuration, snapshot_id=snapshot_id, created_at_utc=created_at_utc
            )
        return self._row_to_config(row)

    def _ensure_configuration(
        self,
        configuration: RoleConfiguration,
        *,
        snapshot_id: str | None,
        created_at_utc: str | None,
    ) -> sqlite3.Row:
        """Insert-or-get one configuration snapshot (caller holds a transaction)."""
        if not isinstance(configuration, RoleConfiguration):
            raise RuntimeValidationError(
                "configuration: must be a validated RoleConfiguration"
            )
        canonical = canonical_json(configuration.as_mapping())
        digest = _sha256_hex(canonical)
        existing = self._config_row_by_digest(digest)
        if existing is not None:
            # Revalidate before reuse: a corrupt row must never be handed back.
            self._validate_config_row(existing)
            return existing
        if snapshot_id is None:
            snapshot_id = "rcfg_" + uuid.uuid4().hex
        snapshot_id = validate_identifier(snapshot_id, "snapshot_id")
        if created_at_utc is None:
            created = _utc_now()
        else:
            created = _require_utc_timestamp(created_at_utc, "created_at_utc")
        row = self._conn().execute(
            "SELECT COALESCE(MAX(config_version), 0) AS newest "
            "FROM runtime_config_snapshot WHERE project_id = ?",
            (self.project_id,),
        ).fetchone()
        next_version = int(row["newest"]) + 1
        self._conn().execute(
            "INSERT INTO runtime_config_snapshot "
            "(project_id, snapshot_id, config_version, config_digest, canonical, "
            " created_at_utc) VALUES (?, ?, ?, ?, ?, ?)",
            (self.project_id, snapshot_id, next_version, digest, canonical, created),
        )
        stored = self._config_row_by_digest(digest)
        assert stored is not None  # just written in this transaction
        self._validate_config_row(stored)
        return stored

    def _validate_config_row(self, row: sqlite3.Row) -> RuntimeConfigSnapshot:
        """Structurally revalidate a configuration row; fail closed on corruption."""
        if row["project_id"] != self.project_id:
            raise RuntimeCorruptionError("configuration: foreign project row.")
        snapshot_id = validate_identifier(row["snapshot_id"], "snapshot_id")
        version = row["config_version"]
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise RuntimeCorruptionError("configuration: invalid snapshot version.")
        _require_utc_timestamp(row["created_at_utc"], "created_at_utc")
        canonical = bytes(row["canonical"])
        if _sha256_hex(canonical) != row["config_digest"]:
            raise RuntimeCorruptionError("configuration: stored digest mismatch.")
        try:
            parsed = json.loads(canonical.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise RuntimeCorruptionError(
                "configuration: canonical bytes are not decodable JSON."
            ) from exc
        try:
            rebuilt = RoleConfiguration.from_mapping(parsed)
        except (RecordValidationError, ValueError) as exc:
            raise RuntimeCorruptionError(
                "configuration: canonical bytes are not a valid role configuration."
            ) from exc
        if _sha256_hex(canonical_json(rebuilt.as_mapping())) != row["config_digest"]:
            raise RuntimeCorruptionError("configuration: canonical form is not stable.")
        return RuntimeConfigSnapshot(
            project_id=row["project_id"],
            snapshot_id=snapshot_id,
            config_version=version,
            config_digest=row["config_digest"],
            created_at_utc=row["created_at_utc"],
            canonical=canonical,
        )

    def get_configuration(self, snapshot_id: str) -> RuntimeConfigSnapshot | None:
        snapshot_id = validate_identifier(snapshot_id, "snapshot_id")
        row = self._conn().execute(
            "SELECT * FROM runtime_config_snapshot WHERE project_id = ? AND "
            "snapshot_id = ?",
            (self.project_id, snapshot_id),
        ).fetchone()
        return None if row is None else self._validate_config_row(row)

    def get_configuration_by_digest(
        self, config_digest: str
    ) -> RuntimeConfigSnapshot | None:
        digest = _bare_hex(config_digest, "config_digest")
        row = self._config_row_by_digest(digest)
        return None if row is None else self._validate_config_row(row)

    def list_configurations(self) -> list[RuntimeConfigSnapshot]:
        rows = self._conn().execute(
            "SELECT * FROM runtime_config_snapshot WHERE project_id = ? "
            "ORDER BY config_version",
            (self.project_id,),
        ).fetchall()
        return [self._validate_config_row(row) for row in rows]

    def verify_configuration(self, snapshot_id: str) -> bool:
        """Convenience probe: ``True`` iff the snapshot exists and revalidates.

        The primary read (:meth:`get_configuration`) **raises**
        :class:`RuntimeCorruptionError` on a corrupt row rather than returning it;
        this probe only collapses a missing/corrupt row to ``False`` for callers
        (e.g. a backup check) that want a boolean.
        """
        try:
            return self.get_configuration(snapshot_id) is not None
        except RuntimeCorruptionError:
            return False

    def _config_row_by_digest(self, digest: str) -> sqlite3.Row | None:
        return self._conn().execute(
            "SELECT * FROM runtime_config_snapshot WHERE project_id = ? AND "
            "config_digest = ?",
            (self.project_id, digest),
        ).fetchone()

    def _row_to_config(self, row: sqlite3.Row) -> RuntimeConfigSnapshot:
        return self._validate_config_row(row)

    # -- session mappings ---------------------------------------------------- #
    def record_session_mapping(
        self,
        *,
        service_instance_id: str,
        clean_location: str,
        session_id: str,
        role: str,
        model: object,
        configuration: RoleConfiguration,
        opencode_project_id: str | None = None,
        created_at_utc: str | None = None,
    ) -> RuntimeSessionMapping:
        """Persist (or return) an immutable app-owned session mapping.

        ``role`` must be a known scientific role and ``model`` must be **exactly**
        the concrete provider/model/variant configured for that role; a foreign
        role or model (or a variant-only substitution) is refused. ``session_id``
        must be a URL-safe OpenCode session id and ``service_instance_id``/
        ``opencode_project_id`` URL-safe opaques, so no pathlike id is recorded.
        A repeated ``session_id`` with an identical mapping is idempotent; a
        divergent reuse raises :class:`RuntimeConflictError`.
        """
        service_instance_id = _require_url_safe(
            service_instance_id, "service_instance_id"
        )
        if opencode_project_id is not None:
            opencode_project_id = _require_url_safe(
                opencode_project_id, "opencode_project_id"
            )
        clean_location = _require_clean_location(clean_location)
        session_id = _require_session_id(session_id)
        if role not in SCIENTIFIC_ROLES:
            raise RuntimeValidationError("role: must name a known scientific role")
        model_ref = model if isinstance(model, ModelRef) else ModelRef.from_mapping(model)
        if not isinstance(configuration, RoleConfiguration):
            raise RuntimeValidationError(
                "configuration: must be a validated RoleConfiguration"
            )
        configured = configuration.for_role(role).model
        if (model_ref.provider_id, model_ref.model_id, model_ref.variant) != (
            configured.provider_id,
            configured.model_id,
            configured.variant,
        ):
            raise RuntimeValidationError(
                "model: does not match the configured model for this role; "
                "refusing a foreign model substitution"
            )
        mapping_id = _scoped_id("rmap", self.project_id, session_id)
        created = (
            _utc_now()
            if created_at_utc is None
            else _require_utc_timestamp(created_at_utc, "created_at_utc")
        )

        with self._store.transaction():
            config_row = self._ensure_configuration(
                configuration, snapshot_id=None, created_at_utc=None
            )
            existing = self._session_row_by_session(session_id)
            if existing is not None:
                # Revalidate the stored row so a corrupt mapping cannot be treated
                # as an idempotent replay.
                self._validate_session_row(existing)
                if self._mapping_signature(existing) != (
                    service_instance_id,
                    opencode_project_id,
                    clean_location,
                    role,
                    model_ref.provider_id,
                    model_ref.model_id,
                    model_ref.variant,
                    config_row["config_digest"],
                ):
                    raise RuntimeConflictError(
                        f"session {session_id!r} is already mapped with different "
                        f"values; refusing a divergent session mapping."
                    )
                return self._validate_session_row(existing)
            self._conn().execute(
                "INSERT INTO runtime_session_mapping "
                "(project_id, mapping_id, service_instance_id, opencode_project_id, "
                " clean_location, session_id, role, provider_id, model_id, variant, "
                " config_digest, config_snapshot_id, created_at_utc) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.project_id,
                    mapping_id,
                    service_instance_id,
                    opencode_project_id,
                    clean_location,
                    session_id,
                    role,
                    model_ref.provider_id,
                    model_ref.model_id,
                    model_ref.variant,
                    config_row["config_digest"],
                    config_row["snapshot_id"],
                    created,
                ),
            )
        stored = self.get_session_mapping(mapping_id)
        assert stored is not None  # just committed
        return stored

    def _validate_session_row(self, row: sqlite3.Row) -> RuntimeSessionMapping:
        """Structurally revalidate a session mapping and its config link."""
        if row["project_id"] != self.project_id:
            raise RuntimeCorruptionError("session mapping: foreign project row.")
        _require_url_safe(row["service_instance_id"], "service_instance_id")
        if row["opencode_project_id"] is not None:
            _require_url_safe(row["opencode_project_id"], "opencode_project_id")
        _require_clean_location(row["clean_location"])
        _require_session_id(row["session_id"])
        role = row["role"]
        if role not in SCIENTIFIC_ROLES:
            raise RuntimeCorruptionError("session mapping: unknown role.")
        _require_utc_timestamp(row["created_at_utc"], "created_at_utc")
        config_row = self._config_row_by_digest(row["config_digest"])
        if config_row is None or config_row["snapshot_id"] != row["config_snapshot_id"]:
            raise RuntimeCorruptionError(
                "session mapping: config snapshot link does not match its digest."
            )
        config = self._validate_config_row(config_row)
        try:
            configured = config.role_configuration().for_role(role).model
        except (RecordValidationError, ValueError) as exc:
            raise RuntimeCorruptionError(
                "session mapping: role is not configured in its snapshot."
            ) from exc
        if (row["provider_id"], row["model_id"], row["variant"]) != (
            configured.provider_id,
            configured.model_id,
            configured.variant,
        ):
            raise RuntimeCorruptionError(
                "session mapping: recorded model does not match its configuration."
            )
        return RuntimeSessionMapping(
            project_id=row["project_id"],
            mapping_id=row["mapping_id"],
            service_instance_id=row["service_instance_id"],
            opencode_project_id=row["opencode_project_id"],
            clean_location=row["clean_location"],
            session_id=row["session_id"],
            role=role,
            provider_id=row["provider_id"],
            model_id=row["model_id"],
            variant=row["variant"],
            config_digest=row["config_digest"],
            config_snapshot_id=row["config_snapshot_id"],
            created_at_utc=row["created_at_utc"],
        )

    def get_session_mapping(self, mapping_id: str) -> RuntimeSessionMapping | None:
        mapping_id = validate_identifier(mapping_id, "mapping_id")
        row = self._conn().execute(
            "SELECT * FROM runtime_session_mapping WHERE project_id = ? AND "
            "mapping_id = ?",
            (self.project_id, mapping_id),
        ).fetchone()
        return None if row is None else self._validate_session_row(row)

    def get_session_mapping_for(self, session_id: str) -> RuntimeSessionMapping | None:
        session_id = _require_session_id(session_id)
        row = self._session_row_by_session(session_id)
        return None if row is None else self._validate_session_row(row)

    def list_session_mappings(self) -> list[RuntimeSessionMapping]:
        rows = self._conn().execute(
            "SELECT * FROM runtime_session_mapping WHERE project_id = ? ORDER BY rowid",
            (self.project_id,),
        ).fetchall()
        return [self._validate_session_row(row) for row in rows]

    def _session_row_by_session(self, session_id: str) -> sqlite3.Row | None:
        return self._conn().execute(
            "SELECT * FROM runtime_session_mapping WHERE project_id = ? AND "
            "session_id = ?",
            (self.project_id, session_id),
        ).fetchone()

    @staticmethod
    def _mapping_signature(row: sqlite3.Row) -> tuple:
        return (
            row["service_instance_id"],
            row["opencode_project_id"],
            row["clean_location"],
            row["role"],
            row["provider_id"],
            row["model_id"],
            row["variant"],
            row["config_digest"],
        )

    # -- bounded tasks ------------------------------------------------------- #
    def record_task(
        self,
        task: BoundedTask,
        *,
        request_id: str,
        configuration: RoleConfiguration,
        created_at_utc: str | None = None,
    ) -> RuntimeTaskRecord:
        """Persist (or return) an immutable bounded task keyed by ``request_id``.

        A repeated ``request_id`` with the **same** task fingerprint returns the
        validated historical row (even after the target has moved on); a
        *divergent* task under the same key raises :class:`RuntimeConflictError`.
        Recording a **new** task verifies, before any write, that the exact target
        and every live input revision exist in this project, that their **actual
        content bytes** recompute to the stored digest/byte size and the pinned
        digest, that their pinned rights records are current and storage-allowed,
        and that the target **and every live input** is the artifact's **current**
        revision — a missing, foreign, digest-mismatched, superseded-rights, or
        stale reference fails closed.
        """
        if not isinstance(task, BoundedTask):
            raise RuntimeValidationError("task: must be a validated BoundedTask")
        request_id = validate_identifier(request_id, "request_id")
        if task.project_id != self.project_id:
            raise RuntimeValidationError(
                "task.project_id: must equal this store's project; refusing a "
                "foreign project."
            )
        if not isinstance(configuration, RoleConfiguration):
            raise RuntimeValidationError(
                "configuration: must be a validated RoleConfiguration"
            )
        configured = configuration.for_role(task.role)
        if configured != task.role_config:
            raise RuntimeValidationError(
                "task.role_config: does not match the supplied configuration; "
                "refusing to relabel the task to another role or model."
            )
        fingerprint = task.fingerprint()
        canonical = canonical_json(task.to_mapping())
        task_id = _scoped_id("rtask", self.project_id, request_id)
        created = (
            _utc_now()
            if created_at_utc is None
            else _require_utc_timestamp(created_at_utc, "created_at_utc")
        )

        with self._store.transaction():
            existing = self._task_row_by_request(request_id)
            if existing is not None:
                # Revalidate the stored row so a corrupt row is never treated as a
                # replay of the requested task.
                stored = self._validate_task_row(existing)
                if stored.fingerprint != fingerprint:
                    raise RuntimeConflictError(
                        f"request {request_id!r} was already recorded with a "
                        f"different task; refusing to create a second task."
                    )
                return stored
            config_row = self._ensure_configuration(
                configuration, snapshot_id=None, created_at_utc=None
            )
            self._verify_new_task_refs(task)
            self._conn().execute(
                "INSERT INTO runtime_bounded_task "
                "(project_id, task_id, request_id, fingerprint, role, config_digest, "
                " config_snapshot_id, target_artifact_id, target_revision_id, "
                " prompt_version, size_limit, step_limit, canonical, created_at_utc) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.project_id,
                    task_id,
                    request_id,
                    fingerprint,
                    task.role,
                    config_row["config_digest"],
                    config_row["snapshot_id"],
                    task.target["artifact_id"],
                    task.target["revision_id"],
                    task.prompt_version,
                    task.size_limit,
                    task.step_limit,
                    canonical,
                    created,
                ),
            )
            for index, ref in enumerate(task.input_refs):
                self._conn().execute(
                    "INSERT INTO runtime_task_input "
                    "(project_id, task_id, ordinal, artifact_id, revision_id, digest) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        self.project_id,
                        task_id,
                        index,
                        ref["artifact_id"],
                        ref["revision_id"],
                        ref["digest"],
                    ),
                )
        stored = self.get_task(task_id)
        assert stored is not None  # just committed
        return stored

    def _verify_new_task_refs(self, task: BoundedTask) -> None:
        """Fail-closed checks for a NEW task: content, digest, rights, currentness."""
        self._verify_live_ref(task.target, require_digest=None)
        for ref in task.input_refs:
            self._verify_live_ref(
                {
                    "project_id": self.project_id,
                    "artifact_id": ref["artifact_id"],
                    "revision_id": ref["revision_id"],
                },
                require_digest=ref["digest"],
            )

    def _verify_live_ref(
        self, ref: Mapping[str, str], *, require_digest: str | None
    ) -> None:
        """Verify one live ref: exists, current, content+digest, storage rights."""
        revision = self._artifacts.get_revision(ref["artifact_id"], ref["revision_id"])
        if revision is None:
            raise RuntimeValidationError(
                f"ref {ref['artifact_id']!r}/{ref['revision_id']!r} does not exist "
                f"in this project."
            )
        current = self._artifacts.current_revision(ref["artifact_id"])
        if current is None or current.revision_id != ref["revision_id"]:
            current_id = None if current is None else current.revision_id
            raise RuntimeValidationError(
                f"ref {ref['revision_id']!r} is not the current revision of "
                f"{ref['artifact_id']!r} (current {current_id!r}); refusing a "
                f"stale/outdated reference."
            )
        self._verify_revision_integrity(ref["artifact_id"], ref["revision_id"])
        if require_digest is not None:
            content = self._artifacts.read_content(ref["artifact_id"], ref["revision_id"])
            if _sha256_hex(content) != _bare_hex(require_digest, "digest"):
                raise RuntimeValidationError(
                    f"ref {ref['artifact_id']!r}/{ref['revision_id']!r} digest does "
                    f"not match the stored content."
                )

    def _verify_revision_integrity(self, artifact_id: str, revision_id: str) -> None:
        """Recompute a revision's ACTUAL content bytes against its stored metadata."""
        revision = self._artifacts.get_revision(artifact_id, revision_id)
        if revision is None:
            raise RuntimeValidationError(
                f"revision {artifact_id!r}/{revision_id!r} does not exist."
            )
        content = self._artifacts.read_content(artifact_id, revision_id)
        expected = _bare_hex(revision.content_sha256, "content_sha256")
        if len(content) != revision.byte_size or _sha256_hex(content) != expected:
            raise RuntimeValidationError(
                f"revision {artifact_id!r}/{revision_id!r} content does not match "
                f"its recorded digest/byte size."
            )
        self._verify_pinned_rights(revision, require_current=True)

    def _verify_pinned_rights(self, revision, *, require_current: bool) -> None:
        """Verify a revision's pinned rights canonical identity/subject (+storage).

        Only **storage** is required (never a hosted-transfer grant). A superseded
        rights record fails closed when ``require_current`` (a live/write path);
        a historical read validates identity/subject structurally only.
        """
        ref = revision.rights_ref
        row = self._conn().execute(
            "SELECT canonical, content_sha256 FROM rights_record WHERE project_id = ? "
            "AND rights_id = ? AND record_revision = ?",
            (self.project_id, ref.rights_id, ref.record_revision),
        ).fetchone()
        if row is None:
            raise RuntimeValidationError(
                f"rights record {ref.as_dict()!r} is missing."
            )
        canonical = bytes(row["canonical"])
        if _sha256_hex(canonical) != _bare_hex(row["content_sha256"], "content_sha256"):
            raise RuntimeValidationError("rights record canonical digest mismatch.")
        try:
            record = json.loads(canonical.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise RuntimeValidationError(
                "rights record is not decodable JSON."
            ) from exc
        if not isinstance(record, dict):
            raise RuntimeValidationError("rights record is not an object.")
        if (
            record.get("project_id") != self.project_id
            or record.get("rights_id") != ref.rights_id
            or record.get("record_revision") != ref.record_revision
        ):
            raise RuntimeValidationError("rights record identity mismatch.")
        subject = record.get("subject")
        if not (
            isinstance(subject, dict)
            and subject.get("kind") == "source_revision"
            and subject.get("project_id") == revision.project_id
            and subject.get("artifact_id") == revision.artifact_id
            and subject.get("revision_id") == revision.revision_id
        ):
            raise RuntimeValidationError(
                "rights record subject does not bind the pinned revision."
            )
        validated = validate_rights_record(record, self.project_id)
        require_storage_allowed(validated)
        if require_current:
            newest = self._conn().execute(
                "SELECT MAX(record_revision) AS newest FROM rights_record "
                "WHERE project_id = ? AND rights_id = ?",
                (self.project_id, ref.rights_id),
            ).fetchone()["newest"]
            if newest is not None and ref.record_revision < newest:
                raise RuntimeValidationError(
                    f"rights record {ref.rights_id!r} revision "
                    f"{ref.record_revision} is superseded by {newest}; refusing."
                )

    def _validate_task_row(self, row: sqlite3.Row) -> RuntimeTaskRecord:
        """Structurally revalidate a task row, its config link, and child inputs."""
        if row["project_id"] != self.project_id:
            raise RuntimeCorruptionError("task: foreign project row.")
        _require_utc_timestamp(row["created_at_utc"], "created_at_utc")
        _bare_hex(row["fingerprint"], "fingerprint")
        config_row = self._config_row_by_digest(row["config_digest"])
        if config_row is None or config_row["snapshot_id"] != row["config_snapshot_id"]:
            raise RuntimeCorruptionError(
                "task: config snapshot link does not match its digest."
            )
        config = self._validate_config_row(config_row)
        canonical = bytes(row["canonical"])
        try:
            parsed = json.loads(canonical.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise RuntimeCorruptionError(
                "task: canonical bytes are not decodable JSON."
            ) from exc
        try:
            bounded = BoundedTask.from_mapping(
                parsed, configuration=config.role_configuration()
            )
        except (RecordValidationError, ValueError) as exc:
            raise RuntimeCorruptionError(
                "task: canonical bytes are not a valid bounded task."
            ) from exc
        if _sha256_hex(canonical_json(bounded.to_mapping())) != _sha256_hex(canonical):
            raise RuntimeCorruptionError("task: canonical form is not stable.")
        if bounded.project_id != self.project_id or bounded.fingerprint() != row["fingerprint"]:
            raise RuntimeCorruptionError("task: fingerprint does not match canonical bytes.")
        # Projected columns must agree with the canonical task.
        if (
            row["role"] != bounded.role
            or row["target_artifact_id"] != bounded.target["artifact_id"]
            or row["target_revision_id"] != bounded.target["revision_id"]
            or row["prompt_version"] != bounded.prompt_version
            or row["size_limit"] != bounded.size_limit
            or row["step_limit"] != bounded.step_limit
        ):
            raise RuntimeCorruptionError("task: projected columns do not match canonical.")
        input_refs = self._validate_task_inputs(row["task_id"], bounded)
        # Every referenced revision must exist and its pinned digest must match the
        # stored content (historical existence only — never a currentness check).
        target = {
            "project_id": self.project_id,
            "artifact_id": row["target_artifact_id"],
            "revision_id": row["target_revision_id"],
        }
        self._verify_structural_ref(target, None)
        for ref in input_refs:
            self._verify_structural_ref(dict(ref), ref["digest"])
        return RuntimeTaskRecord(
            project_id=row["project_id"],
            task_id=row["task_id"],
            request_id=row["request_id"],
            fingerprint=row["fingerprint"],
            role=row["role"],
            config_digest=row["config_digest"],
            config_snapshot_id=row["config_snapshot_id"],
            target=MappingProxyType(target),
            input_refs=input_refs,
            prompt_version=row["prompt_version"],
            size_limit=row["size_limit"],
            step_limit=row["step_limit"],
            created_at_utc=row["created_at_utc"],
            canonical=canonical,
        )

    def _validate_task_inputs(
        self, task_id: str, bounded: BoundedTask
    ) -> tuple[Mapping[str, str], ...]:
        rows = self._conn().execute(
            "SELECT ordinal, artifact_id, revision_id, digest FROM runtime_task_input "
            "WHERE project_id = ? AND task_id = ? ORDER BY ordinal",
            (self.project_id, task_id),
        ).fetchall()
        if len(rows) != len(bounded.input_refs):
            raise RuntimeCorruptionError("task: input child row count mismatch.")
        out: list[Mapping[str, str]] = []
        for index, row in enumerate(rows):
            if row["ordinal"] != index:
                raise RuntimeCorruptionError("task: input child ordinals are not contiguous.")
            canonical_ref = bounded.input_refs[index]
            if (
                row["artifact_id"] != canonical_ref["artifact_id"]
                or row["revision_id"] != canonical_ref["revision_id"]
                or row["digest"] != canonical_ref["digest"]
            ):
                raise RuntimeCorruptionError(
                    "task: input child row does not match canonical bytes."
                )
            out.append(
                MappingProxyType(
                    {
                        "project_id": self.project_id,
                        "artifact_id": row["artifact_id"],
                        "revision_id": row["revision_id"],
                        "digest": row["digest"],
                    }
                )
            )
        return tuple(out)

    def _verify_structural_ref(
        self, ref: Mapping[str, str], require_digest: str | None
    ) -> None:
        """Verify a ref exists, its stored metadata is intact, and (optionally) digest."""
        revision = self._artifacts.get_revision(ref["artifact_id"], ref["revision_id"])
        if revision is None:
            raise RuntimeCorruptionError(
                f"task: referenced revision {ref['artifact_id']!r}/"
                f"{ref['revision_id']!r} is missing."
            )
        content = self._artifacts.read_content(ref["artifact_id"], ref["revision_id"])
        expected = _bare_hex(revision.content_sha256, "content_sha256")
        if len(content) != revision.byte_size or _sha256_hex(content) != expected:
            raise RuntimeCorruptionError(
                "task: referenced revision content does not match its metadata."
            )
        if require_digest is not None and _sha256_hex(content) != _bare_hex(
            require_digest, "digest"
        ):
            raise RuntimeCorruptionError(
                "task: pinned input digest does not match the referenced revision."
            )
        self._verify_pinned_rights(revision, require_current=False)

    def get_task(self, task_id: str) -> RuntimeTaskRecord | None:
        task_id = validate_identifier(task_id, "task_id")
        row = self._conn().execute(
            "SELECT * FROM runtime_bounded_task WHERE project_id = ? AND task_id = ?",
            (self.project_id, task_id),
        ).fetchone()
        return None if row is None else self._validate_task_row(row)

    def get_task_by_request(self, request_id: str) -> RuntimeTaskRecord | None:
        request_id = validate_identifier(request_id, "request_id")
        row = self._task_row_by_request(request_id)
        return None if row is None else self._validate_task_row(row)

    def list_tasks(self) -> list[RuntimeTaskRecord]:
        rows = self._conn().execute(
            "SELECT * FROM runtime_bounded_task WHERE project_id = ? ORDER BY rowid",
            (self.project_id,),
        ).fetchall()
        return [self._validate_task_row(row) for row in rows]

    def _task_row_by_request(self, request_id: str) -> sqlite3.Row | None:
        return self._conn().execute(
            "SELECT * FROM runtime_bounded_task WHERE project_id = ? AND request_id = ?",
            (self.project_id, request_id),
        ).fetchone()

    # -- proposals ----------------------------------------------------------- #
    def record_proposal(
        self,
        task: RuntimeTaskRecord | BoundedTask,
        payload: object,
        *,
        configuration: RoleConfiguration,
        created_at_utc: str | None = None,
    ) -> RuntimeProposalRecord:
        """Persist (or return) a non-authorizing proposal for a persisted task.

        ``task`` is the trusted, already-persisted :class:`RuntimeTaskRecord` (a
        bare :class:`BoundedTask` is accepted only when exactly one persisted task
        matches its fingerprint). The proposal is derived by
        :func:`review_article_agent.runtime.roles.submit_proposal` from the task's
        **trusted** role configuration and the payload's ``{"content": ...}``
        envelope; an in-band ``actor``/``proposal_id``/digest field in the payload
        is rejected, never trusted.

        The persisted identity is a **task-scoped record id** (``record_id``), so
        two identical task rows each keep their own proposal; a replay of the same
        persisted task + payload is idempotent; a divergent record under the same
        (task, machine proposal id) raises :class:`RuntimeConflictError`.
        """
        if not isinstance(configuration, RoleConfiguration):
            raise RuntimeValidationError(
                "configuration: must be a validated RoleConfiguration"
            )
        record = self._resolve_persisted_task(task)
        bounded, proposal = self._derive_proposal(record, payload, configuration)
        canonical = canonical_json(proposal.to_mapping())
        stored_digest = _sha256_hex(canonical)
        record_id = _scoped_id(
            "prop", self.project_id, record.task_id + "\x00" + proposal.proposal_id
        )
        created = (
            _utc_now()
            if created_at_utc is None
            else _require_utc_timestamp(created_at_utc, "created_at_utc")
        )

        with self._store.transaction():
            existing = self._proposal_row_for(record.task_id, proposal.proposal_id)
            if existing is not None:
                self._validate_proposal_row(existing)
                if bytes(existing["canonical"]) != canonical:
                    raise RuntimeConflictError(
                        f"proposal {proposal.proposal_id!r} already exists for this "
                        f"task with different content; refusing a divergent proposal."
                    )
                return self._validate_proposal_row(existing)
            self._conn().execute(
                "INSERT INTO runtime_proposal "
                "(project_id, record_id, proposal_id, task_id, role, provider_id, "
                " model_id, variant, target_artifact_id, target_revision_id, "
                " task_digest, input_digest, payload_digest, stored_digest, "
                " canonical, created_at_utc) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.project_id,
                    record_id,
                    proposal.proposal_id,
                    record.task_id,
                    proposal.role,
                    proposal.model.provider_id,
                    proposal.model.model_id,
                    proposal.model.variant,
                    proposal.target["artifact_id"],
                    proposal.target["revision_id"],
                    proposal.task_digest,
                    proposal.input_digest,
                    proposal.payload_digest,
                    stored_digest,
                    canonical,
                    created,
                ),
            )
        stored = self.get_proposal(record_id)
        assert stored is not None  # just committed
        return stored

    def _derive_proposal(
        self, record: RuntimeTaskRecord, payload: object, configuration: RoleConfiguration
    ):
        """Rebuild the bounded task from stored bytes and derive the proposal anew."""
        try:
            bounded = BoundedTask.from_mapping(
                json.loads(record.canonical.decode("utf-8")),
                configuration=configuration,
            )
        except Exception as exc:  # noqa: BLE001 - re-raise as a typed runtime error
            if isinstance(exc, RuntimeRepositoryError):
                raise
            raise RuntimeValidationError(
                "task: stored task does not revalidate against the supplied "
                "configuration."
            ) from exc
        if bounded.fingerprint() != record.fingerprint:
            raise RuntimeValidationError(
                "task: stored task fingerprint does not match; refusing a relabelled "
                "task."
            )
        return bounded, submit_proposal(bounded, payload, configuration=configuration)

    def _resolve_persisted_task(
        self, task: RuntimeTaskRecord | BoundedTask
    ) -> RuntimeTaskRecord:
        if isinstance(task, RuntimeTaskRecord):
            if task.project_id != self.project_id:
                raise RuntimeValidationError("task: does not belong to this project.")
            stored = self.get_task(task.task_id)
            if stored is None:
                raise RuntimeValidationError("task: is not persisted in this project.")
            if stored.fingerprint != task.fingerprint or stored.task_id != task.task_id:
                raise RuntimeValidationError(
                    "task: persisted task does not match; refusing."
                )
            return stored
        if isinstance(task, BoundedTask):
            if task.project_id != self.project_id:
                raise RuntimeValidationError("task: must belong to this project.")
            matches = [row for row in self.list_tasks() if row.fingerprint == task.fingerprint()]
            if not matches:
                raise RuntimeValidationError(
                    "task: must be recorded before a proposal is recorded."
                )
            if len(matches) > 1:
                raise RuntimeValidationError(
                    "task: matches more than one persisted task; pass the specific "
                    "persisted task record."
                )
            return matches[0]
        raise RuntimeValidationError(
            "task: must be a persisted RuntimeTaskRecord or a BoundedTask."
        )

    def _validate_proposal_row(self, row: sqlite3.Row) -> RuntimeProposalRecord:
        """Revalidate a proposal row and re-derive its provenance from its task."""
        if row["project_id"] != self.project_id:
            raise RuntimeCorruptionError("proposal: foreign project row.")
        _require_utc_timestamp(row["created_at_utc"], "created_at_utc")
        canonical = bytes(row["canonical"])
        if _sha256_hex(canonical) != row["stored_digest"]:
            raise RuntimeCorruptionError("proposal: stored digest mismatch.")
        try:
            parsed = json.loads(canonical.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise RuntimeCorruptionError(
                "proposal: canonical bytes are not decodable JSON."
            ) from exc
        if not isinstance(parsed, dict) or parsed.get("kind") != "proposal":
            raise RuntimeCorruptionError("proposal: canonical bytes are not a proposal.")
        task_row = self._conn().execute(
            "SELECT * FROM runtime_bounded_task WHERE project_id = ? AND task_id = ?",
            (self.project_id, row["task_id"]),
        ).fetchone()
        if task_row is None:
            raise RuntimeCorruptionError("proposal: task link row is missing.")
        task = self._validate_task_row(task_row)
        expected_record_id = _scoped_id(
            "prop", self.project_id, task.task_id + "\x00" + row["proposal_id"]
        )
        if row["record_id"] != expected_record_id:
            raise RuntimeCorruptionError("proposal: record id is not task-scoped.")
        # Re-derive the proposal from the persisted task + its content, so a
        # tampered payload/provenance/digest cannot survive a read.
        try:
            bounded = BoundedTask.from_mapping(
                json.loads(task.canonical.decode("utf-8")),
                configuration=self._config_role_configuration(task),
            )
            derived = submit_proposal(
                bounded, {"content": parsed.get("content")},
                configuration=self._config_role_configuration(task),
            )
        except (RecordValidationError, ValueError) as exc:
            raise RuntimeCorruptionError(
                "proposal: cannot re-derive the proposal from its task."
            ) from exc
        drift = (
            derived.proposal_id != row["proposal_id"]
            or derived.role != row["role"]
            or derived.model.provider_id != row["provider_id"]
            or derived.model.model_id != row["model_id"]
            or derived.model.variant != row["variant"]
            or derived.target["artifact_id"] != row["target_artifact_id"]
            or derived.target["revision_id"] != row["target_revision_id"]
            or derived.task_digest != row["task_digest"]
            or derived.input_digest != row["input_digest"]
            or derived.payload_digest != row["payload_digest"]
        )
        if drift:
            raise RuntimeCorruptionError(
                "proposal: re-derived provenance does not match the stored row."
            )
        return RuntimeProposalRecord(
            project_id=row["project_id"],
            record_id=row["record_id"],
            proposal_id=row["proposal_id"],
            task_id=row["task_id"],
            role=row["role"],
            provider_id=row["provider_id"],
            model_id=row["model_id"],
            variant=row["variant"],
            target=MappingProxyType(
                {
                    "project_id": self.project_id,
                    "artifact_id": row["target_artifact_id"],
                    "revision_id": row["target_revision_id"],
                }
            ),
            task_digest=row["task_digest"],
            input_digest=row["input_digest"],
            payload_digest=row["payload_digest"],
            stored_digest=row["stored_digest"],
            created_at_utc=row["created_at_utc"],
            canonical=canonical,
        )

    def _config_role_configuration(self, task: RuntimeTaskRecord) -> RoleConfiguration:
        config_row = self._config_row_by_digest(task.config_digest)
        if config_row is None:
            raise RuntimeCorruptionError("task: config snapshot is missing.")
        return self._validate_config_row(config_row).role_configuration()

    def get_proposal(self, record_id: str) -> RuntimeProposalRecord | None:
        record_id = validate_identifier(record_id, "record_id")
        row = self._conn().execute(
            "SELECT * FROM runtime_proposal WHERE project_id = ? AND record_id = ?",
            (self.project_id, record_id),
        ).fetchone()
        return None if row is None else self._validate_proposal_row(row)

    def get_proposal_for(
        self, task_id: str, proposal_id: str
    ) -> RuntimeProposalRecord | None:
        task_id = validate_identifier(task_id, "task_id")
        proposal_id = _bare_hex(proposal_id, "proposal_id")
        row = self._proposal_row_for(task_id, proposal_id)
        return None if row is None else self._validate_proposal_row(row)

    def verify_proposal(self, record_id: str) -> bool:
        """Convenience probe: ``True`` iff the proposal exists and revalidates.

        The primary read (:meth:`get_proposal`) **raises**
        :class:`RuntimeCorruptionError` on a corrupt row; this probe only collapses
        a missing/corrupt row to ``False``.
        """
        try:
            return self.get_proposal(record_id) is not None
        except RuntimeCorruptionError:
            return False

    def proposals_for_task(self, task_id: str) -> list[RuntimeProposalRecord]:
        task_id = validate_identifier(task_id, "task_id")
        rows = self._conn().execute(
            "SELECT * FROM runtime_proposal WHERE project_id = ? AND task_id = ? "
            "ORDER BY rowid",
            (self.project_id, task_id),
        ).fetchall()
        return [self._validate_proposal_row(row) for row in rows]

    def list_proposals(self) -> list[RuntimeProposalRecord]:
        rows = self._conn().execute(
            "SELECT * FROM runtime_proposal WHERE project_id = ? ORDER BY rowid",
            (self.project_id,),
        ).fetchall()
        return [self._validate_proposal_row(row) for row in rows]

    def _proposal_row_for(self, task_id: str, proposal_id: str) -> sqlite3.Row | None:
        return self._conn().execute(
            "SELECT * FROM runtime_proposal WHERE project_id = ? AND task_id = ? "
            "AND proposal_id = ?",
            (self.project_id, task_id, proposal_id),
        ).fetchone()

    # -- whole-store integrity (shared with a later backup verifier) --------- #
    def verify_all_integrity(self) -> list[str]:
        """Return a list of integrity problems across every runtime row (empty = ok).

        Exported so a later backup verifier can reuse this instead of duplicating
        the structural/hash/link revalidation. Each entry is a short, static,
        non-secret description.
        """
        return self._integrity_problems()

    def _integrity_problems(self) -> list[str]:
        problems: list[str] = []
        checks = (
            ("runtime_config_snapshot", self._validate_config_row),
            ("runtime_session_mapping", self._validate_session_row),
            ("runtime_bounded_task", self._validate_task_row),
            ("runtime_proposal", self._validate_proposal_row),
        )
        for table, validator in checks:
            rows = self._conn().execute(
                f"SELECT * FROM {table} WHERE project_id = ? ORDER BY rowid",
                (self.project_id,),
            ).fetchall()
            for row in rows:
                try:
                    validator(row)
                except RuntimeRepositoryError as exc:
                    problems.append(f"{table}: {exc}")
        return problems


#: Alias so a later worker facade may import either name.
AgentRuntimeRepository = RuntimeRepository


def store_integrity_problems(
    conn: sqlite3.Connection, project_id: str
) -> list[str]:
    """Revalidate every runtime row for ``project_id`` on ``conn`` (read-only).

    Reusable by a later backup verifier. Returns a list of static problem
    descriptions (empty when every row is structurally intact and internally
    consistent). The connection is never written to.
    """
    repo = RuntimeRepository(_ReadOnlyStoreView(conn, project_id))
    return repo.verify_all_integrity()


def verify_store_integrity(conn: sqlite3.Connection, project_id: str) -> bool:
    """Return ``True`` iff every runtime row for ``project_id`` revalidates."""
    return not store_integrity_problems(conn, project_id)
