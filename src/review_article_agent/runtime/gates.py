"""Read-only preparation and revalidation of an exact human-gate snapshot.

This module is the **format/pin validation** layer that a later CLI owner-auth
step and a later approval repository will point at. It has one job: given a
verified :class:`~review_article_agent.storage.store.ProjectStore` and an exact
target revision, it captures the target plus its **full transitive dependency
closure** as an immutable, canonically-digested
:class:`PreparedGate` — and it can later **revalidate** that prepared snapshot
against the store, atomically and read-only.

Authority
---------

* ``docs/implementation/04-agent-runtime.md`` deliverable 5 ("App-owned human
  approvals presented through CLI") and acceptance criteria 1, 2, and 4.
* ``docs/contracts/02-gates-and-runtime.md`` §1.3 (authority provenance is
  out-of-band; a payload cannot mint authority), §2.1/§2.2 (revision-specific
  approvals over an exact, immutable dependency snapshot), §3 (stale vs
  invalidated vs superseded), and §6.1 (``VersionSet``).
* Baseline "Gates and scientific invariants"
  (``docs/specification.md`` lines 54-55): approvals bind to immutable artifact
  revisions and dependencies — never filenames or generic tool permissions — and
  material edits invalidate affected checks.

What this module **is**
-----------------------

* :func:`prepare_gate_snapshot` — read-only capture of an exact
  :class:`PreparedGate`: the named target (entry 0) plus every transitively
  reachable revision, each pinned by ``{project_id, artifact_id, revision_id}``,
  its canonical content digest, its byte size, and its pinned rights key and
  rights-record digest. All entries share one project. A same-artifact
  ``version_of`` ancestor is captured as a historical ``version_lineage`` pin:
  it is verified exactly but is **never** compared against its artifact's
  current head (so honest history is not spuriously stale). Any live input that
  is not the artifact's current revision — or a pinned rights record a newer
  ``record_revision`` has superseded — blocks preparation.
* :func:`revalidate_prepared` — recomputes the exact structure from the
  prepared pins and re-checks **every** pin in one read transaction. A changed
  current target/dependency/rights is a typed ``stale``; a corrupt/missing
  digest, byte size, or rights record is a typed ``invalid``; a cross-project
  entry/target is ``denied``.

What this module **is not**
---------------------------

* It writes **nothing**. It records no approval row and mutates no table; a
  generic saved permission or proposal payload can never be converted into an
  approval here. Approval recording and the out-of-band owner confirmation are
  later tasks.
* It **creates no authority**. A :class:`PreparedGate` is inert data; the
  :class:`~review_article_agent.runtime.owner_auth.OwnerAction` it yields is an
  action *description*, not a claim of authority (C02 §1.3). A :class:`PreparedGate`
  cannot be hand-constructed (:meth:`PreparedGate._mint` is the only minter), and
  even a forged one grants nothing because :func:`revalidate_prepared`
  re-derives everything from the store.
* It performs **no** scientific validation and authorizes **no** execution or
  export. Final-release scientific validation and export remain unimplemented;
  preparing a gate for human review is not passing a scientific gate.

Reuse
-----

The exact-closure traversal deliberately **mirrors** the tested read-only
snapshot semantics of
:class:`~review_article_agent.storage.approvals.ApprovalRepository` (target
entry 0, live-wins over ancestry, transitive ``version_of`` lineage, one shared
project) using the tested
:class:`~review_article_agent.storage.artifacts.ArtifactRepository` read API,
but it is a **bounded iterative traversal of its own**: the entry bound is
enforced *during* the walk, so a pathological graph cannot be walked without
limit before the bound is noticed. It reuses the approvals module's role
vocabulary and ancestry constant so the two closures cannot drift. This module
never calls the fixture *write* path and never mutates the store.

LLM-annotated test file companion: ``tests/test_gate_snapshots.py``.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import types
from collections import deque
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator

from ..domain.errors import ReviewAgentError
from ..domain.records import (
    RecordValidationError,
    canonical_json,
    require_storage_allowed,
    validate_identifier,
    validate_ref,
    validate_rights_record,
)
from ..storage.approvals import (
    SNAPSHOT_ENTRY_ROLES,
    VERSION_LINEAGE_ROLE,
    FixtureSnapshotEntry,
    _ANCESTRY_ROLE,
    _KIND_TO_ROLE,
)
from ..storage.artifacts import ArtifactRepository, sha256_digest
from .owner_auth import (
    APPROVAL_DECISIONS,
    APPROVAL_GATE_KINDS,
    MAX_ACTION_IDENTIFIER_CHARS,
    MAX_RATIONALE_CHARS,
    OwnerAction,
)

__all__ = [
    "HUMAN_GATE_SNAPSHOT_SCHEMA_VERSION",
    "HUMAN_GATE_CONTEXT_KIND",
    "VERSION_SLOT_NOT_APPLICABLE",
    "HUMAN_GATE_REVALIDATION_STATUSES",
    "MAX_GATE_SNAPSHOT_ENTRIES",
    "GateSnapshotError",
    "GateDefinitionError",
    "GateTargetError",
    "GateDependencyError",
    "GateContentIntegrityError",
    "GateRightsError",
    "GateSupersededInputError",
    "PreparedGateStaleError",
    "PreparedGateInvalidError",
    "PreparedGateDeniedError",
    "PreparedSnapshotEntry",
    "PreparedGate",
    "GateRevalidation",
    "prepare_gate_snapshot",
    "revalidate_prepared",
]

#: Version of the *prepared gate snapshot representation* (independent of the
#: store schema version, which is recorded inside the snapshot).
HUMAN_GATE_SNAPSHOT_SCHEMA_VERSION = 1

#: The C02 §6.1 ``context_kind`` for a human-only decision. A human-only gate
#: sets ``model``/``prompt`` ``not_applicable`` and MUST NOT invent a model id.
HUMAN_GATE_CONTEXT_KIND = "human_authored"

#: C02 §6.1 ``VersionSlot.state`` used for every slot this preparation does not
#: need to name: no fabricated version/digest is emitted.
VERSION_SLOT_NOT_APPLICABLE = "not_applicable"

#: Computed revalidation outcomes (C02 §3). Never stored — always recomputed.
HUMAN_GATE_REVALIDATION_STATUSES = ("unchanged", "stale", "invalid", "denied")

#: Bound on the size of one exact dependency closure, so a pathological graph
#: cannot be walked without limit. The traversal itself is cycle-safe and the
#: bound is enforced **during** the walk (never after a full unbounded walk).
MAX_GATE_SNAPSHOT_ENTRIES = 10000


# --------------------------------------------------------------------------- #
# Static, curated problem messages
# --------------------------------------------------------------------------- #
# These never contain a revision/artifact/rights/gate identifier, caller text, or
# source/rights bytes: a revalidation outcome (and any typed error built from it)
# is safe to surface without leaking store or caller content.
_PROBLEM_CROSS_PROJECT = (
    "the prepared snapshot belongs to a different project than the store"
)
_PROBLEM_SCHEMA_VERSION = (
    "the prepared snapshot representation version is not supported"
)
_PROBLEM_GATE_METADATA = (
    "the prepared gate identity is malformed or not one of the approved values"
)
_PROBLEM_TARGET_REFERENCE = (
    "the prepared target is not an exact same-project revision reference"
)
_PROBLEM_TARGET_INCLUSION = (
    "the prepared snapshot's first entry is not the exact target revision"
)
_PROBLEM_SNAPSHOT_ID = "the prepared snapshot id does not match its digest"
_PROBLEM_CLOSURE_MISSING = (
    "a revision in the re-derived dependency closure is missing from the store"
)
_PROBLEM_CLOSURE_MISMATCH = (
    "the prepared entries do not match the re-derived exact dependency closure"
)
_PROBLEM_CANONICAL_BYTES = (
    "the prepared canonical bytes do not match the re-derived structure"
)
_PROBLEM_DIGEST = (
    "the prepared snapshot digest does not match the re-derived structure"
)
_PROBLEM_VERSION_SET = (
    "the prepared version set does not match the re-derived closure"
)
_PROBLEM_STORE_DATA = (
    "a stored revision or rights record is malformed and cannot be verified"
)
_PROBLEM_REVISION_MISSING = "a pinned revision is missing from the store"
_PROBLEM_CONTENT_MISMATCH = (
    "a pinned revision's stored bytes do not match its recorded digest"
)
_PROBLEM_CONTENT_SIZE = (
    "a pinned revision's stored bytes do not match its recorded byte size"
)
_PROBLEM_CONTENT_CORRUPT = "a pinned revision's stored content is malformed"
_PROBLEM_RIGHTS_MISSING = "a pinned rights record is missing from the store"
_PROBLEM_RIGHTS_CORRUPT = "a pinned rights record's stored bytes are malformed"
_PROBLEM_RIGHTS_IDENTITY = (
    "a pinned rights record does not identify the pinned revision"
)
_PROBLEM_RIGHTS_INVALID = (
    "a pinned rights record is not a valid storage-allowed record"
)
_PROBLEM_RIGHTS_SUPERSEDED = (
    "a pinned rights record has been superseded by a newer revision"
)
_PROBLEM_LIVE_DRIFT = "a live input is not the current revision of its artifact"


# --------------------------------------------------------------------------- #
# Errors (static, curated, non-reflective)
# --------------------------------------------------------------------------- #
class GateSnapshotError(ReviewAgentError):
    """Base class for every human-gate snapshot preparation failure.

    Every message is **static and curated**: it never echoes source bytes,
    rights bytes, credentials, or the caller's raw action text.
    """


class GateDefinitionError(GateSnapshotError, ValueError):
    """The gate identity (kind/id/decision/rationale) is malformed or unapproved.

    Raised for an unrecognized or fixture ``gate_kind``, an unapproved
    ``decision``, an invalid/over-long ``gate_id``, or an empty/over-long
    ``rationale``. A generic tool permission is never a gate kind.
    """


class GateTargetError(GateSnapshotError, ValueError):
    """The prepared target is not an exact same-project revision reference."""


class GateDependencyError(GateSnapshotError):
    """A reachable revision is missing, or the closure exceeds its bound."""


class GateContentIntegrityError(GateSnapshotError):
    """A pinned revision's stored bytes disagree with its digest or byte size."""


