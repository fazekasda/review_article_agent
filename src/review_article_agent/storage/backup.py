"""Consistent private local backup/export snapshot and verified atomic restore.

This module implements the ``ARCH-MIGRATE`` / Step 02 deliverable *"consistent
backup/export and restore with manifest/schema versions; secrets excluded"*
(``docs/implementation/02-project-foundation.md`` deliverable line 11, acceptance
line 20). It sits on top of an already-verified
:class:`~review_article_agent.storage.store.ProjectStore` and the registry, and
performs **no** scientific processing, approval, worker, or model work.

What it is
----------

A **private local storage snapshot** of one project's SQLite store, plus a
versioned manifest, and a verified atomic restore of that snapshot. It is
deliberately **not** a manuscript export or a redistribution package: see
``docs/storage.md``. Archives must never be published and credentials must never
be stored as artifact content.

How consistency is achieved
---------------------------

A backup is produced with SQLite's **online backup API**
(:meth:`sqlite3.Connection.backup`) into a fresh, staged snapshot database — the
accepted mechanism (``03-policy-decisions.md`` §ARCH-MIGRATE). It never copies
the live database file or its ``-wal``/``-shm`` sidecars directly, so committed
WAL content is captured transactionally and a concurrent writer cannot produce a
torn backup. Immutable content is a SQLite BLOB, so the snapshot captures **all**
project data (identity, artifacts, revisions, rights records, dependencies,
import requests, and local fixture approvals) in one consistent database.

The manifest is derived **from the closed snapshot**, never from live state, so
the recorded digests describe exactly the bytes that were captured.

Snapshot contents (allowlist)
-----------------------------

A backup directory contains **only** two allowlisted members:

* ``manifest.json`` — the versioned manifest (``format_version`` 1);
* ``snapshot.sqlite3`` — the closed, self-contained snapshot database.

The project folder, ``.env``/external config, the application registry, provider
credentials, and any loose files are **never** part of a snapshot. The
application never scans for or automatically ingests secrets.

Verification is fail-closed
---------------------------

:func:`verify_backup` opens the supplied backup **read-only** and rejects, with a
stable :class:`BackupError`: a symlinked directory/member/sidecar, unexpected
members, an unsafe/traversing manifest filename, an invalid or newer manifest or
store schema, an identity mismatch, a failed ``integrity_check`` or
``foreign_key_check``, a recomputed artifact/rights BLOB mismatch, a
cross-project key, an invalid rights record, a dangling dependency/request
reference, and any approval snapshot whose stored digest/structure does not
verify. It recomputes **every** revision/rights BLOB digest and byte size, so
rewriting the outer database hash in the manifest does **not** bypass inner
validation.

Restore validates first and is atomic
-------------------------------------

:func:`restore_backup` fully verifies the backup **before** creating anything,
then stages the snapshot into a new owner-private app-owned directory and renames
it into place **before** registering (register-last). On any registration or
verification failure only its own directories are removed; no half registry row
and no stale approvals remain, and unrelated files in the target root are
preserved. It never creates a new scientific identity: the restored project keeps
the snapshot's exact ``project_id``.

Only the current store schema (version 3) is supported. A newer store schema is
refused clearly; migrations from older schemas remain a separate, independently
tested path.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote

from ..domain.errors import (
    ProjectNotFound,
    ProjectPathError,
    StoreError,
    UnsupportedSchemaVersion,
)
from ..domain.records import (
    RecordValidationError,
    canonical_json,
    validate_rights_record,
)
from . import paths, schema
from .approvals import ApprovalRepository
from .artifacts import ArtifactRepository
from .store import ProjectStore

#: The manifest ``format`` discriminator (guards against a foreign JSON file).
BACKUP_FORMAT = "review_article_agent.private_storage_backup"

#: The only supported manifest format version.
BACKUP_FORMAT_VERSION = 1

#: The only supported store schema version for a snapshot.
SUPPORTED_STORE_SCHEMA_VERSION = schema.STORE_SCHEMA_VERSION

#: Fixed allowlisted member names inside a backup directory.
MANIFEST_FILENAME = "manifest.json"
SNAPSHOT_FILENAME = "snapshot.sqlite3"

#: The exact set of tables a schema-3 project snapshot may contain. Any other
#: table (or a missing one) means the snapshot is not our storage and is refused,
#: so arbitrary/external data is never accepted as valid storage.
_ALLOWED_SNAPSHOT_TABLES = frozenset(
    {
        "meta",
        "project",
        "artifact",
        "rights_record",
        "revision",
        "dependency",
        "import_request",
        "fixture_approval",
        "fixture_approval_entry",
    }
)

#: The exact keys permitted in the manifest object.
_MANIFEST_KEYS = frozenset(
    {
        "format",
        "format_version",
        "created_at_utc",
        "project_id",
        "app_version",
        "store_schema_version",
        "database",
        "counts",
        "artifact_digest",
        "revision_digest",
        "rights_digest",
        "approvals",
        "notice",
    }
)

#: The exact keys permitted in the manifest ``database`` object.
_DATABASE_KEYS = frozenset({"filename", "sha256", "byte_size"})

#: The exact keys permitted in each manifest ``approvals`` entry.
_APPROVAL_KEYS = frozenset({"approval_id", "snapshot_digest", "entry_count"})

#: The exact keys permitted in the manifest ``counts`` object.
_COUNT_KEYS = frozenset(
    {
        "artifacts",
        "revisions",
        "rights_records",
        "dependencies",
        "import_requests",
        "approvals",
        "approval_entries",
    }
)

#: Deterministic aggregate-digest column sets (identity + integrity fields).
_ARTIFACT_DIGEST_KEYS = (
    "project_id",
    "artifact_id",
    "kind",
    "created_at_utc",
    "created_by",
    "label",
    "status",
)
_REVISION_DIGEST_KEYS = (
    "project_id",
    "artifact_id",
    "revision_id",
    "parent_revision_id",
    "content_sha256",
    "byte_size",
    "media_type",
    "origin_locator",
    "acquired_at_utc",
    "acquired_by",
    "provenance_method",
    "provenance_actor",
    "provenance_performed_at_utc",
    "rights_project_id",
    "rights_id",
    "rights_record_revision",
)
_RIGHTS_DIGEST_KEYS = (
    "project_id",
    "rights_id",
    "record_revision",
    "subject_kind",
    "subject_project_id",
    "subject_artifact_id",
    "subject_revision_id",
    "content_sha256",
)

#: Human-readable non-claim carried in every manifest.
_MANIFEST_NOTICE = (
    "Private local storage snapshot for backup/restore only. It is not a "
    "manuscript export or a redistribution package; never publish an archive and "
    "never store credentials as artifact content."
)


class BackupError(StoreError):
    """A backup/verify/restore operation failed."""


class BackupConflictError(BackupError):
    """A restore conflicts with existing registered state.

    Raised when the same project is already registered at another location, or
    when the target location is registered to a different project. The existing
    state is never modified.
    """


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _sha256_file(path: str) -> tuple[str, int]:
    """Return ``(sha256:<hex>, byte_size)`` for a file, reading it in chunks."""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
    return "sha256:" + digest.hexdigest(), size


def _digest_rows(rows: list[sqlite3.Row], keys: tuple[str, ...]) -> str:
    """Deterministic aggregate digest over the given columns of ``rows``."""
    digest = hashlib.sha256()
    for row in rows:
        digest.update(canonical_json({key: row[key] for key in keys}))
        digest.update(b"\n")
    return "sha256:" + digest.hexdigest()


def _open_readonly(db_path: str) -> sqlite3.Connection:
    """Open a database **read-only** (``mode=ro``); no writable fallback."""
    uri = "file:" + quote(db_path) + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    except sqlite3.Error as exc:
        raise BackupError(
            f"could not open the snapshot database at {db_path!r} read-only."
        ) from exc
    conn.row_factory = sqlite3.Row
    return conn


def _user_tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
    }


def _assert_known_schema(version: int, tables: set[str]) -> None:
    """Fail-closed schema gate for a snapshot database."""
    if version > SUPPORTED_STORE_SCHEMA_VERSION:
        raise UnsupportedSchemaVersion(
            label="backup store snapshot",
            found=version,
            supported=SUPPORTED_STORE_SCHEMA_VERSION,
        )
    if version != SUPPORTED_STORE_SCHEMA_VERSION:
        raise BackupError(
            f"the snapshot records store schema version {version}; only schema "
            f"version {SUPPORTED_STORE_SCHEMA_VERSION} backups are supported."
        )
    unexpected = tables - _ALLOWED_SNAPSHOT_TABLES
    if unexpected:
        raise BackupError(
            "the snapshot contains unexpected table(s) "
            f"{', '.join(sorted(unexpected))}; refusing to accept unknown or "
            "external data as valid storage."
        )
    missing = _ALLOWED_SNAPSHOT_TABLES - tables
    if missing:
        raise BackupError(
            "the snapshot is missing required table(s) "
            f"{', '.join(sorted(missing))}; it is not a complete snapshot."
        )


@dataclass
class _SnapshotStoreView:
    """A minimal read-only ``store`` view for :class:`ApprovalRepository`.

    Exposes exactly the surface the approval/artifact repositories read
    (``connection``, ``project_id``, ``schema_version``) so a snapshot can be
    verified without opening a writable :class:`ProjectStore`.
    """

    connection: sqlite3.Connection
    project_id: str
    schema_version: int


# --------------------------------------------------------------------------- #
# Online snapshot
# --------------------------------------------------------------------------- #
def _snapshot_database(source: sqlite3.Connection, dest_path: str) -> None:
    """Copy the live database into ``dest_path`` with SQLite's online API.

    Uses :meth:`sqlite3.Connection.backup` so the destination is a
    transactionally consistent copy that includes committed WAL content; a
    concurrent writer does not produce a torn file. The destination is a fresh
    file (journal mode ``delete``) and is closed before it is hashed.
    """
    try:
        dest = sqlite3.connect(dest_path)
    except sqlite3.Error as exc:
        raise BackupError(
            f"could not create a snapshot database at {dest_path!r}."
        ) from exc
    try:
        source.backup(dest)
        # The online backup copies page 1, including the source's ``write
        # version`` byte, so the destination can inherit WAL mode. Force an
        # explicit rollback-journal mode so the snapshot is a single
        # self-contained file with no live ``-wal``/``-shm`` sidecars.
        try:
            dest.execute("PRAGMA journal_mode = DELETE")
        except sqlite3.Error:  # pragma: no cover - best effort
            pass
    except sqlite3.Error as exc:
        dest.close()
        raise BackupError(
            "could not create a consistent SQLite snapshot of the live store; "
            "the backup was aborted and nothing was kept."
        ) from exc
    dest.close()
    for sidecar in (dest_path + "-wal", dest_path + "-shm", dest_path + "-journal"):
        try:
            os.unlink(sidecar)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #
def _build_manifest(
    snapshot_path: str, project_id: str, app_version: str
) -> dict[str, object]:
    """Derive the manifest **from the closed snapshot**, never from live state."""
    database_sha, database_size = _sha256_file(snapshot_path)
    conn = _open_readonly(snapshot_path)
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        tables = _user_tables(conn)
        _assert_known_schema(version, tables)

        identity = conn.execute("SELECT project_id FROM project").fetchall()
        if len(identity) != 1 or identity[0]["project_id"] != project_id:
            raise BackupError(
                "the live store identity changed while snapshotting; aborting "
                "the backup rather than recording a mismatched manifest."
            )

        artifacts = conn.execute(
            "SELECT * FROM artifact ORDER BY artifact_id"
        ).fetchall()
        revisions = conn.execute(
            "SELECT * FROM revision ORDER BY artifact_id, revision_id"
        ).fetchall()
        rights = conn.execute(
            "SELECT * FROM rights_record ORDER BY rights_id, record_revision"
        ).fetchall()
        dependency_count = int(
            conn.execute("SELECT COUNT(*) AS n FROM dependency").fetchone()["n"]
        )
        import_count = int(
            conn.execute("SELECT COUNT(*) AS n FROM import_request").fetchone()["n"]
        )
        approval_count = int(
            conn.execute("SELECT COUNT(*) AS n FROM fixture_approval").fetchone()["n"]
        )
        entry_count = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM fixture_approval_entry"
            ).fetchone()["n"]
        )
        approval_rows = conn.execute(
            "SELECT approval_id, snapshot_digest FROM fixture_approval "
            "ORDER BY approval_id"
        ).fetchall()
        per_approval_entries = {
            row["approval_id"]: int(row["n"])
            for row in conn.execute(
                "SELECT approval_id, COUNT(*) AS n FROM fixture_approval_entry "
                "GROUP BY approval_id"
            ).fetchall()
        }
    finally:
        conn.close()

    approvals = [
        {
            "approval_id": row["approval_id"],
            "snapshot_digest": row["snapshot_digest"],
            "entry_count": per_approval_entries.get(row["approval_id"], 0),
        }
        for row in approval_rows
    ]

    return {
        "format": BACKUP_FORMAT,
        "format_version": BACKUP_FORMAT_VERSION,
        "created_at_utc": _utc_now(),
        "project_id": project_id,
        "app_version": app_version,
        "store_schema_version": version,
        "database": {
            "filename": SNAPSHOT_FILENAME,
            "sha256": database_sha,
            "byte_size": database_size,
        },
        "counts": {
            "artifacts": len(artifacts),
            "revisions": len(revisions),
            "rights_records": len(rights),
            "dependencies": dependency_count,
            "import_requests": import_count,
            "approvals": approval_count,
            "approval_entries": entry_count,
        },
        "artifact_digest": _digest_rows(artifacts, _ARTIFACT_DIGEST_KEYS),
        "revision_digest": _digest_rows(revisions, _REVISION_DIGEST_KEYS),
        "rights_digest": _digest_rows(rights, _RIGHTS_DIGEST_KEYS),
        "approvals": approvals,
        "notice": _MANIFEST_NOTICE,
    }


def _write_manifest(path: str, manifest: dict[str, object]) -> None:
    """Write the manifest owner-private (``0o600``) as canonical JSON."""
    data = canonical_json(manifest)
    with open(path, "wb") as handle:
        handle.write(data)
    paths.ensure_owner_private_file(path)


# --------------------------------------------------------------------------- #
# Create
# --------------------------------------------------------------------------- #
def create_backup(store, destination) -> dict[str, object]:
    """Create a consistent private backup of ``store`` at ``destination``.

    ``store`` is an open :class:`~review_article_agent.storage.store.ProjectStore`;
    ``destination`` is the backup directory to create (it **must not** already
    exist). The snapshot is produced with SQLite's online backup API and the
    manifest is derived from the closed snapshot. All output is owner-private
    (directory ``0o700``, files ``0o600``) and the backup contains only
    ``manifest.json`` and ``snapshot.sqlite3``. Returns a JSON-safe summary.
    """
    dest = os.path.abspath(os.fspath(destination))
    if not dest or dest == os.sep:
        raise BackupError("a backup destination path is required.")
    source = store.connection  # asserts the store is open
    project_id = store.project_id
    app_version = store.app_version

    if os.path.lexists(dest):
        raise BackupError(
            f"backup destination {dest!r} already exists; refusing to overwrite "
            f"it. Choose a new, empty destination path."
        )

    parent = os.path.dirname(dest)
    staging = os.path.join(
        parent, "." + os.path.basename(dest) + ".staging-" + uuid.uuid4().hex
    )
    try:
        paths.ensure_owner_private_dir(staging)
        snapshot_path = os.path.join(staging, SNAPSHOT_FILENAME)
        _snapshot_database(source, snapshot_path)
        paths.ensure_owner_private_file(snapshot_path)
        manifest = _build_manifest(snapshot_path, project_id, app_version)
        _write_manifest(os.path.join(staging, MANIFEST_FILENAME), manifest)
        os.rename(staging, dest)
    except BaseException:
        paths.remove_dir_best_effort(staging)
        raise

    return {
        "backup_dir": dest,
        "manifest_path": os.path.join(dest, MANIFEST_FILENAME),
        "database_path": os.path.join(dest, SNAPSHOT_FILENAME),
        "project_id": project_id,
        "format_version": BACKUP_FORMAT_VERSION,
        "store_schema_version": manifest["store_schema_version"],
        "database_sha256": manifest["database"]["sha256"],
        "counts": manifest["counts"],
    }


# --------------------------------------------------------------------------- #
# Verify
# --------------------------------------------------------------------------- #
def _parse_sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise BackupError(f"{field}: must be a 'sha256:<hex>' string")
    hexpart = value[len("sha256:") :]
    if len(hexpart) != 64 or any(c not in "0123456789abcdef" for c in hexpart):
        raise BackupError(f"{field}: must be a 'sha256:<64 lowercase hex>' string")
    return value


def _require_int(value: object, field: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BackupError(f"{field}: must be an integer")
    if positive and value < 1:
        raise BackupError(f"{field}: must be a positive integer")
    if not positive and value < 0:
        raise BackupError(f"{field}: must not be negative")
    return value


def _reject_unknown_keys(obj: dict, allowed: frozenset, field: str) -> None:
    extra = sorted(set(obj) - allowed)
    if extra:
        raise BackupError(f"{field}: unknown field(s): {', '.join(extra)}")


def _read_manifest(manifest_path: str) -> dict[str, object]:
    """Read and strictly validate the manifest (never trusts its shape)."""
    try:
        with open(manifest_path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise BackupError(f"could not read the backup manifest at {manifest_path!r}.") from exc
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise BackupError("the backup manifest is not valid JSON.") from exc
    if not isinstance(manifest, dict):
        raise BackupError("the backup manifest must be a JSON object.")
    _reject_unknown_keys(manifest, _MANIFEST_KEYS, "manifest")
    if manifest.get("format") != BACKUP_FORMAT:
        raise BackupError(
            "the backup manifest has an unrecognised format; this is not a "
            "review_article_agent private storage backup."
        )
    format_version = _require_int(manifest.get("format_version"), "format_version")
    if format_version > BACKUP_FORMAT_VERSION:
        raise UnsupportedSchemaVersion(
            label="backup manifest",
            found=format_version,
            supported=BACKUP_FORMAT_VERSION,
        )
    if format_version < 1:
        raise BackupError("format_version: must be >= 1")
    for key in ("project_id", "app_version", "created_at_utc", "notice"):
        if not isinstance(manifest.get(key), str):
            raise BackupError(f"manifest.{key}: must be a string")
    paths.validate_project_id(manifest["project_id"])

    store_schema_version = _require_int(
        manifest.get("store_schema_version"), "store_schema_version"
    )
    if store_schema_version > SUPPORTED_STORE_SCHEMA_VERSION:
        raise UnsupportedSchemaVersion(
            label="backup store snapshot",
            found=store_schema_version,
            supported=SUPPORTED_STORE_SCHEMA_VERSION,
        )
    if store_schema_version != SUPPORTED_STORE_SCHEMA_VERSION:
        raise BackupError(
            f"store_schema_version: {store_schema_version} is not a supported "
            f"snapshot schema ({SUPPORTED_STORE_SCHEMA_VERSION})."
        )

    database = manifest.get("database")
    if not isinstance(database, dict):
        raise BackupError("manifest.database: must be an object")
    _reject_unknown_keys(database, _DATABASE_KEYS, "manifest.database")
    filename = database.get("filename")
    if filename != SNAPSHOT_FILENAME:
        raise BackupError(
            f"manifest.database.filename: expected {SNAPSHOT_FILENAME!r} (a safe "
            f"fixed name), got {filename!r}."
        )
    _parse_sha256(database.get("sha256"), "manifest.database.sha256")
    _require_int(database.get("byte_size"), "manifest.database.byte_size", positive=True)

    counts = manifest.get("counts")
    if not isinstance(counts, dict):
        raise BackupError("manifest.counts: must be an object")
    _reject_unknown_keys(counts, _COUNT_KEYS, "manifest.counts")
    for key in _COUNT_KEYS:
        _require_int(counts.get(key), f"manifest.counts.{key}")

    for key in ("artifact_digest", "revision_digest", "rights_digest"):
        _parse_sha256(manifest.get(key), f"manifest.{key}")

    approvals = manifest.get("approvals")
    if not isinstance(approvals, list):
        raise BackupError("manifest.approvals: must be a list")
    seen_ids: set[str] = set()
    for index, entry in enumerate(approvals):
        if not isinstance(entry, dict):
            raise BackupError(f"manifest.approvals[{index}]: must be an object")
        _reject_unknown_keys(
            entry, _APPROVAL_KEYS, f"manifest.approvals[{index}]"
        )
        approval_id = entry.get("approval_id")
        if not isinstance(approval_id, str) or not approval_id:
            raise BackupError(f"manifest.approvals[{index}].approval_id: must be a string")
        if approval_id in seen_ids:
            raise BackupError(f"manifest.approvals: duplicate entry {approval_id!r}")
        seen_ids.add(approval_id)
        _parse_sha256(
            entry.get("snapshot_digest"),
            f"manifest.approvals[{index}].snapshot_digest",
        )
        _require_int(
            entry.get("entry_count"),
            f"manifest.approvals[{index}].entry_count",
        )
    return manifest


def _verify_relations(conn: sqlite3.Connection, project_id: str, version: int) -> None:
    """Recompute + re-validate every row's inner integrity and relations."""
    # ---- rights records: BLOB digest + full record re-validation ---------- #
    rights_rows = conn.execute(
        "SELECT * FROM rights_record ORDER BY rights_id, record_revision"
    ).fetchall()
    for row in rights_rows:
        canonical = bytes(row["canonical"])
        if _sha256_bytes(canonical) != row["content_sha256"]:
            raise BackupError(
                f"rights record {row['rights_id']!r} revision "
                f"{row['record_revision']} has a content digest that does not "
                f"match its stored bytes."
            )
        if row["project_id"] != project_id or row["subject_project_id"] != project_id:
            raise BackupError(
                "a rights record carries a cross-project key; refusing."
            )
        if row["subject_kind"] not in ("source_revision", "subject_artifact_ref"):
            raise BackupError(
                f"rights record {row['rights_id']!r} has an unrecognised subject kind."
            )
        try:
            parsed = json.loads(canonical.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise BackupError(
                f"rights record {row['rights_id']!r} canonical bytes are not JSON."
            ) from exc
        try:
            validate_rights_record(parsed, project_id)
        except RecordValidationError as exc:
            raise BackupError(
                f"rights record {row['rights_id']!r} revision "
                f"{row['record_revision']} is not a valid rights record."
            ) from exc
        if row["subject_kind"] == "source_revision":
            subject = conn.execute(
                "SELECT 1 AS ok FROM revision WHERE project_id = ? AND "
                "artifact_id = ? AND revision_id = ?",
                (
                    project_id,
                    row["subject_artifact_id"],
                    row["subject_revision_id"],
                ),
            ).fetchone()
            if subject is None:
                raise BackupError(
                    f"rights record {row['rights_id']!r} names a subject revision "
                    f"that does not exist in this project."
                )

    # ---- revisions: exact BLOB digest + byte size + same-project rights --- #
    revisions = conn.execute(
        "SELECT * FROM revision ORDER BY artifact_id, revision_id"
    ).fetchall()
    for row in revisions:
        content = bytes(row["content"])
        if _sha256_bytes(content) != row["content_sha256"]:
            raise BackupError(
                f"revision {row['artifact_id']!r}/{row['revision_id']!r} content "
                f"digest does not match its stored bytes."
            )
        if len(content) != row["byte_size"]:
            raise BackupError(
                f"revision {row['artifact_id']!r}/{row['revision_id']!r} byte size "
                f"does not match its stored content."
            )
        if row["project_id"] != project_id or row["rights_project_id"] != project_id:
            raise BackupError("a revision carries a cross-project key; refusing.")
        rights = conn.execute(
            "SELECT 1 AS ok FROM rights_record WHERE project_id = ? AND "
            "rights_id = ? AND record_revision = ?",
            (project_id, row["rights_id"], row["rights_record_revision"]),
        ).fetchone()
        if rights is None:
            raise BackupError(
                f"revision {row['artifact_id']!r}/{row['revision_id']!r} pins a "
                f"rights record that does not exist in this project."
            )

    # ---- dependencies + import requests: no dangling / cross-project refs - #
    for row in conn.execute("SELECT * FROM dependency").fetchall():
        if row["project_id"] != project_id or row["to_project_id"] != project_id:
            raise BackupError("a dependency carries a cross-project key; refusing.")
    for row in conn.execute("SELECT * FROM import_request").fetchall():
        if row["project_id"] != project_id:
            raise BackupError("an import request carries a cross-project key; refusing.")
        target = conn.execute(
            "SELECT 1 AS ok FROM revision WHERE project_id = ? AND artifact_id = ? "
            "AND revision_id = ?",
            (project_id, row["artifact_id"], row["revision_id"]),
        ).fetchone()
        if target is None:
            raise BackupError(
                f"import request {row['request_id']!r} references a revision that "
                f"does not exist in this project."
            )

    # ---- local fixture approvals: non-authorizing + exact snapshots ------- #
    approval_rows = conn.execute("SELECT * FROM fixture_approval").fetchall()
    approvals_repo = ApprovalRepository(
        _SnapshotStoreView(conn, project_id, version)
    )
    for row in approval_rows:
        if row["project_id"] != project_id:
            raise BackupError("a fixture approval carries a cross-project key; refusing.")
        if row["scope"] != "storage_fixture":
            raise BackupError(
                f"fixture approval {row['approval_id']!r} has an unexpected scope; "
                f"refusing."
            )
        if row["authorizes_execution"] != 0 or row["actor_authenticated"] != 0:
            raise BackupError(
                f"fixture approval {row['approval_id']!r} records an authorizing/"
                f"authenticated constant; refusing a snapshot that claims authority."
            )
        if row["storage_schema_version"] != version:
            raise BackupError(
                f"fixture approval {row['approval_id']!r} records store schema "
                f"version {row['storage_schema_version']}, not {version}."
            )
        for entry in conn.execute(
            "SELECT * FROM fixture_approval_entry WHERE project_id = ? AND "
            "approval_id = ? ORDER BY entry_index",
            (project_id, row["approval_id"]),
        ).fetchall():
            revision = conn.execute(
                "SELECT * FROM revision WHERE project_id = ? AND artifact_id = ? "
                "AND revision_id = ?",
                (project_id, entry["artifact_id"], entry["revision_id"]),
            ).fetchone()
            if revision is None:
                raise BackupError(
                    f"fixture approval {row['approval_id']!r} entry "
                    f"{entry['entry_index']} references a missing revision."
                )
            if (
                entry["content_sha256"] != revision["content_sha256"]
                or entry["byte_size"] != revision["byte_size"]
                or entry["rights_id"] != revision["rights_id"]
                or entry["rights_record_revision"] != revision["rights_record_revision"]
            ):
                raise BackupError(
                    f"fixture approval {row['approval_id']!r} entry "
                    f"{entry['entry_index']} does not match the exact pinned "
                    f"revision (digest/size/rights)."
                )
        verification = approvals_repo.verify_snapshot(row["approval_id"])
        if not verification.ok:
            raise BackupError(
                f"fixture approval {row['approval_id']!r} snapshot does not verify: "
                f"{'; '.join(verification.problems)}"
            )


def verify_backup(backup_dir) -> dict[str, object]:
    """Read-only, fail-closed verification of a private backup directory.

    Returns a JSON-safe summary on success; raises :class:`BackupError` (or
    :class:`~review_article_agent.domain.errors.UnsupportedSchemaVersion` for a
    newer manifest/store schema) on any inconsistency. The supplied backup is
    never modified (the snapshot is opened ``mode=ro``).
    """
    directory = os.path.abspath(os.fspath(backup_dir))
    if not directory:
        raise BackupError("a backup directory path is required.")
    try:
        paths.assert_not_symlink(directory, what="backup directory")
    except ProjectPathError as exc:
        raise BackupError(str(exc)) from exc
    if not os.path.isdir(directory):
        raise BackupError(f"no backup directory found at {directory!r}.")

    entries = sorted(os.listdir(directory))
    expected_members = sorted([MANIFEST_FILENAME, SNAPSHOT_FILENAME])
    for name in entries:
        member = os.path.join(directory, name)
        try:
            paths.assert_not_symlink(member, what="backup member")
        except ProjectPathError as exc:
            raise BackupError(str(exc)) from exc
        if not os.path.isfile(member):
            raise BackupError(
                f"the backup contains an unexpected non-file member: {name!r}."
            )
    if entries != expected_members:
        raise BackupError(
            f"the backup directory must contain exactly {expected_members}; found "
            f"{entries}. Unexpected members (or sidecars) are refused."
        )

    manifest_path = os.path.join(directory, MANIFEST_FILENAME)
    manifest = _read_manifest(manifest_path)
    snapshot_path = os.path.join(directory, SNAPSHOT_FILENAME)

    actual_sha, actual_size = _sha256_file(snapshot_path)
    if actual_sha != manifest["database"]["sha256"]:
        raise BackupError(
            "the snapshot database hash does not match the manifest; the backup "
            "is corrupt or was modified."
        )
    if actual_size != manifest["database"]["byte_size"]:
        raise BackupError(
            "the snapshot database byte size does not match the manifest."
        )

    conn = _open_readonly(snapshot_path)
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version > SUPPORTED_STORE_SCHEMA_VERSION:
            raise UnsupportedSchemaVersion(
                label="backup store snapshot",
                found=version,
                supported=SUPPORTED_STORE_SCHEMA_VERSION,
            )
        if version != manifest["store_schema_version"]:
            raise BackupError(
                f"the snapshot records store schema version {version}, but the "
                f"manifest claims {manifest['store_schema_version']}."
            )
        _assert_known_schema(version, _user_tables(conn))

        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise BackupError(f"snapshot integrity_check failed: {integrity!r}.")
        fk_problems = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk_problems:
            raise BackupError(
                "snapshot foreign_key_check reported dangling references; refusing."
            )

        identity = conn.execute("SELECT project_id FROM project").fetchall()
        if len(identity) != 1:
            raise BackupError("snapshot does not contain exactly one project identity.")
        project_id = identity[0]["project_id"]
        if project_id != manifest["project_id"]:
            raise BackupError(
                "the snapshot project identity does not match the manifest; "
                "refusing a mismatched backup."
            )
        paths.validate_project_id(project_id)

        _verify_relations(conn, project_id, version)

        # Recompute aggregate digests and counts from the snapshot itself.
        artifacts = conn.execute(
            "SELECT * FROM artifact ORDER BY artifact_id"
        ).fetchall()
        revisions = conn.execute(
            "SELECT * FROM revision ORDER BY artifact_id, revision_id"
        ).fetchall()
        rights = conn.execute(
            "SELECT * FROM rights_record ORDER BY rights_id, record_revision"
        ).fetchall()
        counts = {
            "artifacts": len(artifacts),
            "revisions": len(revisions),
            "rights_records": len(rights),
            "dependencies": int(
                conn.execute("SELECT COUNT(*) AS n FROM dependency").fetchone()["n"]
            ),
            "import_requests": int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM import_request"
                ).fetchone()["n"]
            ),
            "approvals": int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM fixture_approval"
                ).fetchone()["n"]
            ),
            "approval_entries": int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM fixture_approval_entry"
                ).fetchone()["n"]
            ),
        }
        if counts != manifest["counts"]:
            raise BackupError(
                "the snapshot row counts do not match the manifest; the backup "
                "is inconsistent."
            )
        if _digest_rows(artifacts, _ARTIFACT_DIGEST_KEYS) != manifest["artifact_digest"]:
            raise BackupError("the artifact digest does not match the manifest.")
        if _digest_rows(revisions, _REVISION_DIGEST_KEYS) != manifest["revision_digest"]:
            raise BackupError("the revision digest does not match the manifest.")
        if _digest_rows(rights, _RIGHTS_DIGEST_KEYS) != manifest["rights_digest"]:
            raise BackupError("the rights digest does not match the manifest.")

        per_approval_entries = {
            row["approval_id"]: int(row["n"])
            for row in conn.execute(
                "SELECT approval_id, COUNT(*) AS n FROM fixture_approval_entry "
                "GROUP BY approval_id"
            ).fetchall()
        }
        actual_approvals = [
            {
                "approval_id": row["approval_id"],
                "snapshot_digest": row["snapshot_digest"],
                "entry_count": per_approval_entries.get(row["approval_id"], 0),
            }
            for row in conn.execute(
                "SELECT approval_id, snapshot_digest FROM fixture_approval "
                "ORDER BY approval_id"
            ).fetchall()
        ]
        if actual_approvals != manifest["approvals"]:
            raise BackupError(
                "the fixture approval snapshots do not match the manifest; "
                "refusing a backup with altered historical approvals."
            )
    finally:
        conn.close()

    return {
        "ok": True,
        "backup_dir": directory,
        "project_id": project_id,
        "format_version": manifest["format_version"],
        "store_schema_version": version,
        "database_sha256": actual_sha,
        "counts": counts,
        "approvals": actual_approvals,
    }


