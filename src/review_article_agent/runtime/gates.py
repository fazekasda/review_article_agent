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

The exact-closure traversal deliberately reuses the tested read-only snapshot
builder of :class:`~review_article_agent.storage.approvals.ApprovalRepository`
(so the closure semantics — target entry 0, live-wins over ancestry, transitive
``version_of`` lineage — match what a later approval repository records) and the
tested :class:`~review_article_agent.storage.artifacts.ArtifactRepository`
read API. This module never calls the fixture *write* path.

LLM-annotated test file companion: ``tests/test_gate_snapshots.py``.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import types
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
    ApprovalRepository,
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
#: cannot be walked without limit. The traversal itself is cycle-safe.
MAX_GATE_SNAPSHOT_ENTRIES = 10000


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
        setter(self, "_version_set", _deep_copy_plain(version_set))
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
        """The C02 §6.1 ``VersionSet`` (immutable, human-authored)."""
        return _deep_freeze(self._version_set)

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
    """Run a block in one consistent read transaction.

    Opens a deferred ``BEGIN`` **only** when the connection is not already in a
    transaction, so the pinned reads are one coherent snapshot without ever
    taking a write lock or mutating the store.
    """
    if conn.in_transaction:
        yield conn
        return
    try:
        conn.execute("BEGIN")
    except sqlite3.Error:
        yield conn
        return
    try:
        yield conn
    finally:
        try:
            conn.execute("COMMIT")
        except sqlite3.Error:  # pragma: no cover - best effort
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover - best effort
                pass


