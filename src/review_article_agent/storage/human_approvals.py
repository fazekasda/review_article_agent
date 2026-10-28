"""Atomic, durable, exact named-gate human approvals over a verified store.

Authority
---------

* ``docs/implementation/04-agent-runtime.md`` deliverable 5 ("App-owned human
  approvals presented through CLI") and acceptance criteria 1, 2, and 4.
* ``docs/contracts/02-gates-and-runtime.md`` §1.3 (authority provenance is
  out-of-band; a payload cannot mint authority), §2 (revision-specific approvals
  over an exact, immutable dependency snapshot), §3 (stale vs invalidated vs
  superseded), and §6.1 (``VersionSet``).
* Baseline "Gates and scientific invariants" (``docs/specification.md`` lines
  54-55): approvals bind to immutable artifact revisions and dependencies — never
  filenames or generic tool permissions — and an agent cannot approve its own
  artifact.

What this module **is**
-----------------------

* :meth:`HumanApprovalRepository.record_decision` — the *only* way a human
  approval row is created. It takes an already-prepared
  :class:`~review_article_agent.runtime.gates.PreparedGate` and the opaque
  :class:`~review_article_agent.runtime.owner_auth.OwnerApproval` capability minted
  by a real out-of-band controlling-terminal confirmation, and in **one
  ``BEGIN IMMEDIATE`` transaction** it:
  1. re-derives and re-checks the entire prepared snapshot against the store
     (every current pin, the full transitive closure, rights, digests, and the
     recorded schema versions) with
     :func:`~review_article_agent.runtime.gates.revalidate_prepared`;
  2. consumes the capability **once** against the *exact* prepared action with
     :func:`~review_article_agent.runtime.owner_auth.consume_owner_approval`,
     deriving the human actor and the ``owner_auth_ref``/evidence from the
     out-of-band check;
  3. persists the append-only owner-auth evidence, the exact canonical snapshot,
     and the approval (with its snapshot entries) **together**, so a failed write
     leaves **zero** rows.
* :meth:`HumanApprovalRepository.evaluate` — recomputes, on every read, the
  C02 §3 lifecycle status (``active``/``stale``/``invalidated``/``superseded``).
* :meth:`HumanApprovalRepository.named_gate_satisfied` — the *only* satisfaction
  claim: one exact named gate instance (``gate_kind`` + ``gate_id`` + target
  artifact) is satisfied **iff** its current approval is ``active`` and
  ``approved``.
* :func:`integrity_problems` — a read-only export a later backup verifier can
  reuse to revalidate every human-approval row, detect foreign/orphan/dangling
  rows, and accept legitimate historical ``stale``/``superseded`` rows (no
  current-head requirement).

What this module deliberately **is not**
----------------------------------------

* It is **not** a global execution, manuscript-export, scientific-correctness, or
  final-release-validation claim. An approval satisfies **one named gate
  instance** only; ``authorizes_execution``/``authorizes_export`` are always
  ``False`` and are pinned to ``0`` at the database.
* It does **not** mint, trust, or accept in-band authority: there is no
  ``actor``/``authenticated``/``owner_evidence``/``imported_approval`` keyword and
  no boolean or mapping that can stand in for the out-of-band capability. A saved
  generic tool permission can never satisfy an outline/pilot/exclusion gate.
* It never mutates the artifact/scientific state or a fixture approval; it only
  appends rows to its own three tables. A consumed capability that a failed write
  rolled back requires a **fresh** confirmation (safe, conservative).

LLM-annotated test file companion: ``tests/test_human_approvals.py``.
"""

from __future__ import annotations

import functools
import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping

from ..domain.errors import StoreError
from ..domain.records import (
    RecordValidationError,
    canonical_json,
    require_storage_allowed,
    validate_identifier,
    validate_rights_record,
)
from ..runtime.gates import (
    HUMAN_GATE_SNAPSHOT_SCHEMA_VERSION,
    MAX_GATE_SNAPSHOT_ENTRIES,
    GateSnapshotError,
    PreparedGate,
    PreparedSnapshotEntry,
    _build_structure,
    _build_version_set,
    revalidate_prepared,
)
from ..runtime.owner_auth import (
    APPROVAL_DECISIONS,
    APPROVAL_GATE_KINDS,
    OWNER_APPROVAL_METHOD,
    ActionDigestMismatch,
    ApprovalAlreadyConsumed,
    ApprovalExpired,
    CrossProcessCapabilityError,
    InvalidCapabilityError,
    OwnerAction,
    OwnerApproval,
    OwnerAuthError,
    OwnerAuthEvidence,
    OwnerIdentityError,
    OwnerUidMismatch,
    action_digest,
    consume_owner_approval,
)
from .agent_runtime import validate_version_set
from .approvals import SNAPSHOT_ENTRY_ROLES, VERSION_LINEAGE_ROLE
from .artifacts import ArtifactRepository, sha256_digest
from .schema import STORE_SCHEMA_VERSION

__all__ = [
    "HUMAN_APPROVAL_STATUSES",
    "ACTOR_KIND_HUMAN",
    "AUTHORITY_OWNER_AUTHENTICATED",
    "HumanApprovalError",
    "HumanApprovalDeniedError",
    "HumanApprovalCorruptionError",
    "HumanApprovalRecord",
    "HumanApprovalStatus",
    "HumanApprovalVerification",
    "HumanApprovalRepository",
    "integrity_problems",
]

#: Computed lifecycle statuses (C02 §3). Never stored — always recomputed.
HUMAN_APPROVAL_STATUSES = ("active", "stale", "invalidated", "superseded")

#: The single actor kind and authority a human approval may carry (C02 §2.1/§1.3).
ACTOR_KIND_HUMAN = "human"
AUTHORITY_OWNER_AUTHENTICATED = "owner_authenticated"

#: A lowercase hex SHA-256 digest (the contract's single digest algorithm).
_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

#: The exact keys of the persisted owner-auth evidence canonical record
#: (:meth:`review_article_agent.runtime.owner_auth.OwnerAuthEvidence.as_record`).
#: A canonical blob that carries any other key is not the exact safe projection
#: and is refused, so an extra (potentially authority-bearing) field is never
#: trusted silently.
_EVIDENCE_KEYS = frozenset(
    {"auth_id", "method", "owner_uid", "verified_at", "action_digest"}
)