class GateRightsError(GateSnapshotError):
    """A pinned rights record is missing, corrupt, misidentified, superseded, or
    not a valid storage-allowed record bound to the pinned revision."""


class GateSupersededInputError(GateSnapshotError):
    """A live input is not the current revision of its artifact."""


class PreparedGateStaleError(GateSnapshotError):
    """A current target/dependency/rights changed since the snapshot was prepared."""


class PreparedGateInvalidError(GateSnapshotError):
    """A stored digest, byte size, or rights record no longer matches its pin."""


class PreparedGateDeniedError(GateSnapshotError):
    """The prepared snapshot and the store disagree on project identity."""


# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PreparedSnapshotEntry:
    """One exact, immutable prepared snapshot entry.

    Pins the revision identity, the canonical content digest, the byte size, and
    the pinned rights key **and** the rights record's canonical bytes digest.
    It carries **no** source bytes and no rights bytes — only their digests.
    """

    project_id: str
    artifact_id: str
    revision_id: str
    role: str
    content_sha256: str
    byte_size: int
    rights_id: str
    rights_record_revision: int
    rights_digest: str

    def as_dict(self) -> dict[str, object]:
        """Return the canonical (C02 §2.2 shaped) snapshot entry object."""
        return {
            "project_id": self.project_id,
            "artifact_id": self.artifact_id,
            "revision_id": self.revision_id,
            "digest": self.content_sha256,
            "role": self.role,
            "byte_size": self.byte_size,
            "rights": {
                "project_id": self.project_id,
                "rights_id": self.rights_id,
                "record_revision": self.rights_record_revision,
                "digest": self.rights_digest,
            },
        }


class _MintGuard:
    """Private sentinel type for :class:`PreparedGate` construction."""


#: Private sentinel. Reaching it requires importing a private, underscore-prefixed
#: module attribute — deliberately outside both the public API and the
#: application-model path. It is **not** a security boundary against a same-UID
#: native process; the real defence is that :func:`revalidate_prepared`
#: re-derives every pin from the store.
_MINT = _MintGuard()


