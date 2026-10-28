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
* **Immutable rows + recomputed digests.** ``schema`` migration 5 pins every
  runtime row immutable at the database (never ``UPDATE``d/``DELETE``d), and the
  repository recomputes the SHA-256 of the exact canonical bytes it wrote, so a
  tampered row is detected on read rather than trusted.
* **Exact, idempotent bounded tasks keyed by ``request_id``.** A repeated request
  key with the *same* task returns the historical row; a *divergent* task under
  the same key raises :class:`RuntimeConflictError`. The full trusted
  ``BoundedTask`` fingerprint (role config, exact target, input pins + digests,
  scope, prompt version, size/step limits) is persisted as canonical bytes.
* **Fail-closed reference verification.** Recording a **new** task requires every
  target/input revision to already exist in this project, to match the pinned
  digest/current rights appropriate to *storage* (never a hosted-transfer grant),
  and the task target to be the artifact's **current** revision; a missing,
  foreign, digest-mismatched, superseded, or stale reference is refused before
  any row is written. A historical task stays readable/auditable even after its
  target moves on.
* **Non-authorizing proposals.** :meth:`RuntimeRepository.record_proposal`
  accepts a **trusted, already-persisted** task plus a payload and calls the
  integrated :func:`review_article_agent.runtime.roles.submit_proposal`, so the
  model provenance and every digest are **derived anew** — an in-band payload
  ``actor``/``id``/``digest`` is never trusted. A proposal is stored in its own
  table and is **never** written into the artifact/approval/scientific state.
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
object exposes an ``as_mapping()`` (or ``to_mapping()``) JSON-safe view.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
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
    "RuntimeProposalRecord",
    "RuntimeRepository",
    "RuntimeRepositoryError",
    "RuntimeSessionMapping",
    "RuntimeTaskRecord",
    "RuntimeValidationError",
    "validate_version_set",
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
        raise RuntimeValidationError(
            f"{field}: must be a lowercase hex SHA-256 digest"
        )
    return text


def _require_nonempty_str(value: object, field: str) -> str:
    if not isinstance(value, str) or value == "" or value != value.strip():
        raise RuntimeValidationError(
            f"{field}: must be non-empty with no leading/trailing whitespace"
        )
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise RuntimeValidationError(f"{field}: must not contain control characters")
    return value