# --------------------------------------------------------------------------- #
# Static, curated problem messages
# --------------------------------------------------------------------------- #
# These never contain a revision/artifact/rights/gate identifier, caller text, or
# source/rights bytes: a denial, status, or integrity problem is safe to surface.
_MSG_DENIED_GENERIC = (
    "the human approval was denied before any row was written; re-prepare the "
    "gate from current state and request a fresh out-of-band confirmation."
)
_MSG_DENIED_STALE = (
    "the prepared gate snapshot is no longer current; the approval was denied "
    "before any row was written. Re-prepare the gate and confirm again."
)
_MSG_DENIED_INVALID = (
    "the prepared gate snapshot or a pinned input fails integrity revalidation; "
    "the approval was denied before any row was written."
)
_MSG_DENIED_CROSS_PROJECT = (
    "the prepared gate snapshot belongs to a different project; the approval was "
    "denied before any row was written."
)
_MSG_DENIED_NOT_PREPARED = (
    "an approval requires a snapshot minted from the store by "
    "prepare_gate_snapshot; a mapping, digest, boolean, or imported record grants "
    "nothing."
)
_MSG_CORRUPT_APPROVAL = "human approval: the stored row fails integrity revalidation"
_MSG_CORRUPT_EVIDENCE = (
    "owner auth evidence: the stored row fails integrity revalidation"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _bare_hex(value: object, field: str) -> str:
    """Normalize a digest to bare lowercase hex, failing closed otherwise."""
    if not isinstance(value, str):
        raise HumanApprovalCorruptionError(f"{field}: a SHA-256 digest must be a string")
    text = value[7:] if value.startswith("sha256:") else value
    if _HEX_RE.fullmatch(text) is None:
        raise HumanApprovalCorruptionError(f"{field}: must be a lowercase hex digest")
    return text


def _is_supported_storage_schema(value: object) -> bool:
    """``True`` iff ``value`` is a supported project store schema version.

    A legitimate older version (``1..STORE_SCHEMA_VERSION``) recorded inside a
    historical approval is preserved; a future version, a boolean, or a
    non-integer is not a schema version at all.
    """
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 1 <= value <= STORE_SCHEMA_VERSION
    )


def _plain(value: Any) -> Any:
    """Return a plain (non-proxy) deep copy of a JSON-shaped value."""
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _safe_str(value: object) -> str:
    return value if isinstance(value, str) else ""


# --------------------------------------------------------------------------- #
# Errors (static, curated, non-reflective)
# --------------------------------------------------------------------------- #
class HumanApprovalError(StoreError):
    """A durable human-approval operation failed."""


class HumanApprovalDeniedError(HumanApprovalError):
    """The approval was denied **before** any row was written.

    ``reason`` is a short, static, value-free token (never an identifier, caller
    text, or stored byte) so a caller can distinguish a stale snapshot from a
    rejected/expired/foreign capability without the message reflecting input.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class HumanApprovalCorruptionError(HumanApprovalError):
    """A stored human-approval row fails structural/hash/link revalidation."""


def _fail_closed_row(what: str):
    """Decorate a stored-row validator so a malformed row fails closed.

    A stored row is untrusted input. Any helper that rejects it — a typed record
    error, a ``ValueError``, a missing column, a non-string field — must surface
    as the single typed :class:`HumanApprovalCorruptionError` rather than leak a
    raw exception. The message is static: it never echoes the offending value.
    """

    def decorate(func):
        @functools.wraps(func)
        def wrapper(self, row):
            try:
                return func(self, row)
            except HumanApprovalCorruptionError:
                raise
            except (
                RecordValidationError,
                OwnerAuthError,
                ValueError,
                TypeError,
                KeyError,
                IndexError,
                UnicodeError,
                sqlite3.Error,
            ) as exc:
                raise HumanApprovalCorruptionError(f"{what}: stored row is malformed.") from exc

        return wrapper

    return decorate


# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class HumanApprovalRecord:
    """One immutable human-approval row (as stored, fully revalidated).

    It carries the exact target, gate identity, decision, rationale, the human
    actor/authority, the out-of-band ``owner_auth_ref``, and the canonical
    snapshot bytes (digests and pinned refs only — never source content).
    """

    project_id: str
    approval_id: str
    target: Mapping[str, str]
    gate_kind: str
    gate_id: str
    decision: str
    rationale: str
    granted_at_utc: str
    snapshot_id: str
    snapshot_digest: str
    snapshot_schema_version: int
    storage_schema_version: int
    owner_auth_id: str
    owner_auth_method: str
    owner_auth_action_digest: str
    owner_uid: int
    actor_kind: str
    authority: str
    created_at_utc: str
    canonical: bytes

    @property
    def owner_auth_ref(self) -> str:
        """The pointer to the out-of-band owner-auth check (never proof itself)."""
        return self.owner_auth_id

    def to_mapping(self) -> dict[str, Any]:
        """A JSON-safe view (no canonical bytes, no source content)."""
        return {
            "project_id": self.project_id,
            "approval_id": self.approval_id,
            "target": dict(self.target),
            "gate_kind": self.gate_kind,
            "gate_id": self.gate_id,
            "decision": self.decision,
            "granted_at_utc": self.granted_at_utc,
            "snapshot_id": self.snapshot_id,
            "snapshot_digest": self.snapshot_digest,
            "snapshot_schema_version": self.snapshot_schema_version,
            "storage_schema_version": self.storage_schema_version,
            "granted_by": {
                "actor_kind": self.actor_kind,
                "authority": self.authority,
                "owner_auth_ref": self.owner_auth_id,
            },
            "created_at_utc": self.created_at_utc,
        }

    def __repr__(self) -> str:
        # Only the digest — never caller text or source/rights bytes.
        return "<HumanApprovalRecord snapshot digest=" + self.snapshot_digest + ">"


@dataclass(frozen=True)
class HumanApprovalStatus:
    """Computed lifecycle status/effect of one approval (C02 §3).

    ``authorizes_execution`` and ``authorizes_export`` are always ``False``: a
    named-gate approval never authorizes global execution, export, scientific
    correctness, or final release.
    """

    approval_id: str
    status: str
    decision: str
    gate_kind: str
    gate_id: str
    target: Mapping[str, str]
    snapshot_digest: str
    owner_auth_ref: str | None
    authorizes_execution: bool
    authorizes_export: bool
    reasons: tuple[str, ...]
    superseded_by: str | None = None

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    @property
    def satisfies_named_gate(self) -> bool:
        """``True`` iff this exact named gate is satisfied (``active`` + ``approved``)."""
        return self.status == "active" and self.decision == "approved"

    def __repr__(self) -> str:
        return (
            "<HumanApprovalStatus status="
            + self.status
            + " decision="
            + self.decision
            + " digest="
            + self.snapshot_digest
            + ">"
        )


@dataclass(frozen=True)
class HumanApprovalVerification:
    """Structural verification of one approval's snapshot (backup verifier)."""

    approval_id: str
    ok: bool
    stored_digest: str
    recomputed_digest: str
    problems: tuple[str, ...]


