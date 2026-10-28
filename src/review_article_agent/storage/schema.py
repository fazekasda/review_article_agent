"""Versioned, transactional SQLite schema migrations.

Migration state is tracked with SQLite's built-in ``PRAGMA user_version`` — the
simplest durable version marker that needs no bootstrap table. Each step runs
inside its own ``BEGIN IMMEDIATE`` transaction and advances ``user_version`` in
the same transaction, so a failed migration rolls back completely and leaves
the previous version intact (``ARCH-MIGRATE``; step doc line 21).

A database written by a **newer** schema version is refused with
:class:`~review_article_agent.domain.errors.UnsupportedSchemaVersion` rather
than read or downgraded.

Both the per-project store and the application registry keep independent,
independently-versioned schemas. Later workers (artifacts, approvals, backup)
add new numbered migrations without rewriting an existing one, so prior data —
including future artifact rows — is preserved across upgrades.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Callable, Mapping
from urllib.parse import quote

from ..domain.errors import MigrationError, StoreError, UnsupportedSchemaVersion


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


#: SQL statements that must hold for every connection.
COMMON_PRAGMAS = (
    "PRAGMA foreign_keys = ON",
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = FULL",
)

Migration = Callable[[sqlite3.Connection], None]

# --------------------------------------------------------------------------- #
# Per-project store schema
# --------------------------------------------------------------------------- #
STORE_SCHEMA_VERSION = 8

#: Registry maps project IDs to names and locations, records the single
#: *selected* project, and records durable **execution activation** (which
#: project, if any, is authorized to execute). It is *not* a cache of project
#: data.
REGISTRY_SCHEMA_VERSION = 2


def _store_v1(conn: sqlite3.Connection) -> None:
    """Create the store's identity/metadata tables.

    Only project identity and application metadata live here in Step 02. Later
    steps add artifact/approval tables via new migrations; the ``meta`` table
    stays stable so migrations can record their own bookkeeping.
    """
    conn.execute(
        """
        CREATE TABLE meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE project (
            project_id    TEXT PRIMARY KEY,
            name          TEXT,
            created_at_utc TEXT NOT NULL,
            app_version   TEXT NOT NULL
        )
        """
    )


#: Statuses an :class:`ArtifactRecord` identity row may hold (C01 §5.1). Lifecycle
#: only; it never alters revision immutability.
ARTIFACT_STATUSES = ("active", "superseded", "withdrawn")


def _store_v2(conn: sqlite3.Connection) -> None:
    """Add the immutable artifact/revision/rights/dependency/import tables.

    This migration only *adds* tables and triggers; it never rewrites v1 rows, so
    a store created at version 1 keeps its ``project``/``meta`` identity metadata
    (``ARCH-ASSETS``; step doc line 21).

    Immutability is enforced **at the database**, not only in the Python API: a
    revision's content, hash, provenance, dependency set, and pinned rights key
    can never be rewritten by an ``UPDATE``, and a written row can never be
    ``DELETE``d. A change is therefore always a **new** revision row. Composite
    foreign keys keep every dependency pinned to existing same-project revisions
    and every revision pinned to an existing rights record, so a failed
    transaction can leave no dangling reference (all rows are written together).
    """
    conn.execute(
        """
        CREATE TABLE artifact (
            project_id     TEXT NOT NULL,
            artifact_id    TEXT NOT NULL,
            kind           TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            created_by     TEXT NOT NULL,
            label          TEXT,
            status         TEXT NOT NULL DEFAULT 'active',
            PRIMARY KEY (project_id, artifact_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE rights_record (
            project_id          TEXT NOT NULL,
            rights_id           TEXT NOT NULL,
            record_revision     INTEGER NOT NULL,
            subject_kind        TEXT NOT NULL,
            subject_project_id  TEXT NOT NULL,
            subject_artifact_id TEXT NOT NULL,
            subject_revision_id TEXT,
            canonical           BLOB NOT NULL,
            content_sha256      TEXT NOT NULL,
            recorded_at_utc     TEXT NOT NULL,
            PRIMARY KEY (project_id, rights_id, record_revision)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE revision (
            project_id                   TEXT NOT NULL,
            artifact_id                  TEXT NOT NULL,
            revision_id                  TEXT NOT NULL,
            parent_revision_id           TEXT,
            content                      BLOB NOT NULL,
            content_sha256               TEXT NOT NULL,
            byte_size                    INTEGER NOT NULL,
            media_type                   TEXT,
            origin_locator               TEXT NOT NULL,
            acquired_at_utc              TEXT NOT NULL,
            acquired_by                  TEXT NOT NULL,
            provenance_method            TEXT NOT NULL,
            provenance_actor             TEXT NOT NULL,
            provenance_performed_at_utc  TEXT NOT NULL,
            rights_project_id            TEXT NOT NULL,
            rights_id                    TEXT NOT NULL,
            rights_record_revision       INTEGER NOT NULL,
            immutable                    INTEGER NOT NULL DEFAULT 1
                                         CHECK (immutable = 1),
            PRIMARY KEY (project_id, artifact_id, revision_id),
            FOREIGN KEY (project_id, artifact_id)
                REFERENCES artifact (project_id, artifact_id),
            FOREIGN KEY (project_id, artifact_id, parent_revision_id)
                REFERENCES revision (project_id, artifact_id, revision_id),
            FOREIGN KEY (rights_project_id, rights_id, rights_record_revision)
                REFERENCES rights_record (project_id, rights_id, record_revision)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE dependency (
            project_id        TEXT NOT NULL,
            from_artifact_id  TEXT NOT NULL,
            from_revision_id  TEXT NOT NULL,
            dependency_id     TEXT NOT NULL,
            role              TEXT NOT NULL,
            to_project_id     TEXT NOT NULL,
            to_artifact_id    TEXT NOT NULL,
            to_revision_id    TEXT NOT NULL,
            created_at_utc    TEXT NOT NULL,
            PRIMARY KEY (project_id, from_artifact_id, from_revision_id, dependency_id),
            FOREIGN KEY (project_id, from_artifact_id, from_revision_id)
                REFERENCES revision (project_id, artifact_id, revision_id),
            FOREIGN KEY (to_project_id, to_artifact_id, to_revision_id)
                REFERENCES revision (project_id, artifact_id, revision_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE import_request (
            project_id     TEXT NOT NULL,
            request_id     TEXT NOT NULL,
            fingerprint    TEXT NOT NULL,
            artifact_id    TEXT NOT NULL,
            revision_id    TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            PRIMARY KEY (project_id, request_id)
        )
        """
    )

    # Identity columns of a logical artifact are fixed at creation; only the
    # lifecycle ``status``/``label`` may change, and the row is never deleted.
    conn.execute(
        """
        CREATE TRIGGER artifact_identity_immutable
        BEFORE UPDATE ON artifact
        WHEN NEW.project_id <> OLD.project_id
          OR NEW.artifact_id <> OLD.artifact_id
          OR NEW.kind <> OLD.kind
          OR NEW.created_at_utc <> OLD.created_at_utc
          OR NEW.created_by <> OLD.created_by
        BEGIN
            SELECT RAISE(ABORT, 'artifact identity rows are immutable');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER artifact_no_delete
        BEFORE DELETE ON artifact
        BEGIN
            SELECT RAISE(ABORT, 'artifact identity rows are immutable');
        END
        """
    )
    for table in ("revision", "rights_record", "dependency", "import_request"):
        conn.execute(
            f"""
            CREATE TRIGGER {table}_no_update
            BEFORE UPDATE ON {table}
            BEGIN
                SELECT RAISE(ABORT, '{table} rows are immutable');
            END
            """
        )
        conn.execute(
            f"""
            CREATE TRIGGER {table}_no_delete
            BEFORE DELETE ON {table}
            BEGIN
                SELECT RAISE(ABORT, '{table} rows are immutable');
            END
            """
        )


def _store_v3(conn: sqlite3.Connection) -> None:
    """Add the immutable *local storage-fixture approval* tables.

    These tables back ``storage/approvals.py``: a deliberately local, synthetic
    approval/check layer that records an exact target revision plus its full
    transitive dependency closure. It is **not** a scientific approval and can
    never authorize execution or export (``authorizes_execution`` is pinned to
    ``0`` and ``scope`` to ``'storage_fixture'`` by ``CHECK`` constraints).

    Only tables and triggers are added; migration 1/2 rows (project identity,
    artifacts, revisions, rights, dependencies, import requests) are never
    rewritten, so their exact bytes/refs are preserved across the upgrade
    (``ARCH-ASSETS``; step doc line 21). Immutability is enforced at the
    database: an approval row and its snapshot entries can never be ``UPDATE``d
    or ``DELETE``d, so a change is always a **new** approval row. Composite
    foreign keys pin every approval and every snapshot entry to an existing
    same-project revision, so a failed transaction can leave no dangling
    approval.
    """
    conn.execute(
        """
        CREATE TABLE fixture_approval (
            project_id             TEXT NOT NULL,
            approval_id            TEXT NOT NULL,
            target_artifact_id     TEXT NOT NULL,
            target_revision_id     TEXT NOT NULL,
            gate_kind              TEXT NOT NULL,
            gate_id                TEXT NOT NULL,
            decision               TEXT NOT NULL,
            scope                  TEXT NOT NULL DEFAULT 'storage_fixture'
                                   CHECK (scope = 'storage_fixture'),
            authorizes_execution   INTEGER NOT NULL DEFAULT 0
                                   CHECK (authorizes_execution = 0),
            actor_label            TEXT NOT NULL,
            actor_authenticated    INTEGER NOT NULL DEFAULT 0
                                   CHECK (actor_authenticated = 0),
            rationale              TEXT NOT NULL,
            granted_at_utc         TEXT NOT NULL,
            snapshot_id            TEXT NOT NULL,
            snapshot_digest        TEXT NOT NULL,
            storage_schema_version INTEGER NOT NULL,
            context_kind           TEXT NOT NULL,
            context_note           TEXT,
            created_at_utc         TEXT NOT NULL,
            PRIMARY KEY (project_id, approval_id),
            FOREIGN KEY (project_id, target_artifact_id, target_revision_id)
                REFERENCES revision (project_id, artifact_id, revision_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE fixture_approval_entry (
            project_id             TEXT NOT NULL,
            approval_id            TEXT NOT NULL,
            entry_index            INTEGER NOT NULL,
            artifact_id            TEXT NOT NULL,
            revision_id            TEXT NOT NULL,
            content_sha256         TEXT NOT NULL,
            role                   TEXT NOT NULL,
            rights_id              TEXT NOT NULL,
            rights_record_revision INTEGER NOT NULL,
            byte_size              INTEGER NOT NULL,
            PRIMARY KEY (project_id, approval_id, entry_index),
            FOREIGN KEY (project_id, approval_id)
                REFERENCES fixture_approval (project_id, approval_id),
            FOREIGN KEY (project_id, artifact_id, revision_id)
                REFERENCES revision (project_id, artifact_id, revision_id)
        )
        """
    )
    for table in ("fixture_approval", "fixture_approval_entry"):
        conn.execute(
            f"""
            CREATE TRIGGER {table}_no_update
            BEFORE UPDATE ON {table}
            BEGIN
                SELECT RAISE(ABORT, '{table} rows are immutable');
            END
            """
        )
        conn.execute(
            f"""
            CREATE TRIGGER {table}_no_delete
            BEFORE DELETE ON {table}
            BEGIN
                SELECT RAISE(ABORT, '{table} rows are immutable');
            END
            """
        )


def _store_v4(conn: sqlite3.Connection) -> None:
    """Add the durable synthetic job/attempt/budget tables (C02 §5.6/§6.3/§8).

    These tables back the Step 03 durable worker's ``runtime/jobs.py``:
    per-project **synthetic** jobs and dispatch attempts, an at-most-once
    accepted-result row keyed by the attempt's *fence*, and the exact-decimal
    budget reserves recorded **before** a synthetic external dispatch is
    admitted (``reserve before dispatch``, C02 §5.6/§6.3).

    Only new tables and triggers are added; migration 1/2/3 rows (project
    identity, artifacts, revisions, rights, dependencies, import requests, and
    fixture approvals) are never rewritten, so their exact bytes/refs are
    preserved across the upgrade (``ARCH-ASSETS``; step doc line 21).

    Design notes enforced at the database:

    * **Job identity is explicit and idempotent.** ``job_id`` is derived from
      ``(project_id, request_id)`` and ``UNIQUE(project_id, request_id)`` makes
      a repeated submit with the same request key return the same single row.
    * **Fencing.** ``job.current_fence`` is the highest fence issued for a job;
      ``job_attempt`` rows are unique per ``(job, fence)`` and per
      ``(job, attempt_no)``. Accepting a result only ever writes through the
      *current* fence.
    * **At-most-once accepted result.** ``job_accepted_result`` is unique per
      ``(job, fence)`` **and** per logical job, and is immutable (never
      ``UPDATE``d/``DELETE``d) so a stale attempt cannot overwrite a newer
      authorized result (C02 §8.2/§8.3).
    * **Reserve before dispatch.** ``job_budget_reserve`` holds exact-decimal
      reserve amounts; a reserve is retained (never zeroed) until an explicit
      settlement replaces it, so an ``unknown`` cost keeps its reserve
      (C02 §5.6/§6.3).
    """
    conn.execute(
        """
        CREATE TABLE job (
            project_id         TEXT NOT NULL,
            job_id             TEXT NOT NULL,
            request_id         TEXT NOT NULL,
            fingerprint        TEXT NOT NULL,
            task_kind          TEXT NOT NULL,
            parameters_json    TEXT NOT NULL,
            state              TEXT NOT NULL CHECK (state IN (
                'queued', 'admitted', 'running', 'waiting_human',
                'waiting_external', 'paused', 'cancelled', 'completed',
                'failed', 'interrupted', 'reconciliation_required')),
            outcome_unresolved INTEGER NOT NULL DEFAULT 0
                               CHECK (outcome_unresolved IN (0, 1)),
            resume_state       TEXT,
            current_attempt_id TEXT,
            current_fence      INTEGER NOT NULL DEFAULT 0,
            attempts_total     INTEGER NOT NULL DEFAULT 0,
            retries_used       INTEGER NOT NULL DEFAULT 0,
            accepted_fence     INTEGER,
            created_at_utc     TEXT NOT NULL,
            updated_at_utc     TEXT NOT NULL,
            PRIMARY KEY (project_id, job_id),
            UNIQUE (project_id, request_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE job_attempt (
            project_id         TEXT NOT NULL,
            attempt_id         TEXT NOT NULL,
            job_id             TEXT NOT NULL,
            fence              INTEGER NOT NULL,
            attempt_no         INTEGER NOT NULL,
            dispatch_kind      TEXT NOT NULL CHECK (dispatch_kind IN (
                'local', 'external_synthetic')),
            state              TEXT NOT NULL CHECK (state IN (
                'queued', 'admitted', 'running', 'succeeded',
                'definitive_failure', 'ambiguous', 'interrupted')),
            prior_outcome      TEXT NOT NULL CHECK (prior_outcome IN (
                'none', 'definitive_failure', 'ambiguous', 'succeeded',
                'interrupted')),
            retry_authorized_by TEXT,
            outcome_unresolved INTEGER NOT NULL DEFAULT 0
                               CHECK (outcome_unresolved IN (0, 1)),
            admission_id       TEXT,
            external_ref       TEXT,
            checkpoint_json    TEXT,
            reserve_usd        TEXT,
            cost_state         TEXT NOT NULL DEFAULT 'not_incurred',
            cost_amount_usd    TEXT,
            started_at_utc     TEXT NOT NULL,
            ended_at_utc       TEXT,
            PRIMARY KEY (project_id, attempt_id),
            UNIQUE (project_id, job_id, fence),
            UNIQUE (project_id, job_id, attempt_no),
            FOREIGN KEY (project_id, job_id)
                REFERENCES job (project_id, job_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE job_accepted_result (
            project_id     TEXT NOT NULL,
            job_id         TEXT NOT NULL,
            attempt_id     TEXT NOT NULL,
            fence          INTEGER NOT NULL,
            result_kind    TEXT NOT NULL,
            result_json    TEXT NOT NULL,
            result_digest  TEXT NOT NULL,
            accepted_at_utc TEXT NOT NULL,
            PRIMARY KEY (project_id, job_id, fence),
            UNIQUE (project_id, job_id),
            FOREIGN KEY (project_id, job_id)
                REFERENCES job (project_id, job_id),
            FOREIGN KEY (project_id, attempt_id)
                REFERENCES job_attempt (project_id, attempt_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE job_budget_policy (
            project_id       TEXT PRIMARY KEY,
            project_cap_usd  TEXT NOT NULL,
            stage_cap_usd    TEXT NOT NULL,
            updated_at_utc   TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE job_budget_reserve (
            project_id     TEXT NOT NULL,
            reserve_id     TEXT NOT NULL,
            job_id         TEXT NOT NULL,
            attempt_id     TEXT,
            stage          TEXT NOT NULL,
            amount_usd     TEXT NOT NULL,
            state          TEXT NOT NULL CHECK (state IN (
                'held', 'retained', 'settled')),
            created_at_utc TEXT NOT NULL,
            PRIMARY KEY (project_id, reserve_id),
            FOREIGN KEY (project_id, job_id)
                REFERENCES job (project_id, job_id)
        )
        """
    )

    # The accepted result is the *fenced* exactly-once local write: once written
    # it can never be rewritten or removed, so a later (even newer-fenced)
    # attempt cannot overwrite an already-authorized result (C02 §8.2/§8.3).
    conn.execute(
        """
        CREATE TRIGGER job_accepted_result_no_update
        BEFORE UPDATE ON job_accepted_result
        BEGIN
            SELECT RAISE(ABORT, 'job_accepted_result rows are immutable');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER job_accepted_result_no_delete
        BEFORE DELETE ON job_accepted_result
        BEGIN
            SELECT RAISE(ABORT, 'job_accepted_result rows are immutable');
        END
        """
    )

    # A budget reserve's amount is the upper bound recorded before dispatch and
    # must never shrink (C02 §5.6/§6.3); settlement only changes its state.
    conn.execute(
        """
        CREATE TRIGGER job_budget_reserve_amount_immutable
        BEFORE UPDATE ON job_budget_reserve
        WHEN NEW.amount_usd <> OLD.amount_usd
        BEGIN
            SELECT RAISE(ABORT, 'job_budget_reserve amount is immutable');
        END
        """
    )


def _store_v5(conn: sqlite3.Connection) -> None:
    """Add the durable immutable **runtime task/session/proposal mappings**.

    These tables back :mod:`review_article_agent.storage.agent_runtime`: per-project
    configuration snapshots, app-owned OpenCode service/project/session mappings,
    bounded-task records, and non-authorizing proposal records (C02 §1/§6.1/§8;
    step 04 deliverables 1-2 and acceptance 3/4/7).

    Only new tables and triggers are added; migrations 1-4 (project identity,
    artifacts, revisions, rights, dependencies, import requests, fixture
    approvals, and the synthetic job/attempt/budget tables) are never rewritten,
    so their exact bytes/refs are preserved across the upgrade (``ARCH-ASSETS``;
    step doc line 21). Immutability is enforced at the database: every runtime
    row is immutable (never ``UPDATE``d/``DELETE``d), so a change is always a
    **new** row. Composite foreign keys pin every task/proposal reference to an
    existing same-project revision and every proposal to its persisted task, so a
    failed transaction can leave no dangling reference.

    The schema deliberately stores **no** endpoint, password, provider key, or
    challenge value: those never reach a durable row (spec lines 62-63;
    ``docs/storage.md``). ``config_digest``/``fingerprint``/``stored_digest``
    values are recomputed from the exact canonical bytes the application wrote.

    ``runtime_proposal`` is keyed by an immutable **task-scoped** ``record_id``
    (unique per ``(project_id, task_id, proposal_id)``), so the machine
    ``proposal_id`` alone never collapses two identical task rows into one
    proposal: the logical task association is preserved.
    """
    conn.execute(
        """
        CREATE TABLE runtime_config_snapshot (
            project_id     TEXT NOT NULL,
            snapshot_id    TEXT NOT NULL,
            config_version INTEGER NOT NULL,
            config_digest  TEXT NOT NULL,
            canonical      BLOB NOT NULL,
            created_at_utc TEXT NOT NULL,
            PRIMARY KEY (project_id, snapshot_id),
            UNIQUE (project_id, config_digest),
            UNIQUE (project_id, config_version)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE runtime_session_mapping (
            project_id          TEXT NOT NULL,
            mapping_id          TEXT NOT NULL,
            service_instance_id TEXT NOT NULL,
            opencode_project_id TEXT,
            clean_location      TEXT NOT NULL,
            session_id          TEXT NOT NULL,
            role                TEXT NOT NULL,
            provider_id         TEXT NOT NULL,
            model_id            TEXT NOT NULL,
            variant             TEXT,
            config_digest       TEXT NOT NULL,
            config_snapshot_id  TEXT NOT NULL,
            created_at_utc      TEXT NOT NULL,
            PRIMARY KEY (project_id, mapping_id),
            UNIQUE (project_id, session_id),
            FOREIGN KEY (project_id, config_snapshot_id)
                REFERENCES runtime_config_snapshot (project_id, snapshot_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE runtime_bounded_task (
            project_id         TEXT NOT NULL,
            task_id            TEXT NOT NULL,
            request_id         TEXT NOT NULL,
            fingerprint        TEXT NOT NULL,
            role               TEXT NOT NULL,
            config_digest      TEXT NOT NULL,
            config_snapshot_id TEXT NOT NULL,
            target_artifact_id TEXT NOT NULL,
            target_revision_id TEXT NOT NULL,
            prompt_version     TEXT NOT NULL,
            size_limit         INTEGER NOT NULL,
            step_limit         INTEGER NOT NULL,
            canonical          BLOB NOT NULL,
            created_at_utc     TEXT NOT NULL,
            PRIMARY KEY (project_id, task_id),
            UNIQUE (project_id, request_id),
            FOREIGN KEY (project_id, config_snapshot_id)
                REFERENCES runtime_config_snapshot (project_id, snapshot_id),
            FOREIGN KEY (project_id, target_artifact_id, target_revision_id)
                REFERENCES revision (project_id, artifact_id, revision_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE runtime_task_input (
            project_id  TEXT NOT NULL,
            task_id     TEXT NOT NULL,
            ordinal     INTEGER NOT NULL,
            artifact_id TEXT NOT NULL,
            revision_id TEXT NOT NULL,
            digest      TEXT NOT NULL,
            PRIMARY KEY (project_id, task_id, ordinal),
            FOREIGN KEY (project_id, task_id)
                REFERENCES runtime_bounded_task (project_id, task_id),
            FOREIGN KEY (project_id, artifact_id, revision_id)
                REFERENCES revision (project_id, artifact_id, revision_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE runtime_proposal (
            project_id         TEXT NOT NULL,
            record_id          TEXT NOT NULL,
            proposal_id        TEXT NOT NULL,
            task_id            TEXT NOT NULL,
            role               TEXT NOT NULL,
            provider_id        TEXT NOT NULL,
            model_id           TEXT NOT NULL,
            variant            TEXT,
            target_artifact_id TEXT NOT NULL,
            target_revision_id TEXT NOT NULL,
            task_digest        TEXT NOT NULL,
            input_digest       TEXT NOT NULL,
            payload_digest     TEXT NOT NULL,
            stored_digest      TEXT NOT NULL,
            canonical          BLOB NOT NULL,
            created_at_utc     TEXT NOT NULL,
            PRIMARY KEY (project_id, record_id),
            UNIQUE (project_id, task_id, proposal_id),
            FOREIGN KEY (project_id, task_id)
                REFERENCES runtime_bounded_task (project_id, task_id)
        )
        """
    )

    for table in (
        "runtime_config_snapshot",
        "runtime_session_mapping",
        "runtime_bounded_task",
        "runtime_task_input",
        "runtime_proposal",
    ):
        conn.execute(
            f"""
            CREATE TRIGGER {table}_no_update
            BEFORE UPDATE ON {table}
            BEGIN
                SELECT RAISE(ABORT, '{table} rows are immutable');
            END
            """
        )
        conn.execute(
            f"""
            CREATE TRIGGER {table}_no_delete
            BEFORE DELETE ON {table}
            BEGIN
                SELECT RAISE(ABORT, '{table} rows are immutable');
            END
            """
        )


def _store_v6(conn: sqlite3.Connection) -> None:
    """Add the durable **runtime dispatch + route-separated cost ledger** (C02 §5.6/§6.3/§8).

    These tables back :mod:`review_article_agent.runtime.dispatch`: one immutable
    *logical-call identity* row per dispatch (pinning the existing runtime task and
    its fingerprint, the request/logical-call key, the ``opencode_session``/
    ``direct_call`` route and its implied accounting bucket, the stage, the
    canonical input/version digests, the exact-decimal pre-dispatch reserve, and
    the immutable bound-evidence digest) plus a **mutable operational row** holding
    the dispatch's explicit safe state (``reserved``/``submitted``/``admitted``/
    ``completed``/``failed``/``unknown``), its fence, the retained admission, the
    single fenced local result, and the settled cost.

    Only new tables and triggers are added; migrations 1-5 (project identity,
    artifacts, revisions, rights, dependencies, import requests, fixture approvals,
    the synthetic job/attempt/budget tables, and the runtime task/session/proposal
    mappings) are never rewritten, so their exact bytes/refs are preserved across
    the upgrade (``ARCH-ASSETS``; step doc line 21).

    Design notes enforced at the database:

    * **Append-only identity + mutable operational row.** ``runtime_dispatch`` is
      immutable (never ``UPDATE``d/``DELETE``d), so a request's pinned task,
      fingerprint, route/bucket, stage, digests, reserve, and bound-evidence digest
      can never be rewritten; only the paired ``runtime_dispatch_state`` row
      changes, so a change is always a *state transition*, never a re-identity.
    * **Logical-call identity is explicit and idempotent.** ``dispatch_id`` is
      derived from ``(project_id, request_id)`` and ``UNIQUE(project_id,
      request_id)`` makes a repeated reserve with the same key return the same one
      row, so a reserve can never double-count. ``UNIQUE(project_id, task_id)``
      additionally makes "at most one logical dispatch per task" (initial scope,
      no retries) a database invariant rather than only a repository check.
    * **Route/bucket separation.** ``CHECK ((route = 'direct_call') =
      (accounting_bucket = 'direct_call'))`` makes a ``direct_call`` cost
      un-aggregatable into OpenCode session statistics (C02 §6.3).
    * **No durable admission on the direct route.** A trigger aborts any
      ``runtime_dispatch_state`` row that carries an ``admission_id`` **or** an
      ``admission_ref`` while its dispatch's route is ``direct_call`` (C02
      §8.1/§8.1.1) — the direct Jev route has no proven durable/idempotent
      admission.
    * **Submitted means something was spent-or-maybe-spent.** A ``CHECK`` forbids
      a ``submitted``/``admitted``/``unknown`` row from claiming ``cost_state =
      'not_incurred'`` (C02 §6.3/§8.3), and the initial scope has exactly one
      fence (``current_fence = 1``); re-fencing is not part of this step.
    * **Conservative cost accounting.** The exact-decimal ``reserve_usd`` is
      recorded before dispatch and counted until settlement; an ``unknown``/
      ``not_incurred`` cost records **no** amount (never ``0``) and retains its
      reserve (C02 §5.6/§6.3).

    The schema deliberately stores **no** endpoint, credential, provider key, or
    response content: only opaque ids, exact-decimal amounts, and canonical
    SHA-256 digests (spec lines 62-63; ``docs/storage.md``).
    """
    conn.execute(
        """
        CREATE TABLE runtime_dispatch (
            project_id            TEXT NOT NULL,
            dispatch_id           TEXT NOT NULL,
            request_id            TEXT NOT NULL,
            task_id               TEXT NOT NULL,
            task_fingerprint      TEXT NOT NULL,
            route                 TEXT NOT NULL CHECK (route IN (
                'opencode_session', 'direct_call')),
            accounting_bucket     TEXT NOT NULL CHECK (accounting_bucket IN (
                'session_statistics', 'direct_call')),
            stage                 TEXT NOT NULL,
            input_digest          TEXT NOT NULL,
            version_digest        TEXT NOT NULL,
            reserve_usd           TEXT NOT NULL,
            bound_evidence_digest TEXT NOT NULL,
            created_at_utc        TEXT NOT NULL,
            PRIMARY KEY (project_id, dispatch_id),
            UNIQUE (project_id, request_id),
            -- Initial scope permits at most one logical dispatch per task (no
            -- retries); enforced here as well as in the repository so the
            -- database itself cannot hold a second dispatch for the same task.
            UNIQUE (project_id, task_id),
            CHECK ((route = 'direct_call') = (accounting_bucket = 'direct_call')),
            FOREIGN KEY (project_id, task_id)
                REFERENCES runtime_bounded_task (project_id, task_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE runtime_dispatch_state (
            project_id      TEXT NOT NULL,
            dispatch_id     TEXT NOT NULL,
            state           TEXT NOT NULL CHECK (state IN (
                'reserved', 'submitted', 'admitted', 'completed', 'failed',
                'unknown')),
            current_fence   INTEGER NOT NULL DEFAULT 1 CHECK (current_fence = 1),
            admission_id    TEXT,
            admission_ref   TEXT,
            result_kind     TEXT,
            result_ref      TEXT,
            result_digest   TEXT,
            cost_state      TEXT NOT NULL DEFAULT 'not_incurred' CHECK (cost_state IN (
                'known', 'estimated', 'unknown', 'not_incurred')),
            cost_amount_usd TEXT,
            started_at_utc  TEXT,
            ended_at_utc    TEXT,
            updated_at_utc  TEXT NOT NULL,
            PRIMARY KEY (project_id, dispatch_id),
            CHECK ((state = 'completed') = (result_digest IS NOT NULL)),
            CHECK ((result_kind IS NULL) = (result_digest IS NULL)),
            CHECK ((result_ref IS NULL) = (result_digest IS NULL)),
            CHECK ((cost_state IN ('known', 'estimated'))
                   = (cost_amount_usd IS NOT NULL)),
            CHECK (admission_ref IS NULL OR admission_id IS NOT NULL),
            -- A dispatch that has been submitted (or is in-flight/ambiguous) can
            -- never claim that no cost was incurred (C02 §6.3/§8.3).
            CHECK (state NOT IN ('submitted', 'admitted', 'unknown')
                   OR cost_state <> 'not_incurred'),
            FOREIGN KEY (project_id, dispatch_id)
                REFERENCES runtime_dispatch (project_id, dispatch_id)
        )
        """
    )

    # The logical-call identity row is append-only: its pinned task, route/bucket,
    # stage, digests, reserve, and bound evidence can never be rewritten or removed.
    conn.execute(
        """
        CREATE TRIGGER runtime_dispatch_no_update
        BEFORE UPDATE ON runtime_dispatch
        BEGIN
            SELECT RAISE(ABORT, 'runtime_dispatch identity rows are immutable');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER runtime_dispatch_no_delete
        BEFORE DELETE ON runtime_dispatch
        BEGIN
            SELECT RAISE(ABORT, 'runtime_dispatch identity rows are immutable');
        END
        """
    )

    # The direct route has no proven durable/idempotent admission, so no
    # operational row may ever carry an admission id *or* ref for a direct_call
    # dispatch (C02 §8.1/§8.1.1).
    for event in ("INSERT", "UPDATE"):
        conn.execute(
            f"""
            CREATE TRIGGER runtime_dispatch_state_direct_no_admission_{event.lower()}
            BEFORE {event} ON runtime_dispatch_state
            WHEN (NEW.admission_id IS NOT NULL OR NEW.admission_ref IS NOT NULL)
              AND (SELECT route FROM runtime_dispatch
                   WHERE project_id = NEW.project_id
                     AND dispatch_id = NEW.dispatch_id) = 'direct_call'
            BEGIN
                SELECT RAISE(ABORT,
                    'a direct_call dispatch cannot carry a durable admission');
            END
            """
        )


def _store_v7(conn: sqlite3.Connection) -> None:
    """Add the durable **exact named-gate human approval** tables (C02 §1.3/§2/§3/§6.1).

    These tables back :mod:`review_article_agent.storage.human_approvals`: an
    append-only out-of-band owner-auth evidence row, the immutable human approval
    it authorizes, and the exact dependency-snapshot entries the approval binds.

    Only new tables and triggers are added; migrations 1-6 (project identity,
    artifacts, revisions, rights, dependencies, import requests, fixture approvals,
    the synthetic job/attempt/budget tables, the runtime task/session/proposal
    mappings, and the runtime dispatch/cost ledger) are never rewritten, so their
    exact bytes/refs are preserved across the upgrade (``ARCH-ASSETS``; step doc
    line 21).

    Design notes enforced at the database:

    * **Append-only.** Every row is immutable (never ``UPDATE``d/``DELETE``d), so
      a change is always a **new** approval row; a re-approval never revises an
      existing row, and a historical ``stale``/``superseded`` row is preserved.
    * **Authority is out-of-band, never a column a payload can set.** ``actor_kind``
      is pinned to ``'human'`` and ``authority`` to ``'owner_authenticated'``, and
      ``authorizes_execution``/``authorizes_export`` are pinned to ``0`` by
      ``CHECK`` constraints. The approval only *points at* its out-of-band
      ``owner_auth_evidence`` row (C02 §1.3/§11.0).
    * **Exact project/target binding.** Composite foreign keys pin the target and
      every snapshot entry to an existing same-project revision and pin the
      approval to the exact owner-auth evidence row, so a failed transaction can
      leave no dangling reference.

    The schema deliberately stores **no** challenge, credential, or capability: the
    evidence is a safe projection (auth id, method, owner uid, time, action digest)
    and the snapshot holds only digests and pinned refs, never source bytes
    (spec lines 62-63; ``docs/storage.md``).
    """
    conn.execute(
        """
        CREATE TABLE owner_auth_evidence (
            project_id      TEXT NOT NULL,
            auth_id         TEXT NOT NULL,
            method          TEXT NOT NULL,
            owner_uid       INTEGER NOT NULL,
            action_digest   TEXT NOT NULL,
            verified_at_utc TEXT NOT NULL,
            canonical       BLOB NOT NULL,
            content_sha256  TEXT NOT NULL,
            created_at_utc  TEXT NOT NULL,
            PRIMARY KEY (project_id, auth_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE human_approval (
            project_id               TEXT NOT NULL,
            approval_id              TEXT NOT NULL,
            target_artifact_id       TEXT NOT NULL,
            target_revision_id       TEXT NOT NULL,
            gate_kind                TEXT NOT NULL,
            gate_id                  TEXT NOT NULL,
            decision                 TEXT NOT NULL
                                     CHECK (decision IN ('approved', 'rejected')),
            rationale                TEXT NOT NULL,
            granted_at_utc           TEXT NOT NULL,
            snapshot_id              TEXT NOT NULL,
            snapshot_digest          TEXT NOT NULL,
            snapshot_canonical       BLOB NOT NULL,
            snapshot_schema_version  INTEGER NOT NULL,
            storage_schema_version   INTEGER NOT NULL,
            version_set_canonical    BLOB NOT NULL,
            owner_auth_id            TEXT NOT NULL,
            owner_auth_method        TEXT NOT NULL,
            owner_auth_action_digest TEXT NOT NULL,
            owner_uid                INTEGER NOT NULL,
            actor_kind               TEXT NOT NULL DEFAULT 'human'
                                     CHECK (actor_kind = 'human'),
            authority                TEXT NOT NULL DEFAULT 'owner_authenticated'
                                     CHECK (authority = 'owner_authenticated'),
            authorizes_execution     INTEGER NOT NULL DEFAULT 0
                                     CHECK (authorizes_execution = 0),
            authorizes_export        INTEGER NOT NULL DEFAULT 0
                                     CHECK (authorizes_export = 0),
            created_at_utc           TEXT NOT NULL,
            PRIMARY KEY (project_id, approval_id),
            FOREIGN KEY (project_id, target_artifact_id, target_revision_id)
                REFERENCES revision (project_id, artifact_id, revision_id),
            FOREIGN KEY (project_id, owner_auth_id)
                REFERENCES owner_auth_evidence (project_id, auth_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE human_approval_entry (
            project_id             TEXT NOT NULL,
            approval_id            TEXT NOT NULL,
            entry_index            INTEGER NOT NULL,
            artifact_id            TEXT NOT NULL,
            revision_id            TEXT NOT NULL,
            role                   TEXT NOT NULL,
            content_sha256         TEXT NOT NULL,
            byte_size              INTEGER NOT NULL,
            rights_id              TEXT NOT NULL,
            rights_record_revision INTEGER NOT NULL,
            rights_digest          TEXT NOT NULL,
            PRIMARY KEY (project_id, approval_id, entry_index),
            FOREIGN KEY (project_id, approval_id)
                REFERENCES human_approval (project_id, approval_id),
            FOREIGN KEY (project_id, artifact_id, revision_id)
                REFERENCES revision (project_id, artifact_id, revision_id)
        )
        """
    )

    for table in ("owner_auth_evidence", "human_approval", "human_approval_entry"):
        conn.execute(
            f"""
            CREATE TRIGGER {table}_no_update
            BEFORE UPDATE ON {table}
            BEGIN
                SELECT RAISE(ABORT, '{table} rows are immutable');
            END
            """
        )
        conn.execute(
            f"""
            CREATE TRIGGER {table}_no_delete
            BEFORE DELETE ON {table}
            BEGIN
                SELECT RAISE(ABORT, '{table} rows are immutable');
            END
            """
        )


def _store_v8(conn: sqlite3.Connection) -> None:
    """Add the durable **pre-dispatch Jev decision request intent** table (C02 §6.1/§6.2/§6.3/§8.1.1).

    This table backs :mod:`review_article_agent.storage.decision_intents`: the
    immutable, canonical record of the **exact question** a future direct Jev
    dispatch intends to ask, recorded **before** the dispatch is submitted so a
    later runner/result can prove the exact question refs/versions and the exact
    request binding.

    Only new tables and triggers are added; migrations 1-7 (project identity,
    artifacts, revisions, rights, dependencies, import requests, fixture approvals,
    the synthetic job/attempt/budget tables, the runtime task/session/proposal
    mappings, the runtime dispatch/cost ledger, and the human approvals) are never
    rewritten, so their exact bytes/refs are preserved across the upgrade
    (``ARCH-ASSETS``; step doc line 21).

    Design notes enforced at the database:

    * **One immutable intent per logical dispatch.** The row is keyed by the
      same-project ``(project_id, dispatch_id)`` and foreign-keys the existing
      ``runtime_dispatch`` identity and its ``runtime_bounded_task``, so an intent
      can never name a foreign project, a missing dispatch, or a task other than
      the one the dispatch already pins.
    * **Append-only.** The row is immutable (never ``UPDATE``d/``DELETE``d), so a
      request's pinned question refs, versions, request digest/bytes/bound/pricing,
      and canonical bytes can never be rewritten; a change is always a **new**
      intent (which cannot happen for the same dispatch — divergence is refused by
      the repository).
    * **Canonical bytes are the truth.** ``canonical`` holds the exact canonical
      JSON of the recorded intent and ``content_digest`` is its recomputed SHA-256;
      the projected columns (target ref, ``request_sha256``, ``version_digest``, …)
      must agree with the canonical bytes on every read, so a tampered column is
      detected rather than trusted.
    * **No credential, capability, or response content.** Only opaque ids, exact
      decimal amounts, SHA-256 digests, and the bounded canonical request/question
      metadata are stored (spec lines 62-63; ``docs/storage.md``).

    ``route`` is pinned to ``direct_call``: only the direct Jev route has a
    request intent recorded here (§8.1.1).
    """
    conn.execute(
        """
        CREATE TABLE decision_request_intent (
            project_id            TEXT NOT NULL,
            dispatch_id           TEXT NOT NULL,
            intent_id             TEXT NOT NULL,
            schema_version        INTEGER NOT NULL,
            task_id               TEXT NOT NULL,
            task_fingerprint      TEXT NOT NULL,
            route                 TEXT NOT NULL CHECK (route = 'direct_call'),
            stage                 TEXT NOT NULL,
            input_digest          TEXT NOT NULL,
            version_digest        TEXT NOT NULL,
            reserve_usd           TEXT NOT NULL,
            bound_evidence_digest TEXT NOT NULL,
            request_sha256        TEXT NOT NULL,
            request_bytes         INTEGER NOT NULL,
            cost_upper_bound_usd  TEXT NOT NULL,
            pricing_version       TEXT NOT NULL,
            prompt_version        TEXT NOT NULL,
            target_artifact_id    TEXT NOT NULL,
            target_revision_id    TEXT NOT NULL,
            question_count        INTEGER NOT NULL,
            canonical             BLOB NOT NULL,
            content_digest        TEXT NOT NULL,
            created_at_utc        TEXT NOT NULL,
            PRIMARY KEY (project_id, dispatch_id),
            UNIQUE (project_id, intent_id),
            FOREIGN KEY (project_id, dispatch_id)
                REFERENCES runtime_dispatch (project_id, dispatch_id),
            FOREIGN KEY (project_id, task_id)
                REFERENCES runtime_bounded_task (project_id, task_id),
            FOREIGN KEY (project_id, target_artifact_id, target_revision_id)
                REFERENCES revision (project_id, artifact_id, revision_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TRIGGER decision_request_intent_no_update
        BEFORE UPDATE ON decision_request_intent
        BEGIN
            SELECT RAISE(ABORT, 'decision_request_intent rows are immutable');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER decision_request_intent_no_delete
        BEFORE DELETE ON decision_request_intent
        BEGIN
            SELECT RAISE(ABORT, 'decision_request_intent rows are immutable');
        END
        """
    )


STORE_MIGRATIONS: Mapping[int, Migration] = {
    1: _store_v1,
    2: _store_v2,
    3: _store_v3,
    4: _store_v4,
    5: _store_v5,
    6: _store_v6,
    7: _store_v7,
    8: _store_v8,
}


def _registry_v1(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE projects (
            project_id     TEXT PRIMARY KEY,
            name           TEXT NOT NULL,
            root_path      TEXT NOT NULL UNIQUE,
            created_at_utc TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE active_project (
            singleton      INTEGER PRIMARY KEY CHECK (singleton = 1),
            project_id     TEXT NOT NULL REFERENCES projects(project_id),
            selected_at_utc TEXT NOT NULL
        )
        """
    )


def _registry_v2(conn: sqlite3.Connection) -> None:
    """Add the durable **execution-activation** singleton (C02 §8.5).

    Selection (``active_project``) is a UI/bookkeeping choice and never proof
    that a project is executing. Execution activation is a *separate* durable
    record: at most one project may hold it, and it is fenced by an opaque
    ``activation_token`` so a stale engine can never release it or authorize a
    local write after a switch (``ARCH-ACTIVE-ENF``; step 03 AC4/AC5).

    ``state`` is one of ``none``/``active``/``draining``.  ``active_project_id``
    and ``activation_token`` are ``NULL`` **iff** ``state = 'none'`` — enforced
    at the database, so a half-cleared activation can never persist (C02 §8.5,
    §11.6).  This migration only *adds* a table and seeds the single ``none``
    row; existing ``projects``/``active_project`` rows are never rewritten, so
    mappings and the selected project are preserved across the upgrade.
    """
    conn.execute(
        """
        CREATE TABLE execution_activation (
            singleton         INTEGER PRIMARY KEY CHECK (singleton = 1),
            state             TEXT NOT NULL
                              CHECK (state IN ('none', 'active', 'draining')),
            active_project_id TEXT REFERENCES projects(project_id),
            activation_token  TEXT,
            activated_at_utc  TEXT,
            updated_at_utc    TEXT NOT NULL,
            CHECK ((state = 'none') = (active_project_id IS NULL)),
            CHECK ((state = 'none') = (activation_token IS NULL))
        )
        """
    )
    conn.execute(
        "INSERT INTO execution_activation "
        "(singleton, state, active_project_id, activation_token, "
        " activated_at_utc, updated_at_utc) "
        "VALUES (1, 'none', NULL, NULL, NULL, ?)",
        (_utc_now(),),
    )


REGISTRY_MIGRATIONS: Mapping[int, Migration] = {
    1: _registry_v1,
    2: _registry_v2,
}


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
def read_user_version(conn: sqlite3.Connection) -> int:
    """Return the current ``PRAGMA user_version`` as an int."""
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row is not None else 0


def migrate(
    conn: sqlite3.Connection,
    *,
    target_version: int,
    migrations: Mapping[int, Migration],
    label: str,
) -> int:
    """Bring ``conn`` up to ``target_version`` transactionally.

    Returns the resulting version. Raises :class:`UnsupportedSchemaVersion` if
    the database is newer than ``target_version`` and :class:`MigrationError`
    if any step fails (that step is rolled back; earlier applied steps remain).

    Concurrency: the version is re-read **under the ``BEGIN IMMEDIATE`` write
    lock** before each step. Two connections that both observed an old version
    therefore do not both run the same step: the loser, once it holds the lock,
    sees the step already applied and skips it (and refuses a version newer than
    ``target_version`` that appeared while waiting). This keeps a single
    transactional migration path without a duplicate-``CREATE TABLE`` failure.
    """
    while True:
        current = read_user_version(conn)
        if current > target_version:
            raise UnsupportedSchemaVersion(
                label=label, found=current, supported=target_version
            )
        if current == target_version:
            return current

        version = current + 1
        step = migrations.get(version)
        if step is None:
            raise MigrationError(
                label=label,
                target=version,
                reason="no migration registered for this version",
            )
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:  # pragma: no cover - environment dependent
            raise MigrationError(
                label=label, target=version, reason="could not begin transaction"
            ) from exc
        try:
            # Re-read under the write lock: a concurrent migrator may have
            # applied this step (or advanced past it) while we waited to begin.
            locked = read_user_version(conn)
            if locked > target_version:
                conn.execute("ROLLBACK")
                raise UnsupportedSchemaVersion(
                    label=label, found=locked, supported=target_version
                )
            if locked < version:
                step(conn)
                # ``PRAGMA`` does not accept bound parameters; ``version`` is an
                # int from our own closed range, never caller text.
                conn.execute(f"PRAGMA user_version = {int(version)}")
            conn.execute("COMMIT")
        except UnsupportedSchemaVersion:
            raise
        except Exception as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover - rollback best effort
                pass
            raise MigrationError(
                label=label, target=version, reason=_summarise(exc)
            ) from exc
        # Loop re-reads the committed version and either applies the next step
        # or returns.


def _summarise(exc: BaseException) -> str:
    """Summarise an exception for an actionable, secret-free message."""
    if isinstance(exc, sqlite3.Error):
        # sqlite messages describe the failure (e.g. "table already exists")
        # and never contain application data or credentials.
        return f"sqlite: {exc}"
    return exc.__class__.__name__


def read_only_probe(db_path: str) -> tuple[int, set[str]]:
    """Return ``(user_version, user_table_names)`` **read-only**.

    Used to classify an existing file (registry or store) as empty, ours, or
    foreign *before* any pragma/migration is applied. Opens with ``mode=ro``
    **only** — there is no writable fallback; a read-only open failure raises a
    clean :class:`StoreError` and an absent path stays absent.

    As with any WAL read, SQLite may create read-only ``-shm``/``-wal``
    shared-memory sidecars in the directory; the database file itself is not
    modified and no ``journal_mode`` change or migration occurs.
    """
    try:
        conn: sqlite3.Connection = sqlite3.connect(
            "file:" + quote(db_path) + "?mode=ro", uri=True, isolation_level=None
        )
    except sqlite3.Error as exc:
        raise StoreError(
            f"could not open the local database at {db_path!r} read-only "
            f"({exc.__class__.__name__}); refusing to fall back to a writable "
            f"open."
        ) from exc
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    except sqlite3.DatabaseError as exc:
        raise StoreError(
            f"the file at {db_path!r} is not a readable SQLite database."
        ) from exc
    finally:
        conn.close()
    return version, tables


def open_connection(db_path: str) -> sqlite3.Connection:
    """Open a store/registry connection configured for this package.

    ``isolation_level=None`` puts the connection in autocommit mode so the
    migration and transaction helpers control ``BEGIN``/``COMMIT`` explicitly.
    """
    try:
        conn = sqlite3.connect(db_path, isolation_level=None, timeout=30.0)
    except sqlite3.Error as exc:
        raise StoreError(
            f"could not open the local database at {db_path!r}."
        ) from exc
    conn.row_factory = sqlite3.Row
    for pragma in COMMON_PRAGMAS:
        try:
            conn.execute(pragma)
        except sqlite3.Error as exc:
            conn.close()
            raise StoreError(
                f"could not configure the local database at {db_path!r}."
            ) from exc
    return conn