class PreparedGate:
    """An immutable, canonical prepared human-gate snapshot.

    Properties:

    * **Minted only here.** Its constructor demands the private :data:`_MINT`
      sentinel, so a caller cannot hand-build a validated-looking snapshot from
      a mapping, digest, boolean, or saved-permission payload. Only
      :func:`prepare_gate_snapshot` mints it, and only after reading and
      verifying the store.
    * **Immutable.** Attributes are ``__slots__``-backed and read-only; the
      target and version set are exposed through read-only mappings and the
      entries are a tuple of frozen dataclasses.
    * **Deterministic.** The canonical bytes and the snapshot id/digest bind the
      target, gate identity, decision, rationale, version set, and every entry —
      no random value and no wall-clock time is part of the hashed structure.
    * **Inert.** It authorizes nothing: it has no execution/export authority, and
      the owner action it yields is a description, not a claim of authority.
    """

    __slots__ = (
        "_project_id",
        "_target",
        "_gate_kind",
        "_gate_id",
        "_decision",
        "_rationale",
        "_snapshot_id",
        "_digest",
        "_canonical_bytes",
        "_entries",
        "_version_set",
        "_storage_schema_version",
        "_snapshot_schema_version",
    )

    def __init__(
        self,
        _guard: object,
        *,
        project_id: str,
        target: Mapping,
        gate_kind: str,
        gate_id: str,
        decision: str,
        rationale: str,
        snapshot_id: str,
        digest: str,
        canonical_bytes: bytes,
        entries: tuple[PreparedSnapshotEntry, ...],
        version_set: dict,
        storage_schema_version: int,
        snapshot_schema_version: int,
    ) -> None:
        if _guard is not _MINT:
            raise GateSnapshotError(
                "a prepared gate snapshot is minted only by prepare_gate_snapshot "
                "after it has read and verified the store; it cannot be built from "
                "a mapping, digest, boolean, or imported record."
            )
        setter = object.__setattr__
        setter(self, "_project_id", project_id)
        setter(self, "_target", types.MappingProxyType(dict(target)))
        setter(self, "_gate_kind", gate_kind)
        setter(self, "_gate_id", gate_id)
        setter(self, "_decision", decision)
        setter(self, "_rationale", rationale)
        setter(self, "_snapshot_id", snapshot_id)
        setter(self, "_digest", digest)
        setter(self, "_canonical_bytes", bytes(canonical_bytes))
        setter(self, "_entries", tuple(entries))
        setter(self, "_version_set", _deep_freeze(_deep_copy_plain(version_set)))
        setter(self, "_storage_schema_version", storage_schema_version)
        setter(self, "_snapshot_schema_version", snapshot_schema_version)

    # -- immutability --------------------------------------------------------- #
    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("PreparedGate is immutable.")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("PreparedGate is immutable.")

    def __repr__(self) -> str:
        # Only the digest — never source/rights bytes or caller text.
        return "<PreparedGate human-gate snapshot digest=" + self._digest + ">"

    @classmethod
    def _mint(cls, **kwargs: object) -> "PreparedGate":
        return cls(_MINT, **kwargs)  # type: ignore[arg-type]

    # -- read-only surface ---------------------------------------------------- #
    @property
    def project_id(self) -> str:
        return self._project_id

    @property
    def target(self) -> Mapping:
        """The exact target reference (an immutable mapping)."""
        return self._target

    @property
    def gate_kind(self) -> str:
        return self._gate_kind

    @property
    def gate_id(self) -> str:
        return self._gate_id

    @property
    def decision(self) -> str:
        return self._decision

    @property
    def rationale(self) -> str:
        return self._rationale

    @property
    def snapshot_id(self) -> str:
        return self._snapshot_id

    @property
    def digest(self) -> str:
        """Canonical lowercase hex SHA-256 of :attr:`canonical_bytes`.

        Matches :class:`~review_article_agent.runtime.owner_auth.OwnerAction`'s
        normalized ``snapshot_digest``, so the action binds this exact snapshot.
        """
        return self._digest

    @property
    def canonical_bytes(self) -> bytes:
        """The canonical JSON bytes the digest is taken over."""
        return self._canonical_bytes

    @property
    def entries(self) -> tuple[PreparedSnapshotEntry, ...]:
        return self._entries

    @property
    def version_set(self) -> Mapping:
        """The C02 §6.1 ``VersionSet`` (immutable, frozen at construction)."""
        return self._version_set

    @property
    def storage_schema_version(self) -> int:
        return self._storage_schema_version

    @property
    def snapshot_schema_version(self) -> int:
        return self._snapshot_schema_version

    @property
    def authorizes_execution(self) -> bool:
        """Always ``False``: preparing a snapshot authorizes no execution."""
        return False

    @property
    def authorizes_export(self) -> bool:
        """Always ``False``: preparing a snapshot authorizes no export."""
        return False

    # -- the owner action a later CLI confirms -------------------------------- #
    def owner_action(self) -> OwnerAction:
        """Return the exact :class:`OwnerAction` this snapshot describes.

        The action is a *description* the out-of-band owner confirmation will
        bind; it is not authority. The action's ``snapshot_digest`` is this
        snapshot's canonical digest.
        """
        return OwnerAction(
            project_id=self._project_id,
            target=dict(self._target),
            snapshot_digest=self._digest,
            gate_kind=self._gate_kind,
            gate_id=self._gate_id,
            decision=self._decision,
            rationale=self._rationale,
        )


@dataclass(frozen=True)
class GateRevalidation:
    """The outcome of re-checking one prepared snapshot against the store."""

    project_id: str
    gate_kind: str
    gate_id: str
    status: str
    prepared_digest: str
    recomputed_digest: str
    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        """``True`` iff the prepared snapshot is still exactly current."""
        return self.status == "unchanged"

    def raise_for_status(self) -> None:
        """Raise the typed error for a non-``unchanged`` outcome, else return."""
        if self.status == "unchanged":
            return
        error = {
            "stale": PreparedGateStaleError,
            "invalid": PreparedGateInvalidError,
            "denied": PreparedGateDeniedError,
        }.get(self.status, GateSnapshotError)
        raise error(
            f"the prepared gate snapshot is {self.status}; it cannot be used to "
            f"authorize a decision and must be re-prepared from current state."
        )


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _deep_copy_plain(value: object) -> object:
    """Return a plain (non-proxy) deep copy of a JSON-shaped value."""
    if isinstance(value, Mapping):
        return {key: _deep_copy_plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_deep_copy_plain(item) for item in value]
    return value


