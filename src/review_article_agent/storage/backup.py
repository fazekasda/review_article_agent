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
import requests, local fixture approvals, the durable synthetic
job/attempt/accepted-result/budget rows under store schema 4, and the durable
runtime configuration/session/task/proposal mappings under store schema 5) in one
consistent database.

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
validation. Under store schema 4 it additionally re-validates every durable
job/attempt/accepted-result/budget row (identity and parameter/result
fingerprints, fences, counters, state/outcome flags, checkpoint JSON,
bounded-exact-decimal budgets/reserves, and cross-project/dangling references),
and under store schema 5 it additionally re-validates every durable runtime
configuration/session/task/proposal mapping (recomputed canonical digests and
fingerprints, projected columns, config/task/input/proposal links, and
cross-project keys) via the shared
:func:`~review_article_agent.storage.agent_runtime.store_integrity_problems`
sweep — without ever requiring a historical task to still name the *current*
revision, and without treating a proposal as an approval. Under store schema 6 it
additionally re-validates every durable runtime dispatch and its operational row
(the route/bucket identity, digests, exact-decimal reserve/settled cost, state/
result/cost consistency, and the linked task) via the shared
:func:`~review_article_agent.runtime.dispatch.runtime_dispatch_integrity_problems`
sweep, and under store schema 7 it additionally re-validates every durable
human-approval row (the recomputed snapshot digest/structure, projected columns,
entry pins, and the out-of-band owner-auth binding) via the shared
:func:`~review_article_agent.storage.human_approvals.integrity_problems` sweep —
preserving historical ``stale``/``superseded`` approvals and never reading a
capability/challenge. Under store schema 8 it additionally re-validates every
durable pre-dispatch Jev decision-request intent (recomputed canonical bytes and
content digest, projected columns, the exact question refs/versions, the parent
dispatch/task links, and the recomputed request binding) via the shared
:func:`~review_article_agent.storage.decision_intents.decision_intent_integrity_problems`
sweep — never requiring the intent to still name the *current* head revision and
never reading a credential, capability, or authority. Under store schema 9 it
additionally re-validates every durable full per-question Jev decision-result
observation (the whole-call set row and each per-question record independently,
re-deriving the canonical bytes/digest, the projected columns, the parent
dispatch/task/intent pins, the exact complete ordered question set against the
recorded intent, and the per-record refs/versions/cost/provider envelope) via the
shared
:func:`~review_article_agent.storage.decision_results.decision_result_integrity_problems`
sweep — never requiring the target to still name the *current* head revision,
never reading a credential, capability, or authority, and never re-performing an
accept or reinterpreting a stored row as an execution, a retry, or a new
acceptance (a committed row's accepted triple is preserved verbatim; an ``unknown``
cost stays ``unknown``, never ``0``). Under store schema 10 it additionally
re-validates every durable pre-transport session-turn intent (the exact canonical
plan/message bytes and their digests, the projected session/message/admission
columns, and the independent binding to the persisted dispatch/task/mapping/
configuration parents) via the shared
:func:`~review_article_agent.storage.session_intents.session_intent_integrity_problems`
sweep — read-only and without requiring a live service, a live filesystem path, or
the target's *current* head revision.

Restore validates first and is atomic
-------------------------------------

:func:`restore_backup` fully verifies the backup **before** creating anything,
then stages the snapshot into a new owner-private app-owned directory and renames
it into place **before** registering (register-last). On any registration or
verification failure only its own directories are removed; no half registry row
and no stale approvals remain, and unrelated files in the target root are
preserved. It never creates a new scientific identity: the restored project keeps
the snapshot's exact ``project_id``.

Supported store schemas are **3 through 10**. Schema 3 is the pre-Step-03 schema
(identity, artifacts, revisions, rights, dependencies, import requests, and local
fixture approvals); schema 4 adds the durable worker's synthetic job/attempt/
accepted-result/budget tables (``runtime/jobs.py``); schema 5 adds the durable
runtime configuration/session/task/proposal mapping tables
(``storage/agent_runtime.py``); schema 6 adds the runtime dispatch/cost ledger
(``runtime/dispatch.py``); schema 7 adds the out-of-band human approvals
(``storage/human_approvals.py``); schema 8 adds the immutable durable
pre-dispatch Jev decision-request intent (``storage/decision_intents.py``);
schema 9 adds the immutable durable full per-question Jev decision-result
observation (``storage/decision_results.py``); and schema 10 adds the immutable
durable pre-transport session-turn intent correlation
(``storage/session_intents.py``). Each is
validated against the exact schema the
canonical migrations generate for **that** version. A schema-4 snapshot
additionally re-validates every job row read-only (identity and parameter/result
fingerprints, fences, counters, state/outcome flags, checkpoint JSON, bounded
exact-decimal budgets/reserves, and cross-project/dangling references); a
schema-5 snapshot additionally re-validates every runtime mapping row read-only
(recomputed canonical digests/fingerprints, projected columns, config/task/input/
proposal links, and cross-project keys). A newer store schema is refused clearly.
A schema-3/4 snapshot is restored as-is and is upgraded to the current schema by
the ordinary ``ProjectStore`` migration on first open; no format churn is needed
because the manifest ``format_version`` stays 1 and the version-specific
validation is driven by the recorded ``store_schema_version`` through the
extensible ``_SCHEMA_VALIDATORS`` hook (so a later schema-6 checkpoint can append
its own validator without disturbing schema-3/4/5 compatibility).
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
from decimal import Decimal
from urllib.parse import quote

from ..domain.errors import (
    InvalidProjectId,
    ProjectNotFound,
    ProjectPathError,
    StoreError,
    UnsupportedSchemaVersion,
)
from ..domain.records import (
    RecordValidationError,
    canonical_json,
    require_storage_allowed,
    validate_identifier,
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

#: The oldest store schema version a snapshot may record. Schema 3 is the
#: pre-Step-03 schema; schema 4 adds the durable job/attempt/budget tables. A
#: schema-3 snapshot is verified against the canonical schema-3 definition and is
#: upgraded to the current schema by the ordinary ``ProjectStore`` migration on
#: first open.
MIN_SUPPORTED_STORE_SCHEMA_VERSION = 3

#: The closed set of store schema versions a snapshot may record (``3..current``).
SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS = frozenset(
    range(MIN_SUPPORTED_STORE_SCHEMA_VERSION, SUPPORTED_STORE_SCHEMA_VERSION + 1)
)

#: The store schema version at (or above) which the durable job/attempt/budget
#: tables exist and must be inner-validated.
_JOB_SCHEMA_VERSION = 4

#: The store schema version at (or above) which the durable runtime
#: configuration/session/task/proposal mapping tables exist and must be
#: inner-validated.
_RUNTIME_SCHEMA_VERSION = 5

#: The store schema version at (or above) which the durable runtime dispatch +
#: route-separated cost ledger tables exist and must be inner-validated.
_DISPATCH_SCHEMA_VERSION = 6

#: The store schema version at (or above) which the durable exact named-gate
#: human-approval tables exist and must be inner-validated.
_HUMAN_APPROVAL_SCHEMA_VERSION = 7

#: The store schema version at (or above) which the immutable durable
#: pre-dispatch Jev decision-request intent table exists and must be
#: inner-validated.
_INTENT_SCHEMA_VERSION = 8

#: The store schema version at (or above) which the immutable durable
#: full per-question Jev decision-result observation tables (the set row and its
#: per-question rows) exist and must be inner-validated.
_DECISION_RESULT_SCHEMA_VERSION = 9

#: The store schema version at (or above) which the immutable durable
#: pre-transport session-turn intent correlation table exists and must be
#: inner-validated.
_SESSION_INTENT_SCHEMA_VERSION = 10

#: Store schema versions whose *recorded* approval ``storage_schema_version`` is
#: accepted (the historical version survives a restore where supported; an
#: arbitrary/unknown version is refused). Bounded to ``1..current``.
_SUPPORTED_RECORDED_SCHEMA_VERSIONS = frozenset(
    range(1, SUPPORTED_STORE_SCHEMA_VERSION + 1)
)

#: Fixed allowlisted member names inside a backup directory.
MANIFEST_FILENAME = "manifest.json"
SNAPSHOT_FILENAME = "snapshot.sqlite3"

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


def _schema_objects(conn: sqlite3.Connection) -> dict[tuple[str, str], str]:
    """Return ``{(type, name): sql}`` for every user schema object.

    Covers tables, indices, triggers, and views; SQLite-internal objects
    (``sqlite_*``, e.g. implicit indexes) are ignored.
    """
    return {
        (row["type"], row["name"]): row["sql"]
        for row in conn.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        )
    }


