"""Durable authorized source import with exact-byte duplicate occurrences.

Step 06 (acquisition) storage layer, built **on top of** the existing
:class:`~review_article_agent.storage.artifacts.ArtifactRepository`. It adds no
schema, CLI, worker, or scientific processing, and it does **not** depend on any
acquisition validator.

What it guarantees
------------------

* **Separate rights gates (C03 section 5).** A source's bytes are stored only when
  its ``RightsRecord`` allows ``acquisition`` *and* ``storage`` (separately), the
  record is the latest ``record_revision`` for its ``rights_id``, and privacy
  denial is clear. ``unknown`` storage stores **no** bytes; the metadata-only path
  is :meth:`AssetImportRepository.record_missing`, which never fabricates bytes
  or a revision.
* **Occurrence != revision (C01 sections 5.8, 6.1).** Every call records one typed,
  immutable ``acquisition_import_occurrence`` v1 envelope as a ``report``
  artifact revision (id derived deterministically from ``request_id``). The
  record is an honest **proposal**: it authorizes no use and claims no
  scientific acceptance. Its own ``RightsRecord`` is supplied separately by the
  caller; this module never invents an unrestricted one.
* **Exact-byte duplicates are global (C01 §6.1 R1).** Equal bytes inside the
  *same* explicit document artifact reuse the historical source revision (even
  from a different origin) and record a new occurrence with the new origin,
  rights and time. Equal bytes requested under a *different* document artifact
  are vetoed atomically: the import raises a static ``cross_artifact_duplicate``
  conflict carrying safe ``matching_refs`` and a guidance string, and writes no
  second artifact, revision, rights record or occurrence. Only document source
  revisions are candidates (``artifact.kind = 'document'``); produced report
  bytes are never treated as a canonical paper. The caller must
  explicitly target the canonical artifact (with rights bound to its exact
  source revision) and retry, or record a ``no_full_text_acquired`` occurrence —
  identity and rights are never inferred from bytes. Changed rights that cannot
  authorize reuse of the exact existing source fail closed.
* **Atomic.** Source revision, rights, artifact rows and occurrence are written in
  one outer ``BEGIN IMMEDIATE`` transaction; the repository's own inner writes
  run as SAVEPOINTs, so a failure leaves no orphan artifact or revision.
* **Idempotent / conflict-aware.** An exact replay of a ``request_id`` returns the
  historical occurrence/source refs (also after restart or a head change) and
  authorizes nothing new; a divergent replay raises a conflict.

Errors are static and carry no peer-supplied text.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

from ..domain.records import (
    RecordValidationError,
    canonical_json,
    validate_identifier,
    validate_ref,
    validate_rights_record,
)
from ..storage import opmemo
from ..storage.artifacts import (
    ArtifactConflictError,
    ArtifactRepository,
    ArtifactRepositoryError,
    DependencyRef,
    RightsRef,
    sha256_digest,
)

#: Hard input bounds (source bytes, identifiers, free-text metadata).
MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_IDENTIFIER_CHARS = 200
MAX_LOCATOR_CHARS = 2048
MAX_REASON_CHARS = 1000
MAX_DEPENDENCIES = 64
MAX_RIGHTS_BYTES = 64 * 1024
#: Technical bound on the number of same-hash/same-size candidate revisions the
#: global exact-byte scan will byte-compare. Candidate rows are found by a
#: metadata-only query; only this many are ever read as BLOBs. Exceeding it is an
#: explicit diagnostic failure (never a silent truncation of the scan).
MAX_BYTE_COLLISION_CANDIDATES = 256

OCCURRENCE_DOCUMENT_TYPE = "acquisition_import_occurrence"
OCCURRENCE_RECORD_VERSION = 1
DISPOSITIONS = (
    "created_new_revision",
    "duplicate_bytes_of_existing_revision",
    "no_full_text_acquired",
)
#: Explicit relationships a caller may declare on a source revision. ``version_of``
#: is derived from ``parent_revision_id`` only; nothing is inferred from titles.
IMPORT_DEPENDENCY_ROLES = (
    "reports",
    "uses_dataset",
    "describes_experiment",
    "derived_from",
)

#: Static guidance attached to a ``cross_artifact_duplicate`` conflict. It names
#: no peer text and authorizes nothing: the caller must reconcile explicitly.
CROSS_ARTIFACT_DUPLICATE_GUIDANCE = (
    "identical bytes are already stored under another document artifact in this "
    "project (C01 6.1 R1); no second source revision or independent artifact was "
    "created. Target the canonical artifact explicitly with rights bound to its "
    "exact source revision and retry, or record a no_full_text_acquired occurrence. "
    "Do not infer shared identity or fabricate rights from byte equality."
)

_MESSAGES = {
    "invalid_input": "import input is invalid or out of bounds.",
    "content_too_large": "source bytes exceed the import size bound.",
    "rights_invalid": "the supplied rights record is invalid for this subject.",
    "acquisition_not_allowed": "rights do not allow acquisition.",
    "storage_not_allowed": "rights do not allow storage; no bytes were retained.",
    "privacy_denied": "rights record a privacy denial; import blocked.",
    "rights_superseded": "the supplied rights record is superseded by a newer revision.",
    "rights_conflict": "rights cannot authorize reuse of the exact existing source.",
    "request_conflict": "request_id was already used with different arguments.",
    "source_rejected": "the source revision was rejected by the artifact store.",
    "occurrence_rejected": "the occurrence record was rejected by the artifact store.",
    "dependency_conflict": "declared dependencies differ from the existing source.",
    "kind_conflict": "the artifact id is already used for a different kind.",
    "occurrence_invalid": "the stored occurrence failed validation.",
    "cross_artifact_duplicate": (
        "identical bytes already exist under another artifact; no second source "
        "revision was created."
    ),
    "duplicate_scan_overflow": (
        "too many same-hash candidate revisions to confirm an exact-byte match "
        "safely; the scan was not truncated."
    ),
    "storage_failure": "the local database operation failed.",
}


class AssetImportError(ArtifactRepositoryError):
    """An import failed; carries a static ``code`` and a peer-text-free message.

    A ``cross_artifact_duplicate`` conflict additionally carries ``matching_refs``
    (a tuple of :class:`ByteMatchRef` naming where the exact bytes already live)
    and a static ``guidance`` string. Both are safe, immutable values; they
    authorize nothing and never carry peer-supplied text.
    """

    def __init__(self, code: str) -> None:
        super().__init__(_MESSAGES[code])
        self.code = code
        self.matching_refs: tuple[ByteMatchRef, ...] = ()
        self.guidance: str | None = None


class AssetImportConflictError(AssetImportError, ArtifactConflictError):
    """The request conflicts with already-recorded immutable state."""


_CONFLICT_CODES = frozenset(
    {
        "request_conflict",
        "rights_conflict",
        "rights_superseded",
        "dependency_conflict",
        "kind_conflict",
        "cross_artifact_duplicate",
    }
)


def _fail(code: str):
    cls = AssetImportConflictError if code in _CONFLICT_CODES else AssetImportError
    return cls(code)


def _cross_artifact_conflict(matching_refs) -> AssetImportConflictError:
    """A static veto of a second revision for bytes already stored elsewhere."""
    err = AssetImportConflictError("cross_artifact_duplicate")
    err.matching_refs = tuple(matching_refs)
    err.guidance = CROSS_ARTIFACT_DUPLICATE_GUIDANCE
    return err


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Typed immutable occurrence
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ArtifactRevisionRef:
    project_id: str
    artifact_id: str
    revision_id: str

    def as_dict(self) -> dict[str, str]:
        return {
            "project_id": self.project_id,
            "artifact_id": self.artifact_id,
            "revision_id": self.revision_id,
        }


@dataclass(frozen=True)
class ByteMatchRef:
    """A safe, immutable reference to a stored revision with identical bytes.

    Returned on a ``cross_artifact_duplicate`` conflict so a caller can see
    *where* the exact bytes already live and reconcile explicitly. It is opaque
    and authorizes nothing: it resolves no rights, rebinds no subject and makes
    no claim of shared identity, study, or scientific independence.
    """

    project_id: str
    artifact_id: str
    revision_id: str
    content_hash: str
    byte_size: int
    media_type: str | None

    def as_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "artifact_id": self.artifact_id,
            "revision_id": self.revision_id,
            "content_hash": self.content_hash,
            "byte_size": self.byte_size,
            "media_type": self.media_type,
        }


@dataclass(frozen=True)
class ImportOccurrence:
    """A validated ``acquisition_import_occurrence`` v1 record (proposal only)."""

    occurrence_ref: ArtifactRevisionRef
    request_id: str
    operation: str
    artifact_id: str
    origin_locator: str
    disposition: str
    content_hash: str | None
    byte_size: int | None
    acquired_at: str
    source_ref: ArtifactRevisionRef | None
    source_rights_ref: RightsRef
    source_revision_rights_ref: RightsRef | None
    occurrence_rights_ref: RightsRef
    parent_revision_id: str | None
    dependencies: tuple[DependencyRef, ...]
    reason: str | None
    request_digest: str
    proposal_only: bool = True
    authorizes_use: bool = False

    def result(self, *, replayed: bool) -> dict:
        return {
            "source_ref": None if self.source_ref is None else self.source_ref.as_dict(),
            "occurrence_ref": self.occurrence_ref.as_dict(),
            "disposition": self.disposition,
            "sha256": self.content_hash,
            "byte_size": self.byte_size,
            "replayed": replayed,
        }


_PAYLOAD_KEYS = frozenset(
    {
        "record_type", "record_version", "proposal_only", "authorizes_use",
        "scientific_acceptance", "operation", "request_id", "project_id",
        "artifact_id", "origin_locator", "content_hash", "byte_size",
        "acquired_at", "acquired_by", "disposition", "source_ref",
        "source_rights_ref", "source_revision_rights_ref", "parent_revision_id",
        "dependencies", "reason", "request_digest",
    }
)


def _no_duplicate_keys(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


class _NestedStore:
    """Adapt a store so ``ArtifactRepository`` writes nest as SAVEPOINTs.

    ``ProjectStore.transaction`` cannot nest. The wrapper owns one outer
    ``BEGIN IMMEDIATE``; every inner repository transaction becomes a savepoint
    that rolls back to its mark on failure, and the outer rollback then discards
    everything.
    """

    def __init__(self, store) -> None:
        self._store = store

    @property
    def project_id(self) -> str:
        return self._store.project_id

    @property
    def connection(self) -> sqlite3.Connection:
        return self._store.connection

    @contextmanager
    def transaction(self):
        conn = self._store.connection
        if not conn.in_transaction:
            with self._store.transaction():
                yield conn
            return
        name = "sp_" + uuid.uuid4().hex
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield conn
        except BaseException:
            conn.execute(f"ROLLBACK TO {name}")
            # A savepoint rollback reverts writes without decreasing
            # ``total_changes``; drop the memo so reverted state is never
            # served from a stale entry.
            opmemo.invalidate(conn)
            conn.execute(f"RELEASE {name}")
            raise
        else:
            conn.execute(f"RELEASE {name}")


# --------------------------------------------------------------------------- #
# Pure input helpers
# --------------------------------------------------------------------------- #
def _ident(value: object) -> str:
    try:
        validate_identifier(value, "identifier")
    except RecordValidationError:
        raise _fail("invalid_input") from None
    if len(value) > MAX_IDENTIFIER_CHARS:  # type: ignore[arg-type]
        raise _fail("invalid_input")
    return value  # type: ignore[return-value]


def _text(value: object, limit: int) -> str:
    if (
        not isinstance(value, str)
        or value == ""
        or value != value.strip()
        or len(value) > limit
        or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)
    ):
        raise _fail("invalid_input")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise _fail("invalid_input") from None
    return value


def _norm_rights(record: object, project_id: str, subject: dict | None = None) -> dict:
    if not isinstance(record, dict):
        raise _fail("rights_invalid")
    try:
        validated = validate_rights_record(record, project_id, subject=subject)
        if len(canonical_json(validated)) > MAX_RIGHTS_BYTES:
            raise _fail("rights_invalid")
    except RecordValidationError:
        raise _fail("rights_invalid") from None
    return validated


def _require_allows_source(record: dict) -> None:
    """Acquisition and storage are separate gates; privacy denial blocks both."""
    if record["privacy"]["denial"] is not False:
        raise _fail("privacy_denied")
    ops = record["operations"]
    if ops["acquisition"] != "allowed":
        raise _fail("acquisition_not_allowed")
    if ops["storage"] != "allowed":
        raise _fail("storage_not_allowed")


def _essence(record: dict) -> dict:
    return {k: record[k] for k in ("operations", "basis", "restrictions", "privacy")}


def _norm_dependencies(value: object, project_id: str) -> tuple[DependencyRef, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > MAX_DEPENDENCIES:
        raise _fail("invalid_input")
    out: list[DependencyRef] = []
    for item in value:
        if isinstance(item, DependencyRef):
            ref, role = item.as_ref(), item.role
        elif isinstance(item, dict) and "role" in item:
            ref, role = {k: v for k, v in item.items() if k != "role"}, item["role"]
        else:
            raise _fail("invalid_input")
        if role not in IMPORT_DEPENDENCY_ROLES:
            raise _fail("invalid_input")
        try:
            ref = validate_ref(ref, project_id)
        except RecordValidationError:
            raise _fail("invalid_input") from None
        for part in ref.values():
            _ident(part)
        out.append(DependencyRef(ref["project_id"], ref["artifact_id"], ref["revision_id"], role))
    keyed = {(d.artifact_id, d.revision_id, d.role): d for d in out}
    if len(keyed) != len(out):
        raise _fail("invalid_input")
    return tuple(sorted(out, key=lambda d: (d.artifact_id, d.revision_id, d.role)))


def _deps_json(deps) -> list[dict]:
    return [d.as_dict() for d in deps]


def _rights_dict(ref: RightsRef) -> dict:
    return ref.as_dict()


# --------------------------------------------------------------------------- #
# Repository
# --------------------------------------------------------------------------- #
class AssetImportRepository:
    """Source import + import-occurrence recording over an ``ArtifactRepository``."""

    def __init__(self, store_or_repository) -> None:
        store = (
            store_or_repository._store
            if isinstance(store_or_repository, ArtifactRepository)
            else store_or_repository
        )
        self._store = store
        self._repo = ArtifactRepository(_NestedStore(store))

    @property
    def project_id(self) -> str:
        return self._store.project_id

    def occurrence_artifact_id(self, request_id: str) -> str:
        """Deterministic report-artifact id for ``request_id`` in this project."""
        _ident(request_id)
        digest = hashlib.sha256(f"{self.project_id}\x00{request_id}".encode()).hexdigest()
        return "occ_" + digest[:40]

    # -- public writes ------------------------------------------------------- #
    def import_asset(
        self,
        *,
        artifact_id: str,
        content: bytes,
        origin_locator: str,
        rights: dict,
        revision_id: str,
        request_id: str,
        occurrence_rights: dict,
        occurrence_revision_id: str,
        parent_revision_id: str | None = None,
        dependencies=(),
    ) -> dict:
        project = self.project_id
        artifact_id = _ident(artifact_id)
        revision_id = _ident(revision_id)
        request_id = _ident(request_id)
        occurrence_revision_id = _ident(occurrence_revision_id)
        if parent_revision_id is not None:
            parent_revision_id = _ident(parent_revision_id)
        origin_locator = _text(origin_locator, MAX_LOCATOR_CHARS)
        if not isinstance(content, (bytes, bytearray)) or len(content) == 0:
            raise _fail("invalid_input")
        if len(content) > MAX_SOURCE_BYTES:
            raise _fail("content_too_large")
        data = bytes(content)
        deps = _norm_dependencies(dependencies, project)
        sha = sha256_digest(data)

        source_rights = _norm_rights(rights, project)
        _require_allows_source(source_rights)  # before any DB access: nothing retained
        report_id = self.occurrence_artifact_id(request_id)
        occ_rights = self._norm_occurrence_rights(
            occurrence_rights, report_id, occurrence_revision_id
        )
        digest = self._digest(
            operation="import", artifact_id=artifact_id, content_sha256=sha,
            byte_size=len(data), origin_locator=origin_locator, revision_id=revision_id,
            parent_revision_id=parent_revision_id, rights=source_rights,
            occurrence_revision_id=occurrence_revision_id, occurrence_rights=occ_rights,
            dependencies=deps, reason=None,
        )

        try:
            with self._store.transaction():
                replay = self._replay(report_id, "import", digest)
                if replay is not None:
                    return replay.result(replayed=True)
                # Global exact-byte check, serialized in this same outer
                # transaction BEFORE any new artifact/revision is written. The
                # candidate set is found by a metadata-only query; only genuine
                # same-hash/same-size candidates are read as bytes.
                existing, cross = self._find_exact_bytes(artifact_id, sha, data)
                if existing is None and cross:
                    # Bytes already stored under a different document artifact:
                    # create no second source revision or independent artifact.
                    raise _cross_artifact_conflict(cross)
                self._ensure_document(artifact_id)
                if existing is None:
                    disposition = "created_new_revision"
                    source_rev = self._new_source(
                        artifact_id, data, origin_locator, source_rights, revision_id,
                        parent_revision_id, deps,
                    )
                    source_rights_ref = source_rev.rights_ref
                else:
                    disposition = "duplicate_bytes_of_existing_revision"
                    source_rev, source_rights_ref = self._reuse_source(
                        existing, source_rights, revision_id, deps
                    )
                payload = self._payload(
                    operation="import", request_id=request_id, artifact_id=artifact_id,
                    origin_locator=origin_locator, content_hash=sha, byte_size=len(data),
                    disposition=disposition,
                    source_ref=ArtifactRevisionRef(project, artifact_id, source_rev.revision_id),
                    source_rights_ref=source_rights_ref,
                    source_revision_rights_ref=source_rev.rights_ref,
                    parent_revision_id=parent_revision_id, dependencies=deps,
                    reason=None, request_digest=digest,
                )
                occ = self._write_occurrence(
                    report_id, occurrence_revision_id, occ_rights, payload,
                    ArtifactRevisionRef(project, artifact_id, source_rev.revision_id),
                )
                return occ.result(replayed=False)
        except AssetImportError:
            raise
        except sqlite3.Error:
            raise _fail("storage_failure") from None

    def record_missing(
        self,
        *,
        artifact_id: str,
        origin_locator: str,
        rights: dict,
        request_id: str,
        occurrence_rights: dict,
        occurrence_revision_id: str,
        reason: str,
    ) -> dict:
        """Record a ``no_full_text_acquired`` event: no source revision, no bytes."""
        project = self.project_id
        artifact_id = _ident(artifact_id)
        request_id = _ident(request_id)
        occurrence_revision_id = _ident(occurrence_revision_id)
        origin_locator = _text(origin_locator, MAX_LOCATOR_CHARS)
        reason = _text(reason, MAX_REASON_CHARS)
        subject_rights = _norm_rights(rights, project)
        subject = subject_rights["subject"]
        if subject["kind"] != "subject_artifact_ref" or subject["artifact_id"] != artifact_id:
            raise _fail("rights_invalid")
        report_id = self.occurrence_artifact_id(request_id)
        occ_rights = self._norm_occurrence_rights(
            occurrence_rights, report_id, occurrence_revision_id
        )
        digest = self._digest(
            operation="missing", artifact_id=artifact_id, content_sha256=None,
            byte_size=None, origin_locator=origin_locator, revision_id=None,
            parent_revision_id=None, rights=subject_rights,
            occurrence_revision_id=occurrence_revision_id, occurrence_rights=occ_rights,
            dependencies=(), reason=reason,
        )
        try:
            with self._store.transaction():
                replay = self._replay(report_id, "missing", digest)
                if replay is not None:
                    return replay.result(replayed=True)
                self._ensure_document(artifact_id)
                try:
                    rights_ref = self._repo.store_rights_record(subject_rights)
                except ArtifactConflictError:
                    raise _fail("rights_conflict") from None
                except (RecordValidationError, ArtifactRepositoryError):
                    raise _fail("rights_invalid") from None
                payload = self._payload(
                    operation="missing", request_id=request_id, artifact_id=artifact_id,
                    origin_locator=origin_locator, content_hash=None, byte_size=None,
                    disposition="no_full_text_acquired", source_ref=None,
                    source_rights_ref=rights_ref, source_revision_rights_ref=None,
                    parent_revision_id=None, dependencies=(), reason=reason,
                    request_digest=digest,
                )
                occ = self._write_occurrence(
                    report_id, occurrence_revision_id, occ_rights, payload, None
                )
                return occ.result(replayed=False)
        except AssetImportError:
            raise
        except sqlite3.Error:
            raise _fail("storage_failure") from None

    # -- public reads -------------------------------------------------------- #
    def get_occurrence(self, occurrence_ref) -> ImportOccurrence:
        """Load and fully re-validate an occurrence against the stored source."""
        try:
            ref = validate_ref(occurrence_ref, self.project_id)
        except RecordValidationError:
            raise _fail("invalid_input") from None
        for part in ref.values():
            _ident(part)
        try:
            return self._load(
                ArtifactRevisionRef(ref["project_id"], ref["artifact_id"], ref["revision_id"])
            )
        except sqlite3.Error:
            raise _fail("storage_failure") from None

    def find_request(self, request_id: str) -> ImportOccurrence | None:
        report_id = self.occurrence_artifact_id(request_id)
        try:
            if self._repo.get_artifact(report_id) is None:
                return None
            revisions = self._repo.list_revisions(report_id)
            if len(revisions) != 1:
                raise _fail("occurrence_invalid")
            occ = self._load(
                ArtifactRevisionRef(self.project_id, report_id, revisions[0].revision_id)
            )
        except sqlite3.Error:
            raise _fail("storage_failure") from None
        if occ.request_id != request_id:
            raise _fail("occurrence_invalid")
        return occ

    # -- internals: write ---------------------------------------------------- #
    def _norm_occurrence_rights(self, record, report_id: str, revision_id: str) -> dict:
        subject = {
            "kind": "source_revision",
            "project_id": self.project_id,
            "artifact_id": report_id,
            "revision_id": revision_id,
        }
        validated = _norm_rights(record, self.project_id, subject=subject)
        if (
            validated["operations"]["storage"] != "allowed"
            or validated["privacy"]["denial"] is not False
        ):
            raise _fail("rights_invalid")
        return validated

    def _digest(self, *, dependencies, rights, occurrence_rights, **fields) -> str:
        body = dict(fields)
        body["rights_digest"] = sha256_digest(canonical_json(rights))
        body["occurrence_rights_digest"] = sha256_digest(canonical_json(occurrence_rights))
        body["dependencies"] = _deps_json(dependencies)
        return sha256_digest(canonical_json(body))

    def _ensure_document(self, artifact_id: str) -> None:
        try:
            self._repo.create_artifact(artifact_id, kind="document", created_by="import")
        except ArtifactConflictError:
            raise _fail("kind_conflict") from None
        except (RecordValidationError, ArtifactRepositoryError):
            raise _fail("invalid_input") from None

    def _find_exact_bytes(self, artifact_id: str, sha: str, data: bytes):
        """Find revisions whose bytes are exactly ``data``, project-wide.

        Returns ``(same_artifact_revision | None, cross_artifact_refs)``. Only
        genuine **document source** revisions are candidates (:class:`artifact`
        ``kind='document'``): produced report/occurrence bytes are never treated
        as a canonical paper. The candidate set is found with a **metadata-only**
        query bound by ``LIMIT cap+1`` — so the bound is applied before any
        ``fetchall`` and only those candidates are read as BLOBs and
        byte-compared; unrelated project content is never loaded. A candidate
        count above :data:`MAX_BYTE_COLLISION_CANDIDATES` is an explicit
        diagnostic failure — the scan is never silently truncated.
        """
        rows = self._store.connection.execute(
            "SELECT r.artifact_id AS artifact_id, r.revision_id AS revision_id "
            "FROM revision AS r "
            "JOIN artifact AS a ON a.project_id = r.project_id "
            "AND a.artifact_id = r.artifact_id "
            "WHERE r.project_id = ? AND r.content_sha256 = ? AND r.byte_size = ? "
            "AND a.kind = 'document' "
            "ORDER BY r.rowid LIMIT ?",
            (self.project_id, sha, len(data), MAX_BYTE_COLLISION_CANDIDATES + 1),
        ).fetchall()
        if len(rows) > MAX_BYTE_COLLISION_CANDIDATES:
            raise _fail("duplicate_scan_overflow")
        same = None
        cross: list[ByteMatchRef] = []
        for row in rows:  # actual byte equality is authoritative, not just the hash
            candidate_id = row["artifact_id"]
            revision_id = row["revision_id"]
            if self._repo.read_content(candidate_id, revision_id) != data:
                continue
            revision = self._repo.get_revision(candidate_id, revision_id)
            if revision is None:
                continue
            if candidate_id == artifact_id:
                if same is None:
                    same = revision
            else:
                cross.append(
                    ByteMatchRef(
                        project_id=self.project_id,
                        artifact_id=candidate_id,
                        revision_id=revision_id,
                        content_hash=revision.content_sha256,
                        byte_size=revision.byte_size,
                        media_type=revision.media_type,
                    )
                )
        return same, tuple(cross)

    def _new_source(self, artifact_id, data, origin, rights, revision_id, parent, deps):
        subject = {
            "kind": "source_revision", "project_id": self.project_id,
            "artifact_id": artifact_id, "revision_id": revision_id,
        }
        if rights["subject"] != subject:
            raise _fail("rights_invalid")
        all_deps = list(deps)
        if parent is not None:
            all_deps.append(DependencyRef(self.project_id, artifact_id, parent, "version_of"))
        try:
            return self._repo.import_revision(
                artifact_id=artifact_id, content=data, origin_locator=origin,
                rights=rights, revision_id=revision_id, parent_revision_id=parent,
                dependencies=all_deps, acquired_by="import",
            )
        except ArtifactConflictError:
            raise _fail("rights_superseded") from None
        except (RecordValidationError, ArtifactRepositoryError):
            raise _fail("source_rejected") from None

    def _latest_rights(self, rights_id: str) -> dict:
        row = self._store.connection.execute(
            "SELECT MAX(record_revision) AS newest FROM rights_record "
            "WHERE project_id = ? AND rights_id = ?",
            (self.project_id, rights_id),
        ).fetchone()
        if row is None or row["newest"] is None:
            raise _fail("rights_conflict")
        record = self._repo.get_rights_record(RightsRef(self.project_id, rights_id, row["newest"]))
        if record is None:
            raise _fail("rights_conflict")
        return record

    def _reuse_source(self, existing, supplied, proposed_revision_id, deps):
        """Authorize reuse of the exact historical revision, or fail closed."""
        subject = supplied["subject"]
        if subject["kind"] != "source_revision" or subject["artifact_id"] != existing.artifact_id:
            raise _fail("rights_invalid")
        ref = RightsRef(self.project_id, supplied["rights_id"], supplied["record_revision"])
        newest = self._latest_rights(supplied["rights_id"]) if self._rights_known(ref.rights_id) else None
        if newest is not None and ref.record_revision < newest["record_revision"]:
            raise _fail("rights_superseded")
        # The exact existing source must still be authorized by its LATEST rights.
        latest_existing = self._latest_rights(existing.rights_ref.rights_id)
        # It must bind THIS exact stored source (project/artifact/revision): a newer
        # record that names another subject, or is malformed, never authorizes reuse.
        exact = {
            "kind": "source_revision", "project_id": self.project_id,
            "artifact_id": existing.artifact_id, "revision_id": existing.revision_id,
        }
        try:
            validated_latest = validate_rights_record(
                latest_existing, self.project_id, subject=exact
            )
        except RecordValidationError:
            raise _fail("rights_conflict") from None
        try:
            _require_allows_source(validated_latest)
        except AssetImportError:
            raise _fail("rights_conflict") from None
        # Pin the LATEST applicable record, never the stale original pin.
        pin = RightsRef(
            self.project_id, validated_latest["rights_id"], validated_latest["record_revision"]
        )
        if subject["revision_id"] == existing.revision_id:
            pin = ref  # supplied record binds the exact existing source: store + pin it
        elif subject["revision_id"] == proposed_revision_id:
            # Bound to a revision that is never created, so it is not persisted
            # (a rights record must name an existing revision). Identical terms
            # reuse the existing source's pinned rights; anything else conflicts.
            if _essence(supplied) != _essence(validated_latest):
                raise _fail("rights_conflict")
        else:
            raise _fail("rights_invalid")
        # Declared relationships may not be silently dropped.
        stored = tuple(
            sorted(
                (d for d in existing.dependencies if d.role != "version_of"),
                key=lambda d: (d.artifact_id, d.revision_id, d.role),
            )
        )
        if stored != deps:
            raise _fail("dependency_conflict")
        if pin == ref:
            try:
                self._repo.store_rights_record(supplied)
            except ArtifactConflictError:
                raise _fail("rights_conflict") from None
            except (RecordValidationError, ArtifactRepositoryError):
                raise _fail("rights_invalid") from None
        return existing, pin

    def _rights_known(self, rights_id: str) -> bool:
        return (
            self._store.connection.execute(
                "SELECT 1 FROM rights_record WHERE project_id = ? AND rights_id = ? LIMIT 1",
                (self.project_id, rights_id),
            ).fetchone()
            is not None
        )

    def _payload(
        self, *, operation, request_id, artifact_id, origin_locator, content_hash,
        byte_size, disposition, source_ref, source_rights_ref,
        source_revision_rights_ref, parent_revision_id, dependencies, reason,
        request_digest,
    ) -> dict:
        return {
            "record_type": OCCURRENCE_DOCUMENT_TYPE,
            "record_version": OCCURRENCE_RECORD_VERSION,
            "proposal_only": True,
            "authorizes_use": False,
            "scientific_acceptance": "not_assessed",
            "operation": operation,
            "request_id": request_id,
            "project_id": self.project_id,
            "artifact_id": artifact_id,
            "origin_locator": origin_locator,
            "content_hash": (
                {"status": "known", "algorithm": "sha256", "value": content_hash}
                if content_hash is not None
                else {"status": "not_applicable", "reason": reason}
            ),
            "byte_size": byte_size,
            "acquired_at": _utc_now(),
            "acquired_by": "import",
            "disposition": disposition,
            "source_ref": None if source_ref is None else source_ref.as_dict(),
            "source_rights_ref": _rights_dict(source_rights_ref),
            "source_revision_rights_ref": (
                None if source_revision_rights_ref is None
                else _rights_dict(source_revision_rights_ref)
            ),
            "parent_revision_id": parent_revision_id,
            "dependencies": _deps_json(dependencies),
            "reason": reason,
            "request_digest": request_digest,
        }

    def _write_occurrence(self, report_id, revision_id, occ_rights, payload, source_ref):
        try:
            self._repo.create_artifact(report_id, kind="report", created_by="import")
            deps = (
                []
                if source_ref is None
                else [DependencyRef(source_ref.project_id, source_ref.artifact_id,
                                    source_ref.revision_id, "derived_from")]
            )
            self._repo.import_revision(
                artifact_id=report_id,
                content={
                    "schema_version": 1,
                    "document_type": OCCURRENCE_DOCUMENT_TYPE,
                    "payload": payload,
                },
                origin_locator="acquisition-import:" + payload["request_id"],
                rights=occ_rights, revision_id=revision_id, dependencies=deps,
                acquired_by="import",
            )
        except (ArtifactConflictError, RecordValidationError, ArtifactRepositoryError):
            raise _fail("occurrence_rejected") from None
        return self._load(ArtifactRevisionRef(self.project_id, report_id, revision_id))

    def _replay(self, report_id: str, operation: str, digest: str):
        artifact = self._repo.get_artifact(report_id)
        if artifact is None:
            return None
        revisions = self._repo.list_revisions(report_id)
        if artifact.kind != "report" or len(revisions) != 1:
            raise _fail("request_conflict")
        occ = self._load(ArtifactRevisionRef(self.project_id, report_id, revisions[0].revision_id))
        if occ.operation != operation or occ.request_digest != digest:
            raise _fail("request_conflict")
        return occ

    # -- internals: read / validate ------------------------------------------ #
    def _load(self, ref: ArtifactRevisionRef) -> ImportOccurrence:
        bad = _fail("occurrence_invalid")
        repo = self._repo
        if ref.project_id != self.project_id:
            raise bad
        artifact = repo.get_artifact(ref.artifact_id)
        revision = repo.get_revision(ref.artifact_id, ref.revision_id)
        if artifact is None or artifact.kind != "report" or revision is None:
            raise bad
        raw = repo.read_content(ref.artifact_id, ref.revision_id)
        if sha256_digest(raw) != revision.content_sha256 or len(raw) != revision.byte_size:
            raise bad
        try:
            envelope = json.loads(
                raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys
            )
            # Only the exact canonical encoding written by this module is accepted.
            if canonical_json(envelope) != raw:
                raise bad
        except (UnicodeError, ValueError, RecordValidationError):
            raise bad from None
        if (
            not isinstance(envelope, dict)
            or set(envelope) != {"schema_version", "document_type", "payload"}
            or type(envelope["schema_version"]) is not int
            or envelope["schema_version"] != 1
            or envelope["document_type"] != OCCURRENCE_DOCUMENT_TYPE
            or not isinstance(envelope["payload"], dict)
        ):
            raise bad
        p = envelope["payload"]
        if set(p) != _PAYLOAD_KEYS:
            raise bad
        try:
            occ = self._build(ref, revision, p)
        except (RecordValidationError, KeyError, TypeError, ValueError, AssetImportError):
            raise bad from None
        return occ

    def _rights_ref(self, value) -> RightsRef:
        if (
            not isinstance(value, dict)
            or set(value) != {"project_id", "rights_id", "record_revision"}
            or value["project_id"] != self.project_id
        ):
            raise ValueError
        ref = RightsRef(self.project_id, _ident(value["rights_id"]), value["record_revision"])
        if (
            isinstance(ref.record_revision, bool)
            or not isinstance(ref.record_revision, int)
            or ref.record_revision < 1
            or self._repo.get_rights_record(ref) is None
        ):
            raise ValueError
        return ref

    def _build(self, ref, revision, p) -> ImportOccurrence:
        project = self.project_id
        if (
            p["record_type"] != OCCURRENCE_DOCUMENT_TYPE
            or type(p["record_version"]) is not int
            or p["record_version"] != OCCURRENCE_RECORD_VERSION
            or p["proposal_only"] is not True
            or p["authorizes_use"] is not False
            or p["scientific_acceptance"] != "not_assessed"
            or p["project_id"] != project
            or p["operation"] not in ("import", "missing")
            or p["acquired_by"] != "import"
            or p["disposition"] not in DISPOSITIONS
        ):
            raise ValueError
        request_id = _ident(p["request_id"])
        if ref.artifact_id != self.occurrence_artifact_id(request_id):
            raise ValueError
        artifact_id = _ident(p["artifact_id"])
        origin = _text(p["origin_locator"], MAX_LOCATOR_CHARS)
        acquired_at = _text(p["acquired_at"], 64)
        digest = _text(p["request_digest"], 100)
        parent = p["parent_revision_id"]
        if parent is not None:
            parent = _ident(parent)
        deps = _norm_dependencies(p["dependencies"], project)
        if _deps_json(deps) != p["dependencies"]:
            raise ValueError
        src_rights = self._rights_ref(p["source_rights_ref"])
        occ_rights = revision.rights_ref
        if self._repo.get_rights_record(occ_rights) is None:
            raise ValueError
        disposition = p["disposition"]
        hash_field = p["content_hash"]

        if disposition == "no_full_text_acquired":
            reason = _text(p["reason"], MAX_REASON_CHARS)
            if (
                p["operation"] != "missing"
                or p["source_ref"] is not None
                or p["source_revision_rights_ref"] is not None
                or p["byte_size"] is not None
                or parent is not None
                or deps
                or hash_field != {"status": "not_applicable", "reason": reason}
                or revision.dependencies
            ):
                raise ValueError
            rights_doc = self._repo.get_rights_record(src_rights)
            if rights_doc["subject"] != {
                "kind": "subject_artifact_ref", "project_id": project, "artifact_id": artifact_id,
            }:
                raise ValueError
            return ImportOccurrence(
                occurrence_ref=ref, request_id=request_id, operation="missing",
                artifact_id=artifact_id, origin_locator=origin, disposition=disposition,
                content_hash=None, byte_size=None, acquired_at=acquired_at, source_ref=None,
                source_rights_ref=src_rights, source_revision_rights_ref=None,
                occurrence_rights_ref=occ_rights, parent_revision_id=None, dependencies=(),
                reason=reason, request_digest=digest,
            )

        # --- a source revision must actually exist and match the record ---
        if p["operation"] != "import" or p["reason"] is not None:
            raise ValueError
        sref = validate_ref(p["source_ref"], project)
        if sref["artifact_id"] != artifact_id or set(p["source_ref"]) != {
            "project_id", "artifact_id", "revision_id",
        }:
            raise ValueError
        source = self._repo.get_revision(artifact_id, _ident(sref["revision_id"]))
        art = self._repo.get_artifact(artifact_id)
        if source is None or art is None or art.kind != "document":
            raise ValueError
        sha = source.content_sha256
        if (
            hash_field != {"status": "known", "algorithm": "sha256", "value": sha}
            or p["byte_size"] != source.byte_size
            or isinstance(p["byte_size"], bool)
        ):
            raise ValueError
        if sha256_digest(self._repo.read_content(artifact_id, source.revision_id)) != sha:
            raise ValueError
        if self._rights_ref(p["source_revision_rights_ref"]) != source.rights_ref:
            raise ValueError
        # The rights record this occurrence pins must bind the EXACT stored source.
        pinned = self._repo.get_rights_record(src_rights)
        if pinned["subject"] != {
            "kind": "source_revision", "project_id": project,
            "artifact_id": artifact_id, "revision_id": source.revision_id,
        }:
            raise ValueError
        stored = tuple(
            sorted(
                (d for d in source.dependencies if d.role != "version_of"),
                key=lambda d: (d.artifact_id, d.revision_id, d.role),
            )
        )
        if stored != deps:
            raise ValueError
        if disposition == "created_new_revision":
            if (
                source.parent_revision_id != parent
                or source.origin_locator != origin
                or src_rights != source.rights_ref
            ):
                raise ValueError
        # revision-level link: the occurrence depends on exactly this source.
        want = (
            DependencyRef(project, artifact_id, source.revision_id, "derived_from"),
        )
        if tuple(revision.dependencies) != want:
            raise ValueError
        return ImportOccurrence(
            occurrence_ref=ref, request_id=request_id, operation="import",
            artifact_id=artifact_id, origin_locator=origin, disposition=disposition,
            content_hash=sha, byte_size=source.byte_size, acquired_at=acquired_at,
            source_ref=ArtifactRevisionRef(project, artifact_id, source.revision_id),
            source_rights_ref=src_rights, source_revision_rights_ref=source.rights_ref,
            occurrence_rights_ref=occ_rights, parent_revision_id=parent,
            dependencies=deps, reason=None, request_digest=digest,
        )


__all__ = [
    "AssetImportConflictError",
    "AssetImportError",
    "AssetImportRepository",
    "ArtifactRevisionRef",
    "ByteMatchRef",
    "CROSS_ARTIFACT_DUPLICATE_GUIDANCE",
    "ImportOccurrence",
    "MAX_BYTE_COLLISION_CANDIDATES",
    "MAX_SOURCE_BYTES",
    "OCCURRENCE_DOCUMENT_TYPE",
]
