"""Immutable artifact revisions and idempotent validated imports.

This module is the storage-side artifact layer for Step 02. It sits *on top of*
an already-verified :class:`~review_article_agent.storage.store.ProjectStore`
(the caller resolves registry paths with the path-repair layer and passes a live
store in), and it reuses the pure-stdlib identity/rights validators in
:mod:`review_article_agent.domain.records`. It performs **no** scientific
processing, approval, worker, or model work.

What it guarantees
------------------

* **Logical identity vs immutable revision (C01 §3.2).** An :class:`Artifact` is
  one logical identity (``{project_id, artifact_id}``); a :class:`Revision` is
  one immutable snapshot (``{project_id, artifact_id, revision_id}``). A change
  is always a **new** revision row — never an in-place edit. The DB itself
  refuses an ``UPDATE``/``DELETE`` of a revision, rights record, dependency, or
  import request (``schema`` migration 2).
* **No hash-as-artifact / no title merging (C01 §3.3).** ``content_sha256`` is a
  byte-integrity/duplicate key only, never an artifact or science identity.
  Duplicate detection is scoped to a single ``artifact_id``; equal bytes in two
  different logical artifacts stay two distinct revisions.
* **Every revision pins a canonical RightsRecord to *this* revision.** The
  rights record's subject must be ``source_revision`` for the exact
  ``revision_id`` (validated with ``subject=``). Content bytes are persisted
  **only** when ``require_storage_allowed`` passes; raw or missing-storage-right
  content is rejected. A fully validated record whose storage is ``unknown`` may
  still be persisted as **metadata only** via :meth:`ArtifactRepository.store_rights_record`
  (the C03 §5.2 quarantine path) — never with its bytes.
* **Exact, same-project dependencies (C01 §5.5).** Each dependency is an exact
  ``RefRevision`` (optionally typed by a C01 role, default ``derived_from``) that
  must already exist in this project; the edge graph is persisted for later
  invalidation.
* **Idempotent, conflict-aware imports.** A repeated ``request_id`` returns the
  same revision; a divergent request for the same ``request_id`` raises
  :class:`ArtifactConflictError`. Repeated *identical* imports (same artifact,
  parent, dependencies, and rights) safely reuse the existing revision.
* **Transactional.** Every write runs in one ``BEGIN IMMEDIATE`` transaction; a
  failure rolls back all rows (revision, rights, dependencies, import request)
  with no half state and no dangling foreign-key reference.

Non-claims
----------

The JSON payload accepted here is validated only as a **generic versioned
document envelope** (``schema_version``, ``document_type``, ``payload``); it is
**not** a full scientific ``EvidenceRecord`` and passing here claims no
scientific validity. ``actor``/``recorded_by``/provenance values are untrusted
data retained for traceability and confer **no** authority. The repository never
reads external credentials or configuration.
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from ..domain.errors import StoreError
from ..domain.records import (
    ARTIFACT_KINDS,
    RecordValidationError,
    canonical_json,
    require_storage_allowed,
    validate_identifier,
    validate_ref,
    validate_rights_record,
)

#: Provenance method enum reused from C01 §5.3 (``human | model | import | derived``).
PROVENANCE_METHODS = ("human", "model", "import", "derived")

#: Actor class enum for acquisition/provenance (C01 §5.1/§5.2).
ACQUIRED_BY = ("user", "agent", "import")

#: C01 §5.5 dependency relation roles. Generic artifact dependencies use
#: ``derived_from``; a ``version_of`` edge is the explicit parent link.
DEPENDENCY_ROLES = (
    "version_of",
    "reports",
    "uses_dataset",
    "describes_experiment",
    "extracted_from",
    "derived_from",
    "summarizes",
    "supports_claim",
    "duplicate_bytes_of",
)

#: The only supported generic JSON document-envelope schema version.
SUPPORTED_DOCUMENT_SCHEMA_VERSION = 1

#: Exact keys permitted in a generic versioned JSON document envelope.
_DOCUMENT_KEYS = ("schema_version", "document_type", "payload")


class ArtifactRepositoryError(StoreError):
    """An artifact repository operation failed."""


class ArtifactConflictError(ArtifactRepositoryError):
    """A request conflicts with already-recorded immutable state.

    Raised for a divergent reuse of an existing ``request_id`` or a rights
    record whose identity already exists with different canonical bytes. The
    conflicting state is never overwritten.
    """


# --------------------------------------------------------------------------- #
# Small value objects
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RightsRef:
    """Pinned ``RightsRecordRef`` = ``{project_id, rights_id, record_revision}``."""

    project_id: str
    rights_id: str
    record_revision: int

    def as_dict(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "rights_id": self.rights_id,
            "record_revision": self.record_revision,
        }


@dataclass(frozen=True)
class DependencyRef:
    """An exact, same-project dependency edge (to_revision + C01 role)."""

    project_id: str
    artifact_id: str
    revision_id: str
    role: str

    def as_ref(self) -> dict[str, str]:
        return {
            "project_id": self.project_id,
            "artifact_id": self.artifact_id,
            "revision_id": self.revision_id,
        }

    def as_dict(self) -> dict[str, str]:
        ref = self.as_ref()
        ref["role"] = self.role
        return ref


@dataclass(frozen=True)
class Artifact:
    """A logical artifact identity (C01 §5.1)."""

    project_id: str
    artifact_id: str
    kind: str
    created_at_utc: str
    created_by: str
    label: str | None
    status: str


@dataclass(frozen=True)
class Revision:
    """One immutable revision's metadata (content is read separately)."""

    project_id: str
    artifact_id: str
    revision_id: str
    parent_revision_id: str | None
    content_sha256: str
    byte_size: int
    media_type: str | None
    origin_locator: str
    acquired_at_utc: str
    acquired_by: str
    rights_ref: RightsRef
    provenance_method: str
    provenance_actor: str
    provenance_performed_at_utc: str
    dependencies: tuple[DependencyRef, ...]


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


