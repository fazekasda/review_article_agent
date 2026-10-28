"""Acceptance tests for atomic, durable, exact named-gate human approvals.

Synthetic data only (no secrets, no licensed source text, no credentials, no
network, no model calls, no paid calls). These tests assert the behavior of
``review_article_agent.storage.human_approvals``:

* a real out-of-band confirmation (simulated by patching **only** the owner-auth
  low-level terminal seams, never a capability bypass) produces one durable,
  exact approval bound to the prepared snapshot, the human actor, and the
  out-of-band ``owner_auth_ref``;
* stale pins, a wrong/expired/replayed/forked/foreign-uid/forged capability, an
  imported permission mapping, and a cross-project snapshot all deny **before any
  row is written**;
* a failed write rolls back to **zero** rows in all three tables;
* corruption of the canonical snapshot, the columns, an entry pin, or the
  owner-auth evidence makes a read fail closed and a status ``invalidated``;
* a newer revision, an amended rights record, or a newer approval (including a
  rejection) yields ``stale``/``superseded`` on read, while honest historical
  rows stay structurally verifiable;
* a saved generic tool permission cannot satisfy a *different* named gate
  instance, and no approval claims global execution/export/scientific validity;
* the additive migration 6→7 preserves existing rows.

Run with::

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_human_approvals.py'
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.domain.records import canonical_json  # noqa: E402
from review_article_agent.runtime import owner_auth  # noqa: E402
from review_article_agent.runtime.gates import (  # noqa: E402
    PreparedGate,
    prepare_gate_snapshot,
)
from review_article_agent.runtime.owner_auth import (  # noqa: E402
    OwnerApproval,
    action_digest,
)
from review_article_agent.storage import ArtifactRepository, ProjectStore  # noqa: E402
from review_article_agent.storage import paths, schema  # noqa: E402
from review_article_agent.storage.artifacts import sha256_digest  # noqa: E402
from review_article_agent.storage.human_approvals import (  # noqa: E402
    HumanApprovalCorruptionError,
    HumanApprovalDeniedError,
    HumanApprovalRepository,
    integrity_problems,
)

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")
_CANARY = b"SECRET-CANARY-9931-source-bytes"
_BASE_TIME = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
_FIXED_CHALLENGE = "TEST-CHAL-0001"
_OWNER_UID = 1000


def _rights(project_id, artifact_id, revision_id, *, record_revision=1, rights_id=None):
    """A synthetic, fully valid RightsRecord v1 bound to one exact revision."""
    return {
        "rights_id": rights_id or f"RIGHTS-{revision_id}",
        "project_id": project_id,
        "record_revision": record_revision,
        "subject": {
            "kind": "source_revision",
            "project_id": project_id,
            "artifact_id": artifact_id,
            "revision_id": revision_id,
        },
        "operations": {op: "allowed" for op in _OPS},
        "basis": {
            op: {
                "kind": "public_domain",
                "evidence": {"ref": "lic:pd-syn", "provenance": "publisher:pd-list"},
            }
            for op in _OPS
        },
        "authorization": None,
        "restrictions": [],
        "privacy": {"personal_data": False, "confidentiality": "public", "denial": False},
        "recorded_by": "user",
        "recorded_at": "2026-10-28T00:00:00Z",
        "derived_status": "valid",
    }


class _FakeTty:
    """A low-level terminal double: only the terminal operations are mocked."""

    def __init__(self, *, replies=(), is_tty=True):
        self._replies = list(replies)
        self._is_tty = is_tty
        self.written: list[str] = []
        self.closed = False

    def is_tty(self):
        return self._is_tty

    def write(self, text, *, deadline=None):
        self.written.append(text)

    def read_line(self, *, max_chars, deadline):
        return self._replies.pop(0) if self._replies else None

    def close(self):
        self.closed = True


class HumanApprovalTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-hapv-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        # Mutable clocks the module's patched owner-auth seams read.
        self.clock = [_BASE_TIME]
        self.mono = [1000.0]

    # -- store / chain ------------------------------------------------------- #
    def new_store(self, name: str = "p") -> ProjectStore:
        store = ProjectStore.create_at(
            paths.app_dir_for(str(self.tmp / name)), paths.new_project_id()
        )
        self.addCleanup(store.close)
        return store

    def _import(self, repo, artifact_id, content, revision_id, *, dependencies=(), parent_revision_id=None):
        return repo.import_revision(
            artifact_id=artifact_id,
            content=content,
            origin_locator="import:synthetic",
            rights=_rights(repo.project_id, artifact_id, revision_id),
            revision_id=revision_id,
            dependencies=list(dependencies),
            parent_revision_id=parent_revision_id,
        )

    def _target(self, store, artifact_id, revision_id):
        return {
            "project_id": store.project_id,
            "artifact_id": artifact_id,
            "revision_id": revision_id,
        }

    def _chain(self, store):
        """base document (canary bytes) <- finding <- report (a live closure)."""
        repo = ArtifactRepository(store)
        pid = store.project_id
        repo.create_artifact("a-base", kind="document")
        self._import(repo, "a-base", _CANARY, "rev-base")
        repo.create_artifact("a-find", kind="finding")
        self._import(
            repo,
            "a-find",
            b"find-bytes",
            "rev-find",
            dependencies=[self._target(store, "a-base", "rev-base")],
        )
        repo.create_artifact("a-rep", kind="report")
        self._import(
            repo,
            "a-rep",
            b"rep-bytes",
            "rev-rep",
            dependencies=[self._target(store, "a-find", "rev-find")],
        )
        return repo

    def prepare(self, store, *, gate_kind="pilot", gate_id="gate_pilot_1", decision="approved"):
        return prepare_gate_snapshot(
            store,
            self._target(store, "a-rep", "rev-rep"),
            gate_kind=gate_kind,
            gate_id=gate_id,
            decision=decision,
            rationale="human review of the exact report revision",
        )

    # -- owner-auth seams (never a capability bypass) ------------------------ #
    def install(self, *, tty=None, challenge=_FIXED_CHALLENGE, owner_uid=_OWNER_UID, real_uid=_OWNER_UID):
        """Patch only the module's private low-level terminal/identity seams."""
        patches = [
            mock.patch.object(owner_auth, "_new_challenge", lambda: challenge),
            mock.patch.object(owner_auth, "_current_owner_uid", lambda: owner_uid),
            mock.patch.object(owner_auth, "_real_uid", lambda: real_uid),
            mock.patch.object(owner_auth, "_utcnow", lambda: self.clock[0]),
            mock.patch.object(owner_auth, "_monotonic", lambda: self.mono[0]),
            mock.patch.object(
                owner_auth,
                "_open_controlling_tty",
                lambda: tty if tty is not None else _FakeTty(replies=[challenge]),
            ),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def mint(self, prepared, **conf_kwargs) -> OwnerApproval:
        return owner_auth.request_owner_confirmation(
            prepared.owner_action(), **conf_kwargs
        )

    def _table_counts(self, store):
        conn = store.connection
        tables = [
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        ]
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        }


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #
class RecordDecisionTest(HumanApprovalTestBase):
    def test_valid_out_of_band_confirmation_records_durable_approval(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            prepared = self.prepare(store)
            self.install()
            cap = self.mint(prepared)
            approvals = HumanApprovalRepository(store)

            record = approvals.record_decision(prepared, cap)
            self.assertEqual(record.project_id, store.project_id)
            self.assertEqual(record.target["artifact_id"], "a-rep")
            self.assertEqual(record.gate_kind, "pilot")
            self.assertEqual(record.gate_id, "gate_pilot_1")
            self.assertEqual(record.decision, "approved")
            self.assertEqual(record.snapshot_digest, prepared.digest)
            self.assertEqual(record.snapshot_id, prepared.snapshot_id)
            self.assertTrue(record.owner_auth_ref.startswith("auth_"))
            self.assertEqual(record.actor_kind, "human")
            self.assertEqual(record.authority, "owner_authenticated")

            status = approvals.evaluate(record.approval_id)
            self.assertEqual(status.status, "active")
            self.assertTrue(status.satisfies_named_gate)
            self.assertFalse(status.authorizes_execution)
            self.assertFalse(status.authorizes_export)

            self.assertTrue(
                approvals.named_gate_satisfied(
                    "a-rep", gate_kind="pilot", gate_id="gate_pilot_1"
                )
            )
            self.assertTrue(approvals.verify_snapshot(record.approval_id).ok)

            # Exactly one evidence + approval + three closure entries.
            counts = self._table_counts(store)
            self.assertEqual(counts["human_approval"], 1)
            self.assertEqual(counts["owner_auth_evidence"], 1)
            self.assertEqual(counts["human_approval_entry"], len(prepared.entries))
            # No source bytes are stored in the approval canonical snapshot.
            self.assertNotIn(_CANARY, record.canonical)

    def test_human_authored_version_set_and_no_invented_model(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            prepared = self.prepare(store)
            self.install()
            approvals = HumanApprovalRepository(store)
            record = approvals.record_decision(prepared, self.mint(prepared))

            row = store.connection.execute(
                "SELECT version_set_canonical FROM human_approval WHERE approval_id = ?",
                (record.approval_id,),
            ).fetchone()
            versions = json.loads(bytes(row["version_set_canonical"]).decode("utf-8"))
            self.assertEqual(versions["context_kind"], "human_authored")
            self.assertEqual(versions["route"], "not_applicable")
            for slot in ("model", "prompt", "rubric", "parser", "adapter"):
                self.assertEqual(versions[slot], {"state": "not_applicable"})

    def test_no_extra_authority_kwargs_accepted(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            prepared = self.prepare(store)
            self.install()
            approvals = HumanApprovalRepository(store)
            cap = self.mint(prepared)
            for extra in (
                {"actor": {"actor_kind": "human"}},
                {"authenticated": True},
                {"owner_evidence": {"auth_id": "auth_x"}},
                {"imported_approval": {"approval_id": "x"}},
                {"authority": "owner_authenticated"},
            ):
                with self.assertRaises(TypeError):
                    approvals.record_decision(prepared, cap, **extra)

    def test_does_not_touch_fixture_approvals_or_artifact_state(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            prepared = self.prepare(store)
            self.install()
            approvals = HumanApprovalRepository(store)
            before_status = store.connection.execute(
                "SELECT status FROM artifact WHERE artifact_id = 'a-rep'"
            ).fetchone()["status"]
            record = approvals.record_decision(prepared, self.mint(prepared))
            self.assertIsNotNone(record)
            counts = self._table_counts(store)
            self.assertEqual(counts["fixture_approval"], 0)
            self.assertEqual(counts["fixture_approval_entry"], 0)
            self.assertEqual(
                store.connection.execute(
                    "SELECT status FROM artifact WHERE artifact_id = 'a-rep'"
                ).fetchone()["status"],
                before_status,
            )


# --------------------------------------------------------------------------- #
# Denials before any write
# --------------------------------------------------------------------------- #
class DenialTest(HumanApprovalTestBase):
    def _assert_no_rows(self, store) -> None:
        counts = self._table_counts(store)
        self.assertEqual(counts["human_approval"], 0)
        self.assertEqual(counts["owner_auth_evidence"], 0)
        self.assertEqual(counts["human_approval_entry"], 0)

    def test_stale_prepared_after_target_edit_denies(self) -> None:
        with self.new_store() as store:
            repo = self._chain(store)
            prepared = self.prepare(store)
            self.install()
            cap = self.mint(prepared)
            # A later revision supersedes the prepared target head.
            self._import(repo, "a-rep", b"rep-v2", "rev-rep-2", parent_revision_id="rev-rep")
            approvals = HumanApprovalRepository(store)
            with self.assertRaises(HumanApprovalDeniedError) as ctx:
                approvals.record_decision(prepared, cap)
            self.assertEqual(ctx.exception.reason, "prepared_stale")
            self._assert_no_rows(store)

    def test_stale_prepared_after_live_dependency_change_denies(self) -> None:
        with self.new_store() as store:
            repo = self._chain(store)
            prepared = self.prepare(store)
            self.install()
            cap = self.mint(prepared)
            self._import(repo, "a-base", b"base-v2", "rev-base-2", parent_revision_id="rev-base")
            approvals = HumanApprovalRepository(store)
            with self.assertRaises(HumanApprovalDeniedError) as ctx:
                approvals.record_decision(prepared, cap)
            self.assertEqual(ctx.exception.reason, "prepared_stale")
            self._assert_no_rows(store)

    def test_wrong_action_capability_denies(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            prepared_a = self.prepare(store, gate_id="gate_pilot_1")
            prepared_b = self.prepare(store, gate_id="gate_pilot_2")
            self.install()
            cap_a = self.mint(prepared_a)
            approvals = HumanApprovalRepository(store)
            with self.assertRaises(HumanApprovalDeniedError) as ctx:
                approvals.record_decision(prepared_b, cap_a)
            self.assertEqual(ctx.exception.reason, "action_mismatch")
            self._assert_no_rows(store)

    def test_expired_capability_denies(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            prepared = self.prepare(store)
            self.install()
            cap = self.mint(prepared, expires_after_seconds=10)
            self.clock[0] = _BASE_TIME + timedelta(seconds=100)
            self.mono[0] += 100
            approvals = HumanApprovalRepository(store)
            with self.assertRaises(HumanApprovalDeniedError) as ctx:
                approvals.record_decision(prepared, cap)
            self.assertEqual(ctx.exception.reason, "approval_expired")
            self._assert_no_rows(store)

    def test_replayed_capability_denies_and_keeps_one_row(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            prepared = self.prepare(store)
            self.install()
            cap = self.mint(prepared)
            approvals = HumanApprovalRepository(store)
            approvals.record_decision(prepared, cap)
            with self.assertRaises(HumanApprovalDeniedError) as ctx:
                approvals.record_decision(prepared, cap)
            self.assertEqual(ctx.exception.reason, "approval_replayed")
            counts = self._table_counts(store)
            self.assertEqual(counts["human_approval"], 1)
            self.assertEqual(counts["owner_auth_evidence"], 1)

    def test_forked_capability_denies(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            prepared = self.prepare(store)
            self.install()
            cap = self.mint(prepared)
            approvals = HumanApprovalRepository(store)
            with mock.patch.object(owner_auth, "_current_pid", lambda: os.getpid() + 1):
                with self.assertRaises(HumanApprovalDeniedError) as ctx:
                    approvals.record_decision(prepared, cap)
            self.assertEqual(ctx.exception.reason, "capability_foreign_process")
            self._assert_no_rows(store)

    def test_foreign_uid_denies(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            prepared = self.prepare(store)
            self.install()
            cap = self.mint(prepared)
            approvals = HumanApprovalRepository(store)
            with mock.patch.object(owner_auth, "_current_owner_uid", lambda: _OWNER_UID + 1):
                with self.assertRaises(HumanApprovalDeniedError) as ctx:
                    approvals.record_decision(prepared, cap)
            self.assertEqual(ctx.exception.reason, "owner_uid_mismatch")
            self._assert_no_rows(store)

    def test_forged_flags_and_imported_permission_deny(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            prepared = self.prepare(store)
            self.install()
            approvals = HumanApprovalRepository(store)
            for forged in (
                True,
                "authenticated",
                {"type": "saved_permission", "scope": "tools", "allowed": True},
                None,
                object(),
            ):
                with self.assertRaises(HumanApprovalDeniedError) as ctx:
                    approvals.record_decision(prepared, forged)
                self.assertEqual(ctx.exception.reason, "capability_invalid")
            self._assert_no_rows(store)

    def test_imported_permission_mapping_as_prepared_denies(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            self.install()
            approvals = HumanApprovalRepository(store)
            with self.assertRaises(HumanApprovalDeniedError) as ctx:
                approvals.record_decision(
                    {"type": "saved_permission", "scope": "tools", "allowed": True},
                    None,
                )
            self.assertEqual(ctx.exception.reason, "prepared_not_prepared")
            self._assert_no_rows(store)

    def test_cross_project_prepared_denies(self) -> None:
        with self.new_store("a") as store_a, self.new_store("b") as store_b:
            self._chain(store_a)
            prepared_a = self.prepare(store_a)
            self.install()
            cap = self.mint(prepared_a)
            approvals_b = HumanApprovalRepository(store_b)
            with self.assertRaises(HumanApprovalDeniedError) as ctx:
                approvals_b.record_decision(prepared_a, cap)
            self.assertEqual(ctx.exception.reason, "prepared_denied")
            self._assert_no_rows(store_b)

    def test_failed_write_rolls_back_to_zero_rows(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            prepared = self.prepare(store)
            self.install()
            cap = self.mint(prepared)
            approvals = HumanApprovalRepository(store)
            real = HumanApprovalRepository._insert_entry

            def _boom(self, approval_id, index, entry):
                if index == 1:
                    raise RuntimeError("injected write failure")
                return real(self, approval_id, index, entry)

            with mock.patch.object(HumanApprovalRepository, "_insert_entry", _boom):
                with self.assertRaises(RuntimeError):
                    approvals.record_decision(prepared, cap)
            self._assert_no_rows(store)
            # The consumed capability is not silently reusable: a fresh
            # confirmation is required (safe, conservative).
            with self.assertRaises(HumanApprovalDeniedError) as ctx:
                approvals.record_decision(prepared, cap)
            self.assertEqual(ctx.exception.reason, "approval_replayed")


# --------------------------------------------------------------------------- #
# Named-gate satisfaction only
# --------------------------------------------------------------------------- #
class NamedGateTest(HumanApprovalTestBase):
    def test_satisfaction_is_per_gate_instance_only(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            prepared = self.prepare(store, gate_kind="outline", gate_id="gate_outline_1")
            self.install()
            approvals = HumanApprovalRepository(store)
            approvals.record_decision(prepared, self.mint(prepared))

            self.assertTrue(
                approvals.named_gate_satisfied(
                    "a-rep", gate_kind="outline", gate_id="gate_outline_1"
                )
            )
            # A different gate kind, a different gate id, and a different gate
            # instance are all unsatisfied: a generic permission cannot satisfy a
            # pilot/exclusion gate.
            for kind, gid in (
                ("pilot", "gate_outline_1"),
                ("exclusion_set", "gate_outline_1"),
                ("outline", "gate_outline_2"),
            ):
                self.assertFalse(
                    approvals.named_gate_satisfied("a-rep", gate_kind=kind, gate_id=gid)
                )

    def test_rejection_does_not_satisfy_a_named_gate(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            prepared = self.prepare(store, decision="rejected")
            self.install()
            approvals = HumanApprovalRepository(store)
            record = approvals.record_decision(prepared, self.mint(prepared))
            status = approvals.evaluate(record.approval_id)
            self.assertEqual(status.status, "active")
            self.assertEqual(status.decision, "rejected")
            self.assertFalse(status.satisfies_named_gate)
            self.assertFalse(
                approvals.named_gate_satisfied(
                    "a-rep", gate_kind="pilot", gate_id="gate_pilot_1"
                )
            )


# --------------------------------------------------------------------------- #
# Lifecycle (stale / invalidated / superseded)
# --------------------------------------------------------------------------- #
class LifecycleTest(HumanApprovalTestBase):
    def _record(self, store, approvals, prepared):
        self.install()
        return approvals.record_decision(prepared, self.mint(prepared))

    def test_new_target_revision_makes_approval_stale(self) -> None:
        with self.new_store() as store:
            repo = self._chain(store)
            approvals = HumanApprovalRepository(store)
            record = self._record(store, approvals, self.prepare(store))
            self._import(repo, "a-rep", b"rep-v2", "rev-rep-2", parent_revision_id="rev-rep")
            status = approvals.evaluate(record.approval_id)
            self.assertEqual(status.status, "stale")
            self.assertFalse(status.satisfies_named_gate)
            self.assertFalse(
                approvals.named_gate_satisfied(
                    "a-rep", gate_kind="pilot", gate_id="gate_pilot_1"
                )
            )

    def test_rights_amendment_makes_approval_stale(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            approvals = HumanApprovalRepository(store)
            record = self._record(store, approvals, self.prepare(store))
            pid = store.project_id
            canonical = canonical_json(
                {"rights_id": "RIGHTS-rev-base", "project_id": pid, "record_revision": 2}
            )
            store.connection.execute(
                "INSERT INTO rights_record "
                "(project_id, rights_id, record_revision, subject_kind, subject_project_id, "
                " subject_artifact_id, subject_revision_id, canonical, content_sha256, "
                " recorded_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (pid, "RIGHTS-rev-base", 2, "source_revision", pid, "a-base", None,
                 canonical, sha256_digest(canonical), "2026-10-28T00:00:00+00:00"),
            )
            self.assertEqual(approvals.evaluate(record.approval_id).status, "stale")

    def test_reapproval_supersedes_the_previous_row(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            prepared = self.prepare(store)
            self.install()
            approvals = HumanApprovalRepository(store)
            first = approvals.record_decision(prepared, self.mint(prepared))
            second = approvals.record_decision(prepared, self.mint(prepared))
            self.assertNotEqual(first.approval_id, second.approval_id)

            first_status = approvals.evaluate(first.approval_id)
            self.assertEqual(first_status.status, "superseded")
            self.assertEqual(first_status.superseded_by, second.approval_id)
            self.assertFalse(first_status.satisfies_named_gate)
            self.assertEqual(approvals.evaluate(second.approval_id).status, "active")
            # Both rows are retained (immutable history).
            self.assertEqual(self._table_counts(store)["human_approval"], 2)

    def test_rejection_supersedes_a_previous_approval(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            self.install()
            approvals = HumanApprovalRepository(store)
            approved = approvals.record_decision(self.prepare(store), self.mint(self.prepare(store)))
            rejected_prepared = self.prepare(store, decision="rejected")
            rejected = approvals.record_decision(
                rejected_prepared, self.mint(rejected_prepared)
            )
            first_status = approvals.evaluate(approved.approval_id)
            self.assertEqual(first_status.status, "superseded")
            self.assertEqual(first_status.superseded_by, rejected.approval_id)
            self.assertFalse(
                approvals.named_gate_satisfied(
                    "a-rep", gate_kind="pilot", gate_id="gate_pilot_1"
                )
            )

    def test_historical_stale_row_stays_structurally_verifiable(self) -> None:
        with self.new_store() as store:
            repo = self._chain(store)
            approvals = HumanApprovalRepository(store)
            record = self._record(store, approvals, self.prepare(store))
            self._import(repo, "a-rep", b"rep-v2", "rev-rep-2", parent_revision_id="rev-rep")
            # Status is stale, but structural verification still accepts it, and
            # the read-only integrity sweep reports no problems.
            self.assertEqual(approvals.evaluate(record.approval_id).status, "stale")
            self.assertTrue(approvals.verify_snapshot(record.approval_id).ok)
            self.assertEqual(approvals.verify_all_integrity(), [])
            self.assertEqual(integrity_problems(store.connection, store.project_id), [])


# --------------------------------------------------------------------------- #
# Corruption (invalidated)
# --------------------------------------------------------------------------- #
class CorruptionTest(HumanApprovalTestBase):
    def _record(self, store, approvals, prepared):
        self.install()
        return approvals.record_decision(prepared, self.mint(prepared))

    def _drop_update_trigger(self, store, table):
        store.connection.execute(f"DROP TRIGGER IF EXISTS {table}_no_update")

    def _drop_delete_trigger(self, store, table):
        store.connection.execute(f"DROP TRIGGER IF EXISTS {table}_no_delete")

    def _rewrite_evidence(self, store, auth_id, mutate, *, method=None, owner_uid=None):
        """Rewrite a stored evidence canonical (and optionally its columns).

        Recomputes the canonical digest so the row stays self-consistent, which
        is what isolates the *semantic* projection checks under test.
        """
        self._drop_update_trigger(store, "owner_auth_evidence")
        row = store.connection.execute(
            "SELECT canonical FROM owner_auth_evidence WHERE project_id = ? "
            "AND auth_id = ?",
            (store.project_id, auth_id),
        ).fetchone()
        record = json.loads(bytes(row["canonical"]).decode("utf-8"))
        mutate(record)
        canonical = canonical_json(record)
        sets = ["canonical = ?", "content_sha256 = ?"]
        params: list = [canonical, sha256_digest(canonical)]
        if method is not None:
            sets.append("method = ?")
            params.append(method)
        if owner_uid is not None:
            sets.append("owner_uid = ?")
            params.append(owner_uid)
        params.extend([store.project_id, auth_id])
        store.connection.execute(
            "UPDATE owner_auth_evidence SET " + ", ".join(sets) + " "
            "WHERE project_id = ? AND auth_id = ?",
            tuple(params),
        )

    def test_corrupt_canonical_snapshot_is_invalid(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            approvals = HumanApprovalRepository(store)
            record = self._record(store, approvals, self.prepare(store))
            self._drop_update_trigger(store, "human_approval")
            store.connection.execute(
                "UPDATE human_approval SET snapshot_canonical = ? WHERE approval_id = ?",
                (b"{}", record.approval_id),
            )
            with self.assertRaises(HumanApprovalCorruptionError):
                approvals.get_approval(record.approval_id)
            self.assertEqual(approvals.evaluate(record.approval_id).status, "invalidated")
            self.assertFalse(approvals.verify_snapshot(record.approval_id).ok)
            self.assertTrue(integrity_problems(store.connection, store.project_id))

    def test_corrupt_owner_auth_evidence_digest_is_invalid(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            approvals = HumanApprovalRepository(store)
            record = self._record(store, approvals, self.prepare(store))
            self._drop_update_trigger(store, "owner_auth_evidence")
            store.connection.execute(
                "UPDATE owner_auth_evidence SET content_sha256 = ? WHERE auth_id = ?",
                ("sha256:" + "0" * 64, record.owner_auth_id),
            )
            with self.assertRaises(HumanApprovalCorruptionError):
                approvals.get_approval(record.approval_id)
            self.assertEqual(approvals.evaluate(record.approval_id).status, "invalidated")
            self.assertTrue(integrity_problems(store.connection, store.project_id))

    def test_corrupt_entry_pin_is_invalid(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            approvals = HumanApprovalRepository(store)
            record = self._record(store, approvals, self.prepare(store))
            self._drop_update_trigger(store, "human_approval_entry")
            store.connection.execute(
                "UPDATE human_approval_entry SET content_sha256 = ? "
                "WHERE approval_id = ? AND entry_index = 0",
                ("sha256:" + "0" * 64, record.approval_id),
            )
            with self.assertRaises(HumanApprovalCorruptionError):
                approvals.get_approval(record.approval_id)
            self.assertEqual(approvals.evaluate(record.approval_id).status, "invalidated")

    def test_corrupt_rationale_column_breaks_the_canonical_binding(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            approvals = HumanApprovalRepository(store)
            record = self._record(store, approvals, self.prepare(store))
            self._drop_update_trigger(store, "human_approval")
            store.connection.execute(
                "UPDATE human_approval SET rationale = ? WHERE approval_id = ?",
                ("tampered rationale", record.approval_id),
            )
            with self.assertRaises(HumanApprovalCorruptionError):
                approvals.get_approval(record.approval_id)
            self.assertEqual(approvals.evaluate(record.approval_id).status, "invalidated")

    def test_orphan_entry_row_is_detected(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            approvals = HumanApprovalRepository(store)
            self._record(store, approvals, self.prepare(store))
            conn = store.connection
            conn.execute("PRAGMA foreign_keys = OFF")
            try:
                conn.execute(
                    "INSERT INTO human_approval_entry "
                    "(project_id, approval_id, entry_index, artifact_id, revision_id, role, "
                    " content_sha256, byte_size, rights_id, rights_record_revision, "
                    " rights_digest) VALUES (?, 'hapv_orphan', 0, 'a-rep', 'rev-rep', "
                    " 'target', ?, 1, 'RIGHTS-x', 1, ?)",
                    (store.project_id, "sha256:" + "0" * 64, "sha256:" + "0" * 64),
                )
            finally:
                conn.execute("PRAGMA foreign_keys = ON")
            problems = integrity_problems(store.connection, store.project_id)
            self.assertTrue(problems)
            self.assertTrue(any("no valid parent" in p for p in problems))

    def test_orphan_evidence_without_approval_is_detected(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            approvals = HumanApprovalRepository(store)
            self._record(store, approvals, self.prepare(store))
            conn = store.connection
            # Simulate a deleted approval (and its entries) whose evidence row
            # remains: the evidence must not look like honest history on its own.
            self._drop_update_trigger(store, "human_approval")
            self._drop_update_trigger(store, "human_approval_entry")
            self._drop_delete_trigger(store, "human_approval")
            self._drop_delete_trigger(store, "human_approval_entry")
            conn.execute("DELETE FROM human_approval_entry")
            conn.execute("DELETE FROM human_approval")
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM owner_auth_evidence").fetchone()[0], 1
            )
            problems = integrity_problems(conn, store.project_id)
            self.assertTrue(
                any("no valid corresponding approval" in p for p in problems)
            )

    def test_corrupt_granted_and_created_timestamps_are_invalid(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            approvals = HumanApprovalRepository(store)
            record = self._record(store, approvals, self.prepare(store))
            self._drop_update_trigger(store, "human_approval")
            store.connection.execute(
                "UPDATE human_approval SET granted_at_utc = 'not-a-time', "
                "created_at_utc = 'not-a-time' WHERE approval_id = ?",
                (record.approval_id,),
            )
            with self.assertRaises(HumanApprovalCorruptionError):
                approvals.get_approval(record.approval_id)
            self.assertEqual(approvals.evaluate(record.approval_id).status, "invalidated")
            self.assertFalse(approvals.verify_snapshot(record.approval_id).ok)
            self.assertTrue(integrity_problems(store.connection, store.project_id))

    def test_granted_time_must_match_evidence_verified_time(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            approvals = HumanApprovalRepository(store)
            record = self._record(store, approvals, self.prepare(store))
            self._drop_update_trigger(store, "human_approval")
            # A *valid* but different instant must not be accepted as the grant.
            store.connection.execute(
                "UPDATE human_approval SET granted_at_utc = ? WHERE approval_id = ?",
                ("2000-01-01T00:00:00+00:00", record.approval_id),
            )
            with self.assertRaises(HumanApprovalCorruptionError):
                approvals.get_approval(record.approval_id)
            self.assertEqual(approvals.evaluate(record.approval_id).status, "invalidated")

    def test_equivalent_utc_offset_for_granted_time_is_accepted(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            approvals = HumanApprovalRepository(store)
            record = self._record(store, approvals, self.prepare(store))
            self._drop_update_trigger(store, "human_approval")
            # The same instant expressed with a different (equivalent) offset is a
            # legitimate historical representation, not corruption.
            instant = datetime.fromisoformat(record.granted_at_utc)
            equivalent = instant.astimezone(timezone(timedelta(hours=-5))).isoformat()
            store.connection.execute(
                "UPDATE human_approval SET granted_at_utc = ? WHERE approval_id = ?",
                (equivalent, record.approval_id),
            )
            self.assertEqual(approvals.evaluate(record.approval_id).status, "active")
            self.assertTrue(approvals.verify_snapshot(record.approval_id).ok)
            self.assertEqual(integrity_problems(store.connection, store.project_id), [])

    def test_evidence_canonical_with_extra_key_is_invalid(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            approvals = HumanApprovalRepository(store)
            record = self._record(store, approvals, self.prepare(store))
            self._rewrite_evidence(
                store,
                record.owner_auth_id,
                lambda rec: rec.__setitem__("authority", "owner_authenticated"),
            )
            with self.assertRaises(HumanApprovalCorruptionError):
                approvals.get_approval(record.approval_id)
            self.assertEqual(approvals.evaluate(record.approval_id).status, "invalidated")
            self.assertTrue(integrity_problems(store.connection, store.project_id))

    def test_evidence_method_must_be_the_approved_method(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            approvals = HumanApprovalRepository(store)
            record = self._record(store, approvals, self.prepare(store))
            self._rewrite_evidence(
                store,
                record.owner_auth_id,
                lambda rec: rec.__setitem__("method", "totp"),
                method="totp",
            )
            with self.assertRaises(HumanApprovalCorruptionError):
                approvals.get_approval(record.approval_id)
            self.assertEqual(approvals.evaluate(record.approval_id).status, "invalidated")

    def test_evidence_owner_uid_boolean_projection_is_invalid(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            approvals = HumanApprovalRepository(store)
            record = self._record(store, approvals, self.prepare(store))
            # A JSON boolean ``true`` compares equal to ``1`` in Python, so the
            # boolean must be refused explicitly rather than trusted as an int.
            self._rewrite_evidence(
                store,
                record.owner_auth_id,
                lambda rec: rec.__setitem__("owner_uid", True),
                owner_uid=1,
            )
            with self.assertRaises(HumanApprovalCorruptionError):
                approvals.get_approval(record.approval_id)
            self.assertEqual(approvals.evaluate(record.approval_id).status, "invalidated")

    def test_reapproval_evidence_pairs_are_not_flagged(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            prepared = self.prepare(store)
            self.install()
            approvals = HumanApprovalRepository(store)
            approvals.record_decision(prepared, self.mint(prepared))
            approvals.record_decision(prepared, self.mint(prepared))
            # Each approval has exactly one corresponding evidence row, so a
            # legitimate re-approval is not mistaken for an orphan/duplicate.
            self.assertEqual(integrity_problems(store.connection, store.project_id), [])

    def test_foreign_project_row_is_detected(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            approvals = HumanApprovalRepository(store)
            self._record(store, approvals, self.prepare(store))
            conn = store.connection
            foreign_pid = "prj_" + "f" * 32
            conn.execute("PRAGMA foreign_keys = OFF")
            try:
                conn.execute(
                    "INSERT INTO owner_auth_evidence "
                    "(project_id, auth_id, method, owner_uid, action_digest, "
                    " verified_at_utc, canonical, content_sha256, created_at_utc) "
                    "VALUES (?, 'auth_foreign', 'controlling_tty_challenge', 1000, ?, "
                    " ?, ?, ?, ?)",
                    (
                        foreign_pid,
                        "a" * 64,
                        "2026-10-28T00:00:00+00:00",
                        b"{}",
                        "sha256:" + "0" * 64,
                        "2026-10-28T00:00:00+00:00",
                    ),
                )
            finally:
                conn.execute("PRAGMA foreign_keys = ON")
            problems = integrity_problems(store.connection, store.project_id)
            self.assertTrue(any("foreign project" in p for p in problems))

    def test_status_and_repr_never_leak_source_content(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            approvals = HumanApprovalRepository(store)
            record = self._record(store, approvals, self.prepare(store))
            status = approvals.evaluate(record.approval_id)
            canary = _CANARY.decode()
            self.assertNotIn(canary, repr(status))
            self.assertNotIn(canary, repr(record))
            for problem in status.reasons:
                self.assertNotIn(canary, problem)


# --------------------------------------------------------------------------- #
# Reopen and migration
# --------------------------------------------------------------------------- #
class ReopenTest(HumanApprovalTestBase):
    def test_approval_survives_reopen(self) -> None:
        store = self.new_store()
        app_dir = store.app_dir
        pid = store.project_id
        self._chain(store)
        prepared = self.prepare(store)
        self.install()
        approvals = HumanApprovalRepository(store)
        record = approvals.record_decision(prepared, self.mint(prepared))
        store.close()

        reopened = ProjectStore.open_at(app_dir, expected_project_id=pid)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.schema_version, schema.STORE_SCHEMA_VERSION)
        reopened_approvals = HumanApprovalRepository(reopened)
        stored = reopened_approvals.get_approval(record.approval_id)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.snapshot_digest, record.snapshot_digest)
        self.assertEqual(
            reopened_approvals.evaluate(record.approval_id).status, "active"
        )
        self.assertEqual(
            integrity_problems(reopened.connection, pid), []
        )


class MigrationTest(HumanApprovalTestBase):
    def test_migration_from_prior_schema_preserves_existing_rows(self) -> None:
        prior = schema.STORE_SCHEMA_VERSION - 1
        root = str(self.tmp / "p7")
        app_dir = paths.app_dir_for(root)
        db_path = paths.store_path_for(root)
        paths.ensure_owner_private_dir(app_dir)
        pid = paths.new_project_id()
        conn = schema.open_connection(db_path)
        try:
            schema.migrate(
                conn,
                target_version=prior,
                migrations=schema.STORE_MIGRATIONS,
                label="project store",
            )
            self.assertEqual(schema.read_user_version(conn), prior)
            conn.execute(
                "INSERT INTO project (project_id, name, created_at_utc, app_version) "
                "VALUES (?, 'prior', '2026-01-01T00:00:00+00:00', '0.0.0')",
                (pid,),
            )
            conn.execute(
                "INSERT INTO artifact (project_id, artifact_id, kind, created_at_utc, "
                " created_by, label, status) VALUES (?, 'a-doc', 'document', "
                " '2026-01-01T00:00:00+00:00', 'import', NULL, 'active')",
                (pid,),
            )
            canonical = canonical_json(_rights(pid, "a-doc", "rev-1"))
            conn.execute(
                "INSERT INTO rights_record (project_id, rights_id, record_revision, "
                " subject_kind, subject_project_id, subject_artifact_id, "
                " subject_revision_id, canonical, content_sha256, recorded_at_utc) "
                "VALUES (?, 'RIGHTS-rev-1', 1, 'source_revision', ?, 'a-doc', 'rev-1', "
                " ?, ?, '2026-01-01T00:00:00+00:00')",
                (pid, pid, canonical, sha256_digest(canonical)),
            )
            content = b"doc-bytes"
            conn.execute(
                "INSERT INTO revision (project_id, artifact_id, revision_id, "
                " parent_revision_id, content, content_sha256, byte_size, media_type, "
                " origin_locator, acquired_at_utc, acquired_by, provenance_method, "
                " provenance_actor, provenance_performed_at_utc, rights_project_id, "
                " rights_id, rights_record_revision) VALUES (?, 'a-doc', 'rev-1', NULL, "
                " ?, ?, ?, NULL, 'import:synthetic', '2026-01-01T00:00:00+00:00', "
                " 'import', 'import', 'user', '2026-01-01T00:00:00+00:00', ?, "
                " 'RIGHTS-rev-1', 1)",
                (pid, content, sha256_digest(content), len(content), pid),
            )
        finally:
            conn.close()

        reopened = ProjectStore.open_at(app_dir, expected_project_id=pid)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.schema_version, schema.STORE_SCHEMA_VERSION)
        names = {
            row[0]
            for row in reopened.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertTrue(
            {"owner_auth_evidence", "human_approval", "human_approval_entry"} <= names
        )
        # Prior rows and bytes are untouched.
        self.assertEqual(
            ArtifactRepository(reopened).read_content("a-doc", "rev-1"), b"doc-bytes"
        )
        self.assertEqual(integrity_problems(reopened.connection, pid), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