def _project_id(store) -> str:
    project_id = getattr(store, "project_id", None)
    if not isinstance(project_id, str) or project_id == "":
        raise GateSnapshotError(
            "the store does not expose a project identity; refusing to prepare a "
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
        return validate_ref(dict(target), project_id)
    except (RecordValidationError, TypeError, ValueError) as exc:
        raise GateTargetError(
            "the gate target is not an exact, same-project "
            "{project_id, artifact_id, revision_id} reference; it cannot be "
            "prepared."
        ) from exc


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


def _exact_closure(store, target_ref: dict[str, str]) -> list:
    """Return the target plus its full transitive dependency closure.

    Reuses the tested read-only snapshot builder of
    :class:`ApprovalRepository` (the fixture *write* path is never called), so
    the closure semantics match what a later approval repository records. The
    traversal is cycle-safe; the result is bounded by
    :data:`MAX_GATE_SNAPSHOT_ENTRIES`.
    """
    approvals = ApprovalRepository(store)
    try:
        entries = approvals._build_snapshot(dict(target_ref))
    except RecordValidationError as exc:
        raise GateDependencyError(
            "a revision reachable from the target does not exist in this project; "
            "the gate snapshot cannot be prepared."
        ) from exc
    if len(entries) > MAX_GATE_SNAPSHOT_ENTRIES:
        raise GateDependencyError(
            f"the dependency closure exceeds the {MAX_GATE_SNAPSHOT_ENTRIES}-entry "
            "bound; the gate snapshot cannot be prepared."
        )
    return entries


def _check_rights_pin(store, pin, findings: _EntryFindings, label: str) -> str | None:
    """Structurally verify one entry's pinned canonical rights record.

    Returns the pinned rights record's canonical digest, or ``None`` when the
    record could not be read/decoded. Appends every problem to ``findings``.
    """
    row = store.connection.execute(
        "SELECT * FROM rights_record WHERE project_id = ? AND rights_id = ? "
        "AND record_revision = ?",
        (pin.project_id, pin.rights_id, pin.rights_record_revision),
    ).fetchone()
    if row is None:
        findings.rights.append(
            f"pinned rights record {pin.rights_id!r}@"
            f"{pin.rights_record_revision} is missing for {label!r}"
        )
        return None
    canonical = bytes(row["canonical"])
    digest = sha256_digest(canonical)
    if digest != row["content_sha256"]:
        findings.rights.append(
            f"pinned rights record bytes are corrupt for {label!r}"
        )
        return None
    try:
        record = json.loads(canonical.decode("utf-8"))
    except (ValueError, UnicodeError):
        findings.rights.append(
            f"pinned rights record is not decodable JSON for {label!r}"
        )
        return None
    if not isinstance(record, dict):
        findings.rights.append(
            f"pinned rights record is not an object for {label!r}"
        )
        return None
    if (
        record.get("project_id") != pin.project_id
        or record.get("rights_id") != pin.rights_id
        or record.get("record_revision") != pin.rights_record_revision
    ):
        findings.rights.append(
            f"pinned rights record identity mismatch for {label!r}"
        )
    subject = record.get("subject")
    if not (
        isinstance(subject, dict)
        and subject.get("kind") == "source_revision"
        and subject.get("project_id") == pin.project_id
        and subject.get("artifact_id") == pin.artifact_id
        and subject.get("revision_id") == pin.revision_id
    ):
        findings.rights.append(
            f"pinned rights record subject does not bind the pinned revision for "
            f"{label!r}"
        )
    elif (
        row["subject_kind"],
        row["subject_project_id"],
        row["subject_artifact_id"],
        row["subject_revision_id"],
    ) != ("source_revision", pin.project_id, pin.artifact_id, pin.revision_id):
        findings.rights.append(
            f"pinned rights record row subject columns are inconsistent for "
            f"{label!r}"
        )
    try:
        validated = validate_rights_record(record, pin.project_id)
        require_storage_allowed(validated)
    except RecordValidationError:
        findings.rights.append(
            f"pinned rights record is not a valid storage-allowed record for "
            f"{label!r}"
        )
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
        findings.superseded_rights.append(
            f"pinned rights record {pin.rights_id!r} has a newer revision "
            f"{newest['newest']} for {label!r}"
        )
    return digest


def _check_entry(store, artifacts: ArtifactRepository, pin) -> _EntryFindings:
    """Re-check every pin of one entry against the store (read-only).

    ``pin`` is any object exposing ``project_id``/``artifact_id``/``revision_id``/
    ``role``/``content_sha256``/``byte_size``/``rights_id``/
    ``rights_record_revision`` (both the fixture builder's entries and
    :class:`PreparedSnapshotEntry` qualify).
    """
    findings = _EntryFindings()
    label = f"{pin.artifact_id!r}/{pin.revision_id!r}"
    revision = artifacts.get_revision(pin.artifact_id, pin.revision_id)
    if revision is None:
        findings.missing.append(f"pinned revision {label} is missing")
        return findings
    if (
        revision.rights_ref.project_id,
        revision.rights_ref.rights_id,
        revision.rights_ref.record_revision,
    ) != (pin.project_id, pin.rights_id, pin.rights_record_revision):
        findings.rights.append(
            f"pinned rights key does not match the revision's rights pin for {label}"
        )
    try:
        content = artifacts.read_content(pin.artifact_id, pin.revision_id)
    except RecordValidationError:
        findings.missing.append(f"pinned revision {label} content is missing")
        return findings
    if revision.content_sha256 != pin.content_sha256:
        findings.content.append(f"stored content digest changed for {label}")
    if revision.byte_size != pin.byte_size or len(content) != pin.byte_size:
        findings.content.append(f"byte size does not match the pin for {label}")
    if sha256_digest(content) != pin.content_sha256:
        findings.content.append(
            f"recomputed content digest does not match the pin for {label}"
        )
    findings.rights_digest = _check_rights_pin(store, pin, findings, label)
    if pin.role != VERSION_LINEAGE_ROLE:
        # A live input (and the target) must be its artifact's current head; a
        # historical ``version_of`` ancestor is deliberately not head-compared.
        current = artifacts.current_revision(pin.artifact_id)
        current_id = None if current is None else current.revision_id
        if current is None or current.revision_id != pin.revision_id:
            findings.drifted_head.append(
                f"current revision of {pin.artifact_id!r} is {current_id!r}, not "
                f"the pinned {pin.revision_id!r}"
            )
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
        closure = _exact_closure(store, ref)
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


def revalidate_prepared(store, prepared: object) -> GateRevalidation:
    """Re-check a prepared snapshot against the store, read-only and atomically.

    Reconstructs the exact canonical structure from the prepared pins and
    re-checks every pin in one read transaction:

    * a current target/dependency revision that moved, or a pinned rights record
      a newer ``record_revision`` has superseded ⇒ ``stale``;
    * a missing revision, a digest/byte-size/rights-bytes corruption, or a
      rights identity/subject mismatch, or a prepared digest that no longer
      matches its reconstructed structure ⇒ ``invalid``;
    * a prepared snapshot and store that disagree on project identity ⇒
      ``denied``.

    This never trusts the object's fields beyond re-deriving them: a forged
    :class:`PreparedGate` (or an object of another type) cannot bypass
    revalidation. Nothing is written and nothing is authorized. Call
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
        denied: list[str] = []
        if prepared.project_id != project_id or prepared.target.get(
            "project_id"
        ) != project_id:
            denied.append(
                "the prepared snapshot belongs to a different project than the "
                "store"
            )
        for pin in prepared.entries:
            if pin.project_id != project_id:
                denied.append(
                    "a prepared snapshot entry belongs to a different project "
                    "than the store"
                )
        if denied:
            return GateRevalidation(
                project_id=project_id,
                gate_kind=prepared.gate_kind,
                gate_id=prepared.gate_id,
                status="denied",
                prepared_digest=prepared.digest,
                recomputed_digest="",
                problems=tuple(denied),
            )

        recomputed_versions = _build_version_set(prepared.entries)
        structure = _build_structure(
            project_id=prepared.project_id,
            target=dict(prepared.target),
            gate_kind=prepared.gate_kind,
            gate_id=prepared.gate_id,
            decision=prepared.decision,
            rationale=prepared.rationale,
            storage_schema_version=prepared.storage_schema_version,
            snapshot_schema_version=prepared.snapshot_schema_version,
            version_set=recomputed_versions,
            entries=prepared.entries,
        )
        recomputed = hashlib.sha256(canonical_json(structure)).hexdigest()

        invalid: list[str] = []
        stale: list[str] = []
        if recomputed != prepared.digest:
            invalid.append(
                "the prepared snapshot digest does not match its reconstructed "
                "structure"
            )
        if canonical_json(recomputed_versions) != canonical_json(
            prepared._version_set
        ):
            invalid.append(
                "the prepared version set does not match its pinned entries"
            )
        for pin in prepared.entries:
            findings = _check_entry(store, artifacts, pin)
            invalid.extend(findings.missing)
            invalid.extend(findings.content)
            invalid.extend(findings.rights)
            stale.extend(findings.superseded_rights)
            stale.extend(findings.drifted_head)

        if invalid:
            status = "invalid"
        elif stale:
            status = "stale"
        else:
            status = "unchanged"

        return GateRevalidation(
            project_id=prepared.project_id,
            gate_kind=prepared.gate_kind,
            gate_id=prepared.gate_id,
            status=status,
            prepared_digest=prepared.digest,
            recomputed_digest=recomputed,
            problems=tuple(invalid + stale),
        )