def _deep_freeze(value: object) -> object:
    """Return a read-only view of a JSON-shaped value (nested mappings frozen)."""
    if isinstance(value, Mapping):
        return types.MappingProxyType(
            {key: _deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    return value


@contextmanager
def _read_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block in one consistent read transaction, failing closed.

    Opens a deferred ``BEGIN`` **only** when the connection is not already in a
    transaction, so the pinned reads are one coherent snapshot without ever
    taking a write lock or mutating the store.

    This **fails closed**: if the store cannot open the read transaction, the
    block is never run (a snapshot read against an unpinned connection is
    refused); if the transaction cannot be committed, the block's result is
    discarded and a typed :class:`GateSnapshotError` is raised rather than
    silently trusting a read whose consistency could not be confirmed.
    """
    if conn.in_transaction:
        yield conn
        return
    try:
        conn.execute("BEGIN")
    except sqlite3.Error as exc:
        raise GateSnapshotError(
            "the store could not open a consistent read transaction; refusing to "
            "prepare or revalidate a gate snapshot from an unpinned read."
        ) from exc
    try:
        yield conn
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:  # pragma: no cover - best effort
            pass
        raise
    try:
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:  # pragma: no cover - best effort
            pass
        raise GateSnapshotError(
            "the store could not commit the read transaction; refusing to trust "
            "an unconfirmed gate snapshot read."
        ) from exc


def _project_id(store) -> str:
    project_id = getattr(store, "project_id", None)
    if not isinstance(project_id, str) or project_id == "":
        raise GateSnapshotError(
            "the store does not expose a project identity; refusing to prepare a "
            "gate snapshot."
        )
    # Bound the project identity with the same owner-action conventions the later
    # confirmation applies, before any store read.
    try:
        validate_identifier(project_id, "project_id")
    except RecordValidationError as exc:
        raise GateDefinitionError(
            "the store's project identity is not a valid identifier; refusing to "
            "prepare a gate snapshot."
        ) from exc
    if len(project_id) > MAX_ACTION_IDENTIFIER_CHARS:
        raise GateDefinitionError(
            f"the store's project identity exceeds the "
            f"{MAX_ACTION_IDENTIFIER_CHARS}-character bound; refusing to prepare a "
            "gate snapshot."
        )
    return project_id


# --------------------------------------------------------------------------- #
# Input validation (C02 §2.1 / §1.3)
# --------------------------------------------------------------------------- #
def _validate_target(target: object, project_id: str) -> dict[str, str]:
    if not isinstance(target, Mapping):
        raise GateTargetError(
            "the gate target must be an exact reference object; a generic saved "
            "permission, tool grant, or proposal payload cannot be a gate target."
        )
    try:
        ref = validate_ref(dict(target), project_id)
        # Bound every target identifier with the owner-action convention, so a
        # pathological artifact/revision id is refused before any store read and
        # can never pass here only to be rejected later by the owner action.
        for key in ("project_id", "artifact_id", "revision_id"):
            if len(ref[key]) > MAX_ACTION_IDENTIFIER_CHARS:
                raise RecordValidationError(
                    f"target.{key}: exceeds the "
                    f"{MAX_ACTION_IDENTIFIER_CHARS}-character bound"
                )
    except (RecordValidationError, TypeError, ValueError) as exc:
        raise GateTargetError(
            "the gate target is not an exact, same-project, bounded "
            "{project_id, artifact_id, revision_id} reference; it cannot be "
            "prepared."
        ) from exc
    return ref


def _validate_gate_kind(gate_kind: object) -> str:
    if gate_kind not in APPROVAL_GATE_KINDS:
        raise GateDefinitionError(
            "the gate kind is not one of the approved gate kinds; a generic tool "
            "permission or an unrecognized/fixture gate kind is refused, never "
            "coerced."
        )
    return gate_kind  # type: ignore[return-value]


def _validate_decision(decision: object) -> str:
    if decision not in APPROVAL_DECISIONS:
        raise GateDefinitionError(
            "the decision is not one of the approved human decisions."
        )
    return decision  # type: ignore[return-value]


def _validate_gate_id(gate_id: object) -> str:
    try:
        normalized = validate_identifier(gate_id, "gate_id")
    except RecordValidationError as exc:
        raise GateDefinitionError(
            "the gate id is not a valid identifier; it cannot be prepared."
        ) from exc
    if len(normalized) > MAX_ACTION_IDENTIFIER_CHARS:
        raise GateDefinitionError(
            f"the gate id exceeds the {MAX_ACTION_IDENTIFIER_CHARS}-character "
            "bound; it cannot be prepared."
        )
    return normalized


def _validate_rationale(rationale: object) -> str:
    if not isinstance(rationale, str) or rationale == "":
        raise GateDefinitionError("the rationale must be a non-empty string.")
    if len(rationale) > MAX_RATIONALE_CHARS:
        raise GateDefinitionError(
            f"the rationale exceeds the {MAX_RATIONALE_CHARS}-character bound; it "
            "cannot be prepared."
        )
    # Apply the owner-action conventions here, before any expensive store read, so
    # a rationale that the later out-of-band confirmation could not render safely
    # (whitespace-only, control/format characters, or an unencodable surrogate) is
    # refused now instead of failing after the whole closure was captured.
    if rationale.strip() == "":
        raise GateDefinitionError(
            "the rationale must contain visible text, not only whitespace; it "
            "cannot be prepared."
        )
    for ch in rationale:
        code = ord(ch)
        if (
            code < 0x20
            or code == 0x7F
            or 0x80 <= code <= 0x9F
            or 0xD800 <= code <= 0xDFFF
        ):
            raise GateDefinitionError(
                "the rationale contains a control character or an invalid Unicode "
                "surrogate; it cannot be prepared."
            )
    try:
        rationale.encode("utf-8", "strict")
    except UnicodeError as exc:  # defensive: a lone surrogate could never render
        raise GateDefinitionError(
            "the rationale cannot be encoded for display; it cannot be prepared."
        ) from exc
    return rationale


# --------------------------------------------------------------------------- #
# Exact closure + integrity
# --------------------------------------------------------------------------- #
@dataclass
class _EntryFindings:
    """Categorised problems found while re-checking one pinned entry."""

    missing: list[str] = field(default_factory=list)
    content: list[str] = field(default_factory=list)
    rights: list[str] = field(default_factory=list)
    superseded_rights: list[str] = field(default_factory=list)
    drifted_head: list[str] = field(default_factory=list)
    rights_digest: str | None = None


def _role_for_artifact(artifacts: ArtifactRepository, artifact_id: str) -> str:
    """Map an artifact kind to a snapshot entry role (approvals vocabulary)."""
    artifact = artifacts.get_artifact(artifact_id)
    kind = artifact.kind if artifact is not None else ""
    return _KIND_TO_ROLE.get(kind, "other")


def _exact_closure(store, artifacts: ArtifactRepository, target_ref: dict[str, str]) -> list:
    """Return the target plus its full transitive dependency closure.

    A **bounded iterative traversal** that mirrors the tested read-only snapshot
    semantics of
    :class:`~review_article_agent.storage.approvals.ApprovalRepository`
    (target entry 0, live-wins over ancestry, transitive ``version_of`` lineage,
    one shared project) without calling its unbounded builder: the
    :data:`MAX_GATE_SNAPSHOT_ENTRIES` bound is enforced **during** the walk, so a
    pathological graph is refused as soon as it exceeds the bound rather than
    after a full unbounded walk. The traversal is cycle-safe.
    """
    target_key = (target_ref["artifact_id"], target_ref["revision_id"])
    order: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    live_keys: set[tuple[str, str]] = {target_key}
    queue: deque[tuple[str, str]] = deque([target_key])
    try:
        while queue:
            key = queue.popleft()
            if key in seen:
                continue
            seen.add(key)
            order.append(key)
            if len(order) > MAX_GATE_SNAPSHOT_ENTRIES:
                raise GateDependencyError(
                    f"the dependency closure exceeds the "
                    f"{MAX_GATE_SNAPSHOT_ENTRIES}-entry bound; the gate snapshot "
                    "cannot be prepared."
                )
            artifact_id, revision_id = key
            if artifacts.get_revision(artifact_id, revision_id) is None:
                raise GateDependencyError(
                    "a revision reachable from the target does not exist in this "
                    "project; the gate snapshot cannot be prepared."
                )
            dependencies = sorted(
                artifacts.list_dependencies(artifact_id, revision_id),
                key=lambda d: (d.artifact_id, d.revision_id, d.role),
            )
            for dep in dependencies:
                child = (dep.artifact_id, dep.revision_id)
                if dep.role != _ANCESTRY_ROLE:
                    # Any live (non-ancestry) edge marks the child as a live
                    # input, even if it is also named as an ancestor ("live wins").
                    live_keys.add(child)
                if child not in seen:
                    queue.append(child)

        entries: list[FixtureSnapshotEntry] = []
        for key in order:
            artifact_id, revision_id = key
            revision = artifacts.get_revision(artifact_id, revision_id)
            if revision is None:  # pragma: no cover - validated during the walk
                raise GateDependencyError(
                    "a revision reachable from the target does not exist in this "
                    "project; the gate snapshot cannot be prepared."
                )
            if key == target_key:
                role = "target"
            elif key in live_keys:
                role = _role_for_artifact(artifacts, artifact_id)
            else:
                role = VERSION_LINEAGE_ROLE
            entries.append(
                FixtureSnapshotEntry(
                    project_id=store.project_id,
                    artifact_id=artifact_id,
                    revision_id=revision_id,
                    content_sha256=revision.content_sha256,
                    role=role,
                    rights_id=revision.rights_ref.rights_id,
                    rights_record_revision=revision.rights_ref.record_revision,
                    byte_size=revision.byte_size,
                )
            )
    except RecordValidationError as exc:
        # A malformed stored reference (invalid id, missing revision) fails closed
        # as a typed dependency error, never a raw validation error.
        raise GateDependencyError(
            "a revision reachable from the target does not exist or is malformed "
            "in this project; the gate snapshot cannot be prepared."
        ) from exc
    return entries


def _check_rights_pin(store, pin, findings: _EntryFindings) -> str | None:
    """Structurally verify one entry's pinned canonical rights record.

    Returns the pinned rights record's canonical digest, or ``None`` when the
    record could not be read/decoded. Appends a **static, curated** problem to
    ``findings`` (never the rights id, revision id, or any stored bytes). A
    malformed stored value — a non-bytes canonical column, undecodable or
    pathologically deep JSON, or a record the validator cannot process — is
    reported as a problem rather than raising a raw ``TypeError``/``RecursionError``.
    """
    row = store.connection.execute(
        "SELECT * FROM rights_record WHERE project_id = ? AND rights_id = ? "
        "AND record_revision = ?",
        (pin.project_id, pin.rights_id, pin.rights_record_revision),
    ).fetchone()
    if row is None:
        findings.rights.append(_PROBLEM_RIGHTS_MISSING)
        return None
    raw_canonical = row["canonical"]
    if not isinstance(raw_canonical, (bytes, bytearray, memoryview)):
        findings.rights.append(_PROBLEM_RIGHTS_CORRUPT)
        return None
    canonical = bytes(raw_canonical)
    try:
        digest = sha256_digest(canonical)
    except TypeError:  # pragma: no cover - defensive; bytes are always hashable
        findings.rights.append(_PROBLEM_RIGHTS_CORRUPT)
        return None
    if digest != row["content_sha256"]:
        findings.rights.append(_PROBLEM_RIGHTS_CORRUPT)
        return None
    try:
        record = json.loads(canonical.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError, TypeError):
        findings.rights.append(_PROBLEM_RIGHTS_CORRUPT)
        return None
    if not isinstance(record, dict):
        findings.rights.append(_PROBLEM_RIGHTS_CORRUPT)
        return None
    if (
        record.get("project_id") != pin.project_id
        or record.get("rights_id") != pin.rights_id
        or record.get("record_revision") != pin.rights_record_revision
    ):
        findings.rights.append(_PROBLEM_RIGHTS_IDENTITY)
    subject = record.get("subject")
    if not (
        isinstance(subject, dict)
        and subject.get("kind") == "source_revision"
        and subject.get("project_id") == pin.project_id
        and subject.get("artifact_id") == pin.artifact_id
        and subject.get("revision_id") == pin.revision_id
    ):
        findings.rights.append(_PROBLEM_RIGHTS_IDENTITY)
    elif (
        row["subject_kind"],
        row["subject_project_id"],
        row["subject_artifact_id"],
        row["subject_revision_id"],
    ) != ("source_revision", pin.project_id, pin.artifact_id, pin.revision_id):
        findings.rights.append(_PROBLEM_RIGHTS_IDENTITY)
    try:
        validated = validate_rights_record(record, pin.project_id)
        require_storage_allowed(validated)
    except RecordValidationError:
        findings.rights.append(_PROBLEM_RIGHTS_INVALID)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        # Deep/odd stored data must be a typed problem, never a raw crash.
        findings.rights.append(_PROBLEM_RIGHTS_CORRUPT)
    newest = store.connection.execute(
        "SELECT MAX(record_revision) AS newest FROM rights_record "
        "WHERE project_id = ? AND rights_id = ?",
        (pin.project_id, pin.rights_id),
    ).fetchone()
    if (
        newest is not None
        and newest["newest"] is not None
        and pin.rights_record_revision < newest["newest"]
    ):
        findings.superseded_rights.append(_PROBLEM_RIGHTS_SUPERSEDED)
    return digest


def _check_entry(store, artifacts: ArtifactRepository, pin) -> _EntryFindings:
    """Re-check every pin of one entry against the store (read-only).

    ``pin`` is any object exposing ``project_id``/``artifact_id``/``revision_id``/
    ``role``/``content_sha256``/``byte_size``/``rights_id``/
    ``rights_record_revision`` (both the fixture builder's entries and
    :class:`PreparedSnapshotEntry` qualify). Every problem is a **static,
    curated** message that never contains an identifier or stored bytes.
    """
    findings = _EntryFindings()
    try:
        revision = artifacts.get_revision(pin.artifact_id, pin.revision_id)
    except (RecordValidationError, TypeError, ValueError):
        findings.missing.append(_PROBLEM_REVISION_MISSING)
        return findings
    if revision is None:
        findings.missing.append(_PROBLEM_REVISION_MISSING)
        return findings
    if (
        revision.rights_ref.project_id,
        revision.rights_ref.rights_id,
        revision.rights_ref.record_revision,
    ) != (pin.project_id, pin.rights_id, pin.rights_record_revision):
        findings.rights.append(_PROBLEM_RIGHTS_IDENTITY)
    try:
        content = artifacts.read_content(pin.artifact_id, pin.revision_id)
    except RecordValidationError:
        findings.missing.append(_PROBLEM_REVISION_MISSING)
        return findings
    except (TypeError, ValueError):
        findings.content.append(_PROBLEM_CONTENT_CORRUPT)
        return findings
    if not isinstance(content, (bytes, bytearray, memoryview)):
        findings.content.append(_PROBLEM_CONTENT_CORRUPT)
        return findings
    content = bytes(content)
    if revision.content_sha256 != pin.content_sha256:
        findings.content.append(_PROBLEM_CONTENT_MISMATCH)
    if revision.byte_size != pin.byte_size or len(content) != pin.byte_size:
        findings.content.append(_PROBLEM_CONTENT_SIZE)
    try:
        recomputed = sha256_digest(content)
    except TypeError:  # pragma: no cover - defensive
        findings.content.append(_PROBLEM_CONTENT_CORRUPT)
        recomputed = None
    if recomputed is not None and recomputed != pin.content_sha256:
        findings.content.append(_PROBLEM_CONTENT_MISMATCH)
    findings.rights_digest = _check_rights_pin(store, pin, findings)
    if pin.role != VERSION_LINEAGE_ROLE:
        # A live input (and the target) must be its artifact's current head; a
        # historical ``version_of`` ancestor is deliberately not head-compared.
        try:
            current = artifacts.current_revision(pin.artifact_id)
        except (RecordValidationError, TypeError, ValueError):
            current = None
        if current is None or current.revision_id != pin.revision_id:
            findings.drifted_head.append(_PROBLEM_LIVE_DRIFT)
    return findings


def _raise_for_prepare(findings: _EntryFindings) -> None:
    """Fail closed (with a static typed error) on any blocking finding."""
    if findings.missing:
        raise GateDependencyError(
            "a referenced revision or rights record in the gate snapshot closure "
            "is missing in this project; the snapshot cannot be prepared."
        )
    if findings.content:
        raise GateContentIntegrityError(
            "a pinned revision's stored bytes do not match its recorded digest or "
            "byte size; the snapshot cannot be prepared."
        )
    if findings.rights:
        raise GateRightsError(
            "a pinned rights record is missing, corrupt, misidentified, unsuited "
            "to its revision, or not a valid storage-allowed record; the snapshot "
            "cannot be prepared."
        )
    if findings.superseded_rights:
        raise GateRightsError(
            "a pinned rights record has been superseded by a newer revision; the "
            "snapshot cannot be prepared."
        )
    if findings.drifted_head:
        raise GateSupersededInputError(
            "a live input is not the current revision of its artifact; the "
            "snapshot cannot be prepared."
        )


# --------------------------------------------------------------------------- #
# Version set and canonical structure (C02 §2.2 / §6.1)
# --------------------------------------------------------------------------- #
def _build_version_set(entries) -> dict[str, object]:
    """Return the C02 §6.1 ``VersionSet`` for a human-only gate.

    ``context_kind`` is ``human_authored``; ``input_digest`` is derived from the
    exact pins; ``model``/``prompt`` are ``not_applicable`` (no fabricated model
    id). Any rubric/parser artifact the stage actually consumed remains an exact
    dependency pin in ``entries`` — this set invents no version for it.
    """
    input_digest = sha256_digest(
        canonical_json([entry.as_dict() for entry in entries])
    )
    slot = {"state": VERSION_SLOT_NOT_APPLICABLE}
    return {
        "context_kind": HUMAN_GATE_CONTEXT_KIND,
        "input_digest": input_digest,
        "model": dict(slot),
        "prompt": dict(slot),
        "rubric": dict(slot),
        "parser": dict(slot),
        "adapter": dict(slot),
        "route": VERSION_SLOT_NOT_APPLICABLE,
    }


def _build_structure(
    *,
    project_id: str,
    target: Mapping,
    gate_kind: str,
    gate_id: str,
    decision: str,
    rationale: str,
    storage_schema_version: int,
    snapshot_schema_version: int,
    version_set: Mapping,
    entries,
) -> dict[str, object]:
    """Return the canonical structural object the snapshot digest is taken over.

    The representation version (``snapshot_schema_version``) and the store
    schema version recorded **inside** the snapshot are explicit, so a later
    schema or representation bump does not change an honest historical digest.
    """
    return {
        "snapshot_schema_version": snapshot_schema_version,
        "storage_schema_version": storage_schema_version,
        "project_id": project_id,
        "target": {
            "project_id": target["project_id"],
            "artifact_id": target["artifact_id"],
            "revision_id": target["revision_id"],
        },
        "gate_kind": gate_kind,
        "gate_id": gate_id,
        "decision": decision,
        "rationale": rationale,
        "context": {"kind": HUMAN_GATE_CONTEXT_KIND},
        "versions": dict(version_set),
        "entries": [entry.as_dict() for entry in entries],
    }


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def prepare_gate_snapshot(
    store,
    target: object,
    *,
    gate_kind: object,
    gate_id: object,
    decision: object,
    rationale: object,
) -> PreparedGate:
    """Prepare an exact, immutable human-gate snapshot (read-only).

    ``store`` is an already-verified
    :class:`~review_article_agent.storage.store.ProjectStore`. ``target`` is an
    exact ``{project_id, artifact_id, revision_id}`` reference; ``gate_kind`` is
    one of the real C02 gate kinds; ``gate_id`` names the specific gate
    instance; ``decision`` is ``approved``/``rejected``; ``rationale`` is the
    non-empty human reason.

    The full transitive dependency closure is captured and verified: every
    referenced revision must exist and be in this project, every revision's
    stored bytes must match its recorded digest **and** byte size, and every
    pinned rights record must exist, be uncorrupted, be identified exactly,
    bind the pinned revision exactly, be storage-allowed and not superseded.
    Every live input must be its artifact's current revision. A missing,
    corrupt, cross-project, superseded, or not-storage-allowed pin blocks
    preparation with a static typed error and writes nothing.

    Deliberately **not** accepted: any in-band authority/authentication field
    (``actor``, ``authority``, ``authenticated``, ``owner_auth_ref``, a payload
    boolean, or an imported record) — such keyword arguments raise
    :class:`TypeError` and are never trusted. A generic saved tool permission or
    proposal payload cannot be a gate target or a gate kind.
    """
    project_id = _project_id(store)
    ref = _validate_target(target, project_id)
    gate_kind = _validate_gate_kind(gate_kind)
    decision = _validate_decision(decision)
    gate_id = _validate_gate_id(gate_id)
    rationale = _validate_rationale(rationale)

    artifacts = ArtifactRepository(store)
    storage_schema_version = store.schema_version
    with _read_transaction(store.connection):
        closure = _exact_closure(store, artifacts, ref)
        entries: list[PreparedSnapshotEntry] = []
        for pin in closure:
            if pin.role not in SNAPSHOT_ENTRY_ROLES:
                raise GateDependencyError(
                    "a closure entry carries a role outside the approved snapshot "
                    "role vocabulary; the snapshot cannot be prepared."
                )
            findings = _check_entry(store, artifacts, pin)
            _raise_for_prepare(findings)
            entries.append(
                PreparedSnapshotEntry(
                    project_id=pin.project_id,
                    artifact_id=pin.artifact_id,
                    revision_id=pin.revision_id,
                    role=pin.role,
                    content_sha256=pin.content_sha256,
                    byte_size=pin.byte_size,
                    rights_id=pin.rights_id,
                    rights_record_revision=pin.rights_record_revision,
                    rights_digest=findings.rights_digest or "",
                )
            )
        version_set = _build_version_set(entries)
        structure = _build_structure(
            project_id=project_id,
            target=ref,
            gate_kind=gate_kind,
            gate_id=gate_id,
            decision=decision,
            rationale=rationale,
            storage_schema_version=storage_schema_version,
            snapshot_schema_version=HUMAN_GATE_SNAPSHOT_SCHEMA_VERSION,
            version_set=version_set,
            entries=entries,
        )
        canonical = canonical_json(structure)
        digest = hashlib.sha256(canonical).hexdigest()
        snapshot_id = "psnap_" + digest

    return PreparedGate._mint(
        project_id=project_id,
        target=ref,
        gate_kind=gate_kind,
        gate_id=gate_id,
        decision=decision,
        rationale=rationale,
        snapshot_id=snapshot_id,
        digest=digest,
        canonical_bytes=canonical,
        entries=tuple(entries),
        version_set=version_set,
        storage_schema_version=storage_schema_version,
        snapshot_schema_version=HUMAN_GATE_SNAPSHOT_SCHEMA_VERSION,
    )


def _entry_signature(entries) -> tuple:
    """Return a comparable (identity, role, digest) signature of snapshot entries.

    Defensive: a forged/hand-built entry that lacks a field contributes ``None``
    rather than raising, so a malformed prepared object is reported as a mismatch
    instead of crashing revalidation.
    """
    signature = []
    for entry in entries:
        signature.append(
            (
                getattr(entry, "project_id", None),
                getattr(entry, "artifact_id", None),
                getattr(entry, "revision_id", None),
                getattr(entry, "role", None),
                getattr(entry, "content_sha256", None),
                getattr(entry, "byte_size", None),
                getattr(entry, "rights_id", None),
                getattr(entry, "rights_record_revision", None),
                getattr(entry, "rights_digest", None),
            )
        )
    return tuple(signature)


def revalidate_prepared(store, prepared: object) -> GateRevalidation:
    """Re-check a prepared snapshot against the store, read-only and atomically.

    This does **not** trust the prepared object: it **re-derives the exact
    dependency closure from the store**, rebuilds the canonical structure from
    the re-derived closure, and compares the prepared entries (set, order, and
    roles), the prepared target inclusion, the representation/schema version,
    the gate metadata, the canonical bytes, the snapshot digest, the snapshot id,
    and the version set. It never relies on the mint sentinel as validation.

    * a current target/dependency revision that moved, or a pinned rights record
      a newer ``record_revision`` has superseded ⇒ ``stale``;
    * a missing revision, a digest/byte-size/rights-bytes corruption, a rights
      identity/subject mismatch, a malformed stored value, or any disagreement
      between the prepared snapshot and the re-derived structure/closure ⇒
      ``invalid``;
    * a prepared snapshot and store that disagree on project identity ⇒
      ``denied``.

    Every problem is a **static, curated** message: no revision/artifact/rights
    identifier, caller text, or stored byte ever appears in the outcome. Nothing
    is written and nothing is authorized. Call
    :meth:`GateRevalidation.raise_for_status` (or check :attr:`~.ok`) before an
    owner confirmation or an approval record is allowed to proceed.
    """
    if not isinstance(prepared, PreparedGate):
        raise GateSnapshotError(
            "revalidation requires a snapshot minted by prepare_gate_snapshot; a "
            "mapping, digest, boolean, or hand-built object grants nothing."
        )

    project_id = _project_id(store)
    artifacts = ArtifactRepository(store)

    with _read_transaction(store.connection):
        prepared_entries = prepared.entries
        target = prepared.target

        denied: list[str] = []
        if prepared.project_id != project_id:
            denied.append(_PROBLEM_CROSS_PROJECT)
        if not isinstance(target, Mapping) or target.get("project_id") != project_id:
            denied.append(_PROBLEM_CROSS_PROJECT)
        if not isinstance(prepared_entries, (tuple, list)):
            denied.append(_PROBLEM_CROSS_PROJECT)
        else:
            for pin in prepared_entries:
                if getattr(pin, "project_id", None) != project_id:
                    denied.append(_PROBLEM_CROSS_PROJECT)
        if denied:
            return GateRevalidation(
                project_id=project_id,
                gate_kind=prepared.gate_kind if isinstance(prepared.gate_kind, str) else "",
                gate_id=prepared.gate_id if isinstance(prepared.gate_id, str) else "",
                status="denied",
                prepared_digest=prepared.digest,
                recomputed_digest="",
                problems=tuple(dict.fromkeys(denied)),
            )

        invalid: list[str] = []
        stale: list[str] = []
        recomputed = ""

        # -- representation / schema --------------------------------------- #
        if prepared.snapshot_schema_version != HUMAN_GATE_SNAPSHOT_SCHEMA_VERSION:
            invalid.append(_PROBLEM_SCHEMA_VERSION)

        # -- gate metadata (owner-action conventions) ---------------------- #
        metadata_ok = True
        try:
            _validate_gate_kind(prepared.gate_kind)
            _validate_decision(prepared.decision)
            _validate_gate_id(prepared.gate_id)
            _validate_rationale(prepared.rationale)
        except GateDefinitionError:
            metadata_ok = False
            invalid.append(_PROBLEM_GATE_METADATA)

        # -- target reference ---------------------------------------------- #
        validated_target: dict[str, str] | None = None
        try:
            validated_target = _validate_target(dict(target), project_id)
        except (GateTargetError, TypeError, ValueError):
            invalid.append(_PROBLEM_TARGET_REFERENCE)

        # -- snapshot id binds the digest ---------------------------------- #
        if not (
            isinstance(prepared.snapshot_id, str)
            and isinstance(prepared.digest, str)
            and prepared.snapshot_id == "psnap_" + prepared.digest
        ):
            invalid.append(_PROBLEM_SNAPSHOT_ID)

        # -- re-derive the exact closure from the store -------------------- #
        derived_entries: list[PreparedSnapshotEntry] | None = None
        if validated_target is not None:
            try:
                derived_pins = _exact_closure(store, artifacts, validated_target)
            except GateDependencyError:
                invalid.append(_PROBLEM_CLOSURE_MISSING)
                derived_pins = None
            except (TypeError, ValueError, UnicodeError, RecursionError):
                invalid.append(_PROBLEM_STORE_DATA)
                derived_pins = None
            if derived_pins is not None:
                derived_entries = []
                for pin in derived_pins:
                    try:
                        findings = _check_entry(store, artifacts, pin)
                    except (TypeError, ValueError, UnicodeError, RecursionError):
                        invalid.append(_PROBLEM_STORE_DATA)
                        continue
                    invalid.extend(findings.missing)
                    invalid.extend(findings.content)
                    invalid.extend(findings.rights)
                    stale.extend(findings.superseded_rights)
                    stale.extend(findings.drifted_head)
                    derived_entries.append(
                        PreparedSnapshotEntry(
                            project_id=pin.project_id,
                            artifact_id=pin.artifact_id,
                            revision_id=pin.revision_id,
                            role=pin.role,
                            content_sha256=pin.content_sha256,
                            byte_size=pin.byte_size,
                            rights_id=pin.rights_id,
                            rights_record_revision=pin.rights_record_revision,
                            rights_digest=findings.rights_digest or "",
                        )
                    )

        if derived_entries is not None:
            # Exact closure: same set, order, roles, and rights pins.
            if _entry_signature(derived_entries) != _entry_signature(prepared_entries):
                invalid.append(_PROBLEM_CLOSURE_MISMATCH)
            # Exact target inclusion: the target is entry 0 with role ``target``.
            if (
                not prepared_entries
                or getattr(prepared_entries[0], "role", None) != "target"
            ):
                invalid.append(_PROBLEM_TARGET_INCLUSION)

            # Canonical structure, bytes, digest, and version set.
            if metadata_ok:
                try:
                    recomputed_versions = _build_version_set(derived_entries)
                    structure = _build_structure(
                        project_id=prepared.project_id,
                        target=validated_target,
                        gate_kind=prepared.gate_kind,
                        gate_id=prepared.gate_id,
                        decision=prepared.decision,
                        rationale=prepared.rationale,
                        storage_schema_version=prepared.storage_schema_version,
                        snapshot_schema_version=prepared.snapshot_schema_version,
                        version_set=recomputed_versions,
                        entries=derived_entries,
                    )
                    recomputed_bytes = canonical_json(structure)
                except (RecordValidationError, TypeError, ValueError, UnicodeError, RecursionError):
                    invalid.append(_PROBLEM_STORE_DATA)
                else:
                    recomputed = hashlib.sha256(recomputed_bytes).hexdigest()
                    if recomputed_bytes != prepared.canonical_bytes:
                        invalid.append(_PROBLEM_CANONICAL_BYTES)
                    if recomputed != prepared.digest:
                        invalid.append(_PROBLEM_DIGEST)
                    try:
                        if canonical_json(
                            _deep_copy_plain(prepared.version_set)
                        ) != canonical_json(recomputed_versions):
                            invalid.append(_PROBLEM_VERSION_SET)
                    except (RecordValidationError, TypeError, ValueError, UnicodeError, RecursionError):
                        invalid.append(_PROBLEM_STORE_DATA)

        if invalid:
            status = "invalid"
        elif stale:
            status = "stale"
        else:
            status = "unchanged"

        return GateRevalidation(
            project_id=prepared.project_id,
            gate_kind=prepared.gate_kind if isinstance(prepared.gate_kind, str) else "",
            gate_id=prepared.gate_id if isinstance(prepared.gate_id, str) else "",
            status=status,
            prepared_digest=prepared.digest,
            recomputed_digest=recomputed,
            problems=tuple(dict.fromkeys(invalid + stale)),
        )