def _require_url_safe(
    value: object, field: str, *, pattern: re.Pattern[str] = _URLSAFE_RE
) -> str:
    """Validate one URL-safe, length-bounded opaque identifier."""
    if not isinstance(value, str) or value == "":
        raise RuntimeValidationError(f"{field}: must be a non-empty string")
    if len(value) > MAX_URL_ID_LENGTH:
        raise RuntimeValidationError(f"{field}: is too long")
    if any(ch.isspace() for ch in value) or pattern.fullmatch(value) is None:
        raise RuntimeValidationError(
            f"{field}: must be a URL-safe opaque identifier"
        )
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
    """One immutable configuration-snapshot row (as stored).

    ``config_version`` is the monotonic per-project snapshot ordinal and
    ``config_digest`` is the recomputed SHA-256 of the exact canonical bytes
    stored in ``canonical`` — never a caller-supplied or fabricated value.
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
    """One immutable persisted bounded task (as stored)."""

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
    """One immutable, non-authorizing persisted proposal (as stored)."""

    project_id: str
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


def _version_text(value: object, field: str) -> str:
    """Validate a non-empty version string, as a :class:`RecordValidationError`."""
    try:
        return _require_nonempty_str(value, field)
    except RuntimeValidationError as exc:
        raise RecordValidationError(str(exc)) from exc


def _version_digest(value: object, field: str) -> str:
    """Validate a bare hex SHA-256, as a :class:`RecordValidationError`."""
    try:
        return _bare_hex(value, field)
    except RuntimeValidationError as exc:
        raise RecordValidationError(str(exc)) from exc


def _normalize_version_slot(value: object, name: str) -> Mapping[str, str]:
    """Validate one ``VersionSlot`` (``{state, ref?}``); never invent a version."""
    if not isinstance(value, Mapping):
        raise RecordValidationError(f"versions.{name}: must be an object")
    for key in value:
        if not isinstance(key, str) or key not in {"state", "ref"}:
            raise RecordValidationError(f"versions.{name}: unknown field {key!r}")
    state = value.get("state")
    if state not in VERSION_SLOT_STATES:
        raise RecordValidationError(
            f"versions.{name}.state: {state!r} is not one of "
            f"{list(VERSION_SLOT_STATES)}"
        )
    if state == "known":
        ref = value.get("ref")
        if not isinstance(ref, Mapping):
            raise RecordValidationError(
                f"versions.{name}: a known slot requires a versioned ref"
            )
        for key in ref:
            if not isinstance(key, str) or key not in {"version", "digest"}:
                raise RecordValidationError(
                    f"versions.{name}.ref: unknown field {key!r}"
                )
        if "version" not in ref or "digest" not in ref:
            raise RecordValidationError(
                f"versions.{name}.ref: must carry version and digest"
            )
        version = _version_text(ref["version"], f"versions.{name}.ref.version")
        digest = _version_digest(ref["digest"], f"versions.{name}.ref.digest")
        return MappingProxyType({"state": "known", "version": version, "digest": digest})
    if "ref" in value:
        raise RecordValidationError(
            f"versions.{name}: a ref is permitted only when the slot is known"
        )
    return MappingProxyType({"state": state})


def validate_version_set(
    value: object, *, consumed: Iterable[str] = ()
) -> dict[str, Any]:
    """Validate a persisted ``VersionSet`` (C02 §6.1) and return its normalized form.

    ``consumed`` names the slots the stage actually consumed (e.g.
    ``("model", "prompt", "adapter")`` for a Jev/typed-decision stage); each named
    slot MUST be ``known`` (an unknown/missing applied version is refused rather
    than fabricated). For a ``human_authored`` set the ``model``/``prompt`` slots
    MUST be ``not_applicable`` (a human-only approval never invents a model id),
    and no slot may be named as consumed.

    Never fabricates a version or digest; an unrecognized enum literal, a missing
    mandatory field, or a digest-shaped but invalid value fails closed with
    :class:`~review_article_agent.domain.records.RecordValidationError`.
    """
    if not isinstance(value, Mapping):
        raise RecordValidationError("versions: must be an object")
    for key in value:
        if not isinstance(key, str) or key not in _VERSION_SET_KEYS:
            raise RecordValidationError(f"versions: unknown field {key!r}")
    missing = sorted(_VERSION_SET_KEYS - set(value))
    if missing:
        raise RecordValidationError(
            f"versions: missing field(s): {', '.join(missing)}"
        )
    context_kind = value["context_kind"]
    if context_kind not in CONTEXT_KINDS:
        raise RecordValidationError(
            f"versions.context_kind: {context_kind!r} is not one of "
            f"{list(CONTEXT_KINDS)}"
        )
    input_digest = _version_digest(value["input_digest"], "versions.input_digest")
    route = value["route"]
    if route not in VERSION_ROUTES:
        raise RecordValidationError(
            f"versions.route: {route!r} is not one of {list(VERSION_ROUTES)}"
        )
    slots = {
        name: _normalize_version_slot(value[name], name) for name in VERSION_SLOT_NAMES
    }
    if context_kind == "human_authored":
        for name in ("model", "prompt"):
            if slots[name]["state"] != "not_applicable":
                raise RecordValidationError(
                    f"versions.{name}: a human_authored set must be not_applicable"
                )
    else:
        for name in ("model", "prompt"):
            if slots[name]["state"] != "known":
                raise RecordValidationError(
                    f"versions.{name}: a {context_kind} set must be known"
                )
    consumed_names = list(consumed)
    if context_kind == "human_authored" and consumed_names:
        raise RecordValidationError(
            "versions: a human_authored set consumes no versioned slot"
        )
    for name in consumed_names:
        if name not in VERSION_SLOT_NAMES:
            raise RecordValidationError(
                f"versions: {name!r} is not a named version slot"
            )
        if slots[name]["state"] != "known":
            raise RecordValidationError(
                f"versions.{name}: a consumed slot must be known, not "
                f"{slots[name]['state']!r}"
            )
    normalized: dict[str, Any] = {
        "context_kind": context_kind,
        "input_digest": input_digest,
        "route": route,
    }
    for name in VERSION_SLOT_NAMES:
        normalized[name] = dict(slots[name])
    return normalized


class PureJsonVersionSet:
    """A validated, immutable ``VersionSet`` (C02 §6.1) for later decision records.

    Pure: no I/O, no model call. Construct from a JSON mapping with
    :meth:`from_mapping` (optionally naming the ``consumed`` slots) and read it
    back with :meth:`as_mapping`.
    """

    __slots__ = ("_mapping",)

    def __init__(self, mapping: Mapping[str, Any]) -> None:
        # A private copy so a later caller mutation cannot change this value.
        self._mapping = json.loads(json.dumps(mapping))

    @classmethod
    def from_mapping(
        cls, value: object, *, consumed: Iterable[str] = ()
    ) -> "PureJsonVersionSet":
        return cls(validate_version_set(value, consumed=consumed))

    @property
    def context_kind(self) -> str:
        return self._mapping["context_kind"]

    @property
    def input_digest(self) -> str:
        return self._mapping["input_digest"]

    @property
    def route(self) -> str:
        return self._mapping["route"]

    def slot(self, name: str) -> dict[str, str]:
        if name not in VERSION_SLOT_NAMES:
            raise RecordValidationError(f"versions: {name!r} is not a named version slot")
        return dict(self._mapping[name])

    def as_mapping(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._mapping))


# --------------------------------------------------------------------------- #
# Repository
# --------------------------------------------------------------------------- #
class RuntimeRepository:
    """Immutable runtime mappings over an already-verified :class:`ProjectStore`.

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
        idempotent (returns the existing row), so a repeated call never creates a
        second snapshot for the same bytes.
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
            return existing
        if snapshot_id is None:
            snapshot_id = "rcfg_" + uuid.uuid4().hex
        snapshot_id = validate_identifier(snapshot_id, "snapshot_id")
        created = (
            _utc_now()
            if created_at_utc is None
            else _require_nonempty_str(created_at_utc, "created_at_utc")
        )
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
        return stored

    def get_configuration(self, snapshot_id: str) -> RuntimeConfigSnapshot | None:
        snapshot_id = validate_identifier(snapshot_id, "snapshot_id")
        row = self._conn().execute(
            "SELECT * FROM runtime_config_snapshot WHERE project_id = ? AND "
            "snapshot_id = ?",
            (self.project_id, snapshot_id),
        ).fetchone()
        return None if row is None else self._row_to_config(row)

    def get_configuration_by_digest(
        self, config_digest: str
    ) -> RuntimeConfigSnapshot | None:
        digest = _bare_hex(config_digest, "config_digest")
        row = self._config_row_by_digest(digest)
        return None if row is None else self._row_to_config(row)

    def list_configurations(self) -> list[RuntimeConfigSnapshot]:
        rows = self._conn().execute(
            "SELECT * FROM runtime_config_snapshot WHERE project_id = ? "
            "ORDER BY config_version",
            (self.project_id,),
        ).fetchall()
        return [self._row_to_config(row) for row in rows]

    def verify_configuration(self, snapshot_id: str) -> bool:
        """Return ``True`` iff the stored snapshot's bytes still hash to its digest."""
        snapshot = self.get_configuration(snapshot_id)
        if snapshot is None:
            raise RuntimeValidationError(
                f"configuration snapshot {snapshot_id!r} does not exist"
            )
        return _sha256_hex(snapshot.canonical) == snapshot.config_digest

    def _config_row_by_digest(self, digest: str) -> sqlite3.Row | None:
        return self._conn().execute(
            "SELECT * FROM runtime_config_snapshot WHERE project_id = ? AND "
            "config_digest = ?",
            (self.project_id, digest),
        ).fetchone()

    @staticmethod
    def _row_to_config(row: sqlite3.Row) -> RuntimeConfigSnapshot:
        return RuntimeConfigSnapshot(
            project_id=row["project_id"],
            snapshot_id=row["snapshot_id"],
            config_version=row["config_version"],
            config_digest=row["config_digest"],
            created_at_utc=row["created_at_utc"],
            canonical=bytes(row["canonical"]),
        )

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
            raise RuntimeValidationError(
                "role: must name a known scientific role"
            )
        model_ref = (
            model if isinstance(model, ModelRef) else ModelRef.from_mapping(model)
        )
        if not isinstance(configuration, RoleConfiguration):
            raise RuntimeValidationError(
                "configuration: must be a validated RoleConfiguration"
            )
        configured = configuration.for_role(role).model
        if (
            model_ref.provider_id,
            model_ref.model_id,
            model_ref.variant,
        ) != (
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
            else _require_nonempty_str(created_at_utc, "created_at_utc")
        )

        with self._store.transaction():
            config_row = self._ensure_configuration(
                configuration, snapshot_id=None, created_at_utc=None
            )
            existing = self._session_row_by_session(session_id)
            if existing is not None:
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
                return self._row_to_session(existing)
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

    def get_session_mapping(self, mapping_id: str) -> RuntimeSessionMapping | None:
        mapping_id = validate_identifier(mapping_id, "mapping_id")
        row = self._conn().execute(
            "SELECT * FROM runtime_session_mapping WHERE project_id = ? AND "
            "mapping_id = ?",
            (self.project_id, mapping_id),
        ).fetchone()
        return None if row is None else self._row_to_session(row)

    def get_session_mapping_for(self, session_id: str) -> RuntimeSessionMapping | None:
        session_id = _require_session_id(session_id)
        row = self._session_row_by_session(session_id)
        return None if row is None else self._row_to_session(row)

    def list_session_mappings(self) -> list[RuntimeSessionMapping]:
        rows = self._conn().execute(
            "SELECT * FROM runtime_session_mapping WHERE project_id = ? ORDER BY rowid",
            (self.project_id,),
        ).fetchall()
        return [self._row_to_session(row) for row in rows]

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

    @staticmethod
    def _row_to_session(row: sqlite3.Row) -> RuntimeSessionMapping:
        return RuntimeSessionMapping(
            project_id=row["project_id"],
            mapping_id=row["mapping_id"],
            service_instance_id=row["service_instance_id"],
            opencode_project_id=row["opencode_project_id"],
            clean_location=row["clean_location"],
            session_id=row["session_id"],
            role=row["role"],
            provider_id=row["provider_id"],
            model_id=row["model_id"],
            variant=row["variant"],
            config_digest=row["config_digest"],
            config_snapshot_id=row["config_snapshot_id"],
            created_at_utc=row["created_at_utc"],
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
        historical row (even after the target has moved on); a *divergent* task
        under the same key raises :class:`RuntimeConflictError`. Recording a
        **new** task verifies, before any write, that the exact target and every
        input revision exist in this project, match the pinned digest and
        current storage-appropriate rights, and that the target is the artifact's
        **current** revision — a missing, foreign, digest-mismatched,
        superseded-rights, or stale target/input fails closed.
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
            else _require_nonempty_str(created_at_utc, "created_at_utc")
        )

        with self._store.transaction():
            existing = self._task_row_by_request(request_id)
            if existing is not None:
                if existing["fingerprint"] != fingerprint:
                    raise RuntimeConflictError(
                        f"request {request_id!r} was already recorded with a "
                        f"different task; refusing to create a second task."
                    )
                return self._row_to_task(existing)
            config_row = self._ensure_configuration(
                configuration, snapshot_id=None, created_at_utc=None
            )
            self._verify_task_refs(task)
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

    def _verify_task_refs(self, task: BoundedTask) -> None:
        """Fail-closed existence/digest/rights/currentness checks for a new task."""
        self._verify_target_current(task)
        for ref in task.input_refs:
            revision = self._artifacts.get_revision(ref["artifact_id"], ref["revision_id"])
            if revision is None:
                raise RuntimeValidationError(
                    f"input ref {ref['artifact_id']!r}/{ref['revision_id']!r} does "
                    f"not exist in this project."
                )
            if _bare_hex(revision.content_sha256, "content_sha256") != ref["digest"]:
                raise RuntimeValidationError(
                    f"input ref {ref['artifact_id']!r}/{ref['revision_id']!r} digest "
                    f"does not match the stored revision."
                )
            self._verify_storage_rights(revision)

    def _verify_target_current(self, task: BoundedTask) -> None:
        target = task.target
        revision = self._artifacts.get_revision(
            target["artifact_id"], target["revision_id"]
        )
        if revision is None:
            raise RuntimeValidationError(
                f"target {target['artifact_id']!r}/{target['revision_id']!r} does "
                f"not exist in this project."
            )
        current = self._artifacts.current_revision(target["artifact_id"])
        if current is None or current.revision_id != target["revision_id"]:
            current_id = None if current is None else current.revision_id
            raise RuntimeValidationError(
                f"target {target['revision_id']!r} is not the current revision of "
                f"{target['artifact_id']!r} (current {current_id!r}); refusing a "
                f"stale/superseded target."
            )
        self._verify_storage_rights(revision)

    def _verify_storage_rights(self, revision) -> None:
        """Verify the revision's pinned rights record is current and storage-allowed.

        Only **storage** (not redistribution/hosted transfer) is required: a
        hosted-model transfer grant is deliberately **not** required or checked
        here. A superseded rights record (a newer ``record_revision`` for the same
        ``rights_id`` exists) fails closed.
        """
        rights_ref = revision.rights_ref
        row = self._conn().execute(
            "SELECT MAX(record_revision) AS newest FROM rights_record "
            "WHERE project_id = ? AND rights_id = ?",
            (self.project_id, rights_ref.rights_id),
        ).fetchone()
        newest = row["newest"] if row is not None else None
        if newest is not None and rights_ref.record_revision < newest:
            raise RuntimeValidationError(
                f"rights record {rights_ref.rights_id!r} revision "
                f"{rights_ref.record_revision} is superseded by {newest}; refusing."
            )
        record = self._artifacts.get_rights_record(rights_ref)
        if record is None:
            raise RuntimeValidationError(
                f"rights record {rights_ref.as_dict()!r} is missing."
            )
        validated = validate_rights_record(record, self.project_id)
        require_storage_allowed(validated)

    def get_task(self, task_id: str) -> RuntimeTaskRecord | None:
        task_id = validate_identifier(task_id, "task_id")
        row = self._conn().execute(
            "SELECT * FROM runtime_bounded_task WHERE project_id = ? AND task_id = ?",
            (self.project_id, task_id),
        ).fetchone()
        return None if row is None else self._row_to_task(row)

    def get_task_by_request(self, request_id: str) -> RuntimeTaskRecord | None:
        request_id = validate_identifier(request_id, "request_id")
        row = self._task_row_by_request(request_id)
        return None if row is None else self._row_to_task(row)

    def list_tasks(self) -> list[RuntimeTaskRecord]:
        rows = self._conn().execute(
            "SELECT * FROM runtime_bounded_task WHERE project_id = ? ORDER BY rowid",
            (self.project_id,),
        ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def _task_row_by_request(self, request_id: str) -> sqlite3.Row | None:
        return self._conn().execute(
            "SELECT * FROM runtime_bounded_task WHERE project_id = ? AND request_id = ?",
            (self.project_id, request_id),
        ).fetchone()

    def _row_to_task(self, row: sqlite3.Row) -> RuntimeTaskRecord:
        inputs = self._conn().execute(
            "SELECT artifact_id, revision_id, digest FROM runtime_task_input "
            "WHERE project_id = ? AND task_id = ? ORDER BY ordinal",
            (self.project_id, row["task_id"]),
        ).fetchall()
        input_refs = tuple(
            MappingProxyType(
                {
                    "project_id": self.project_id,
                    "artifact_id": entry["artifact_id"],
                    "revision_id": entry["revision_id"],
                    "digest": entry["digest"],
                }
            )
            for entry in inputs
        )
        return RuntimeTaskRecord(
            project_id=row["project_id"],
            task_id=row["task_id"],
            request_id=row["request_id"],
            fingerprint=row["fingerprint"],
            role=row["role"],
            config_digest=row["config_digest"],
            config_snapshot_id=row["config_snapshot_id"],
            target=MappingProxyType(
                {
                    "project_id": self.project_id,
                    "artifact_id": row["target_artifact_id"],
                    "revision_id": row["target_revision_id"],
                }
            ),
            input_refs=input_refs,
            prompt_version=row["prompt_version"],
            size_limit=row["size_limit"],
            step_limit=row["step_limit"],
            created_at_utc=row["created_at_utc"],
            canonical=bytes(row["canonical"]),
        )

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
        bare :class:`BoundedTask` is accepted only when its fingerprint matches a
        persisted task). The proposal is derived by
        :func:`review_article_agent.runtime.roles.submit_proposal` from the task's
        **trusted** role configuration and the payload's ``{"content": ...}``
        envelope; an in-band ``actor``/``proposal_id``/digest field in the payload
        is rejected, never trusted. A repeat of the same task+payload is idempotent
        (same derived ``proposal_id``); a divergent record under the same id raises
        :class:`RuntimeConflictError`.
        """
        if not isinstance(configuration, RoleConfiguration):
            raise RuntimeValidationError(
                "configuration: must be a validated RoleConfiguration"
            )
        record = self._resolve_persisted_task(task)
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
        proposal = submit_proposal(bounded, payload, configuration=configuration)
        canonical = canonical_json(proposal.to_mapping())
        stored_digest = _sha256_hex(canonical)
        created = (
            _utc_now()
            if created_at_utc is None
            else _require_nonempty_str(created_at_utc, "created_at_utc")
        )

        with self._store.transaction():
            existing = self._proposal_row(proposal.proposal_id)
            if existing is not None:
                if bytes(existing["canonical"]) != canonical:
                    raise RuntimeConflictError(
                        f"proposal {proposal.proposal_id!r} already exists with "
                        f"different content; refusing a divergent proposal."
                    )
                return self._row_to_proposal(existing)
            self._conn().execute(
                "INSERT INTO runtime_proposal "
                "(project_id, proposal_id, task_id, role, provider_id, model_id, "
                " variant, target_artifact_id, target_revision_id, task_digest, "
                " input_digest, payload_digest, stored_digest, canonical, "
                " created_at_utc) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.project_id,
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
        stored = self.get_proposal(proposal.proposal_id)
        assert stored is not None  # just committed
        return stored

    def _resolve_persisted_task(
        self, task: RuntimeTaskRecord | BoundedTask
    ) -> RuntimeTaskRecord:
        if isinstance(task, RuntimeTaskRecord):
            if task.project_id != self.project_id:
                raise RuntimeValidationError(
                    "task: does not belong to this project."
                )
            stored = self.get_task(task.task_id)
            if stored is None:
                raise RuntimeValidationError(
                    "task: is not persisted in this project."
                )
            if stored.fingerprint != task.fingerprint:
                raise RuntimeValidationError(
                    "task: persisted fingerprint does not match; refusing."
                )
            return stored
        if isinstance(task, BoundedTask):
            if task.project_id != self.project_id:
                raise RuntimeValidationError("task: must belong to this project.")
            for row in self.list_tasks():
                if row.fingerprint == task.fingerprint():
                    return row
            raise RuntimeValidationError(
                "task: must be recorded before a proposal is recorded."
            )
        raise RuntimeValidationError(
            "task: must be a persisted RuntimeTaskRecord or a BoundedTask."
        )

    def get_proposal(self, proposal_id: str) -> RuntimeProposalRecord | None:
        proposal_id = _bare_hex(proposal_id, "proposal_id")
        row = self._proposal_row(proposal_id)
        return None if row is None else self._row_to_proposal(row)

    def proposals_for_task(self, task_id: str) -> list[RuntimeProposalRecord]:
        task_id = validate_identifier(task_id, "task_id")
        rows = self._conn().execute(
            "SELECT * FROM runtime_proposal WHERE project_id = ? AND task_id = ? "
            "ORDER BY rowid",
            (self.project_id, task_id),
        ).fetchall()
        return [self._row_to_proposal(row) for row in rows]

    def list_proposals(self) -> list[RuntimeProposalRecord]:
        rows = self._conn().execute(
            "SELECT * FROM runtime_proposal WHERE project_id = ? ORDER BY rowid",
            (self.project_id,),
        ).fetchall()
        return [self._row_to_proposal(row) for row in rows]

    def verify_proposal(self, proposal_id: str) -> bool:
        """Return ``True`` iff the stored proposal bytes still hash to its digest."""
        proposal = self.get_proposal(proposal_id)
        if proposal is None:
            raise RuntimeValidationError(
                f"proposal {proposal_id!r} does not exist"
            )
        return _sha256_hex(proposal.canonical) == proposal.stored_digest

    def _proposal_row(self, proposal_id: str) -> sqlite3.Row | None:
        return self._conn().execute(
            "SELECT * FROM runtime_proposal WHERE project_id = ? AND proposal_id = ?",
            (self.project_id, proposal_id),
        ).fetchone()

    @staticmethod
    def _row_to_proposal(row: sqlite3.Row) -> RuntimeProposalRecord:
        return RuntimeProposalRecord(
            project_id=row["project_id"],
            proposal_id=row["proposal_id"],
            task_id=row["task_id"],
            role=row["role"],
            provider_id=row["provider_id"],
            model_id=row["model_id"],
            variant=row["variant"],
            target=MappingProxyType(
                {
                    "project_id": row["project_id"],
                    "artifact_id": row["target_artifact_id"],
                    "revision_id": row["target_revision_id"],
                }
            ),
            task_digest=row["task_digest"],
            input_digest=row["input_digest"],
            payload_digest=row["payload_digest"],
            stored_digest=row["stored_digest"],
            created_at_utc=row["created_at_utc"],
            canonical=bytes(row["canonical"]),
        )


#: Alias so a later worker facade may import either name.
AgentRuntimeRepository = RuntimeRepository
