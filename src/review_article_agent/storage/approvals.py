"""Local-storage-only fixture approvals and exact dependency snapshots.

This module is the Step 02 storage-side **approval fixture** layer. It sits on
top of an already-verified :class:`~review_article_agent.storage.store.ProjectStore`
and reuses the immutable-revision reads of
:class:`~review_article_agent.storage.artifacts.ArtifactRepository`.

What this deliberately **is not**
---------------------------------

* It is **not** a scientific approval. No protocol/pilot/final-release gate is
  operational, and no scientific validity or completeness is claimed.
* It is **not** an authority check. :meth:`ApprovalRepository.record_fixture_approval`
  never mints, trusts, or records owner authentication; the recorded
  ``actor_label`` is a descriptive string, never a claim of a human identity, and
  ``actor_authenticated`` is pinned to ``0``.
* An imported/generic artifact payload can **never** be accepted as an approval:
  the approval tables are written **only** by
  :meth:`ApprovalRepository.record_fixture_approval`. A generic
  :meth:`ArtifactRepository.import_revision` of an approval-shaped JSON envelope
  creates a plain revision and no approval.
* An active local fixture approval **cannot authorize execution or export**
  (``authorizes_execution``/``authorizes_export`` are always ``False``, and the
  database pins ``authorizes_execution = 0`` and ``scope = 'storage_fixture'``).

What it guarantees
------------------

* **Exact, immutable snapshot (C02 §2.2).** Recording an approval captures the
  exact target revision **and** its full transitive dependency closure, each
  entry pinned by ``{project_id, artifact_id, revision_id}`` plus the revision's
  canonical ``content_sha256`` digest, the pinned rights key, and byte size. The
  target is always entry 0, so an artifact with no dependencies still produces a
  non-empty, exact snapshot.
* **Immutable rows + recomputed status (C02 §3).** Approval and snapshot rows are
  immutable at the database (never ``UPDATE``d/``DELETE``d). Status is
  **computed** on every read: if any pinned artifact's current revision moved, the
  approval is ``stale``; if a stored digest no longer matches the recomputed
  digest (or a pinned revision is missing), it is ``invalidated``; if a newer
  approval exists for the same local gate instance, the older one is
  ``superseded``; otherwise ``active``. Re-approval creates a **new** row and
  never flips an old row back to ``active``.
* **Verifiable for backup.** :meth:`ApprovalRepository.verify_snapshot` and
  :meth:`ApprovalRepository.snapshot_structure` let a backup verifier reconstruct
  and check the structural snapshot of **any** row — including valid historical
  ``stale``/``superseded`` rows — without rejecting honest history.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from ..domain.errors import StoreError
from ..domain.records import (
    RecordValidationError,
    canonical_json,
    validate_identifier,
    validate_ref,
)
from .artifacts import ArtifactRepository, sha256_digest

#: The only approval scope this local layer records. Never a real mandate.
SCOPE_STORAGE_FIXTURE = "storage_fixture"

#: Fixture-local gate kinds. Deliberately distinct from the real C02 gate set so
#: no real ``protocol``/``pilot``/``final_release`` gate is exposed or implied.
FIXTURE_GATE_KINDS = (
    "fixture_outline",
    "fixture_framework",
    "fixture_artifact_revision",
    "fixture_batch",
    "fixture_review",
)

#: Disposition enum (C02 §2.1).
FIXTURE_DECISIONS = ("approved", "rejected")

#: Computed lifecycle states (C02 §3). Never stored — always recomputed.
FIXTURE_STATUSES = ("active", "stale", "invalidated", "superseded")

#: Snapshot entry roles. ``target`` marks the approved revision itself, so an
#: artifact with no dependencies still has one exact entry (C02 §2.2).
SNAPSHOT_ENTRY_ROLES = (
    "target",
    "source_document",
    "summary",
    "finding",
    "rubric",
    "framework",
    "outline",
    "parser_output",
    "model_output",
    "question_def",
    "other",
)

#: Maps an artifact kind (C01 §4) to a snapshot entry role.
_KIND_TO_ROLE = {
    "document": "source_document",
    "finding": "finding",
    "rubric": "rubric",
    "framework": "framework",
    "outline": "outline",
    "report": "summary",
}

#: Generic, human-authored context marker. This module records **no** fabricated
#: model/agent version; the snapshot is a local, human-authored fixture artifact.
CONTEXT_KIND_HUMAN_AUTHORED = "human_authored"

#: Version of the approval snapshot *structure* (independent of the store
#: schema version, which is recorded inside the snapshot).
STORAGE_SNAPSHOT_SCHEMA_VERSION = 1

#: Default descriptive fixture actor label (never an authenticated human).
DEFAULT_FIXTURE_ACTOR = "local-storage-fixture"


class ApprovalError(StoreError):
    """A local storage-fixture approval operation failed."""


# --------------------------------------------------------------------------- #
# Local validation helpers (mirror the storage layer's fail-closed style)
# --------------------------------------------------------------------------- #
def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_nonempty_str(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise RecordValidationError(f"{field}: must be a string")
    if value == "" or value != value.strip():
        raise RecordValidationError(
            f"{field}: must be non-empty with no leading/trailing whitespace"
        )
    return value


def _require_enum(value: object, allowed: tuple[str, ...], field: str) -> str:
    if value not in allowed:
        raise RecordValidationError(
            f"{field}: {value!r} is not one of {list(allowed)}"
        )
    return value  # type: ignore[return-value]


def _require_timestamp(value: object, field: str) -> str:
    text = _require_nonempty_str(value, field)
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise RecordValidationError(f"{field}: not a valid RFC 3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RecordValidationError(f"{field}: must carry an explicit timezone offset")
    return text


def _digest_of_content(content: bytes) -> str:
    return sha256_digest(content)


# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FixtureSnapshotEntry:
    """One exact, immutable snapshot entry (C02 §2.2 ``SnapshotEntry``)."""

    project_id: str
    artifact_id: str
    revision_id: str
    content_sha256: str
    role: str
    rights_id: str
    rights_record_revision: int
    byte_size: int

    def as_dict(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "artifact_id": self.artifact_id,
            "revision_id": self.revision_id,
            "content_sha256": self.content_sha256,
            "role": self.role,
            "rights_id": self.rights_id,
            "rights_record_revision": self.rights_record_revision,
            "byte_size": self.byte_size,
        }


@dataclass(frozen=True)
class FixtureApproval:
    """One immutable local storage-fixture approval row (raw, as stored)."""

    project_id: str
    approval_id: str
    target_artifact_id: str
    target_revision_id: str
    gate_kind: str
    gate_id: str
    decision: str
    scope: str
    authorizes_execution: bool
    actor_label: str
    actor_authenticated: bool
    rationale: str
    granted_at_utc: str
    snapshot_id: str
    snapshot_digest: str
    storage_schema_version: int
    context_kind: str
    context_note: str | None
    created_at_utc: str

    @property
    def target(self) -> dict[str, str]:
        return {
            "project_id": self.project_id,
            "artifact_id": self.target_artifact_id,
            "revision_id": self.target_revision_id,
        }


@dataclass(frozen=True)
class FixtureApprovalStatus:
    """Computed lifecycle status/effect of one approval (C02 §3).

    ``authorizes_execution`` and ``authorizes_export`` are always ``False``: a
    local fixture approval never authorizes real execution or export.
    """

    approval_id: str
    status: str
    decision: str
    scope: str
    authorizes_execution: bool
    authorizes_export: bool
    reasons: tuple[str, ...]
    snapshot_digest: str
    superseded_by: str | None = None


@dataclass(frozen=True)
class SnapshotVerification:
    """Structural verification of one approval's snapshot (backup verifier)."""

    approval_id: str
    ok: bool
    stored_digest: str
    recomputed_digest: str
    problems: tuple[str, ...]