# --------------------------------------------------------------------------- #
# Restore
# --------------------------------------------------------------------------- #
def restore_backup(backup_dir, root, registry):
    """Verify and atomically restore a backup into ``root``, then register it.

    Validation happens in full **before anything is created**. The snapshot is
    staged into an owner-private app-owned directory and renamed into place
    before ``registry.register_restored`` runs last; on any failure only this
    call's own directories are removed (never an unrelated file), so no half
    registry row and no stale approvals remain. The restored project keeps the
    snapshot's exact ``project_id`` — a new identity is never created.
    """
    report = verify_backup(backup_dir)
    project_id = report["project_id"]
    directory = report["backup_dir"]
    root_path = paths.resolve_root(root)
    app_dir = paths.app_dir_for(root_path)

    # ---- conflict checks (no modifications on any conflict) -------------- #
    same_root_id: str | None = None
    for info in registry.list_projects():
        if info.root_path == root_path:
            same_root_id = info.project_id
            break
    if same_root_id is not None:
        if same_root_id != project_id:
            raise BackupConflictError(
                f"location {root_path!r} is already registered as project "
                f"{same_root_id!r}, not {project_id!r}; refusing to overwrite it."
            )
        # Duplicate restore to the same path + identity: safe no-op.
        info = registry.get(project_id)
        try:
            store = ProjectStore.open_at(app_dir, expected_project_id=project_id)
        except (StoreError, ProjectPathError) as exc:
            raise BackupConflictError(
                f"project {project_id!r} is registered at {root_path!r} but its "
                f"store could not be verified; refusing to modify it."
            ) from exc
        else:
            store.close()
        return info

    try:
        other = registry.get(project_id)
    except ProjectNotFound:
        other = None
    if other is not None:
        raise BackupConflictError(
            f"project {project_id!r} is already registered at {other.root_path!r}; "
            f"restoring a second copy would clone it. Relocate the existing entry "
            f"instead."
        )

    if os.path.lexists(app_dir):
        raise BackupError(
            f"an app-owned directory already exists at {app_dir!r} but is not "
            f"registered; refusing to overwrite it. Register it explicitly or "
            f"remove it if it is stale."
        )

    src_db = os.path.join(directory, SNAPSHOT_FILENAME)
    created_root = paths.ensure_directory(root_path)
    staging = os.path.join(root_path, paths.STAGING_PREFIX + uuid.uuid4().hex)
    renamed = False
    try:
        paths.ensure_owner_private_dir(staging)
        dst_db = os.path.join(staging, paths.STORE_FILENAME)
        shutil.copyfile(src_db, dst_db)
        paths.ensure_owner_private_file(dst_db)
        os.rename(staging, app_dir)
        renamed = True
        info = registry.register_restored(root_path, project_id=project_id)
    except BaseException:
        paths.remove_dir_best_effort(staging)
        if renamed:
            paths.remove_dir_best_effort(app_dir)
        if created_root:
            paths.remove_dir_if_empty(root_path)
        raise
    return info


__all__ = [
    "BACKUP_FORMAT",
    "BACKUP_FORMAT_VERSION",
    "BackupConflictError",
    "BackupError",
    "MANIFEST_FILENAME",
    "SNAPSHOT_FILENAME",
    "SUPPORTED_STORE_SCHEMA_VERSION",
    "create_backup",
    "restore_backup",
    "verify_backup",
]
