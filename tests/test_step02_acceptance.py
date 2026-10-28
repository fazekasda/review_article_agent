"""Step 02 acceptance: WAL write inside the real SQLite backup API, and an exact,
non-authorizing restore history.

Authority: ``docs/implementation/02-project-foundation.md`` acceptance lines
AC5 (restore a consistent snapshot and verify artifact hashes and approvals,
*including while writes were occurring during backup*; do not copy only the live
database file and ignore WAL state) and AC4 (immutable historical approvals;
an approved artifact edited afterward stays honest history and does not silently
re-authorize), and ``ARCH-MIGRATE`` (``docs/contracts/03-policy-decisions.md``)
as recorded in ``docs/storage.md``.

What this proves that a naive before/after test cannot
------------------------------------------------------
The earlier ``test_backup.py::ConcurrentWriteTest`` timestamps a write that
*starts* around ``create_backup`` and could in principle commit after the backup
already finished. Here the concurrent writer is triggered from **inside the real
``sqlite3.Connection.backup`` progress callback** performed by the production
``create_backup`` path, and we require the writer to **commit before the actual
``backup()`` call returns**. That is direct evidence the online backup observed
a real overlapping WAL write, not merely two events in the same wall-clock window.

Self-contained: this module defines its own synthetic, fully valid ``RightsRecord``
helper (it does **not** import private helpers from other test modules). Synthetic
data only; no secrets, no licensed source text, and no paid/service calls.

Run with::

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_step02_acceptance.py'
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
import threading
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.storage import (  # noqa: E402
    ApprovalRepository,
    ArtifactRepository,
    ProjectStore,
    Registry,
)
from review_article_agent.storage import paths  # noqa: E402
from review_article_agent.storage.backup import (  # noqa: E402
    create_backup,
    restore_backup,
    verify_backup,
)

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")

#: Deterministic synthetic source bytes (never random, so failures reproduce).
_BLOB_R1 = b"A" * 2_000_000
_BLOB_R2 = b"B" * 256_000

#: Pages per step for ``sqlite3.Connection.backup``: small enough that a ~2 MB
#: database yields many progress callbacks, so a callback fires while the backup
#: is genuinely still copying.
_PAGES = 16

#: Bounded wait for cross-thread events (never a busy poll / sleep loop).
_BOUND = 15.0


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _rights(project_id: str, artifact_id: str, revision_id: str) -> dict:
    """A synthetic, fully valid RightsRecord v1 bound to one exact revision.

    Local to this module on purpose: the acceptance test must not depend on a
    private helper in another test file.
    """
    return {
        "rights_id": f"RIGHTS-{revision_id}",
        "project_id": project_id,
        "record_revision": 1,
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


class _ProgressBackupConnection:
    """Proxy over a live connection whose ``backup`` runs the **real** API.

    Delegates every other attribute (including ``in_transaction``) to the real
    connection via ``__getattr__`` so the production snapshot path sees an
    unchanged connection surface; only ``backup`` is wrapped so a progress
    callback can rendezvous with the concurrent writer.
    """

    def __init__(self, real, *, on_first_open_page, state):
        self._real = real
        self._on_first_open_page = on_first_open_page
        self._state = state

    def __getattr__(self, name):  # pragma: no cover - thin delegation
        return getattr(self._real, name)

    def backup(self, dest):
        state = self._state

        def progress(status, remaining, total):
            state["callbacks"] += 1
            if remaining > 0 and not state["writer_signalled"]:
                state["writer_signalled"] = True
                state["remaining_at_trigger"] = remaining
                state["total_at_trigger"] = total
                state["backup_returned_at_trigger"] = state["backup_returned"]
                self._on_first_open_page()
                # Bounded wait: the writer must COMMIT while this real backup
                # call is still in progress (still inside this callback).
                if state["writer_done"].wait(_BOUND):
                    state["writer_finished_inside_callback"] = True

        result = self._real.backup(dest, pages=_PAGES, progress=progress)
        state["backup_returned"] = True
        return result


class Step02AcceptanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-step02-accept-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    # -- fixtures ------------------------------------------------------------ #
    def _new_store(self, name: str) -> ProjectStore:
        store = ProjectStore.create_at(
            paths.app_dir_for(str(self.tmp / name)), paths.new_project_id()
        )
        self.addCleanup(store.close)
        return store

    def _import(self, repo: ArtifactRepository, artifact_id: str, content: bytes, revision_id: str, parent=None):
        return repo.import_revision(
            artifact_id=artifact_id,
            content=content,
            origin_locator="import:synthetic",
            rights=_rights(repo.project_id, artifact_id, revision_id),
            revision_id=revision_id,
            parent_revision_id=parent,
        )

    # -- the decisive concurrency proof -------------------------------------- #
    def test_committed_wal_write_lands_inside_real_sqlite_backup(self) -> None:
        store = self._new_store("source")
        project_id = store.project_id
        app_dir = store.app_dir
        repo = ArtifactRepository(store)
        repo.create_artifact("a", kind="document")
        self._import(repo, "a", _BLOB_R1, revision_id="r1")
        approvals = ApprovalRepository(store)
        approval = approvals.record_fixture_approval(
            target={"project_id": project_id, "artifact_id": "a", "revision_id": "r1"},
            gate_kind="fixture_artifact_revision",
            gate_id="gate-concurrency",
            decision="approved",
            rationale="synthetic local storage fixture; not a scientific approval",
        )

        start = threading.Event()   # backup -> writer: begin your commit
        done = threading.Event()    # writer -> backup: commit finished
        errors: list[BaseException] = []
        state = {
            "callbacks": 0,
            "writer_signalled": False,
            "writer_finished_inside_callback": False,
            "backup_returned": False,
            "backup_returned_at_trigger": None,
            "remaining_at_trigger": None,
            "total_at_trigger": None,
            "writer_done": done,
        }

        def writer() -> None:
            wstore = None
            try:
                if not start.wait(_BOUND):
                    errors.append(AssertionError("backup never signalled the writer"))
                    return
                wstore = ProjectStore.open_at(app_dir, expected_project_id=project_id)
                self._import(ArtifactRepository(wstore), "a", _BLOB_R2, revision_id="r2", parent="r1")
            except BaseException as exc:  # surfaced via `errors` below
                errors.append(exc)
            finally:
                if wstore is not None:
                    wstore.close()
                done.set()

        thread = threading.Thread(target=writer, name="raa-wal-writer")
        thread.start()
        real = store.connection
        proxy = _ProgressBackupConnection(real, on_first_open_page=start.set, state=state)
        # The proxy delegates ordinary connection attributes (e.g. the production
        # snapshot path / callers may read ``in_transaction``) via __getattr__.
        self.assertEqual(proxy.in_transaction, real.in_transaction)
        store._conn = proxy  # create_backup reads store.connection -> proxy
        try:
            create_backup(store, self.tmp / "snapshot")
        finally:
            # Release both sides so a failure never leaves a blocked thread, then
            # join with a bound (no busy polling).
            start.set()
            done.set()
            thread.join(_BOUND)

        self.assertFalse(thread.is_alive(), "writer thread did not finish")
        self.assertEqual(errors, [])
        self.assertFalse(real.in_transaction)

        # Direct evidence of real overlap: the writer committed during a live
        # progress callback, before the actual backup() call returned.
        self.assertGreaterEqual(state["callbacks"], 1)
        self.assertIsNotNone(state["remaining_at_trigger"])
        self.assertGreater(state["remaining_at_trigger"], 0)
        self.assertIs(state["backup_returned_at_trigger"], False)
        self.assertTrue(state["writer_finished_inside_callback"], state)
        self.assertTrue(state["backup_returned"])

        # Both the pre-existing r1 and the concurrent r2 are captured.
        report = verify_backup(self.tmp / "snapshot")
        self.assertTrue(report["ok"])
        self.assertEqual(report["project_id"], project_id)
        self.assertEqual(report["counts"]["revisions"], 2)
        self.assertEqual(report["counts"]["rights_records"], 2)
        self.assertEqual(report["counts"]["approvals"], 1)
        self.assertTrue(str(report["database_sha256"]).startswith("sha256:"))

        registry = Registry(self.tmp / "registry")
        info = restore_backup(self.tmp / "snapshot", self.tmp / "restored", registry)
        self.assertEqual(info.project_id, project_id)
        restored = ProjectStore.open_at(
            paths.app_dir_for(info.root_path), expected_project_id=project_id
        )
        self.addCleanup(restored.close)
        rrepo = ArtifactRepository(restored)
        self.assertEqual(rrepo.read_content("a", "r1"), _BLOB_R1)
        self.assertEqual(rrepo.read_content("a", "r2"), _BLOB_R2)
        r1 = next(r for r in rrepo.list_revisions("a") if r.revision_id == "r1")
        self.assertEqual(r1.content_sha256, _sha(_BLOB_R1))
        self.assertEqual(r1.byte_size, len(_BLOB_R1))
        # The restored fixture approval is structurally valid, is honest history
        # (r2 superseded r1), and never authorizes execution.
        rapprovals = ApprovalRepository(restored)
        self.assertTrue(rapprovals.verify_snapshot(approval.approval_id).ok)
        status = rapprovals.evaluate(approval.approval_id)
        self.assertEqual(status.status, "stale")
        self.assertFalse(status.authorizes_execution)
        self.assertFalse(status.authorizes_export)

    # -- exact restore history is non-authorizing ---------------------------- #
    def test_backup_restore_keeps_exact_old_revision_and_non_authorizing_decision(self) -> None:
        store = self._new_store("source2")
        project_id = store.project_id
        repo = ArtifactRepository(store)
        repo.create_artifact("a", kind="document")
        self._import(repo, "a", b"doc-v1", revision_id="rev-1")
        approvals = ApprovalRepository(store)
        approval = approvals.record_fixture_approval(
            target={"project_id": project_id, "artifact_id": "a", "revision_id": "rev-1"},
            gate_kind="fixture_artifact_revision",
            gate_id="gate-history",
            decision="approved",
            rationale="synthetic local storage fixture; not a scientific approval",
        )
        summary = create_backup(store, self.tmp / "snapshot")
        self.assertEqual(summary["counts"]["revisions"], 1)
        self.assertEqual(summary["counts"]["approvals"], 1)

        # A *valid* later edit: a new immutable revision derived from rev-1.
        self._import(repo, "a", b"doc-v2", revision_id="rev-2", parent="rev-1")

        # Source-side: the old revision is untouched; the fixture decision is
        # now stale history and still authorizes nothing.
        self.assertEqual(repo.read_content("a", "rev-1"), b"doc-v1")
        self.assertEqual(approvals.evaluate(approval.approval_id).status, "stale")
        self.assertFalse(approvals.evaluate(approval.approval_id).authorizes_execution)

        registry = Registry(self.tmp / "registry2")
        info = restore_backup(self.tmp / "snapshot", self.tmp / "restored2", registry)
        self.assertEqual(info.project_id, project_id)
        restored = ProjectStore.open_at(
            paths.app_dir_for(info.root_path), expected_project_id=project_id
        )
        self.addCleanup(restored.close)
        rrepo = ArtifactRepository(restored)
        # The snapshot is the exact pre-edit state: only rev-1, byte-identical.
        revisions = rrepo.list_revisions("a")
        self.assertEqual([r.revision_id for r in revisions], ["rev-1"])
        self.assertEqual(rrepo.read_content("a", "rev-1"), b"doc-v1")
        self.assertEqual(revisions[0].content_sha256, _sha(b"doc-v1"))
        self.assertEqual(revisions[0].byte_size, len(b"doc-v1"))

        rapprovals = ApprovalRepository(restored)
        self.assertTrue(rapprovals.verify_snapshot(approval.approval_id).ok)
        restored_status = rapprovals.evaluate(approval.approval_id)
        # r1 is the restored head, so the decision is active but still not an
        # authorization: this API grants nothing.
        self.assertEqual(restored_status.status, "active")
        self.assertFalse(restored_status.authorizes_execution)
        self.assertFalse(restored_status.authorizes_export)

        report = verify_backup(self.tmp / "snapshot")
        self.assertEqual(report["counts"]["revisions"], 1)
        self.assertEqual(report["counts"]["rights_records"], 1)
        self.assertEqual(report["counts"]["approvals"], 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