# --------------------------------------------------------------------------- #
# Repository
# --------------------------------------------------------------------------- #
class ApprovalRepository:
    """Immutable local storage-fixture approvals over a verified store.

    Typical use::

        store = ProjectStore.open_at(app_dir, expected_project_id=pid)
        repo = ArtifactRepository(store)
        approvals = ApprovalRepository(store)

        approvals.record_fixture_approval(
            target={"project_id": pid, "artifact_id": aid, "revision_id": rid},
            gate_kind="fixture_artifact_revision",
            gate_id="gate-local-1",
            decision="approved",
            rationale="synthetic local check fixture; not a scientific approval",
        )
        status = approvals.evaluate(approval_id)  # 'active' | 'stale' | ...
        assert status.authorizes_execution is False
    """

    def __init__(self, store, artifacts: ArtifactRepository | None = None) -> None:
        self._store = store
        self._artifacts = artifacts if artifacts is not None else ArtifactRepository(store)

    @property
    def project_id(self) -> str:
        return self._store.project_id

    def _conn(self) -> sqlite3.Connection:
        return self._store.connection

    # -- identifiers --------------------------------------------------------- #
    @staticmethod
    def new_approval_id() -> str:
        return "apv_" + uuid.uuid4().hex

    # -- writes -------------------------------------------------------------- #
    def record_fixture_approval(
        self,
        *,
        target: object,
        gate_kind: str,
        gate_id: str,
        decision: str,
        rationale: str,
        actor_label: str = DEFAULT_FIXTURE_ACTOR,
        context_note: str | None = None,
        approval_id: str | None = None,
        granted_at_utc: str | None = None,
    ) -> FixtureApproval:
        """Record one **local storage-fixture** approval with an exact snapshot.

        This is the *only* way an approval row is created, and it is explicitly a
        local fixture (never a scientific/human approval authority). ``target`` is
        an exact ``RefRevision``; its full transitive dependency closure is
        snapshotted. The write is one transaction: any validation failure or
        injected error leaves **no** approval row and no dangling entries.

        Deliberately **not** accepted: any in-band authority/authentication field
        (a payload boolean, ``actor_kind``, an ``Actor`` object) — such extra
        keyword arguments raise :class:`TypeError` and are never trusted.
        """
        ref = validate_ref(target, self.project_id)
        gate_kind = _require_enum(gate_kind, FIXTURE_GATE_KINDS, "gate_kind")
        decision = _require_enum(decision, FIXTURE_DECISIONS, "decision")
        rationale = _require_nonempty_str(rationale, "rationale")
        actor_label = _require_nonempty_str(actor_label, "actor_label")
        gate_id = validate_identifier(gate_id, "gate_id")
        if approval_id is None:
            approval_id = self.new_approval_id()
        approval_id = validate_identifier(approval_id, "approval_id")
        if context_note is not None:
            context_note = _require_nonempty_str(context_note, "context_note")
        granted = (
            _utc_now()
            if granted_at_utc is None
            else _require_timestamp(granted_at_utc, "granted_at_utc")
        )

        # Build (and thereby validate) the exact snapshot before any write, so a
        # missing/cross-project reference raises with nothing persisted.
        entries = self._build_snapshot(ref)
        snapshot_id = "snap_" + uuid.uuid4().hex
        structure = self._snapshot_structure(
            target=ref,
            gate_kind=gate_kind,
            gate_id=gate_id,
            decision=decision,
            context_note=context_note,
            entries=entries,
        )
        snapshot_digest = _digest_of_content(canonical_json(structure))
        created = _utc_now()

        with self._store.transaction():
            self._conn().execute(
                "INSERT INTO fixture_approval "
                "(project_id, approval_id, target_artifact_id, target_revision_id, "
                " gate_kind, gate_id, decision, scope, authorizes_execution, "
                " actor_label, actor_authenticated, rationale, granted_at_utc, "
                " snapshot_id, snapshot_digest, storage_schema_version, "
                " context_kind, context_note, created_at_utc) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.project_id,
                    approval_id,
                    ref["artifact_id"],
                    ref["revision_id"],
                    gate_kind,
                    gate_id,
                    decision,
                    SCOPE_STORAGE_FIXTURE,
                    actor_label,
                    rationale,
                    granted,
                    snapshot_id,
                    snapshot_digest,
                    self._store.schema_version,
                    CONTEXT_KIND_HUMAN_AUTHORED,
                    context_note,
                    created,
                ),
            )
            for index, entry in enumerate(entries):
                self._insert_snapshot_entry(approval_id, index, entry)

        stored = self.get_approval(approval_id)
        assert stored is not None  # just written in a committed transaction
        return stored

    def _insert_snapshot_entry(
        self, approval_id: str, index: int, entry: FixtureSnapshotEntry
    ) -> None:
        """Insert one immutable snapshot entry (separate method for fault tests)."""
        self._conn().execute(
            "INSERT INTO fixture_approval_entry "
            "(project_id, approval_id, entry_index, artifact_id, revision_id, "
            " content_sha256, role, rights_id, rights_record_revision, byte_size) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.project_id,
                approval_id,
                index,
                entry.artifact_id,
                entry.revision_id,
                entry.content_sha256,
                entry.role,
                entry.rights_id,
                entry.rights_record_revision,
                entry.byte_size,
            ),
        )

    # -- snapshot construction ----------------------------------------------- #
    def _build_snapshot(self, target_ref: dict[str, str]) -> list[FixtureSnapshotEntry]:
        """Return the exact target + full transitive dependency closure.

        The target is entry 0 (role ``target``); an artifact with no dependencies
        therefore still yields a non-empty exact snapshot. Every referenced
        revision must exist in this project.
        """
        entries: list[FixtureSnapshotEntry] = []
        seen: set[tuple[str, str]] = set()
        queue: list[tuple[str, str, str]] = [
            (target_ref["artifact_id"], target_ref["revision_id"], "target")
        ]
        while queue:
            artifact_id, revision_id, role = queue.pop(0)
            key = (artifact_id, revision_id)
            if key in seen:
                continue
            seen.add(key)
            revision = self._artifacts.get_revision(artifact_id, revision_id)
            if revision is None:
                raise RecordValidationError(
                    f"approval snapshot: revision {artifact_id!r}/{revision_id!r} "
                    f"does not exist in this project"
                )
            entries.append(
                FixtureSnapshotEntry(
                    project_id=self.project_id,
                    artifact_id=artifact_id,
                    revision_id=revision_id,
                    content_sha256=revision.content_sha256,
                    role=role,
                    rights_id=revision.rights_ref.rights_id,
                    rights_record_revision=revision.rights_ref.record_revision,
                    byte_size=revision.byte_size,
                )
            )
            dependencies = sorted(
                self._artifacts.list_dependencies(artifact_id, revision_id),
                key=lambda d: (d.artifact_id, d.revision_id, d.role),
            )
            for dep in dependencies:
                queue.append(
                    (dep.artifact_id, dep.revision_id, self._role_for(dep.artifact_id))
                )
        return entries

    def _role_for(self, artifact_id: str) -> str:
        artifact = self._artifacts.get_artifact(artifact_id)
        kind = artifact.kind if artifact is not None else ""
        return _KIND_TO_ROLE.get(kind, "other")

    def _snapshot_structure(
        self,
        *,
        target: dict[str, str],
        gate_kind: str,
        gate_id: str,
        decision: str,
        context_note: str | None,
        entries: list[FixtureSnapshotEntry],
    ) -> dict[str, object]:
        """Return the canonical structural snapshot object (source of the digest)."""
        return {
            "snapshot_schema_version": STORAGE_SNAPSHOT_SCHEMA_VERSION,
            "storage_schema_version": self._store.schema_version,
            "project_id": self.project_id,
            "target": {
                "project_id": target["project_id"],
                "artifact_id": target["artifact_id"],
                "revision_id": target["revision_id"],
            },
            "gate_kind": gate_kind,
            "gate_id": gate_id,
            "decision": decision,
            "scope": SCOPE_STORAGE_FIXTURE,
            "context": {"kind": CONTEXT_KIND_HUMAN_AUTHORED, "note": context_note},
            "entries": [entry.as_dict() for entry in entries],
        }

    # -- reads --------------------------------------------------------------- #
    def _approval_row(self, approval_id: str) -> sqlite3.Row | None:
        approval_id = validate_identifier(approval_id, "approval_id")
        return self._conn().execute(
            "SELECT rowid AS _rowid, * FROM fixture_approval "
            "WHERE project_id = ? AND approval_id = ?",
            (self.project_id, approval_id),
        ).fetchone()

    def get_approval(self, approval_id: str) -> FixtureApproval | None:
        row = self._approval_row(approval_id)
        return None if row is None else self._row_to_approval(row)

    def list_approvals(self) -> list[FixtureApproval]:
        rows = self._conn().execute(
            "SELECT rowid AS _rowid, * FROM fixture_approval "
            "WHERE project_id = ? ORDER BY rowid",
            (self.project_id,),
        ).fetchall()
        return [self._row_to_approval(row) for row in rows]

    def snapshot_entries(self, approval_id: str) -> list[FixtureSnapshotEntry]:
        row = self._approval_row(approval_id)
        if row is None:
            raise RecordValidationError(f"approval {approval_id!r} does not exist")
        return self._entry_rows(row["approval_id"])

    def _entry_rows(self, approval_id: str) -> list[FixtureSnapshotEntry]:
        rows = self._conn().execute(
            "SELECT * FROM fixture_approval_entry "
            "WHERE project_id = ? AND approval_id = ? ORDER BY entry_index",
            (self.project_id, approval_id),
        ).fetchall()
        return [
            FixtureSnapshotEntry(
                project_id=row["project_id"],
                artifact_id=row["artifact_id"],
                revision_id=row["revision_id"],
                content_sha256=row["content_sha256"],
                role=row["role"],
                rights_id=row["rights_id"],
                rights_record_revision=row["rights_record_revision"],
                byte_size=row["byte_size"],
            )
            for row in rows
        ]

    @staticmethod
    def _row_to_approval(row: sqlite3.Row) -> FixtureApproval:
        return FixtureApproval(
            project_id=row["project_id"],
            approval_id=row["approval_id"],
            target_artifact_id=row["target_artifact_id"],
            target_revision_id=row["target_revision_id"],
            gate_kind=row["gate_kind"],
            gate_id=row["gate_id"],
            decision=row["decision"],
            scope=row["scope"],
            authorizes_execution=bool(row["authorizes_execution"]),
            actor_label=row["actor_label"],
            actor_authenticated=bool(row["actor_authenticated"]),
            rationale=row["rationale"],
            granted_at_utc=row["granted_at_utc"],
            snapshot_id=row["snapshot_id"],
            snapshot_digest=row["snapshot_digest"],
            storage_schema_version=row["storage_schema_version"],
            context_kind=row["context_kind"],
            context_note=row["context_note"],
            created_at_utc=row["created_at_utc"],
        )

    def current_approval(
        self, artifact_id: str, *, gate_kind: str, gate_id: str
    ) -> FixtureApproval | None:
        """The latest approval for one local gate instance + target artifact.

        The derived "current local approval" for a target; it may itself be
        ``stale`` after a later edit and does not approve the newer revision.
        """
        artifact_id = validate_identifier(artifact_id, "artifact_id")
        gate_kind = _require_enum(gate_kind, FIXTURE_GATE_KINDS, "gate_kind")
        gate_id = validate_identifier(gate_id, "gate_id")
        row = self._conn().execute(
            "SELECT rowid AS _rowid, * FROM fixture_approval "
            "WHERE project_id = ? AND gate_kind = ? AND gate_id = ? "
            "AND target_artifact_id = ? ORDER BY rowid DESC LIMIT 1",
            (self.project_id, gate_kind, gate_id, artifact_id),
        ).fetchone()
        return None if row is None else self._row_to_approval(row)

    # -- verification / status ----------------------------------------------- #
    def snapshot_structure(
        self, approval_id: str
    ) -> dict[str, object]:
        """Reconstruct the canonical structural snapshot from stored rows.

        Exposed so a backup verifier can rebuild and check the exact snapshot of
        any row, including historical ``stale``/``superseded`` rows.
        """
        row = self._approval_row(approval_id)
        if row is None:
            raise RecordValidationError(f"approval {approval_id!r} does not exist")
        entries = self._entry_rows(row["approval_id"])
        return self._snapshot_structure(
            target={
                "project_id": row["project_id"],
                "artifact_id": row["target_artifact_id"],
                "revision_id": row["target_revision_id"],
            },
            gate_kind=row["gate_kind"],
            gate_id=row["gate_id"],
            decision=row["decision"],
            context_note=row["context_note"],
            entries=entries,
        )

    def verify_snapshot(self, approval_id: str) -> SnapshotVerification:
        """Structurally verify one approval's snapshot (staleness-agnostic).

        Checks that the stored ``snapshot_digest`` matches the digest recomputed
        from the stored entries, and that each entry's recorded digest still
        matches the revision's actual stored content. A valid stale/superseded
        historical row verifies ``ok`` — honest history is never rejected.
        """
        row = self._approval_row(approval_id)
        if row is None:
            raise RecordValidationError(f"approval {approval_id!r} does not exist")
        entries = self._entry_rows(row["approval_id"])
        structure = self._snapshot_structure(
            target={
                "project_id": row["project_id"],
                "artifact_id": row["target_artifact_id"],
                "revision_id": row["target_revision_id"],
            },
            gate_kind=row["gate_kind"],
            gate_id=row["gate_id"],
            decision=row["decision"],
            context_note=row["context_note"],
            entries=entries,
        )
        recomputed = _digest_of_content(canonical_json(structure))
        problems: list[str] = []
        if recomputed != row["snapshot_digest"]:
            problems.append(
                "stored snapshot digest does not match the recomputed structural digest"
            )
        for entry in entries:
            try:
                content = self._artifacts.read_content(
                    entry.artifact_id, entry.revision_id
                )
            except RecordValidationError:
                problems.append(
                    f"pinned revision {entry.artifact_id!r}/{entry.revision_id!r} "
                    f"is missing"
                )
                continue
            if _digest_of_content(content) != entry.content_sha256:
                problems.append(
                    f"content digest mismatch for "
                    f"{entry.artifact_id!r}/{entry.revision_id!r}"
                )
        return SnapshotVerification(
            approval_id=row["approval_id"],
            ok=not problems,
            stored_digest=row["snapshot_digest"],
            recomputed_digest=recomputed,
            problems=tuple(problems),
        )

    def evaluate(self, approval_id: str) -> FixtureApprovalStatus:
        """Compute the current lifecycle status/effect of one approval (C02 §3).

        Recomputes the snapshot digest and every pinned artifact's current head on
        every call, so a new revision of *any* transitive dependency (or of the
        target) makes the affected approval ``stale`` while unrelated approvals
        stay ``active``; a corrupt/mismatched stored digest ``invalidates`` it;
        and a newer approval for the same gate instance ``supersedes`` the older
        one. The returned effect never authorizes execution or export.
        """
        row = self._approval_row(approval_id)
        if row is None:
            raise RecordValidationError(f"approval {approval_id!r} does not exist")
        entries = self._entry_rows(row["approval_id"])
        reasons: list[str] = []

        # 1) Structural / digest integrity first (invalidated wins).
        structure = self._snapshot_structure(
            target={
                "project_id": row["project_id"],
                "artifact_id": row["target_artifact_id"],
                "revision_id": row["target_revision_id"],
            },
            gate_kind=row["gate_kind"],
            gate_id=row["gate_id"],
            decision=row["decision"],
            context_note=row["context_note"],
            entries=entries,
        )
        invalidated = False
        if _digest_of_content(canonical_json(structure)) != row["snapshot_digest"]:
            invalidated = True
            reasons.append("snapshot digest mismatch; the stored snapshot is corrupt")
        for entry in entries:
            revision = self._artifacts.get_revision(
                entry.artifact_id, entry.revision_id
            )
            if revision is None:
                invalidated = True
                reasons.append(
                    f"pinned revision {entry.artifact_id!r}/{entry.revision_id!r} "
                    f"is missing"
                )
                continue
            if revision.content_sha256 != entry.content_sha256:
                invalidated = True
                reasons.append(
                    f"stored content digest mismatch for "
                    f"{entry.artifact_id!r}/{entry.revision_id!r}"
                )
                continue
            content = self._artifacts.read_content(entry.artifact_id, entry.revision_id)
            if _digest_of_content(content) != entry.content_sha256:
                invalidated = True
                reasons.append(
                    f"recomputed content digest mismatch for "
                    f"{entry.artifact_id!r}/{entry.revision_id!r}"
                )

        superseded_by: str | None = None
        if invalidated:
            status = "invalidated"
        else:
            newer = self._conn().execute(
                "SELECT approval_id FROM fixture_approval "
                "WHERE project_id = ? AND gate_kind = ? AND gate_id = ? "
                "AND target_artifact_id = ? AND rowid > ? "
                "ORDER BY rowid LIMIT 1",
                (
                    self.project_id,
                    row["gate_kind"],
                    row["gate_id"],
                    row["target_artifact_id"],
                    row["_rowid"],
                ),
            ).fetchone()
            if newer is not None:
                status = "superseded"
                superseded_by = newer["approval_id"]
                reasons.append(
                    f"superseded by a newer local approval {superseded_by!r} for the "
                    f"same gate instance"
                )
            else:
                stale = False
                for entry in entries:
                    current = self._artifacts.current_revision(entry.artifact_id)
                    if current is None or current.revision_id != entry.revision_id:
                        stale = True
                        current_id = None if current is None else current.revision_id
                        reasons.append(
                            f"current revision of {entry.artifact_id!r} is "
                            f"{current_id!r}, not the approved {entry.revision_id!r}"
                        )
                status = "stale" if stale else "active"

        return FixtureApprovalStatus(
            approval_id=row["approval_id"],
            status=status,
            decision=row["decision"],
            scope=row["scope"],
            authorizes_execution=False,
            authorizes_export=False,
            reasons=tuple(reasons),
            snapshot_digest=row["snapshot_digest"],
            superseded_by=superseded_by,
        )


__all__ = [
    "ApprovalError",
    "ApprovalRepository",
    "CONTEXT_KIND_HUMAN_AUTHORED",
    "DEFAULT_FIXTURE_ACTOR",
    "FIXTURE_DECISIONS",
    "FIXTURE_GATE_KINDS",
    "FIXTURE_STATUSES",
    "FixtureApproval",
    "FixtureApprovalStatus",
    "FixtureSnapshotEntry",
    "SCOPE_STORAGE_FIXTURE",
    "SNAPSHOT_ENTRY_ROLES",
    "STORAGE_SNAPSHOT_SCHEMA_VERSION",
    "SnapshotVerification",
]