class _ReadOnlyStoreView:
    """The exact surface the repository reads from a store (never writes)."""

    __slots__ = ("connection", "project_id", "schema_version")

    def __init__(self, conn: sqlite3.Connection, project_id: str) -> None:
        self.connection = conn
        self.project_id = project_id
        self.schema_version = 0


# --------------------------------------------------------------------------- #
# Repository
# --------------------------------------------------------------------------- #
class HumanApprovalRepository:
    """Immutable, exact named-gate human approvals over a verified store.

    Typical use::

        prepared = prepare_gate_snapshot(store, target, gate_kind="pilot",
                                         gate_id="gate_pilot_1", decision="approved",
                                         rationale="...")
        capability = request_owner_confirmation(prepared.owner_action())  # real tty
        approvals = HumanApprovalRepository(store)
        record = approvals.record_decision(prepared, capability)
        assert approvals.evaluate(record.approval_id).satisfies_named_gate
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

    # -- write --------------------------------------------------------------- #
    def record_decision(
        self,
        prepared: PreparedGate,
        capability: OwnerApproval,
    ) -> HumanApprovalRecord:
        """Atomically record one human approval for an exact prepared gate.

        The **only** accepted inputs are an already-prepared
        :class:`~review_article_agent.runtime.gates.PreparedGate` and the opaque
        :class:`~review_article_agent.runtime.owner_auth.OwnerApproval` capability
        minted by a real out-of-band confirmation. There is deliberately **no**
        ``actor``/``authenticated``/``owner_evidence``/``imported_approval``
        keyword: any extra keyword raises :class:`TypeError` and a mapping,
        boolean, or imported permission can never stand in for either argument.

        Everything happens in one ``BEGIN IMMEDIATE`` transaction: the prepared
        snapshot is re-derived and re-checked against the store, then the
        capability is consumed **once** against the exact prepared action, then
        the owner-auth evidence, the canonical snapshot, and the approval are
        written together. A stale/invalid/cross-project snapshot, or a
        wrong/expired/replayed/forked/foreign-uid/forged capability, is **denied
        before any row is written**.
        """
        if not isinstance(prepared, PreparedGate):
            raise HumanApprovalDeniedError(
                _MSG_DENIED_NOT_PREPARED, reason="prepared_not_prepared"
            )

        approval_id = "hapv_" + uuid.uuid4().hex
        created = _utc_now()

        with self._store.transaction():
            # (1) Re-derive and re-check the exact prepared snapshot, inside this
            #     write transaction, so the pinned reads and the write are one
            #     coherent unit. A stale/invalid/cross-project snapshot denies
            #     before the capability is touched.
            try:
                outcome = revalidate_prepared(self._store, prepared)
            except GateSnapshotError as exc:
                raise HumanApprovalDeniedError(
                    _MSG_DENIED_INVALID, reason="prepared_unverifiable"
                ) from exc
            if outcome.status != "unchanged":
                raise HumanApprovalDeniedError(
                    self._prepared_denial_message(outcome.status),
                    reason="prepared_" + outcome.status,
                )

            # (2) Consume the out-of-band capability exactly once against the
            #     exact prepared action; derive the human actor and owner_auth_ref
            #     from the returned evidence (never from caller input).
            action = prepared.owner_action()
            try:
                evidence = consume_owner_approval(capability, action)
            except OwnerAuthError as exc:
                raise HumanApprovalDeniedError(
                    _MSG_DENIED_GENERIC, reason=_capability_deny_reason(exc)
                ) from exc

            # (3) Persist the append-only evidence, the approval, and its exact
            #     snapshot entries together.
            self._insert_evidence(evidence, created)
            self._insert_approval(prepared, evidence, approval_id, created)
            for index, entry in enumerate(prepared.entries):
                self._insert_entry(approval_id, index, entry)

        stored = self.get_approval(approval_id)
        assert stored is not None  # just written in a committed transaction
        return stored

    @staticmethod
    def _prepared_denial_message(status: str) -> str:
        return {
            "stale": _MSG_DENIED_STALE,
            "invalid": _MSG_DENIED_INVALID,
            "denied": _MSG_DENIED_CROSS_PROJECT,
        }.get(status, _MSG_DENIED_GENERIC)

    def _insert_evidence(self, evidence: OwnerAuthEvidence, created: str) -> None:
        """Append one out-of-band owner-auth evidence row (separate for fault tests)."""
        record = evidence.as_record()
        canonical = canonical_json(record)
        self._conn().execute(
            "INSERT INTO owner_auth_evidence "
            "(project_id, auth_id, method, owner_uid, action_digest, verified_at_utc, "
            " canonical, content_sha256, created_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.project_id,
                evidence.auth_id,
                evidence.method,
                evidence.owner_uid,
                evidence.action_digest,
                record["verified_at"],
                canonical,
                sha256_digest(canonical),
                created,
            ),
        )

    def _insert_approval(
        self,
        prepared: PreparedGate,
        evidence: OwnerAuthEvidence,
        approval_id: str,
        created: str,
    ) -> None:
        """Insert one immutable human approval (separate method for fault tests)."""
        target = prepared.target
        version_canonical = canonical_json(_plain(prepared.version_set))
        granted = evidence.verified_at.astimezone(timezone.utc).isoformat()
        self._conn().execute(
            "INSERT INTO human_approval "
            "(project_id, approval_id, target_artifact_id, target_revision_id, "
            " gate_kind, gate_id, decision, rationale, granted_at_utc, snapshot_id, "
            " snapshot_digest, snapshot_canonical, snapshot_schema_version, "
            " storage_schema_version, version_set_canonical, owner_auth_id, "
            " owner_auth_method, owner_auth_action_digest, owner_uid, actor_kind, "
            " authority, authorizes_execution, authorizes_export, created_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "        0, 0, ?)",
            (
                self.project_id,
                approval_id,
                target["artifact_id"],
                target["revision_id"],
                prepared.gate_kind,
                prepared.gate_id,
                prepared.decision,
                prepared.rationale,
                granted,
                prepared.snapshot_id,
                prepared.digest,
                prepared.canonical_bytes,
                prepared.snapshot_schema_version,
                prepared.storage_schema_version,
                version_canonical,
                evidence.auth_id,
                evidence.method,
                evidence.action_digest,
                evidence.owner_uid,
                ACTOR_KIND_HUMAN,
                AUTHORITY_OWNER_AUTHENTICATED,
                created,
            ),
        )

    def _insert_entry(
        self, approval_id: str, index: int, entry: PreparedSnapshotEntry
    ) -> None:
        """Insert one immutable snapshot entry (separate method for fault tests)."""
        self._conn().execute(
            "INSERT INTO human_approval_entry "
            "(project_id, approval_id, entry_index, artifact_id, revision_id, role, "
            " content_sha256, byte_size, rights_id, rights_record_revision, "
            " rights_digest) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.project_id,
                approval_id,
                index,
                entry.artifact_id,
                entry.revision_id,
                entry.role,
                entry.content_sha256,
                entry.byte_size,
                entry.rights_id,
                entry.rights_record_revision,
                entry.rights_digest,
            ),
        )

    # -- raw reads ----------------------------------------------------------- #
    def _approval_row(self, approval_id: str) -> sqlite3.Row | None:
        approval_id = validate_identifier(approval_id, "approval_id")
        return self._conn().execute(
            "SELECT rowid AS _rowid, * FROM human_approval "
            "WHERE project_id = ? AND approval_id = ?",
            (self.project_id, approval_id),
        ).fetchone()

    def get_approval(self, approval_id: str) -> HumanApprovalRecord | None:
        """Return one fully revalidated approval, or ``None`` if it does not exist.

        Raises :class:`HumanApprovalCorruptionError` on a corrupt stored row: a
        malformed row is never handed to a caller unchecked.
        """
        row = self._approval_row(approval_id)
        return None if row is None else self._validate_approval_row(row)

    def list_approvals(self) -> list[HumanApprovalRecord]:
        rows = self._conn().execute(
            "SELECT rowid AS _rowid, * FROM human_approval "
            "WHERE project_id = ? ORDER BY rowid",
            (self.project_id,),
        ).fetchall()
        return [self._validate_approval_row(row) for row in rows]

    def current_approval(
        self, artifact_id: str, *, gate_kind: str, gate_id: str
    ) -> HumanApprovalRecord | None:
        """The latest approval for one exact named gate instance + target artifact.

        The derived "current" approval; it may itself be ``stale`` after a later
        edit and never carries forward to a newer revision.
        """
        artifact_id = validate_identifier(artifact_id, "artifact_id")
        gate_kind = _require_gate_kind(gate_kind)
        gate_id = validate_identifier(gate_id, "gate_id")
        row = self._conn().execute(
            "SELECT rowid AS _rowid, * FROM human_approval "
            "WHERE project_id = ? AND gate_kind = ? AND gate_id = ? "
            "AND target_artifact_id = ? ORDER BY rowid DESC LIMIT 1",
            (self.project_id, gate_kind, gate_id, artifact_id),
        ).fetchone()
        return None if row is None else self._validate_approval_row(row)

    # -- structural revalidation --------------------------------------------- #
    def _entry_rows(self, approval_id: str) -> list[PreparedSnapshotEntry]:
        """Load and revalidate the exact snapshot entries (bounded before walk).

        The entry count is checked **before** any row is materialised, so a
        pathological/corrupt stored closure cannot be walked without limit.
        """
        count = self._conn().execute(
            "SELECT COUNT(*) AS n FROM human_approval_entry "
            "WHERE project_id = ? AND approval_id = ?",
            (self.project_id, approval_id),
        ).fetchone()["n"]
        if count < 1:
            raise HumanApprovalCorruptionError(
                "human approval: the snapshot has no entries."
            )
        if count > MAX_GATE_SNAPSHOT_ENTRIES:
            raise HumanApprovalCorruptionError(
                "human approval: the snapshot exceeds the entry bound."
            )
        rows = self._conn().execute(
            "SELECT * FROM human_approval_entry WHERE project_id = ? AND "
            "approval_id = ? ORDER BY entry_index",
            (self.project_id, approval_id),
        ).fetchall()
        entries: list[PreparedSnapshotEntry] = []
        for expected_index, row in enumerate(rows):
            if row["entry_index"] != expected_index:
                raise HumanApprovalCorruptionError(
                    "human approval: snapshot entry ordinals are not contiguous."
                )
            if row["role"] not in SNAPSHOT_ENTRY_ROLES:
                raise HumanApprovalCorruptionError(
                    "human approval: a snapshot entry role is not in the approved "
                    "vocabulary."
                )
            entries.append(
                PreparedSnapshotEntry(
                    project_id=self.project_id,
                    artifact_id=row["artifact_id"],
                    revision_id=row["revision_id"],
                    role=row["role"],
                    content_sha256=row["content_sha256"],
                    byte_size=row["byte_size"],
                    rights_id=row["rights_id"],
                    rights_record_revision=row["rights_record_revision"],
                    rights_digest=row["rights_digest"],
                )
            )
        return entries

    def _verify_entry_pins(self, entries: list[PreparedSnapshotEntry]) -> None:
        """Structurally verify every entry's content/size/rights pin (no head check)."""
        for entry in entries:
            revision = self._artifacts.get_revision(entry.artifact_id, entry.revision_id)
            if revision is None:
                raise HumanApprovalCorruptionError(
                    "human approval: a pinned revision is missing."
                )
            content = self._artifacts.read_content(entry.artifact_id, entry.revision_id)
            if len(content) != entry.byte_size or sha256_digest(content) != entry.content_sha256:
                raise HumanApprovalCorruptionError(
                    "human approval: a pinned revision's content does not match its "
                    "digest or byte size."
                )
            if (
                revision.content_sha256 != entry.content_sha256
                or revision.byte_size != entry.byte_size
            ):
                raise HumanApprovalCorruptionError(
                    "human approval: a pinned revision's stored metadata does not "
                    "match the snapshot."
                )
            if (
                revision.rights_ref.rights_id != entry.rights_id
                or revision.rights_ref.record_revision != entry.rights_record_revision
            ):
                raise HumanApprovalCorruptionError(
                    "human approval: a pinned rights key does not match its revision."
                )
            self._verify_rights_pin(entry)

    def _verify_rights_pin(self, entry: PreparedSnapshotEntry) -> None:
        row = self._conn().execute(
            "SELECT canonical, content_sha256, subject_kind, subject_project_id, "
            " subject_artifact_id, subject_revision_id FROM rights_record "
            "WHERE project_id = ? AND rights_id = ? AND record_revision = ?",
            (self.project_id, entry.rights_id, entry.rights_record_revision),
        ).fetchone()
        if row is None:
            raise HumanApprovalCorruptionError(
                "human approval: a pinned rights record is missing."
            )
        canonical = bytes(row["canonical"])
        if sha256_digest(canonical) != row["content_sha256"]:
            raise HumanApprovalCorruptionError(
                "human approval: a pinned rights record's canonical digest is corrupt."
            )
        if entry.rights_digest and entry.rights_digest != row["content_sha256"]:
            raise HumanApprovalCorruptionError(
                "human approval: a pinned rights digest does not match the record."
            )
        parsed = json.loads(canonical.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise HumanApprovalCorruptionError(
                "human approval: a pinned rights record is not an object."
            )
        if (
            parsed.get("project_id") != self.project_id
            or parsed.get("rights_id") != entry.rights_id
            or parsed.get("record_revision") != entry.rights_record_revision
        ):
            raise HumanApprovalCorruptionError(
                "human approval: a pinned rights record's identity is corrupt."
            )
        subject = parsed.get("subject")
        if not (
            isinstance(subject, dict)
            and subject.get("kind") == "source_revision"
            and subject.get("project_id") == entry.project_id
            and subject.get("artifact_id") == entry.artifact_id
            and subject.get("revision_id") == entry.revision_id
        ):
            raise HumanApprovalCorruptionError(
                "human approval: a pinned rights record's subject does not bind the "
                "pinned revision."
            )
        if (
            row["subject_kind"],
            row["subject_project_id"],
            row["subject_artifact_id"],
            row["subject_revision_id"],
        ) != ("source_revision", entry.project_id, entry.artifact_id, entry.revision_id):
            raise HumanApprovalCorruptionError(
                "human approval: a pinned rights row's subject columns disagree."
            )
        validated = validate_rights_record(parsed, self.project_id)
        require_storage_allowed(validated)

    def _evidence_row(self, auth_id: str) -> sqlite3.Row | None:
        return self._conn().execute(
            "SELECT * FROM owner_auth_evidence WHERE project_id = ? AND auth_id = ?",
            (self.project_id, auth_id),
        ).fetchone()

    @_fail_closed_row("owner auth evidence")
    def _validate_evidence_row(self, row: sqlite3.Row) -> OwnerAuthEvidence:
        """Structurally revalidate one owner-auth evidence row.

        The stored canonical blob must be **exactly** the safe projection
        (:data:`_EVIDENCE_KEYS`): an extra or missing key is refused rather than
        trusted. The method must be the approved owner-auth method, the owner uid
        a non-negative integer (never a JSON boolean), every projected column must
        match the canonical record, and both stored timestamps must be valid.
        """
        if row["project_id"] != self.project_id:
            raise HumanApprovalCorruptionError("owner auth evidence: foreign project row.")
        auth_id = validate_identifier(row["auth_id"], "auth_id")
        _bare_hex(row["action_digest"], "action_digest")
        canonical = bytes(row["canonical"])
        if sha256_digest(canonical) != row["content_sha256"]:
            raise HumanApprovalCorruptionError(
                "owner auth evidence: stored canonical digest mismatch."
            )
        parsed = json.loads(canonical.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise HumanApprovalCorruptionError(
                "owner auth evidence: canonical bytes are not an object."
            )
        if set(parsed) != _EVIDENCE_KEYS:
            raise HumanApprovalCorruptionError(
                "owner auth evidence: canonical bytes are not the exact safe "
                "projection."
            )
        owner_uid = parsed.get("owner_uid")
        if isinstance(owner_uid, bool) or not isinstance(owner_uid, int) or owner_uid < 0:
            raise HumanApprovalCorruptionError(
                "owner auth evidence: the owner uid is not a non-negative integer."
            )
        if (
            parsed.get("auth_id") != auth_id
            or parsed.get("method") != row["method"]
            or owner_uid != row["owner_uid"]
            or parsed.get("action_digest") != row["action_digest"]
        ):
            raise HumanApprovalCorruptionError(
                "owner auth evidence: projected columns do not match the canonical "
                "record."
            )
        if row["method"] != OWNER_APPROVAL_METHOD:
            raise HumanApprovalCorruptionError(
                "owner auth evidence: the method is not the approved owner-auth "
                "method."
            )
        verified_at = _require_timestamp(row["verified_at_utc"], "verified_at_utc")
        if parsed.get("verified_at") != row["verified_at_utc"]:
            raise HumanApprovalCorruptionError(
                "owner auth evidence: verified-at column does not match the record."
            )
        _require_timestamp(row["created_at_utc"], "created_at_utc")
        return OwnerAuthEvidence(
            auth_id=auth_id,
            method=row["method"],
            owner_uid=row["owner_uid"],
            verified_at=verified_at,
            action_digest=row["action_digest"],
        )

    @_fail_closed_row("human approval")
    def _validate_approval_row(self, row: sqlite3.Row) -> HumanApprovalRecord:
        """Structurally revalidate a stored approval, its snapshot, and its evidence.

        Checks the exact canonical snapshot bytes/digest, the recomputed
        ``VersionSet`` (``human_authored``, no invented model), every projected
        column, every entry's pin (content/size/rights — **never** a current-head
        requirement, so honest history stays valid), and the out-of-band
        owner-auth binding (the stored action digest equals the digest of the
        stored approval's exact action).
        """
        if row["project_id"] != self.project_id:
            raise HumanApprovalCorruptionError("human approval: foreign project row.")
        approval_id = validate_identifier(row["approval_id"], "approval_id")
        if row["actor_kind"] != ACTOR_KIND_HUMAN:
            raise HumanApprovalCorruptionError(
                "human approval: actor_kind is not the human constant."
            )
        if row["authority"] != AUTHORITY_OWNER_AUTHENTICATED:
            raise HumanApprovalCorruptionError(
                "human approval: authority is not the owner_authenticated constant."
            )
        if row["authorizes_execution"] != 0 or row["authorizes_export"] != 0:
            raise HumanApprovalCorruptionError(
                "human approval: the authorization columns are not pinned to 0."
            )
        gate_kind = row["gate_kind"]
        if gate_kind not in APPROVAL_GATE_KINDS:
            raise HumanApprovalCorruptionError(
                "human approval: the gate kind is not an approved gate kind."
            )
        decision = row["decision"]
        if decision not in APPROVAL_DECISIONS:
            raise HumanApprovalCorruptionError(
                "human approval: the decision is not an approved decision."
            )
        gate_id = validate_identifier(row["gate_id"], "gate_id")
        rationale = row["rationale"]
        if not isinstance(rationale, str) or rationale == "":
            raise HumanApprovalCorruptionError("human approval: the rationale is empty.")
        # Every stored timestamp must be a real, offset-carrying timestamp: a
        # corrupt time is never silently accepted as an "honest" historical row.
        granted_at = _require_timestamp(row["granted_at_utc"], "granted_at_utc")
        _require_timestamp(row["created_at_utc"], "created_at_utc")
        snapshot_digest = row["snapshot_digest"]
        _bare_hex(snapshot_digest, "snapshot_digest")
        if row["snapshot_schema_version"] != HUMAN_GATE_SNAPSHOT_SCHEMA_VERSION:
            raise HumanApprovalCorruptionError(
                "human approval: the snapshot representation version is unsupported."
            )
        if not _is_supported_storage_schema(row["storage_schema_version"]):
            raise HumanApprovalCorruptionError(
                "human approval: the recorded storage schema version is unsupported."
            )
        if row["snapshot_id"] != "psnap_" + snapshot_digest:
            raise HumanApprovalCorruptionError(
                "human approval: the snapshot id does not match its digest."
            )
        target = {
            "project_id": self.project_id,
            "artifact_id": validate_identifier(row["target_artifact_id"], "artifact_id"),
            "revision_id": validate_identifier(row["target_revision_id"], "revision_id"),
        }

        entries = self._entry_rows(approval_id)
        if entries[0].role != "target":
            raise HumanApprovalCorruptionError(
                "human approval: the snapshot's first entry is not the target."
            )
        if entries[0].artifact_id != target["artifact_id"] or (
            entries[0].revision_id != target["revision_id"]
        ):
            raise HumanApprovalCorruptionError(
                "human approval: the target is not the snapshot's first entry."
            )

        # Rebuild the exact canonical structure from the stored columns + entries
        # and compare it byte-for-byte, so the digest, every projected column, and
        # the VersionSet are all bound to one another.
        version_set = _build_version_set(entries)
        structure = _build_structure(
            project_id=self.project_id,
            target=target,
            gate_kind=gate_kind,
            gate_id=gate_id,
            decision=decision,
            rationale=rationale,
            storage_schema_version=row["storage_schema_version"],
            snapshot_schema_version=row["snapshot_schema_version"],
            version_set=version_set,
            entries=entries,
        )
        canonical = canonical_json(structure)
        if canonical != bytes(row["snapshot_canonical"]):
            raise HumanApprovalCorruptionError(
                "human approval: the canonical snapshot does not match its columns."
            )
        if _sha256_hex(canonical) != snapshot_digest:
            raise HumanApprovalCorruptionError(
                "human approval: the snapshot digest does not match its canonical bytes."
            )
        if bytes(row["version_set_canonical"]) != canonical_json(version_set):
            raise HumanApprovalCorruptionError(
                "human approval: the stored version set does not match the snapshot."
            )
        validated_versions = validate_version_set(version_set)
        if validated_versions["context_kind"] != "human_authored":
            raise HumanApprovalCorruptionError(
                "human approval: the version set is not human_authored."
            )

        # Every pin is verified structurally (never against a moving head).
        self._verify_entry_pins(entries)

        # The out-of-band owner-auth binding: the approval only *points at* the
        # evidence row, and the stored action digest must equal the digest of the
        # approval's exact action (project/target/snapshot/gate/decision/rationale).
        owner_auth_id = validate_identifier(row["owner_auth_id"], "owner_auth_id")
        _bare_hex(row["owner_auth_action_digest"], "owner_auth_action_digest")
        evidence_row = self._evidence_row(owner_auth_id)
        if evidence_row is None:
            raise HumanApprovalCorruptionError(
                "human approval: the owner-auth evidence row is missing."
            )
        evidence = self._validate_evidence_row(evidence_row)
        # The approval is *granted* at the instant the out-of-band confirmation
        # succeeded; a stored granted time that does not equal the evidence's
        # verified time (compared as instants, so an equivalent UTC offset is
        # accepted) is a corrupt authority binding.
        if granted_at != evidence.verified_at:
            raise HumanApprovalCorruptionError(
                "human approval: the granted time does not match the owner-auth "
                "evidence."
            )
        derived_action_digest = action_digest(
            OwnerAction(
                project_id=self.project_id,
                target=target,
                snapshot_digest=snapshot_digest,
                gate_kind=gate_kind,
                gate_id=gate_id,
                decision=decision,
                rationale=rationale,
            )
        )
        if derived_action_digest != row["owner_auth_action_digest"]:
            raise HumanApprovalCorruptionError(
                "human approval: the stored action digest does not match the approval's "
                "exact action."
            )
        if evidence.action_digest != row["owner_auth_action_digest"]:
            raise HumanApprovalCorruptionError(
                "human approval: the evidence action digest does not match the approval."
            )
        if evidence.method != row["owner_auth_method"]:
            raise HumanApprovalCorruptionError(
                "human approval: the recorded owner-auth method does not match the "
                "evidence."
            )
        if evidence.owner_uid != row["owner_uid"]:
            raise HumanApprovalCorruptionError(
                "human approval: the recorded owner uid does not match the evidence."
            )

        return HumanApprovalRecord(
            project_id=self.project_id,
            approval_id=approval_id,
            target=MappingProxyType(dict(target)),
            gate_kind=gate_kind,
            gate_id=gate_id,
            decision=decision,
            rationale=rationale,
            granted_at_utc=row["granted_at_utc"],
            snapshot_id=row["snapshot_id"],
            snapshot_digest=snapshot_digest,
            snapshot_schema_version=row["snapshot_schema_version"],
            storage_schema_version=row["storage_schema_version"],
            owner_auth_id=row["owner_auth_id"],
            owner_auth_method=row["owner_auth_method"],
            owner_auth_action_digest=row["owner_auth_action_digest"],
            owner_uid=row["owner_uid"],
            actor_kind=row["actor_kind"],
            authority=row["authority"],
            created_at_utc=row["created_at_utc"],
            canonical=canonical,
        )

    # -- computed status ----------------------------------------------------- #
    def evaluate(self, approval_id: str) -> HumanApprovalStatus:
        """Compute the current lifecycle status/effect of one approval (C02 §3).

        Recomputes the exact canonical snapshot and every pin on every call, so:

        * a corrupt canonical snapshot, a tampered column, a broken entry pin, or a
          corrupt/mismatched owner-auth evidence row makes it ``invalidated``;
        * a newer approval for the same ``gate_kind`` + ``gate_id`` + target
          artifact (including a *rejection*) makes the older one ``superseded``;
        * a new revision of any pinned *live* input or of the target, or a newer
          ``record_revision`` of a pinned rights record, makes it ``stale`` (a
          historical ``version_of`` ancestor is never head-compared);
        * otherwise it is ``active``.

        The returned effect never authorizes execution or export.
        """
        row = self._approval_row(approval_id)
        if row is None:
            raise RecordValidationError(f"human approval {approval_id!r} does not exist")

        target = {
            "project_id": self.project_id,
            "artifact_id": _safe_str(row["target_artifact_id"]),
            "revision_id": _safe_str(row["target_revision_id"]),
        }
        gate_kind = _safe_str(row["gate_kind"])
        gate_id = _safe_str(row["gate_id"])

        try:
            record = self._validate_approval_row(row)
        except HumanApprovalCorruptionError:
            return HumanApprovalStatus(
                approval_id=_safe_str(row["approval_id"]),
                status="invalidated",
                decision=_safe_str(row["decision"]),
                gate_kind=gate_kind,
                gate_id=gate_id,
                target=MappingProxyType(target),
                snapshot_digest=_safe_str(row["snapshot_digest"]),
                owner_auth_ref=None,
                authorizes_execution=False,
                authorizes_export=False,
                reasons=(
                    "the stored approval, its snapshot, or its owner-auth evidence "
                    "fails integrity revalidation",
                ),
            )

        reasons: list[str] = []
        superseded_by: str | None = None
        newer = self._conn().execute(
            "SELECT approval_id FROM human_approval "
            "WHERE project_id = ? AND gate_kind = ? AND gate_id = ? "
            "AND target_artifact_id = ? AND rowid > ? ORDER BY rowid LIMIT 1",
            (self.project_id, record.gate_kind, record.gate_id, target["artifact_id"], row["_rowid"]),
        ).fetchone()
        if newer is not None:
            status = "superseded"
            superseded_by = newer["approval_id"]
            reasons.append(
                "superseded by a newer approval for the same gate instance and target"
            )
        else:
            entries = self._entry_rows(record.approval_id)
            stale = False
            for entry in entries:
                if entry.role == VERSION_LINEAGE_ROLE:
                    continue
                current = self._artifacts.current_revision(entry.artifact_id)
                if current is None or current.revision_id != entry.revision_id:
                    stale = True
                    reasons.append("a live input's current revision changed")
            for entry in entries:
                newer_rights = self._conn().execute(
                    "SELECT record_revision FROM rights_record WHERE project_id = ? "
                    "AND rights_id = ? AND record_revision > ? ORDER BY record_revision "
                    "LIMIT 1",
                    (self.project_id, entry.rights_id, entry.rights_record_revision),
                ).fetchone()
                if newer_rights is not None:
                    stale = True
                    reasons.append("a pinned rights record has been amended")
            status = "stale" if stale else "active"

        return HumanApprovalStatus(
            approval_id=record.approval_id,
            status=status,
            decision=record.decision,
            gate_kind=record.gate_kind,
            gate_id=record.gate_id,
            target=record.target,
            snapshot_digest=record.snapshot_digest,
            owner_auth_ref=record.owner_auth_id,
            authorizes_execution=False,
            authorizes_export=False,
            reasons=tuple(reasons),
            superseded_by=superseded_by,
        )

    def named_gate_satisfied(
        self, artifact_id: str, *, gate_kind: str, gate_id: str
    ) -> bool:
        """``True`` iff one exact named gate instance is satisfied.

        Satisfied **only** when the current approval for that exact
        ``gate_kind`` + ``gate_id`` + target artifact is ``active`` and
        ``approved``. This is a *named-gate* claim only: it is **not** a global
        execution, manuscript-export, scientific-correctness, or final-release
        claim. A rejection, a stale/superseded/invalidated approval, a corrupt
        row, or a different gate instance all return ``False``.
        """
        try:
            current = self.current_approval(
                artifact_id, gate_kind=gate_kind, gate_id=gate_id
            )
            if current is None:
                return False
            return self.evaluate(current.approval_id).satisfies_named_gate
        except HumanApprovalCorruptionError:
            return False

    # -- verification (staleness-agnostic) ----------------------------------- #
    def verify_snapshot(self, approval_id: str) -> HumanApprovalVerification:
        """Structurally verify one approval (staleness-agnostic, no head check).

        Checks the exact canonical snapshot/digest, every projected column, every
        entry pin, and the owner-auth binding. A legitimate historical
        ``stale``/``superseded`` row verifies ``ok`` — honest history is never
        rejected for having moved on.
        """
        row = self._approval_row(approval_id)
        if row is None:
            raise RecordValidationError(f"human approval {approval_id!r} does not exist")
        try:
            record = self._validate_approval_row(row)
        except HumanApprovalCorruptionError:
            return HumanApprovalVerification(
                approval_id=_safe_str(row["approval_id"]),
                ok=False,
                stored_digest=_safe_str(row["snapshot_digest"]),
                recomputed_digest="",
                problems=(_MSG_CORRUPT_APPROVAL,),
            )
        return HumanApprovalVerification(
            approval_id=record.approval_id,
            ok=True,
            stored_digest=record.snapshot_digest,
            recomputed_digest=record.snapshot_digest,
            problems=(),
        )

    def verify_all_integrity(self) -> list[str]:
        """Return static integrity problems across every row (empty = ok).

        Exported so a later backup verifier can reuse this instead of duplicating
        the structural/hash/link revalidation. Detects a foreign-project row, a
        corrupt approval/evidence row, an orphan (parentless) entry row, and an
        owner-auth evidence row **without exactly one** valid corresponding
        approval (a capability is single-use, so an evidence row must back exactly
        one approval — a deleted approval or a duplicated reference is refused).
        It imposes **no** current-head requirement: a legitimate historical
        ``stale``/``superseded`` row is structurally valid.
        """
        problems: list[str] = []
        approval_rows = self._conn().execute(
            "SELECT rowid AS _rowid, * FROM human_approval ORDER BY rowid"
        ).fetchall()
        valid_approval_ids: set[str] = set()
        valid_evidence_refs: dict[str, int] = {}
        for index, row in enumerate(approval_rows):
            if row["project_id"] != self.project_id:
                problems.append(
                    f"human_approval: row {index} belongs to a foreign project"
                )
                continue
            try:
                self._validate_approval_row(row)
            except Exception:  # noqa: BLE001 - a corrupt row must not abort the sweep
                problems.append(
                    f"human_approval: row {index} failed integrity revalidation"
                )
            else:
                valid_approval_ids.add(row["approval_id"])
                auth_id = row["owner_auth_id"]
                if isinstance(auth_id, str):
                    valid_evidence_refs[auth_id] = (
                        valid_evidence_refs.get(auth_id, 0) + 1
                    )

        entry_rows = self._conn().execute(
            "SELECT project_id, approval_id FROM human_approval_entry ORDER BY rowid"
        ).fetchall()
        for index, row in enumerate(entry_rows):
            if row["project_id"] != self.project_id:
                problems.append(
                    f"human_approval_entry: row {index} belongs to a foreign project"
                )
                continue
            if row["approval_id"] not in valid_approval_ids:
                problems.append(
                    f"human_approval_entry: row {index} has no valid parent approval"
                )

        evidence_rows = self._conn().execute(
            "SELECT * FROM owner_auth_evidence ORDER BY rowid"
        ).fetchall()
        for index, row in enumerate(evidence_rows):
            if row["project_id"] != self.project_id:
                problems.append(
                    f"owner_auth_evidence: row {index} belongs to a foreign project"
                )
                continue
            try:
                self._validate_evidence_row(row)
            except Exception:  # noqa: BLE001 - a corrupt row must not abort the sweep
                problems.append(
                    f"owner_auth_evidence: row {index} failed integrity revalidation"
                )
                continue
            references = valid_evidence_refs.get(row["auth_id"], 0)
            if references == 0:
                problems.append(
                    f"owner_auth_evidence: row {index} has no valid corresponding "
                    f"approval"
                )
            elif references > 1:
                problems.append(
                    f"owner_auth_evidence: row {index} is referenced by more than one "
                    f"valid approval"
                )
        return problems


# --------------------------------------------------------------------------- #
# Small validation helpers
# --------------------------------------------------------------------------- #
def _require_gate_kind(value: object) -> str:
    if value not in APPROVAL_GATE_KINDS:
        raise RecordValidationError(
            "gate_kind: is not one of the approved gate kinds"
        )
    return value  # type: ignore[return-value]


def _require_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str) or value == "":
        raise HumanApprovalCorruptionError(f"{field}: must be a timestamp string")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise HumanApprovalCorruptionError(f"{field}: is not a valid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HumanApprovalCorruptionError(f"{field}: must carry an explicit offset")
    return parsed


def _capability_deny_reason(exc: OwnerAuthError) -> str:
    if isinstance(exc, InvalidCapabilityError):
        return "capability_invalid"
    if isinstance(exc, CrossProcessCapabilityError):
        return "capability_foreign_process"
    if isinstance(exc, ActionDigestMismatch):
        return "action_mismatch"
    if isinstance(exc, ApprovalExpired):
        return "approval_expired"
    if isinstance(exc, ApprovalAlreadyConsumed):
        return "approval_replayed"
    if isinstance(exc, OwnerUidMismatch):
        return "owner_uid_mismatch"
    if isinstance(exc, OwnerIdentityError):
        return "owner_identity"
    return "capability_rejected"


# --------------------------------------------------------------------------- #
# Read-only export (shared with a later backup verifier)
# --------------------------------------------------------------------------- #
def integrity_problems(conn: sqlite3.Connection, project_id: str) -> list[str]:
    """Revalidate every human-approval row for ``project_id`` on ``conn``.

    Read-only. Returns a list of static problem descriptions (empty when every
    row is structurally intact, internally consistent, and correctly linked). It
    detects a foreign-project row, a corrupt approval/evidence row, an orphan
    (parentless) entry row, and an owner-auth evidence row without exactly one
    valid corresponding approval (a capability is single-use), and imposes **no**
    current-head requirement — a legitimate historical ``stale``/``superseded``
    row is structurally valid. The connection is never written to.
    """
    repo = HumanApprovalRepository(_ReadOnlyStoreView(conn, project_id))
    return repo.verify_all_integrity()