def sha256_digest(data: bytes) -> str:
    """Return the canonical ``sha256:<hex>`` content digest for ``data``."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _require_enum(value: object, allowed: tuple[str, ...], field: str) -> str:
    if value not in allowed:
        raise RecordValidationError(
            f"{field}: {value!r} is not one of {list(allowed)}"
        )
    return value  # type: ignore[return-value]


def _normalize_rights_ref(value: object) -> RightsRef:
    """Accept a :class:`RightsRef` or a ``RightsRecordRef`` dict and validate it."""
    if isinstance(value, RightsRef):
        candidate = value
    elif isinstance(value, dict):
        keys = set(value)
        if keys != {"project_id", "rights_id", "record_revision"}:
            raise RecordValidationError(
                "rights_ref: must be a RightsRecordRef with exactly "
                "{project_id, rights_id, record_revision}"
            )
        revision = value["record_revision"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise RecordValidationError(
                "rights_ref.record_revision: must be a positive integer"
            )
        candidate = RightsRef(
            project_id=validate_identifier(value["project_id"], "rights_ref.project_id"),
            rights_id=validate_identifier(value["rights_id"], "rights_ref.rights_id"),
            record_revision=revision,
        )
    else:
        raise RecordValidationError(
            "rights: must be a validated RightsRecord object or a RightsRecordRef"
        )
    validate_identifier(candidate.project_id, "rights_ref.project_id")
    validate_identifier(candidate.rights_id, "rights_ref.rights_id")
    return candidate


def _validate_document_envelope(value: dict) -> dict:
    """Validate a generic versioned JSON document envelope (strict).

    This checks the storage envelope only — a schema version, a document type,
    and a portable-JSON payload. It is **not** a scientific record validator.
    """
    if not isinstance(value, dict):
        raise RecordValidationError("content: JSON document envelope must be an object")
    extra = sorted(set(value) - set(_DOCUMENT_KEYS))
    if extra:
        raise RecordValidationError(
            f"content: unknown envelope field(s): {', '.join(extra)}"
        )
    for key in _DOCUMENT_KEYS:
        if key not in value:
            raise RecordValidationError(f"content: missing envelope field {key!r}")
    version = value["schema_version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise RecordValidationError("content.schema_version: must be an integer")
    if version > SUPPORTED_DOCUMENT_SCHEMA_VERSION:
        raise RecordValidationError(
            f"content.schema_version: {version} is newer than supported "
            f"{SUPPORTED_DOCUMENT_SCHEMA_VERSION}; upgrade review_article_agent."
        )
    if version < 1:
        raise RecordValidationError("content.schema_version: must be >= 1")
    validate_identifier(value["document_type"], "content.document_type")
    payload = value["payload"]
    if not isinstance(payload, (dict, list)):
        raise RecordValidationError(
            "content.payload: must be a JSON object or array"
        )
    # canonical_json enforces portable JSON (no NaN/Infinity, string keys, etc.).
    canonical_json(payload)
    return value


def _normalize_dependencies(
    dependencies: object, *, default_role: str, project_id: str, artifact_id: str
) -> list[DependencyRef]:
    if dependencies is None:
        dependencies = []
    if isinstance(dependencies, (str, bytes, dict)):
        raise RecordValidationError(
            "dependencies: must be a list of RefRevision/dependency objects"
        )
    out: list[DependencyRef] = []
    for index, item in enumerate(dependencies):
        field = f"dependencies[{index}]"
        if isinstance(item, DependencyRef):
            ref_value = item.as_ref()
            role = item.role
        elif isinstance(item, dict):
            role = item.get("role", default_role)
            ref_value = {k: v for k, v in item.items() if k != "role"}
        else:
            raise RecordValidationError(f"{field}: must be a RefRevision or dependency object")
        role = _require_enum(role, DEPENDENCY_ROLES, f"{field}.role")
        ref = validate_ref(ref_value, project_id)
        out.append(
            DependencyRef(
                project_id=ref["project_id"],
                artifact_id=ref["artifact_id"],
                revision_id=ref["revision_id"],
                role=role,
            )
        )
    return out


def _dependency_key(dep: DependencyRef) -> tuple[str, str, str, str]:
    return (dep.project_id, dep.artifact_id, dep.revision_id, dep.role)


class ArtifactRepository:
    """Immutable artifact revisions over an already-verified :class:`ProjectStore`.

    Typical use::

        store = ProjectStore.open_at(app_dir, expected_project_id=pid)  # path layer
        repo = ArtifactRepository(store)

        aid = repo.new_artifact_id()
        repo.create_artifact(aid, kind="document", created_by="import")

        rid = repo.new_revision_id()               # caller-assigned for exact binding
        rights = build_valid_rights_record(rid)    # subject binds {..., revision_id: rid}
        rev = repo.import_revision(
            artifact_id=aid,
            revision_id=rid,
            content=b"...source bytes...",
            origin_locator="import:batch-1/a.pdf",
            acquired_by="import",
            rights=rights,
            request_id="import-batch-1:0",         # idempotent retry key
        )
    """

    def __init__(self, store) -> None:
        self._store = store

    # -- identifiers --------------------------------------------------------- #
    @property
    def project_id(self) -> str:
        return self._store.project_id

    def _assert_open(self) -> sqlite3.Connection:
        return self._store.connection

    @staticmethod
    def new_artifact_id() -> str:
        """Return a fresh opaque logical artifact id."""
        return "art_" + uuid.uuid4().hex

    @staticmethod
    def new_revision_id() -> str:
        """Return a fresh opaque revision id (caller-assigned for rights binding)."""
        return "rev_" + uuid.uuid4().hex

    # -- artifact identity --------------------------------------------------- #
    def create_artifact(
        self,
        artifact_id: str | None = None,
        *,
        kind: str,
        created_by: str = "import",
        label: str | None = None,
        created_at_utc: str | None = None,
    ) -> Artifact:
        """Create a logical artifact; idempotent for the same id+kind.

        A different ``kind`` for an existing ``artifact_id`` raises
        :class:`ArtifactConflictError` (an id is never reused for a different
        logical artifact).
        """
        conn = self._assert_open()
        kind = _require_enum(kind, ARTIFACT_KINDS, "kind")
        created_by = _require_enum(created_by, ACQUIRED_BY, "created_by")
        if artifact_id is None:
            artifact_id = self.new_artifact_id()
        artifact_id = validate_identifier(artifact_id, "artifact_id")
        if label is not None:
            label = _require_nonempty_str(label, "label")
        created = _utc_now() if created_at_utc is None else _require_timestamp(
            created_at_utc, "created_at_utc"
        )
        with self._store.transaction():
            existing = self._artifact_row(artifact_id)
            if existing is not None:
                if existing["kind"] != kind:
                    raise ArtifactConflictError(
                        f"artifact {artifact_id!r} already exists with kind "
                        f"{existing['kind']!r}, not {kind!r}."
                    )
                return self._row_to_artifact(existing)
            conn.execute(
                "INSERT INTO artifact "
                "(project_id, artifact_id, kind, created_at_utc, created_by, label, status) "
                "VALUES (?, ?, ?, ?, ?, ?, 'active')",
                (self.project_id, artifact_id, kind, created, created_by, label),
            )
            return Artifact(
                self.project_id, artifact_id, kind, created, created_by, label, "active"
            )

    def get_artifact(self, artifact_id: str) -> Artifact | None:
        artifact_id = validate_identifier(artifact_id, "artifact_id")
        row = self._artifact_row(artifact_id)
        return None if row is None else self._row_to_artifact(row)

    def list_artifacts(self) -> list[Artifact]:
        rows = self._assert_open().execute(
            "SELECT * FROM artifact WHERE project_id = ? ORDER BY rowid",
            (self.project_id,),
        ).fetchall()
        return [self._row_to_artifact(row) for row in rows]

    def _artifact_row(self, artifact_id: str) -> sqlite3.Row | None:
        return self._assert_open().execute(
            "SELECT * FROM artifact WHERE project_id = ? AND artifact_id = ?",
            (self.project_id, artifact_id),
        ).fetchone()

    @staticmethod
    def _row_to_artifact(row: sqlite3.Row) -> Artifact:
        return Artifact(
            project_id=row["project_id"],
            artifact_id=row["artifact_id"],
            kind=row["kind"],
            created_at_utc=row["created_at_utc"],
            created_by=row["created_by"],
            label=row["label"],
            status=row["status"],
        )

    # -- rights records ------------------------------------------------------ #
    def store_rights_record(self, raw_record: dict) -> RightsRef:
        """Validate and persist a canonical ``RightsRecord`` (metadata).

        Any fully valid record may be stored — including one whose ``storage`` is
        ``unknown`` (the C03 §5.2 metadata-only quarantine path). This persists
        the record **only**; it stores no source bytes. A record whose identity
        ``{project_id, rights_id, record_revision}`` already exists with different
        canonical bytes raises :class:`ArtifactConflictError`.
        """
        ref, canonical = self._validated_rights(raw_record)
        with self._store.transaction():
            return self._store_rights_row(ref, canonical)

    def get_rights_record(self, rights_ref: RightsRef | dict) -> dict | None:
        ref = _normalize_rights_ref(rights_ref)
        row = self._rights_row(ref)
        if row is None:
            return None
        import json

        return json.loads(row["canonical"].decode("utf-8"))

    def _validated_rights(self, raw_record: dict) -> tuple[RightsRef, bytes]:
        validated = validate_rights_record(raw_record, self.project_id)
        canonical = canonical_json(validated)
        subject = validated["subject"]
        if subject["kind"] != "source_revision":
            raise RecordValidationError(
                "rights_record.subject: a revision's rights record must use the "
                "'source_revision' subject (no content revision is fabricated)"
            )
        ref = RightsRef(
            self.project_id, validated["rights_id"], validated["record_revision"]
        )
        return ref, canonical

    def _rights_row(self, ref: RightsRef) -> sqlite3.Row | None:
        return self._assert_open().execute(
            "SELECT * FROM rights_record WHERE project_id = ? AND rights_id = ? "
            "AND record_revision = ?",
            (ref.project_id, ref.rights_id, ref.record_revision),
        ).fetchone()

    def _store_rights_row(self, ref: RightsRef, canonical: bytes) -> RightsRef:
        """Persist a validated rights record; reject same-identity byte changes."""
        import json

        validated = json.loads(canonical.decode("utf-8"))
        subject = validated["subject"]
        existing = self._rights_row(ref)
        if existing is not None:
            if existing["canonical"] != canonical:
                raise ArtifactConflictError(
                    f"rights record {ref.rights_id!r} revision "
                    f"{ref.record_revision} already exists with different content; "
                    f"a change must be a new record_revision."
                )
            return ref
        self._assert_open().execute(
            "INSERT INTO rights_record "
            "(project_id, rights_id, record_revision, subject_kind, "
            " subject_project_id, subject_artifact_id, subject_revision_id, "
            " canonical, content_sha256, recorded_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ref.project_id,
                ref.rights_id,
                ref.record_revision,
                subject["kind"],
                subject["project_id"],
                subject["artifact_id"],
                subject.get("revision_id"),
                canonical,
                sha256_digest(canonical),
                validated["recorded_at"],
            ),
        )
        return ref

    # -- revisions ----------------------------------------------------------- #
    def import_revision(
        self,
        *,
        artifact_id: str,
        content: object,
        origin_locator: str,
        rights: object,
        revision_id: str | None = None,
        parent_revision_id: str | None = None,
        dependencies: object = (),
        role: str = "derived_from",
        media_type: str | None = None,
        acquired_by: str = "import",
        provenance: dict | None = None,
        request_id: str | None = None,
        acquired_at_utc: str | None = None,
    ) -> Revision:
        """Import (or edit) content as a new immutable revision.

        ``content`` is either raw ``bytes`` (stored as a SQLite BLOB) or a
        generic versioned JSON **document envelope** ``{schema_version,
        document_type, payload}`` (validated, then canonically encoded to bytes).

        ``rights`` is either a raw canonical ``RightsRecord`` (validated; its
        subject must bind ``revision_id`` — so ``revision_id`` is then required)
        or an existing :class:`RightsRef`/``RightsRecordRef``. Content is stored
        **only** when the record permits ``storage``; otherwise the call is
        rejected and nothing is written.

        ``parent_revision_id`` names the exact revision this edit is based on: it
        must already exist in this artifact, which prevents a lost edit against a
        stale/absent base. All arguments are validated and the whole write is one
        transaction; a failure leaves no rows behind.
        """
        conn = self._assert_open()
        # ---- structural validation (before the transaction) ---------------- #
        artifact_id = validate_identifier(artifact_id, "artifact_id")
        if revision_id is not None:
            revision_id = validate_identifier(revision_id, "revision_id")
        if parent_revision_id is not None:
            parent_revision_id = validate_identifier(
                parent_revision_id, "parent_revision_id"
            )
        if request_id is not None:
            request_id = validate_identifier(request_id, "request_id")
        acquired_by = _require_enum(acquired_by, ACQUIRED_BY, "acquired_by")

        content_bytes, resolved_media = self._normalize_content(content, media_type)
        content_sha = sha256_digest(content_bytes)
        deps = _normalize_dependencies(
            dependencies,
            default_role=role,
            project_id=self.project_id,
            artifact_id=artifact_id,
        )
        deps = sorted(deps, key=_dependency_key)

        acquired_at = (
            _utc_now()
            if acquired_at_utc is None
            else _require_timestamp(acquired_at_utc, "acquired_at_utc")
        )
        prov_method, prov_actor, prov_at, prov_origin = self._normalize_provenance(
            provenance, origin_locator=origin_locator, acquired_by=acquired_by,
            performed_at=acquired_at,
        )

        # Normalize rights; a raw record needs revision_id to bind its subject.
        rights_raw: dict | None = None
        fixed_rights_ref: RightsRef | None = None
        if isinstance(rights, dict) and "operations" in rights:
            if revision_id is None:
                raise RecordValidationError(
                    "revision_id: required so the rights record subject can bind "
                    "this exact revision"
                )
            subject = {
                "kind": "source_revision",
                "project_id": self.project_id,
                "artifact_id": artifact_id,
                "revision_id": revision_id,
            }
            validated = validate_rights_record(rights, self.project_id, subject=subject)
            require_storage_allowed(validated)  # raw content only if storage allowed
            rights_raw = validated
            fixed_rights_ref = RightsRef(
                self.project_id, validated["rights_id"], validated["record_revision"]
            )
        else:
            fixed_rights_ref = _normalize_rights_ref(rights)

        # ---- atomic write -------------------------------------------------- #
        with self._store.transaction():
            if self._artifact_row(artifact_id) is None:
                raise RecordValidationError(
                    f"artifact_id: no artifact {artifact_id!r} in this project"
                )

            # request-id idempotency (scoped to this project).
            fingerprint = self._fingerprint(
                artifact_id=artifact_id,
                revision_id=revision_id,
                parent_revision_id=parent_revision_id,
                content_sha256=content_sha,
                dependencies=[d.as_dict() for d in deps],
                rights_ref=fixed_rights_ref.as_dict(),
                media_type=resolved_media,
                origin_locator=prov_origin,
                acquired_by=acquired_by,
                provenance_method=prov_method,
                provenance_actor=prov_actor,
            )
            if request_id is not None:
                prior = self._import_request_row(request_id)
                if prior is not None:
                    if prior["fingerprint"] != fingerprint:
                        raise ArtifactConflictError(
                            f"request_id {request_id!r} was already used with "
                            f"different arguments; a divergent request is refused."
                        )
                    existing = self._revision_row(
                        artifact_id, prior["revision_id"]
                    )
                    if existing is not None:
                        return self._row_to_revision(existing)

            # parent must exist within this artifact.
            if parent_revision_id is not None:
                if self._revision_row(artifact_id, parent_revision_id) is None:
                    raise RecordValidationError(
                        f"parent_revision_id: {parent_revision_id!r} is not an "
                        f"existing revision of artifact {artifact_id!r}"
                    )

            # persist/confirm the rights record, pinned to this revision.
            if rights_raw is not None:
                rights_ref = self._store_rights_row(
                    fixed_rights_ref, canonical_json(rights_raw)
                )
            else:
                rights_ref = fixed_rights_ref
                if self._rights_row(rights_ref) is None:
                    raise RecordValidationError(
                        f"rights: no stored rights record for "
                        f"{rights_ref.as_dict()!r}"
                    )

            # every dependency target must already exist in this project.
            for dep in deps:
                if self._revision_row(dep.artifact_id, dep.revision_id) is None:
                    raise RecordValidationError(
                        f"dependency: revision {dep.as_dict()!r} does not exist "
                        f"in this project"
                    )

            # repeated identical import reuses the existing revision.
            if request_id is None:
                duplicate = self._find_duplicate(
                    artifact_id, content_sha, parent_revision_id, rights_ref, deps
                )
                if duplicate is not None:
                    return duplicate

            if revision_id is None:
                revision_id = self.new_revision_id()
            conn.execute(
                "INSERT INTO revision "
                "(project_id, artifact_id, revision_id, parent_revision_id, content, "
                " content_sha256, byte_size, media_type, origin_locator, "
                " acquired_at_utc, acquired_by, provenance_method, provenance_actor, "
                " provenance_performed_at_utc, rights_project_id, rights_id, "
                " rights_record_revision) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.project_id,
                    artifact_id,
                    revision_id,
                    parent_revision_id,
                    content_bytes,
                    content_sha,
                    len(content_bytes),
                    resolved_media,
                    origin_locator,
                    acquired_at,
                    acquired_by,
                    prov_method,
                    prov_actor,
                    prov_at,
                    rights_ref.project_id,
                    rights_ref.rights_id,
                    rights_ref.record_revision,
                ),
            )
            for dep in deps:
                self._insert_dependency(artifact_id, revision_id, dep)
            if request_id is not None:
                conn.execute(
                    "INSERT INTO import_request "
                    "(project_id, request_id, fingerprint, artifact_id, "
                    " revision_id, created_at_utc) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        self.project_id,
                        request_id,
                        fingerprint,
                        artifact_id,
                        revision_id,
                        _utc_now(),
                    ),
                )
            return self._row_to_revision(
                self._revision_row(artifact_id, revision_id)
            )

    def _normalize_content(
        self, content: object, media_type: str | None
    ) -> tuple[bytes, str | None]:
        if isinstance(content, (bytes, bytearray)):
            data = bytes(content)
            resolved_media = media_type
        elif isinstance(content, dict):
            envelope = _validate_document_envelope(content)
            data = canonical_json(envelope)
            resolved_media = media_type or "application/json"
        else:
            raise RecordValidationError(
                "content: must be bytes or a versioned JSON document envelope object"
            )
        if media_type is not None:
            _require_nonempty_str(media_type, "media_type")
        return data, resolved_media

    def _normalize_provenance(
        self,
        provenance: dict | None,
        *,
        origin_locator: str,
        acquired_by: str,
        performed_at: str,
    ) -> tuple[str, str, str, str]:
        origin_locator = _require_nonempty_str(origin_locator, "origin_locator")
        if provenance is None:
            return "import", acquired_by, performed_at, origin_locator
        if not isinstance(provenance, dict):
            raise RecordValidationError("provenance: must be an object")
        allowed = {"method", "actor", "performed_at", "origin"}
        extra = sorted(set(provenance) - allowed)
        if extra:
            raise RecordValidationError(
                f"provenance: unknown field(s): {', '.join(extra)}"
            )
        for key in ("method", "actor", "performed_at", "origin"):
            if key not in provenance:
                raise RecordValidationError(f"provenance: missing field {key!r}")
        method = _require_enum(provenance["method"], PROVENANCE_METHODS, "provenance.method")
        actor = _require_nonempty_str(provenance["actor"], "provenance.actor")
        at = _require_timestamp(provenance["performed_at"], "provenance.performed_at")
        origin = _require_nonempty_str(provenance["origin"], "provenance.origin")
        return method, actor, at, origin

    def _fingerprint(self, **kwargs: object) -> str:
        return hashlib.sha256(canonical_json(kwargs)).hexdigest()

    def _import_request_row(self, request_id: str) -> sqlite3.Row | None:
        return self._assert_open().execute(
            "SELECT * FROM import_request WHERE project_id = ? AND request_id = ?",
            (self.project_id, request_id),
        ).fetchone()

    def _insert_dependency(
        self, artifact_id: str, revision_id: str, dep: DependencyRef
    ) -> None:
        if dep.artifact_id == artifact_id and dep.revision_id == revision_id:
            raise RecordValidationError(
                "dependencies: a revision cannot depend on itself"
            )
        self._assert_open().execute(
            "INSERT INTO dependency "
            "(project_id, from_artifact_id, from_revision_id, dependency_id, role, "
            " to_project_id, to_artifact_id, to_revision_id, created_at_utc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.project_id,
                artifact_id,
                revision_id,
                "dep_" + uuid.uuid4().hex,
                dep.role,
                dep.project_id,
                dep.artifact_id,
                dep.revision_id,
                _utc_now(),
            ),
        )

    def _revision_row(
        self, artifact_id: str, revision_id: str
    ) -> sqlite3.Row | None:
        return self._assert_open().execute(
            "SELECT * FROM revision WHERE project_id = ? AND artifact_id = ? "
            "AND revision_id = ?",
            (self.project_id, artifact_id, revision_id),
        ).fetchone()

    def _find_duplicate(
        self,
        artifact_id: str,
        content_sha: str,
        parent_revision_id: str | None,
        rights_ref: RightsRef,
        deps: list[DependencyRef],
    ) -> Revision | None:
        """Return an existing equivalent revision, or ``None``.

        Scoped to one artifact: equal bytes in a *different* logical artifact are
        deliberately **not** merged.
        """
        rows = self._assert_open().execute(
            "SELECT * FROM revision WHERE project_id = ? AND artifact_id = ? "
            "AND content_sha256 = ? AND rights_id = ? AND rights_record_revision = ? "
            "ORDER BY rowid",
            (
                self.project_id,
                artifact_id,
                content_sha,
                rights_ref.rights_id,
                rights_ref.record_revision,
            ),
        ).fetchall()
        want = sorted(_dependency_key(d) for d in deps)
        for row in rows:
            row_parent = row["parent_revision_id"]
            if (row_parent or None) != (parent_revision_id or None):
                continue
            existing_deps = sorted(
                _dependency_key(d) for d in self._dependency_rows(artifact_id, row["revision_id"])
            )
            if existing_deps == want:
                return self._row_to_revision(row)
        return None

    def _dependency_rows(
        self, artifact_id: str, revision_id: str
    ) -> list[DependencyRef]:
        rows = self._assert_open().execute(
            "SELECT * FROM dependency WHERE project_id = ? AND from_artifact_id = ? "
            "AND from_revision_id = ? ORDER BY rowid",
            (self.project_id, artifact_id, revision_id),
        ).fetchall()
        return [
            DependencyRef(
                project_id=row["to_project_id"],
                artifact_id=row["to_artifact_id"],
                revision_id=row["to_revision_id"],
                role=row["role"],
            )
            for row in rows
        ]

    @staticmethod
    def _row_to_revision(row: sqlite3.Row) -> Revision:
        return Revision(
            project_id=row["project_id"],
            artifact_id=row["artifact_id"],
            revision_id=row["revision_id"],
            parent_revision_id=row["parent_revision_id"],
            content_sha256=row["content_sha256"],
            byte_size=row["byte_size"],
            media_type=row["media_type"],
            origin_locator=row["origin_locator"],
            acquired_at_utc=row["acquired_at_utc"],
            acquired_by=row["acquired_by"],
            rights_ref=RightsRef(
                row["rights_project_id"],
                row["rights_id"],
                row["rights_record_revision"],
            ),
            provenance_method=row["provenance_method"],
            provenance_actor=row["provenance_actor"],
            provenance_performed_at_utc=row["provenance_performed_at_utc"],
            dependencies=(),
        )

    # -- reads --------------------------------------------------------------- #
    def get_revision(self, artifact_id: str, revision_id: str) -> Revision | None:
        artifact_id = validate_identifier(artifact_id, "artifact_id")
        revision_id = validate_identifier(revision_id, "revision_id")
        row = self._revision_row(artifact_id, revision_id)
        if row is None:
            return None
        return self._revision_with_deps(row)

    def list_revisions(self, artifact_id: str) -> list[Revision]:
        artifact_id = validate_identifier(artifact_id, "artifact_id")
        rows = self._assert_open().execute(
            "SELECT * FROM revision WHERE project_id = ? AND artifact_id = ? "
            "ORDER BY rowid",
            (self.project_id, artifact_id),
        ).fetchall()
        return [self._revision_with_deps(row) for row in rows]

    def current_revision(self, artifact_id: str) -> Revision | None:
        """The most recently written revision of an artifact (or ``None``)."""
        artifact_id = validate_identifier(artifact_id, "artifact_id")
        row = self._assert_open().execute(
            "SELECT * FROM revision WHERE project_id = ? AND artifact_id = ? "
            "ORDER BY rowid DESC LIMIT 1",
            (self.project_id, artifact_id),
        ).fetchone()
        return None if row is None else self._revision_with_deps(row)

    def read_content(self, artifact_id: str, revision_id: str) -> bytes:
        """Return the immutable content bytes of a revision."""
        artifact_id = validate_identifier(artifact_id, "artifact_id")
        revision_id = validate_identifier(revision_id, "revision_id")
        row = self._revision_row(artifact_id, revision_id)
        if row is None:
            raise RecordValidationError(
                f"revision {artifact_id!r}/{revision_id!r} does not exist"
            )
        return bytes(row["content"])

    def list_dependencies(self, artifact_id: str, revision_id: str) -> list[DependencyRef]:
        artifact_id = validate_identifier(artifact_id, "artifact_id")
        revision_id = validate_identifier(revision_id, "revision_id")
        return self._dependency_rows(artifact_id, revision_id)

    def dependents(self, artifact_id: str, revision_id: str) -> list[dict[str, str]]:
        """Return edges that depend on ``{artifact_id, revision_id}``.

        The reverse edge graph for later invalidation: every row here names a
        dependent revision and its role.
        """
        artifact_id = validate_identifier(artifact_id, "artifact_id")
        revision_id = validate_identifier(revision_id, "revision_id")
        rows = self._assert_open().execute(
            "SELECT * FROM dependency WHERE project_id = ? AND to_artifact_id = ? "
            "AND to_revision_id = ? ORDER BY rowid",
            (self.project_id, artifact_id, revision_id),
        ).fetchall()
        return [
            {
                "project_id": row["project_id"],
                "artifact_id": row["from_artifact_id"],
                "revision_id": row["from_revision_id"],
                "role": row["role"],
            }
            for row in rows
        ]

    def _revision_with_deps(self, row: sqlite3.Row) -> Revision:
        base = self._row_to_revision(row)
        deps = tuple(self._dependency_rows(row["artifact_id"], row["revision_id"]))
        return Revision(
            project_id=base.project_id,
            artifact_id=base.artifact_id,
            revision_id=base.revision_id,
            parent_revision_id=base.parent_revision_id,
            content_sha256=base.content_sha256,
            byte_size=base.byte_size,
            media_type=base.media_type,
            origin_locator=base.origin_locator,
            acquired_at_utc=base.acquired_at_utc,
            acquired_by=base.acquired_by,
            rights_ref=base.rights_ref,
            provenance_method=base.provenance_method,
            provenance_actor=base.provenance_actor,
            provenance_performed_at_utc=base.provenance_performed_at_utc,
            dependencies=deps,
        )


__all__ = [
    "ACQUIRED_BY",
    "DEPENDENCY_ROLES",
    "PROVENANCE_METHODS",
    "SUPPORTED_DOCUMENT_SCHEMA_VERSION",
    "Artifact",
    "ArtifactConflictError",
    "ArtifactRepository",
    "ArtifactRepositoryError",
    "DependencyRef",
    "Revision",
    "RightsRef",
    "sha256_digest",
]