_CANONICAL_SCHEMA_OBJECTS: dict[int, dict[tuple[str, str], str]] = {}


def _canonical_schema_objects(version: int) -> dict[tuple[str, str], str]:
    """Build schema ``version`` from the canonical migrations **in memory**.

    Comparing a snapshot against the schema the migrations actually generate
    (not merely against a set of table names) detects a forged database that
    reuses the right names but drops a foreign key or an immutability trigger, or
    adds a hidden column — none of which ``PRAGMA foreign_key_check`` can see. The
    result is cached per version, so schema-3 and schema-4 snapshots each compare
    against their own exact canonical definition.
    """
    cached = _CANONICAL_SCHEMA_OBJECTS.get(version)
    if cached is None:
        conn = sqlite3.connect(":memory:", isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            schema.migrate(
                conn,
                target_version=version,
                migrations=schema.STORE_MIGRATIONS,
                label="project store",
            )
            cached = _schema_objects(conn)
        finally:
            conn.close()
        _CANONICAL_SCHEMA_OBJECTS[version] = cached
    return cached


def _assert_known_schema(conn: sqlite3.Connection, version: int) -> None:
    """Fail-closed schema gate: version **and** exact schema-object fingerprint."""
    if version > SUPPORTED_STORE_SCHEMA_VERSION:
        raise UnsupportedSchemaVersion(
            label="backup store snapshot",
            found=version,
            supported=SUPPORTED_STORE_SCHEMA_VERSION,
        )
    if version not in SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS:
        raise BackupError(
            f"the snapshot records store schema version {version}; only schema "
            f"versions {sorted(SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS)} are supported."
        )
    actual = _schema_objects(conn)
    expected = _canonical_schema_objects(version)
    if actual != expected:
        missing = sorted(f"{kind} {name!r}" for kind, name in expected if (kind, name) not in actual)
        extra = sorted(f"{kind} {name!r}" for kind, name in actual if (kind, name) not in expected)
        altered = sorted(
            f"{kind} {name!r}"
            for (kind, name), sql in expected.items()
            if (kind, name) in actual and actual[(kind, name)] != sql
        )
        details = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if extra:
            details.append("unexpected: " + ", ".join(extra))
        if altered:
            details.append("altered: " + ", ".join(altered))
        raise BackupError(
            f"the snapshot schema does not match the canonical schema-{version} "
            "definition (" + "; ".join(details) + "); refusing a snapshot with "
            "missing, extra, or altered schema objects."
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
    concurrent writer does not produce a torn file. The destination is converted
    to a self-contained rollback-journal database (the online backup copies the
    source's header, so the destination can inherit WAL), and any leftover sidecar
    is treated as a failure rather than silently unlinked.
    """
    try:
        dest = sqlite3.connect(dest_path, isolation_level=None)
    except sqlite3.Error as exc:
        raise BackupError(
            f"could not create a snapshot database at {dest_path!r}."
        ) from exc
    try:
        source.backup(dest)
        # The online backup copies page 1, including the source's ``write
        # version`` byte, so the destination can inherit WAL mode. Convert it to a
        # single self-contained file; a conversion that does not take is a hard
        # failure (never a silent swallow).
        mode = dest.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
        if str(mode).lower() != "delete":
            raise BackupError(
                "could not convert the snapshot to a self-contained "
                "rollback-journal database; aborting the backup."
            )
    except sqlite3.Error as exc:
        dest.close()
        raise BackupError(
            "could not create a consistent SQLite snapshot of the live store; "
            "the backup was aborted and nothing was kept."
        ) from exc
    except BaseException:
        dest.close()
        raise
    dest.close()
    for sidecar in (dest_path + "-wal", dest_path + "-shm", dest_path + "-journal"):
        if os.path.lexists(sidecar):
            raise BackupError(
                f"the snapshot is not self-contained (it left {os.path.basename(sidecar)!r}); "
                f"aborting rather than shipping an uncheckpointed WAL."
            )


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
        _assert_known_schema(conn, version)

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

    if getattr(source, "in_transaction", False):
        raise BackupError(
            "the live store connection has an open transaction; commit or roll "
            "back before creating a backup, so the snapshot is not taken "
            "mid-transaction (which could block)."
        )

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
        # Fully verify the staged backup before publishing it, so a corrupt
        # source never yields a valid-looking backup at ``destination``.
        verify_backup(staging)
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
    # Do not echo the key names: a malformed manifest is untrusted input.
    if set(obj) - allowed:
        raise BackupError(f"{field}: contains unknown field(s); refusing.")


class _DuplicateJSONKey(ValueError):
    """A JSON object with a duplicate key (rejected; never trust the last value)."""


def _no_duplicate_pairs(pairs):
    seen: set[str] = set()
    result: dict = {}
    for key, value in pairs:
        if key in seen:
            raise _DuplicateJSONKey("duplicate JSON object key")
        seen.add(key)
        result[key] = value
    return result


def _reject_json_constant(name: str):
    raise _DuplicateJSONKey(f"non-finite JSON constant {name!r}")


def _load_manifest_json(raw: bytes) -> object:
    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_no_duplicate_pairs,
        parse_constant=_reject_json_constant,
    )


def _read_manifest(manifest_path: str) -> dict[str, object]:
    """Read and strictly validate the manifest (never trusts its shape).

    Malformed JSON — duplicate keys, non-finite constants, over-deep nesting, or
    invalid UTF-8 — is rejected as a :class:`BackupError` without echoing any
    untrusted key or value.
    """
    try:
        with open(manifest_path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise BackupError(f"could not read the backup manifest at {manifest_path!r}.") from exc
    try:
        manifest = _load_manifest_json(raw)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
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
    try:
        paths.validate_project_id(manifest["project_id"])
    except InvalidProjectId as exc:
        raise BackupError(
            "the backup manifest records an invalid project id; refusing."
        ) from exc

    store_schema_version = _require_int(
        manifest.get("store_schema_version"), "store_schema_version"
    )
    if store_schema_version > SUPPORTED_STORE_SCHEMA_VERSION:
        raise UnsupportedSchemaVersion(
            label="backup store snapshot",
            found=store_schema_version,
            supported=SUPPORTED_STORE_SCHEMA_VERSION,
        )
    if store_schema_version not in SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS:
        raise BackupError(
            f"store_schema_version: {store_schema_version} is not a supported "
            f"snapshot schema {sorted(SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS)}."
        )

    database = manifest.get("database")
    if not isinstance(database, dict):
        raise BackupError("manifest.database: must be an object")
    _reject_unknown_keys(database, _DATABASE_KEYS, "manifest.database")
    filename = database.get("filename")
    if filename != SNAPSHOT_FILENAME:
        raise BackupError(
            "manifest.database.filename: must be the fixed safe snapshot "
            "filename; refusing."
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
            raise BackupError("manifest.approvals: contains duplicate approval ids; refusing.")
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
    # ---- rights records: BLOB digest + exact canonical/column bindings ---- #
    rights_rows = conn.execute(
        "SELECT * FROM rights_record ORDER BY rights_id, record_revision"
    ).fetchall()
    rights_by_ref: dict[tuple[str, int], dict] = {}
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
        try:
            parsed = json.loads(canonical.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise BackupError(
                f"rights record {row['rights_id']!r} canonical bytes are not JSON."
            ) from exc
        try:
            validated = validate_rights_record(parsed, project_id)
        except RecordValidationError as exc:
            raise BackupError(
                f"rights record {row['rights_id']!r} revision "
                f"{row['record_revision']} is not a valid rights record."
            ) from exc
        # The canonical record must match the denormalized SQL identity columns,
        # so a record cannot be stored under one identity/subject and parsed as
        # another.
        if (
            validated["project_id"] != row["project_id"]
            or validated["rights_id"] != row["rights_id"]
            or validated["record_revision"] != row["record_revision"]
        ):
            raise BackupError(
                "a rights record's canonical identity does not match its stored "
                "identity columns; refusing."
            )
        subject = validated["subject"]
        if (
            subject["kind"] != row["subject_kind"]
            or subject["project_id"] != row["subject_project_id"]
            or subject["artifact_id"] != row["subject_artifact_id"]
        ):
            raise BackupError(
                "a rights record's canonical subject does not match its stored "
                "subject columns; refusing."
            )
        if subject["kind"] == "source_revision":
            if subject.get("revision_id") != row["subject_revision_id"]:
                raise BackupError(
                    "a rights record's canonical subject revision does not match "
                    "its stored subject column; refusing."
                )
            exists = conn.execute(
                "SELECT 1 AS ok FROM revision WHERE project_id = ? AND "
                "artifact_id = ? AND revision_id = ?",
                (project_id, subject["artifact_id"], subject["revision_id"]),
            ).fetchone()
            if exists is None:
                raise BackupError(
                    f"rights record {row['rights_id']!r} names a subject revision "
                    f"that does not exist in this project."
                )
        elif row["subject_revision_id"] is not None:
            raise BackupError(
                "a rights record with a pre-acquisition subject records a "
                "revision id; refusing."
            )
        rights_by_ref[(row["rights_id"], row["record_revision"])] = validated

    # ---- revisions: exact BLOB digest + exact rights subject + storage ---- #
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
        if (
            conn.execute(
                "SELECT 1 AS ok FROM artifact WHERE project_id = ? AND "
                "artifact_id = ?",
                (project_id, row["artifact_id"]),
            ).fetchone()
            is None
        ):
            raise BackupError(
                "a revision references an artifact that does not exist in this "
                "project; refusing."
            )
        if row["parent_revision_id"] is not None and (
            conn.execute(
                "SELECT 1 AS ok FROM revision WHERE project_id = ? AND "
                "artifact_id = ? AND revision_id = ?",
                (project_id, row["artifact_id"], row["parent_revision_id"]),
            ).fetchone()
            is None
        ):
            raise BackupError(
                "a revision references a parent revision that does not exist; "
                "refusing."
            )
        validated = rights_by_ref.get(
            (row["rights_id"], row["rights_record_revision"])
        )
        if validated is None:
            raise BackupError(
                f"revision {row['artifact_id']!r}/{row['revision_id']!r} pins a "
                f"rights record that does not exist in this project."
            )
        subject = validated["subject"]
        if (
            subject["kind"] != "source_revision"
            or subject["project_id"] != project_id
            or subject["artifact_id"] != row["artifact_id"]
            or subject.get("revision_id") != row["revision_id"]
        ):
            raise BackupError(
                f"revision {row['artifact_id']!r}/{row['revision_id']!r} pins a "
                f"rights record whose subject does not bind this exact revision; "
                f"refusing."
            )
        try:
            require_storage_allowed(validated)
        except RecordValidationError as exc:
            raise BackupError(
                f"revision {row['artifact_id']!r}/{row['revision_id']!r} pins a "
                f"rights record that does not permit storage (or is metadata-only); "
                f"refusing to accept it as authorizing content."
            ) from exc

    # ---- dependencies + import requests: no dangling / cross-project refs - #
    for row in conn.execute("SELECT * FROM dependency").fetchall():
        if row["project_id"] != project_id or row["to_project_id"] != project_id:
            raise BackupError("a dependency carries a cross-project key; refusing.")
        for artifact_id, revision_id in (
            (row["from_artifact_id"], row["from_revision_id"]),
            (row["to_artifact_id"], row["to_revision_id"]),
        ):
            if (
                conn.execute(
                    "SELECT 1 AS ok FROM revision WHERE project_id = ? AND "
                    "artifact_id = ? AND revision_id = ?",
                    (project_id, artifact_id, revision_id),
                ).fetchone()
                is None
            ):
                raise BackupError(
                    "a dependency references a revision that does not exist in "
                    "this project; refusing."
                )
    for row in conn.execute("SELECT * FROM import_request").fetchall():
        if row["project_id"] != project_id:
            raise BackupError("an import request carries a cross-project key; refusing.")
        if (
            conn.execute(
                "SELECT 1 AS ok FROM artifact WHERE project_id = ? AND "
                "artifact_id = ?",
                (project_id, row["artifact_id"]),
            ).fetchone()
            is None
        ):
            raise BackupError(
                "an import request references an artifact that does not exist; "
                "refusing."
            )
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
        if row["storage_schema_version"] not in _SUPPORTED_RECORDED_SCHEMA_VERSIONS:
            raise BackupError(
                f"fixture approval {row['approval_id']!r} records an unsupported "
                f"store schema version {row['storage_schema_version']}; refusing."
            )
        target = conn.execute(
            "SELECT artifact_id FROM revision WHERE project_id = ? AND "
            "artifact_id = ? AND revision_id = ?",
            (project_id, row["target_artifact_id"], row["target_revision_id"]),
        ).fetchone()
        if target is None or target["artifact_id"] != row["target_artifact_id"]:
            raise BackupError(
                f"fixture approval {row['approval_id']!r} targets a revision that "
                f"does not exist in this project; refusing."
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


# --------------------------------------------------------------------------- #
# Durable job/attempt/budget inner validation (schema 4)
# --------------------------------------------------------------------------- #
#: Attempt ``prior_outcome`` vocabulary (mirrors the schema-4 CHECK constraint and
#: ``runtime.jobs``); kept here so a snapshot is validated without importing the
#: runtime repository.
_PRIOR_OUTCOMES = frozenset(
    {"none", "definitive_failure", "ambiguous", "succeeded", "interrupted"}
)

#: Reserve lifecycle states (schema-4 CHECK constraint).
_RESERVE_STATES = frozenset({"held", "retained", "settled"})


def _parse_canonical_json_text(value: object, field: str) -> object:
    """Parse a stored JSON text column and require it to be canonical JSON.

    Every JSON text column is written through ``canonical_json`` (sorted keys,
    compact separators, no non-finite constants). Re-encoding the parsed value and
    comparing the bytes rejects a forged/duplicated-key/non-canonical body, while
    reusing :func:`canonical_json` also enforces the portability/depth bounds.
    """
    if not isinstance(value, str):
        raise BackupError(f"{field}: must be a JSON text string")
    try:
        parsed = json.loads(value)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        # ``json.loads`` can raise RecursionError on pathologically deep nesting;
        # every failure (including an over-long/deep body) is a clean, payload-free
        # BackupError, never a leaked raw payload or an escaping RecursionError.
        raise BackupError(f"{field}: is not valid JSON") from exc
    try:
        canonical = canonical_json(parsed)
    except (RecordValidationError, RecursionError) as exc:
        raise BackupError(f"{field}: is not portable canonical JSON") from exc
    if canonical.decode("utf-8") != value:
        raise BackupError(f"{field}: is not stored as canonical JSON; refusing.")
    return parsed


def _require_exact_decimal(value: object, field: str, *, positive: bool = True) -> Decimal:
    """Validate a stored exact-decimal amount (bounded, finite, non-negative).

    ``positive=True`` (default) additionally requires a strictly positive amount
    and is used for caps and reserves. A **reported cost amount** uses
    ``positive=False``: a synthetic local completion can legitimately report a
    known/estimated actual cost of exactly ``0`` without the reserve shrinking.
    """
    from ..runtime.jobs import _bounded_decimal_amount  # local: avoid layering cost

    try:
        amount = _bounded_decimal_amount(value, field)
    except Exception as exc:  # JobValidationError -> fail closed as a BackupError
        raise BackupError(f"{field}: is not a bounded exact decimal amount") from exc
    if positive and amount == 0:
        raise BackupError(f"{field}: must be greater than zero")
    return amount


def _require_identifier(value: object, field: str) -> str:
    """Validate a stored opaque identifier with the domain identifier validator."""
    try:
        return validate_identifier(value, field)
    except RecordValidationError as exc:
        raise BackupError(f"{field}: is not a valid identifier") from exc


def _verify_job_relations(conn: sqlite3.Connection, project_id: str) -> None:
    """Read-only re-validation of the schema-4 durable job/attempt/budget rows.

    Recomputes every identity and parameter/result fingerprint from the stored
    bytes, re-derives each ``job_id`` from its ``(project_id, request_id)`` key,
    and checks the fences, counters, state/outcome flags, checkpoint JSON, and
    bounded exact-decimal budgets/reserves — so a corrupt archive whose outer
    database hash was rewritten still fails inner verification. Cross-project keys
    and dangling job/attempt references are refused. Schema-3 snapshots have no
    such tables and skip this entirely.
    """
    from ..runtime.jobs import (  # local imports keep the storage layer decoupled
        ATTEMPT_STATES,
        COST_STATES,
        DISPATCH_KINDS,
        JOB_STATES,
        MAX_SAFE_RETRIES,
        _fingerprint,
        _job_id_for,
    )

    job_rows = conn.execute("SELECT * FROM job ORDER BY job_id").fetchall()
    jobs_by_id: dict[str, sqlite3.Row] = {}
    for row in job_rows:
        if row["project_id"] != project_id:
            raise BackupError("a job carries a cross-project key; refusing.")
        job_id = row["job_id"]
        request_id = row["request_id"]
        if not isinstance(job_id, str) or not job_id:
            raise BackupError("a job row has a malformed job id; refusing.")
        _require_identifier(job_id, "job id")
        _require_identifier(request_id, f"job {job_id!r} request key")
        if _job_id_for(project_id, request_id) != job_id:
            raise BackupError(
                f"job {job_id!r} identity does not derive from its project/request "
                f"key; refusing."
            )
        if job_id in jobs_by_id:
            raise BackupError("the snapshot contains a duplicate job id; refusing.")
        if row["state"] not in JOB_STATES:
            raise BackupError(f"job {job_id!r} has an illegal state; refusing.")
        if row["outcome_unresolved"] not in (0, 1):
            raise BackupError(
                f"job {job_id!r} has a malformed unresolved-outcome flag; refusing."
            )
        task_kind = row["task_kind"]
        if not isinstance(task_kind, str) or not task_kind:
            raise BackupError(f"job {job_id!r} has a malformed task kind; refusing.")
        parameters = _parse_canonical_json_text(
            row["parameters_json"], f"job {job_id!r} parameters"
        )
        expected_fp = _fingerprint({"task_kind": task_kind, "parameters": parameters})
        if expected_fp != row["fingerprint"]:
            raise BackupError(
                f"job {job_id!r} parameter fingerprint does not match its stored "
                f"parameters; refusing."
            )
        resume_state = row["resume_state"]
        if resume_state is not None and resume_state not in JOB_STATES:
            raise BackupError(
                f"job {job_id!r} has an illegal resume state; refusing."
            )
        # ``resume_state`` is recorded exactly while a job is paused.
        if (row["state"] == "paused") != (resume_state is not None):
            raise BackupError(
                f"job {job_id!r} has an inconsistent paused/resume state; refusing."
            )
        for field in ("created_at_utc", "updated_at_utc"):
            if not isinstance(row[field], str) or not row[field]:
                raise BackupError(
                    f"job {job_id!r} has a malformed {field}; refusing."
                )
        jobs_by_id[job_id] = row

    attempt_rows = conn.execute(
        "SELECT * FROM job_attempt ORDER BY job_id, attempt_no"
    ).fetchall()
    attempts_by_id: dict[str, sqlite3.Row] = {}
    attempts_by_job: dict[str, list[sqlite3.Row]] = {}
    for row in attempt_rows:
        if row["project_id"] != project_id:
            raise BackupError("a job attempt carries a cross-project key; refusing.")
        attempt_id = row["attempt_id"]
        job_id = row["job_id"]
        if not isinstance(attempt_id, str) or not attempt_id:
            raise BackupError("a job attempt has a malformed attempt id; refusing.")
        _require_identifier(attempt_id, "attempt id")
        if attempt_id in attempts_by_id:
            raise BackupError("the snapshot contains a duplicate attempt id; refusing.")
        if job_id not in jobs_by_id:
            raise BackupError(
                f"attempt {attempt_id!r} references a job that does not exist in "
                f"this project; refusing."
            )
        if row["dispatch_kind"] not in DISPATCH_KINDS:
            raise BackupError(
                f"attempt {attempt_id!r} has an illegal dispatch kind; refusing."
            )
        if row["state"] not in ATTEMPT_STATES:
            raise BackupError(
                f"attempt {attempt_id!r} has an illegal state; refusing."
            )
        if row["prior_outcome"] not in _PRIOR_OUTCOMES:
            raise BackupError(
                f"attempt {attempt_id!r} has an illegal prior outcome; refusing."
            )
        if row["outcome_unresolved"] not in (0, 1):
            raise BackupError(
                f"attempt {attempt_id!r} has a malformed unresolved flag; refusing."
            )
        if row["outcome_unresolved"] == 1 and row["state"] not in (
            "ambiguous",
            "interrupted",
        ):
            raise BackupError(
                f"attempt {attempt_id!r} claims an unresolved outcome in a state "
                f"that cannot carry one; refusing."
            )
        if row["state"] == "ambiguous" and row["outcome_unresolved"] != 1:
            raise BackupError(
                f"attempt {attempt_id!r} is ambiguous but not marked unresolved; "
                f"refusing."
            )
        if not isinstance(row["fence"], int) or row["fence"] < 1:
            raise BackupError(f"attempt {attempt_id!r} has an illegal fence; refusing.")
        if not isinstance(row["attempt_no"], int) or row["attempt_no"] < 1:
            raise BackupError(
                f"attempt {attempt_id!r} has an illegal attempt number; refusing."
            )
        # Dispatch-kind / reserve consistency (reserve-before-dispatch, C02 §5.6).
        if row["dispatch_kind"] == "local":
            if row["reserve_usd"] is not None:
                raise BackupError(
                    f"a local attempt {attempt_id!r} records a budget reserve; "
                    f"refusing."
                )
        else:
            if row["reserve_usd"] is None:
                raise BackupError(
                    f"external attempt {attempt_id!r} has no budget reserve recorded "
                    f"before dispatch; refusing."
                )
            _require_exact_decimal(
                row["reserve_usd"], f"attempt {attempt_id!r} reserve"
            )
        if row["cost_state"] not in COST_STATES:
            raise BackupError(
                f"attempt {attempt_id!r} has an illegal cost state; refusing."
            )
        if row["cost_state"] in ("known", "estimated"):
            if row["cost_amount_usd"] is None:
                raise BackupError(
                    f"attempt {attempt_id!r} records a {row['cost_state']} cost with "
                    f"no amount; refusing."
                )
            _require_exact_decimal(
                row["cost_amount_usd"],
                f"attempt {attempt_id!r} cost amount",
                positive=False,
            )
        elif row["cost_amount_usd"] is not None:
            raise BackupError(
                f"attempt {attempt_id!r} records a cost amount for a "
                f"{row['cost_state']} cost; refusing."
            )
        if row["checkpoint_json"] is not None:
            _parse_canonical_json_text(
                row["checkpoint_json"], f"attempt {attempt_id!r} checkpoint"
            )
        for field in ("retry_authorized_by", "external_ref"):
            value = row[field]
            if value is not None and (
                not isinstance(value, str) or not value or value != value.strip()
            ):
                raise BackupError(
                    f"attempt {attempt_id!r} has a malformed {field}; refusing."
                )
        admission_id = row["admission_id"]
        if admission_id is not None:
            if not isinstance(admission_id, str) or not admission_id:
                raise BackupError(
                    f"attempt {attempt_id!r} has a malformed admission id; refusing."
                )
            try:
                validate_identifier(admission_id, "admission_id")
            except RecordValidationError as exc:
                raise BackupError(
                    f"attempt {attempt_id!r} has an invalid admission id; refusing."
                ) from exc
        if not isinstance(row["started_at_utc"], str) or not row["started_at_utc"]:
            raise BackupError(
                f"attempt {attempt_id!r} has a malformed start timestamp; refusing."
            )
        if row["ended_at_utc"] is not None and (
            not isinstance(row["ended_at_utc"], str) or not row["ended_at_utc"]
        ):
            raise BackupError(
                f"attempt {attempt_id!r} has a malformed end timestamp; refusing."
            )
        attempts_by_id[attempt_id] = row
        attempts_by_job.setdefault(job_id, []).append(row)

    # ---- per-job fences/counters must reconstruct exactly ------------------ #
    for job in job_rows:
        job_id = job["job_id"]
        attempts = attempts_by_job.get(job_id, [])
        if not isinstance(job["current_fence"], int) or job["current_fence"] < 0:
            raise BackupError(f"job {job_id!r} has an illegal current fence; refusing.")
        if not isinstance(job["attempts_total"], int) or job["attempts_total"] < 0:
            raise BackupError(f"job {job_id!r} has an illegal attempt count; refusing.")
        if not isinstance(job["retries_used"], int) or job["retries_used"] < 0:
            raise BackupError(f"job {job_id!r} has an illegal retry count; refusing.")
        if not attempts:
            if (
                job["attempts_total"] != 0
                or job["retries_used"] != 0
                or job["current_fence"] != 0
                or job["current_attempt_id"] is not None
                or job["outcome_unresolved"] != 0
            ):
                raise BackupError(
                    f"job {job_id!r} has no attempts but non-zero counters/fence or "
                    f"a claimed unresolved outcome; refusing."
                )
            continue
        ordered = sorted(attempts, key=lambda a: a["attempt_no"])
        if job["attempts_total"] != len(ordered):
            raise BackupError(
                f"job {job_id!r} attempt count does not match its rows; refusing."
            )
        if job["retries_used"] != len(ordered) - 1:
            raise BackupError(
                f"job {job_id!r} retry count does not match its attempts; refusing."
            )
        if job["retries_used"] > MAX_SAFE_RETRIES:
            raise BackupError(
                f"job {job_id!r} has used more than the bounded {MAX_SAFE_RETRIES} "
                f"safe retries; refusing."
            )
        for index, attempt in enumerate(ordered, start=1):
            if attempt["attempt_no"] != index or attempt["fence"] != index:
                raise BackupError(
                    f"job {job_id!r} attempt fences/numbers are not the contiguous "
                    f"sequence; refusing."
                )
        if job["current_fence"] != ordered[-1]["fence"]:
            raise BackupError(
                f"job {job_id!r} current fence does not match its latest attempt; "
                f"refusing."
            )
        if job["current_attempt_id"] != ordered[-1]["attempt_id"]:
            raise BackupError(
                f"job {job_id!r} current attempt is not its latest attempt; refusing."
            )
        if ordered[0]["prior_outcome"] != "none":
            raise BackupError(
                f"job {job_id!r} first attempt has a non-'none' prior outcome; "
                f"refusing."
            )
        if ordered[0]["retry_authorized_by"] is not None:
            raise BackupError(
                f"job {job_id!r} first attempt records a retry authorization; "
                f"refusing."
            )
        for previous, attempt in zip(ordered, ordered[1:]):
            # A follow-up attempt is only ever a bounded *safe predispatch* retry:
            # the prior attempt must have failed definitively or been a local
            # interruption, must leave no unresolved outcome, and must not have
            # been externally admitted (C02 §8.1.1/§8.2; ``JobRepository``).
            if previous["state"] not in ("definitive_failure", "interrupted"):
                raise BackupError(
                    f"job {job_id!r} attempt {attempt['attempt_no']} follows a "
                    f"{previous['state']!r} prior attempt; refusing."
                )
            if previous["outcome_unresolved"] != 0:
                raise BackupError(
                    f"job {job_id!r} attempt {attempt['attempt_no']} follows an "
                    f"unresolved prior attempt; refusing."
                )
            if previous["admission_id"] is not None:
                raise BackupError(
                    f"job {job_id!r} attempt {attempt['attempt_no']} follows an "
                    f"externally admitted prior attempt; refusing."
                )
            if attempt["prior_outcome"] != previous["state"]:
                raise BackupError(
                    f"job {job_id!r} attempt {attempt['attempt_no']} prior outcome "
                    f"does not match the previous attempt's state; refusing."
                )
            if not attempt["retry_authorized_by"]:
                raise BackupError(
                    f"job {job_id!r} retry attempt {attempt['attempt_no']} has no "
                    f"retry authorization; refusing."
                )

    # ---- accepted results: fenced, current, digest-verified ---------------- #
    accepted_rows = conn.execute(
        "SELECT * FROM job_accepted_result ORDER BY job_id"
    ).fetchall()
    accepted_by_job: dict[str, sqlite3.Row] = {}
    for row in accepted_rows:
        if row["project_id"] != project_id:
            raise BackupError(
                "an accepted job result carries a cross-project key; refusing."
            )
        job_id = row["job_id"]
        if job_id not in jobs_by_id:
            raise BackupError(
                "an accepted job result references a job that does not exist; "
                "refusing."
            )
        if job_id in accepted_by_job:
            raise BackupError(
                "a job has more than one accepted result; refusing."
            )
        attempt = attempts_by_id.get(row["attempt_id"])
        if attempt is None or attempt["job_id"] != job_id:
            raise BackupError(
                f"an accepted result for job {job_id!r} references a missing or "
                f"foreign attempt; refusing."
            )
        job = jobs_by_id[job_id]
        result_kind = row["result_kind"]
        if not isinstance(result_kind, str) or not result_kind:
            raise BackupError("an accepted result has a malformed result kind; refusing.")
        if row["fence"] != attempt["fence"]:
            raise BackupError(
                f"an accepted result for job {job_id!r} does not match its attempt "
                f"fence; refusing."
            )
        if (
            job["current_attempt_id"] != attempt["attempt_id"]
            or job["current_fence"] != attempt["fence"]
        ):
            raise BackupError(
                f"an accepted result for job {job_id!r} is not the job's current "
                f"attempt/fence; refusing."
            )
        if job["accepted_fence"] != row["fence"]:
            raise BackupError(
                f"job {job_id!r} accepted fence does not match its accepted result; "
                f"refusing."
            )
        if attempt["state"] != "succeeded" or attempt["outcome_unresolved"] != 0:
            raise BackupError(
                f"an accepted result for job {job_id!r} is not backed by a succeeded, "
                f"resolved attempt; refusing."
            )
        # An accepted result may only survive on a completed job, or on a
        # paused/cancelled job whose scheduling was held when the result landed
        # (its unresolved bit is cleared, but the held state is preserved).
        if job["state"] not in ("completed", "paused", "cancelled"):
            raise BackupError(
                f"job {job_id!r} holds an accepted result in state {job['state']!r}; "
                f"refusing."
            )
        if job["outcome_unresolved"] != 0:
            raise BackupError(
                f"job {job_id!r} holds an accepted result but claims an unresolved "
                f"outcome; refusing."
            )
        result = _parse_canonical_json_text(
            row["result_json"], f"job {job_id!r} accepted result"
        )
        expected_digest = _fingerprint({"result_kind": result_kind, "result": result})
        if expected_digest != row["result_digest"]:
            raise BackupError(
                f"job {job_id!r} accepted result digest does not match its stored "
                f"result; refusing."
            )
        accepted_by_job[job_id] = row

    # ---- lifecycle correspondence (completed iff accepted result, flags) --- #
    current_attempt_by_job: dict[str, sqlite3.Row | None] = {}
    for job in job_rows:
        attempts = attempts_by_job.get(job["job_id"], [])
        current_attempt_by_job[job["job_id"]] = (
            max(attempts, key=lambda a: a["fence"]) if attempts else None
        )
    for job in job_rows:
        job_id = job["job_id"]
        accepted = accepted_by_job.get(job_id)
        current = current_attempt_by_job[job_id]
        # A job's unresolved bit mirrors its current attempt; a job with no
        # attempt cannot claim a remote unresolved outcome.
        if current is None:
            if job["outcome_unresolved"] != 0:
                raise BackupError(
                    f"job {job_id!r} claims a remote unresolved outcome with no "
                    f"attempt; refusing."
                )
        elif job["outcome_unresolved"] != current["outcome_unresolved"]:
            raise BackupError(
                f"job {job_id!r} unresolved flag does not match its current attempt; "
                f"refusing."
            )
        # ``completed`` is exactly a job whose current attempt has the one,
        # fenced accepted result.
        if job["state"] == "completed" and accepted is None:
            raise BackupError(
                f"job {job_id!r} is completed with no accepted result; refusing."
            )
        # ``accepted_fence`` is set iff an accepted result exists.
        accepted_fence = job["accepted_fence"]
        if accepted_fence is not None:
            if not isinstance(accepted_fence, int) or accepted_fence < 1:
                raise BackupError(
                    f"job {job_id!r} has an illegal accepted fence; refusing."
                )
            if accepted is None or accepted["fence"] != accepted_fence:
                raise BackupError(
                    f"job {job_id!r} records an accepted fence with no matching "
                    f"accepted result; refusing."
                )
        elif accepted is not None:
            raise BackupError(
                f"job {job_id!r} holds an accepted result with no accepted fence; "
                f"refusing."
            )

    # Every succeeded attempt must be exactly the one backed by its job's accepted
    # result (a forged "succeeded" attempt with no accepted result is refused).
    for attempt in attempt_rows:
        if attempt["state"] != "succeeded":
            continue
        accepted = accepted_by_job.get(attempt["job_id"])
        if (
            accepted is None
            or accepted["attempt_id"] != attempt["attempt_id"]
            or accepted["fence"] != attempt["fence"]
        ):
            raise BackupError(
                f"attempt {attempt['attempt_id']!r} is succeeded but is not backed by "
                f"its job's accepted result; refusing."
            )

    # ---- budget policy + reserves ------------------------------------------ #
    for row in conn.execute("SELECT * FROM job_budget_policy").fetchall():
        if row["project_id"] != project_id:
            raise BackupError("a budget policy carries a cross-project key; refusing.")
        _require_exact_decimal(row["project_cap_usd"], "budget project cap")
        _require_exact_decimal(row["stage_cap_usd"], "budget stage cap")

    reserved_attempts: set[str] = set()
    for row in conn.execute("SELECT * FROM job_budget_reserve").fetchall():
        if row["project_id"] != project_id:
            raise BackupError("a budget reserve carries a cross-project key; refusing.")
        job_id = row["job_id"]
        if job_id not in jobs_by_id:
            raise BackupError(
                "a budget reserve references a job that does not exist; refusing."
            )
        if row["state"] not in _RESERVE_STATES:
            raise BackupError("a budget reserve has an illegal state; refusing.")
        stage = row["stage"]
        if not isinstance(stage, str) or not stage or stage != stage.strip():
            raise BackupError("a budget reserve has a malformed stage; refusing.")
        _require_identifier(stage, "budget reserve stage")
        amount = _require_exact_decimal(row["amount_usd"], "budget reserve amount")
        attempt_id = row["attempt_id"]
        if attempt_id is None:
            # A job-scoped reserve is permitted by the schema but is not produced
            # by the dispatcher; keep it valid and counted without an attempt.
            continue
        attempt = attempts_by_id.get(attempt_id)
        if attempt is None:
            raise BackupError(
                "a budget reserve references an attempt that does not exist; "
                "refusing."
            )
        if attempt["job_id"] != job_id:
            raise BackupError(
                "a budget reserve's attempt belongs to a different job; refusing."
            )
        if attempt["dispatch_kind"] != "external_synthetic":
            raise BackupError(
                "a budget reserve is attached to a non-external attempt; refusing."
            )
        if attempt_id in reserved_attempts:
            raise BackupError(
                "an attempt has more than one budget reserve; refusing."
            )
        reserved_attempts.add(attempt_id)
        if attempt["reserve_usd"] is None or Decimal(attempt["reserve_usd"]) != amount:
            raise BackupError(
                "a budget reserve amount does not match its attempt's recorded "
                "reserve; refusing."
            )

    for attempt in attempt_rows:
        if attempt["reserve_usd"] is not None and attempt["attempt_id"] not in reserved_attempts:
            raise BackupError(
                f"attempt {attempt['attempt_id']!r} records a reserve with no matching "
                f"reserve row; refusing."
            )


# --------------------------------------------------------------------------- #
# Durable runtime mapping inner validation (schema 5)
# --------------------------------------------------------------------------- #
#: The five schema-5 tables that hold a project's durable runtime mappings. A
#: backup must contain only rows for its own project in **every** one of them.
_RUNTIME_TABLES = (
    "runtime_config_snapshot",
    "runtime_session_mapping",
    "runtime_bounded_task",
    "runtime_task_input",
    "runtime_proposal",
)


def _verify_runtime_relations(conn: sqlite3.Connection, project_id: str) -> None:
    """Read-only re-validation of the schema-5 durable runtime mapping rows.

    The shared :func:`~review_article_agent.storage.agent_runtime.store_integrity_problems`
    sweep re-derives every stored configuration/session/task/proposal from its
    exact canonical bytes and linked rows (recomputed config/result digests, the
    task fingerprint, projected columns, task inputs, proposal provenance), so a
    corrupt archive whose outer database hash was rewritten still fails closed.

    That helper filters its sweep by ``project_id``; this function therefore also
    scans **every** row of all five runtime tables and refuses any foreign-project
    key, so another project's mapping cannot be smuggled into a snapshot. It never
    requires a historical task's target/inputs to remain the artifact's *current*
    revision (a legitimate later head is valid history), and it never treats a
    proposal as an approval.
    """
    for table in _RUNTIME_TABLES:
        for row in conn.execute(f"SELECT project_id FROM {table}").fetchall():
            if row["project_id"] != project_id:
                raise BackupError(
                    f"the snapshot contains a {table} row for a foreign project; "
                    f"refusing."
                )

    # Local import keeps the storage layer decoupled at module-import time, just
    # as the schema-4 job validator imports ``runtime.jobs`` lazily.
    from .agent_runtime import store_integrity_problems

    problems = store_integrity_problems(conn, project_id)
    if problems:
        # ``store_integrity_problems`` returns only static, value-free strings.
        raise BackupError(
            "the snapshot's durable runtime mappings do not revalidate ("
            + "; ".join(problems)
            + "); refusing."
        )


# --------------------------------------------------------------------------- #
# Durable runtime dispatch inner validation (schema 6)
# --------------------------------------------------------------------------- #
#: The two schema-6 tables that hold a project's durable runtime dispatch ledger.
#: A backup must contain only rows for its own project in **both** of them.
_DISPATCH_TABLES = ("runtime_dispatch", "runtime_dispatch_state")


def _verify_dispatch_relations(conn: sqlite3.Connection, project_id: str) -> None:
    """Read-only re-validation of the schema-6 durable runtime dispatch ledger.

    The shared :func:`~review_article_agent.runtime.dispatch.runtime_dispatch_integrity_problems`
    sweep re-derives every dispatch from its stored bytes and its paired
    operational row (recomputed identity/fingerprint/digest bindings, projected
    columns, the exact-decimal reserve/settled cost, the state/result/cost
    consistency, and the linked runtime task), so a corrupt archive whose outer
    database hash was rewritten still fails inner verification.

    That helper filters its sweep by ``project_id``; this function therefore also
    scans **every** row of both dispatch tables and refuses any foreign-project
    key, so another project's dispatch cannot be smuggled into a snapshot. An
    ``unknown``-cost dispatch is validated as ``unknown`` (its amount is absent,
    never ``0``); the sweep imposes **no** retry/currentness requirement, so a
    legitimate historical ledger row stays valid. Any boundary failure of the
    runtime helper is converted into a curated :class:`BackupError`, so a raw
    dispatch exception is never surfaced.
    """
    for table in _DISPATCH_TABLES:
        for row in conn.execute(f"SELECT project_id FROM {table}").fetchall():
            if row["project_id"] != project_id:
                raise BackupError(
                    f"the snapshot contains a {table} row for a foreign project; "
                    f"refusing."
                )

    # Local imports keep the storage layer decoupled at module-import time, just
    # as the schema-4/5 validators import their repositories lazily.
    try:
        from ..runtime.dispatch import runtime_dispatch_integrity_problems

        problems = runtime_dispatch_integrity_problems(conn, project_id)
    except BackupError:
        raise
    except Exception as exc:  # noqa: BLE001 - a boundary failure is a refusal
        raise BackupError(
            "the snapshot's durable runtime dispatch ledger could not be "
            "revalidated; refusing."
        ) from exc
    if problems:
        # ``runtime_dispatch_integrity_problems`` returns static, value-free strings.
        raise BackupError(
            "the snapshot's durable runtime dispatch ledger does not revalidate ("
            + "; ".join(problems)
            + "); refusing."
        )


# --------------------------------------------------------------------------- #
# Durable human-approval inner validation (schema 7)
# --------------------------------------------------------------------------- #
#: The three schema-7 tables that hold a project's durable exact named-gate human
#: approvals (the out-of-band owner-auth evidence, the immutable approval, and its
#: exact snapshot entries). A backup must contain only rows for its own project in
#: **every** one of them.
_HUMAN_APPROVAL_TABLES = (
    "owner_auth_evidence",
    "human_approval",
    "human_approval_entry",
)


def _verify_human_approval_relations(
    conn: sqlite3.Connection, project_id: str
) -> None:
    """Read-only re-validation of the schema-7 durable human-approval rows.

    The shared :func:`~review_article_agent.storage.human_approvals.integrity_problems`
    sweep re-derives every approval from its exact canonical bytes and linked rows
    (recomputed snapshot digest/structure, every projected column, the entry pins,
    and the out-of-band owner-auth binding), so a corrupt archive whose outer
    database hash was rewritten still fails inner verification. It detects a
    foreign-project row, a corrupt approval/evidence row, an orphan (parentless)
    entry row, and an owner-auth evidence row without exactly one valid
    corresponding approval.

    The sweep imposes **no** current-head requirement, so a legitimate historical
    ``stale``/``superseded`` approval is structurally valid and is preserved. No
    capability/challenge is ever read or required: only the safe evidence
    projection is revalidated. Any boundary failure of the helper is converted
    into a curated :class:`BackupError`, so a raw approval/credential exception is
    never surfaced.
    """
    for table in _HUMAN_APPROVAL_TABLES:
        for row in conn.execute(f"SELECT project_id FROM {table}").fetchall():
            if row["project_id"] != project_id:
                raise BackupError(
                    f"the snapshot contains a {table} row for a foreign project; "
                    f"refusing."
                )

    # Local import keeps the storage layer decoupled at module-import time.
    try:
        from .human_approvals import integrity_problems

        problems = integrity_problems(conn, project_id)
    except BackupError:
        raise
    except Exception as exc:  # noqa: BLE001 - a boundary failure is a refusal
        raise BackupError(
            "the snapshot's durable human approvals could not be revalidated; "
            "refusing."
        ) from exc
    if problems:
        # ``integrity_problems`` returns static, value-free strings.
        raise BackupError(
            "the snapshot's durable human approvals do not revalidate ("
            + "; ".join(problems)
            + "); refusing."
        )


# --------------------------------------------------------------------------- #
# Durable pre-dispatch decision-intent inner validation (schema 8)
# --------------------------------------------------------------------------- #
def _verify_intent_relations(conn: sqlite3.Connection, project_id: str) -> None:
    """Read-only re-validation of the schema-8 durable decision-intent rows.

    The shared
    :func:`~review_article_agent.storage.decision_intents.decision_intent_integrity_problems`
    sweep re-derives every intent from its exact canonical bytes and linked rows
    (recomputed canonical JSON/content digest, every projected column, the exact
    per-question refs and versions, the parent dispatch/task links, and the
    recomputed request binding), so a corrupt archive whose outer database hash was
    rewritten still fails inner verification. It detects a foreign-project row, a
    dangling/orphan intent (missing parent dispatch or task), and a corrupt row.

    The sweep imposes **no** current-head requirement, so a legitimate intent whose
    target head has since advanced remains structurally valid and is preserved. It
    also never reads a credential, capability, or authority: only the recorded
    request/question metadata and opaque ids/digests are revalidated. Any boundary
    failure of the helper is converted into a curated :class:`BackupError`, so a raw
    intent/credential exception is never surfaced.
    """
    # The helper already scans every row in the table and reports a foreign-project
    # row itself, so no separate table foreign-row scan is duplicated here.
    try:
        from .decision_intents import decision_intent_integrity_problems

        problems = decision_intent_integrity_problems(conn, project_id)
    except BackupError:
        raise
    except Exception as exc:  # noqa: BLE001 - a boundary failure is a refusal
        raise BackupError(
            "the snapshot's durable decision request intents could not be "
            "revalidated; refusing."
        ) from exc
    if problems:
        # ``decision_intent_integrity_problems`` returns static, value-free strings.
        raise BackupError(
            "the snapshot's durable decision request intents do not revalidate ("
            + "; ".join(problems)
            + "); refusing."
        )


# --------------------------------------------------------------------------- #
# Durable full per-question decision-result observation inner validation (schema 9)
# --------------------------------------------------------------------------- #
def _verify_decision_result_relations(
    conn: sqlite3.Connection, project_id: str
) -> None:
    """Read-only re-validation of the schema-9 durable decision-result rows.

    The shared
    :func:`~review_article_agent.storage.decision_results.decision_result_integrity_problems`
    sweep independently re-validates every recorded observation: the whole-call
    set row (its exact canonical bytes/content digest, projected columns, parent
    dispatch/task/intent pins, the single dispatch cost, and the provider
    envelope) **and** each per-question row on its own (the question rebuilt from
    the recorded refs, the descriptor digest, options/levels/kind, the version set
    and consumed slots, the request pin, the per-record cost and provider
    envelope, and the exact complete ordered question set measured against the
    recorded intent's own questions — never the set's own array alone). A foreign
    row, a dangling/orphan or partial set, and a corrupt set/record row are all
    reported, so a corrupt archive whose outer database hash was rewritten still
    fails inner verification.

    The sweep imposes **no** current-head requirement (a legitimate observation
    whose target head has since advanced is preserved), performs **no** accept,
    execution, or retry, and never reads a credential, capability, or authority:
    only the recorded provider/cost/ref/version metadata and opaque ids/digests
    are revalidated. These rows are an inert **observation**, not an accepted
    local result; an ``unknown`` cost stays ``unknown`` with no amount (never a
    silent ``0``). Any boundary failure of the helper is converted into a curated
    :class:`BackupError`, so a raw result/credential exception is never surfaced.
    """
    # The helper already scans every row in both result tables and reports a
    # foreign-project row (and an orphan/partial set) itself, so no separate
    # table foreign-row scan is duplicated here.
    try:
        from .decision_results import decision_result_integrity_problems

        problems = decision_result_integrity_problems(conn, project_id)
    except BackupError:
        raise
    except Exception as exc:  # noqa: BLE001 - a boundary failure is a refusal
        raise BackupError(
            "the snapshot's durable decision result observations could not be "
            "revalidated; refusing."
        ) from exc
    if problems:
        # ``decision_result_integrity_problems`` returns static, value-free strings.
        raise BackupError(
            "the snapshot's durable decision result observations do not revalidate ("
            + "; ".join(problems)
            + "); refusing."
        )


# --------------------------------------------------------------------------- #
# Durable pre-transport session-turn intent inner validation (schema 10)
# --------------------------------------------------------------------------- #
def _verify_session_intent_relations(
    conn: sqlite3.Connection, project_id: str
) -> None:
    """Read-only re-validation of the schema-10 durable session-turn intents.

    The shared
    :func:`~review_article_agent.storage.session_intents.session_intent_integrity_problems`
    sweep re-derives every intent from its exact canonical plan/message bytes and
    their digests, checks every projected column against those bytes, and re-binds
    the whole specification to the persisted dispatch/task/session-mapping/
    configuration parents. It reports a foreign-project row, a dangling/orphan
    intent (missing dispatch, task, or session mapping), and a corrupt row, so a
    corrupt archive whose outer database hash was rewritten still fails inner
    verification.

    The sweep is read-only and imposes **no** live-service or live-filesystem
    requirement and **no** current-head requirement: a legitimate intent whose
    target head has since advanced remains structurally valid and is preserved. It
    records correlation only — no credential, capability, admission, approval, or
    execution authority is read or granted. Any boundary failure of the helper is
    converted into a curated :class:`BackupError`, so a raw intent/credential
    exception is never surfaced.
    """
    # The helper already scans every row in the table and reports a foreign-project
    # row itself, so no separate table foreign-row scan is duplicated here.
    try:
        from .session_intents import session_intent_integrity_problems

        problems = session_intent_integrity_problems(conn, project_id)
    except BackupError:
        raise
    except Exception as exc:  # noqa: BLE001 - a boundary failure is a refusal
        raise BackupError(
            "the snapshot's durable session-turn intents could not be "
            "revalidated; refusing."
        ) from exc
    if problems:
        # ``session_intent_integrity_problems`` returns static, value-free strings.
        raise BackupError(
            "the snapshot's durable session-turn intents do not revalidate ("
            + "; ".join(problems)
            + "); refusing."
        )


#: Ordered ``(minimum_store_schema_version, validator)`` hook. Each validator is
#: applied for a snapshot whose recorded schema is **at or above** its minimum, so
#: a later schema checkpoint can append a validator here without touching the
#: historical schema-3/4/5 compatibility paths. Validators are read-only and take
#: ``(connection, project_id)``.
_SCHEMA_VALIDATORS = (
    (_JOB_SCHEMA_VERSION, _verify_job_relations),
    (_RUNTIME_SCHEMA_VERSION, _verify_runtime_relations),
    (_DISPATCH_SCHEMA_VERSION, _verify_dispatch_relations),
    (_HUMAN_APPROVAL_SCHEMA_VERSION, _verify_human_approval_relations),
    (_INTENT_SCHEMA_VERSION, _verify_intent_relations),
    (_DECISION_RESULT_SCHEMA_VERSION, _verify_decision_result_relations),
    (_SESSION_INTENT_SCHEMA_VERSION, _verify_session_intent_relations),
)


def _verify_schema_specific(
    conn: sqlite3.Connection, project_id: str, version: int
) -> None:
    """Run every inner validator whose minimum schema version ``version`` meets."""
    for minimum_version, validator in _SCHEMA_VALIDATORS:
        if version >= minimum_version:
            validator(conn, project_id)


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
        _assert_known_schema(conn, version)

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
        _verify_schema_specific(conn, project_id, version)

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
        "database_byte_size": actual_size,
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
        # Close the TOCTOU window: the backup was verified earlier, but the
        # source could change before/while it is copied. Refuse a substitution by
        # hashing the staged copy against the verified snapshot digest.
        copied_sha, copied_size = _sha256_file(dst_db)
        if (
            copied_sha != report["database_sha256"]
            or copied_size != report["database_byte_size"]
        ):
            raise BackupError(
                "the staged restore copy does not match the verified snapshot "
                "(the source changed between verification and copy); refusing and "
                "leaving no restored project."
            )
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
    "MIN_SUPPORTED_STORE_SCHEMA_VERSION",
    "SNAPSHOT_FILENAME",
    "SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS",
    "SUPPORTED_STORE_SCHEMA_VERSION",
    "create_backup",
    "restore_backup",
    "verify_backup",
]
