"""Focused regression tests for the gate-snapshot revision-closure bug (S19-GATEFIX).

A live input is a node reachable from the target through a path made
**exclusively** of non-ancestry (live-input) edges. Before this fix both the
runtime closure (:func:`review_article_agent.runtime.gates._exact_closure`) and
the storage-fixture closure
(:meth:`review_article_agent.storage.approvals.ApprovalRepository._build_snapshot`)
marked a node live whenever *any* edge reaching it was non-ancestry -- including
the historical inputs of a ``version_of`` ancestor. That made a superseded
ancestor source (e.g. ``brief-r1``) a "live" pin and head-compared it against its
artifact's current head (``brief-r3``), so any artifact revised after its sources
were revised could never be gated again (``GateSupersededInputError``).

These tests pin the corrected semantics:

* a node reachable only through an ancestry edge (including an ancestor's own
  inputs) is historical lineage: pinned for integrity, never head-compared;
* a genuinely stale *live* input is still refused;
* "live wins" over ancestry;
* a previously recorded fixture approval whose stored snapshot used the old
  classification still verifies and evaluates exactly as recorded (role changes
  do not invalidate stored snapshots -- verification reconstructs from stored
  rows);
* end-to-end through the CLI (in-process) on a twice-revised hosted-use batch
  with twice-revised sources.

Synthetic data only; stdlib only. Run::

    PYTHONPATH=src python3 -m unittest discover -s tests \
        -p 'test_gate_snapshot_revision_closure.py'
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.cli import main as climain  # noqa: E402
from review_article_agent.domain.records import canonical_json  # noqa: E402
from review_article_agent.runtime.gates import (  # noqa: E402
    GateSupersededInputError,
    prepare_gate_snapshot,
    revalidate_prepared,
)
from review_article_agent.storage import ArtifactRepository, ProjectStore, paths  # noqa: E402
from review_article_agent.storage.approvals import ApprovalRepository  # noqa: E402

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")
_GATE_KW = dict(gate_kind="artifact_revision", gate_id="gate-1", decision="approved")


def _rights(project_id: str, artifact_id: str, revision_id: str) -> dict:
    """A synthetic, fully valid RightsRecord v1 bound to one exact revision."""
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


def _envelope(text: str = "synthetic") -> dict:
    return {"schema_version": 1, "document_type": "fixture_note", "payload": {"t": text}}


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-s19-gatefix-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def new_store(self, name: str = "p") -> ProjectStore:
        store = ProjectStore.create_at(
            paths.app_dir_for(str(self.tmp / name)), paths.new_project_id()
        )
        self.addCleanup(store.close)
        return store

    def _import(
        self,
        repo: ArtifactRepository,
        artifact_id: str,
        content: bytes,
        revision_id: str,
        *,
        dependencies=(),
        parent_revision_id: str | None = None,
    ):
        return repo.import_revision(
            artifact_id=artifact_id,
            content=content,
            origin_locator="import:synthetic",
            rights=_rights(repo.project_id, artifact_id, revision_id),
            revision_id=revision_id,
            dependencies=list(dependencies),
            parent_revision_id=parent_revision_id,
        )

    def _target(self, store, artifact_id: str, revision_id: str) -> dict:
        return {
            "project_id": store.project_id,
            "artifact_id": artifact_id,
            "revision_id": revision_id,
        }


# --------------------------------------------------------------------------- #
# 1. Regression: a revised ancestor's superseded sources are lineage, not live.
# --------------------------------------------------------------------------- #
class RevisedAncestorClosureTest(_Base):
    def _pilot_scenario(self, store) -> ArtifactRepository:
        """brief/interview r1 -> r3; batch r1 -> r2 -> r3 (pilot shape)."""
        repo = ArtifactRepository(store)
        pid = store.project_id
        repo.create_artifact("brief", kind="document")
        repo.create_artifact("interview", kind="document")
        repo.create_artifact("batch", kind="report")

        self._import(repo, "brief", b"b1", "brief-r1")
        self._import(repo, "interview", b"i1", "interview-r1")
        self._import(
            repo,
            "batch",
            b"x1",
            "batch-r1",
            dependencies=[
                self._target(store, "brief", "brief-r1"),
                self._target(store, "interview", "interview-r1"),
            ],
        )
        # sources revised: r1 -> r3
        self._import(repo, "brief", b"b3", "brief-r3", parent_revision_id="brief-r1")
        self._import(
            repo, "interview", b"i3", "interview-r3", parent_revision_id="interview-r1"
        )
        # amendment batch-r2: live sources at head + own version_of ancestry
        self._import(
            repo,
            "batch",
            b"x2",
            "batch-r2",
            parent_revision_id="batch-r1",
            dependencies=[
                self._target(store, "brief", "brief-r3"),
                self._target(store, "interview", "interview-r3"),
                {
                    "project_id": pid,
                    "artifact_id": "batch",
                    "revision_id": "batch-r1",
                    "role": "version_of",
                },
            ],
        )
        return repo

    def test_revised_ancestor_sources_are_lineage_and_gate_prepares(self) -> None:
        with self.new_store() as store:
            self._pilot_scenario(store)
            prepared = prepare_gate_snapshot(
                store,
                self._target(store, "batch", "batch-r2"),
                rationale="r",
                **_GATE_KW,
            )
            roles = {e.revision_id: e.role for e in prepared.entries}
            self.assertEqual(roles["batch-r2"], "target")
            self.assertEqual(roles["batch-r1"], "version_lineage")
            # The superseded ancestor sources are pinned but classified lineage.
            self.assertEqual(roles["brief-r1"], "version_lineage")
            self.assertEqual(roles["interview-r1"], "version_lineage")
            # The live sources (current heads) are live inputs.
            self.assertEqual(roles["brief-r3"], "source_document")
            self.assertEqual(roles["interview-r3"], "source_document")
            # Lineage pins keep their exact content + rights integrity pins.
            for entry in prepared.entries:
                self.assertTrue(entry.rights_digest.startswith("sha256:"))
                self.assertTrue(entry.content_sha256.startswith("sha256:"))
            # The prepared snapshot revalidates unchanged.
            self.assertEqual(revalidate_prepared(store, prepared).status, "unchanged")


# --------------------------------------------------------------------------- #
# 2. A genuinely stale LIVE input is still refused.
# --------------------------------------------------------------------------- #
class SupersededLiveInputTest(_Base):
    def test_superseded_live_input_still_refused(self) -> None:
        with self.new_store() as store:
            repo = ArtifactRepository(store)
            repo.create_artifact("src", kind="document")
            repo.create_artifact("tgt", kind="report")
            self._import(repo, "src", b"s1", "s-r1")
            self._import(repo, "src", b"s2", "s-r2", parent_revision_id="s-r1")
            # The target's own derived_from still points at the superseded s-r1.
            self._import(
                repo,
                "tgt",
                b"t1",
                "t-r1",
                dependencies=[self._target(store, "src", "s-r1")],
            )
            with self.assertRaises(GateSupersededInputError):
                prepare_gate_snapshot(
                    store,
                    self._target(store, "tgt", "t-r1"),
                    rationale="r",
                    **_GATE_KW,
                )


# --------------------------------------------------------------------------- #
# 3. "Live wins" over ancestry: a both-ways node is live and head-compared.
# --------------------------------------------------------------------------- #
class LiveWinsTest(_Base):
    def test_live_edge_over_ancestry_is_head_compared(self) -> None:
        with self.new_store() as store:
            repo = ArtifactRepository(store)
            pid = store.project_id
            repo.create_artifact("chain", kind="report")
            self._import(repo, "chain", b"v1", "rev-1")
            # rev-1 is reachable through BOTH a version_of ancestry edge and a
            # derived_from live edge: the live edge wins, so rev-1 is
            # head-compared and (being older than the rev-2 head) blocked.
            self._import(
                repo,
                "chain",
                b"v2",
                "rev-2",
                parent_revision_id="rev-1",
                dependencies=[
                    {
                        "project_id": pid,
                        "artifact_id": "chain",
                        "revision_id": "rev-1",
                        "role": "version_of",
                    },
                    {
                        "project_id": pid,
                        "artifact_id": "chain",
                        "revision_id": "rev-1",
                        "role": "derived_from",
                    },
                ],
            )
            with self.assertRaises(GateSupersededInputError):
                prepare_gate_snapshot(
                    store,
                    self._target(store, "chain", "rev-2"),
                    rationale="r",
                    **_GATE_KW,
                )

    def test_lineage_only_ancestor_is_not_head_compared(self) -> None:
        # Control: with only the version_of edge, rev-1 is lineage and prepares.
        with self.new_store() as store:
            repo = ArtifactRepository(store)
            pid = store.project_id
            repo.create_artifact("chain", kind="report")
            self._import(repo, "chain", b"v1", "rev-1")
            self._import(
                repo,
                "chain",
                b"v2",
                "rev-2",
                parent_revision_id="rev-1",
                dependencies=[
                    {
                        "project_id": pid,
                        "artifact_id": "chain",
                        "revision_id": "rev-1",
                        "role": "version_of",
                    }
                ],
            )
            prepared = prepare_gate_snapshot(
                store, self._target(store, "chain", "rev-2"), rationale="r", **_GATE_KW
            )
            self.assertEqual(
                [(e.revision_id, e.role) for e in prepared.entries],
                [("rev-2", "target"), ("rev-1", "version_lineage")],
            )


# --------------------------------------------------------------------------- #
# 4. A pre-fix recorded fixture approval still verifies/evaluates as recorded.
# --------------------------------------------------------------------------- #
class PreFixRecordedApprovalTest(_Base):
    def _rewrite_entry_role(
        self, store, approval_id: str, revision_id: str, new_role: str
    ) -> None:
        """Simulate a pre-fix stored snapshot: reclassify one entry's role.

        The stored snapshot is immutable by trigger, so the test-only trigger is
        dropped (as the corruption helpers elsewhere do), the entry role is
        changed, and the stored digest is recomputed from the reconstructed
        structure so the row is internally consistent -- exactly as it would be
        had it been recorded by the old closure.
        """
        store.connection.execute(
            "DROP TRIGGER IF EXISTS fixture_approval_entry_no_update"
        )
        store.connection.execute("DROP TRIGGER IF EXISTS fixture_approval_no_update")
        store.connection.execute(
            "UPDATE fixture_approval_entry SET role = ? WHERE project_id = ? "
            "AND approval_id = ? AND revision_id = ?",
            (new_role, store.project_id, approval_id, revision_id),
        )
        approvals = ApprovalRepository(store)
        structure = approvals.snapshot_structure(approval_id)
        new_digest = "sha256:" + hashlib.sha256(canonical_json(structure)).hexdigest()
        store.connection.execute(
            "UPDATE fixture_approval SET snapshot_digest = ? WHERE project_id = ? "
            "AND approval_id = ?",
            (new_digest, store.project_id, approval_id),
        )

    def test_pre_fix_snapshot_still_verifies_and_evaluates(self) -> None:
        with self.new_store() as store:
            repo = ArtifactRepository(store)
            pid = store.project_id
            repo.create_artifact("doc", kind="document")
            self._import(repo, "doc", b"doc", "doc-r1")
            repo.create_artifact("rep", kind="report")
            self._import(
                repo,
                "rep",
                b"r1",
                "rep-r1",
                dependencies=[self._target(store, "doc", "doc-r1")],
            )
            self._import(
                repo,
                "rep",
                b"r2",
                "rep-r2",
                parent_revision_id="rep-r1",
                dependencies=[
                    {
                        "project_id": pid,
                        "artifact_id": "rep",
                        "revision_id": "rep-r1",
                        "role": "version_of",
                    }
                ],
            )
            approvals = ApprovalRepository(store)
            appr = approvals.record_fixture_approval(
                target=self._target(store, "rep", "rep-r2"),
                gate_kind="fixture_review",
                gate_id="g",
                decision="approved",
                rationale="r",
            )
            # The new closure classifies the historical source as lineage...
            roles = {
                e.revision_id: e.role for e in approvals.snapshot_entries(appr.approval_id)
            }
            self.assertEqual(roles["doc-r1"], "version_lineage")
            # ...but a snapshot recorded *before* the fix stored it as a live
            # source_document. Rewriting the stored role (and its digest) must not
            # invalidate the recorded snapshot: verification reconstructs from the
            # stored rows, not by re-deriving the closure.
            self._rewrite_entry_role(store, appr.approval_id, "doc-r1", "source_document")
            self.assertTrue(approvals.verify_snapshot(appr.approval_id).ok)
            # It evaluates exactly as recorded: the stored source is head-compared.
            self.assertEqual(approvals.evaluate(appr.approval_id).status, "active")
            self._import(repo, "doc", b"doc-v2", "doc-r2", parent_revision_id="doc-r1")
            self.assertEqual(approvals.evaluate(appr.approval_id).status, "stale")


# --------------------------------------------------------------------------- #
# 5. End-to-end through the CLI (in-process) on a twice-revised batch.
# --------------------------------------------------------------------------- #
class CliRevisionClosureTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-s19-gatefix-cli-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.state = self.tmp / "state"

    def _run(self, *args: str) -> dict:
        out, err = io.StringIO(), io.StringIO()
        rc = climain.main(list(args), stdout=out, stderr=err)
        self.assertEqual(
            rc, 0, f"args={args}\nstdout={out.getvalue()}\nstderr={err.getvalue()}"
        )
        return json.loads(out.getvalue())

    def _write_json(self, name: str, data: object) -> Path:
        path = self.tmp / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def _create_project(self) -> str:
        out = self._run(
            "--data-dir", str(self.state), "create", str(self.tmp / "proj"),
            "--name", "proj",
        )
        return out["project"]["project_id"]

    def _artifact_create(self, pid: str, artifact_id: str, kind: str) -> None:
        self._run(
            "--data-dir", str(self.state), "artifact", "create",
            "--artifact-id", artifact_id, "--kind", kind, "--project", pid,
        )

    def _import(
        self, pid: str, artifact_id: str, revision_id: str, text: str,
        *, parent: str | None = None, deps=None,
    ) -> None:
        content = self._write_json(f"c-{revision_id}.json", _envelope(text))
        rights = self._write_json(f"r-{revision_id}.json", _rights(pid, artifact_id, revision_id))
        args = [
            "--data-dir", str(self.state), "artifact", "import",
            "--artifact-id", artifact_id, "--revision-id", revision_id,
            "--file", str(content), "--rights-file", str(rights), "--project", pid,
        ]
        if parent is not None:
            args += ["--parent", parent]
        if deps is not None:
            args += ["--deps-file", str(self._write_json(f"d-{revision_id}.json", deps))]
        self._run(*args)

    def test_gate_prepare_on_twice_revised_batch(self) -> None:
        pid = self._create_project()
        for artifact_id, kind in (
            ("brief", "document"),
            ("interview", "document"),
            ("batch", "report"),
        ):
            self._artifact_create(pid, artifact_id, kind)

        self._import(pid, "brief", "brief-r1", "b1")
        self._import(pid, "interview", "interview-r1", "i1")
        self._import(
            pid, "batch", "batch-r1", "x1",
            deps=[
                {"project_id": pid, "artifact_id": "brief", "revision_id": "brief-r1"},
                {"project_id": pid, "artifact_id": "interview", "revision_id": "interview-r1"},
            ],
        )
        # sources revised twice: r1 -> r2 -> r3
        self._import(pid, "brief", "brief-r2", "b2", parent="brief-r1")
        self._import(pid, "brief", "brief-r3", "b3", parent="brief-r2")
        self._import(pid, "interview", "interview-r2", "i2", parent="interview-r1")
        self._import(pid, "interview", "interview-r3", "i3", parent="interview-r2")
        # batch revised twice: r1 -> r2 -> r3
        self._import(
            pid, "batch", "batch-r2", "x2", parent="batch-r1",
            deps=[
                {"project_id": pid, "artifact_id": "brief", "revision_id": "brief-r2"},
                {"project_id": pid, "artifact_id": "interview", "revision_id": "interview-r2"},
                {"project_id": pid, "artifact_id": "batch", "revision_id": "batch-r1", "role": "version_of"},
            ],
        )
        self._import(
            pid, "batch", "batch-r3", "x3", parent="batch-r2",
            deps=[
                {"project_id": pid, "artifact_id": "brief", "revision_id": "brief-r3"},
                {"project_id": pid, "artifact_id": "interview", "revision_id": "interview-r3"},
                {"project_id": pid, "artifact_id": "batch", "revision_id": "batch-r2", "role": "version_of"},
            ],
        )

        out = self._run(
            "--data-dir", str(self.state), "gate", "prepare",
            "--project", pid, "--artifact-id", "batch", "--revision-id", "batch-r3",
            "--gate-kind", "artifact_revision", "--gate-id", "g1",
            "--decision", "approved", "--rationale", "synthetic human review",
        )
        gate = out["gate"]
        roles = {d["revision_id"]: d["role"] for d in gate["dependencies"]}
        self.assertEqual(roles["batch-r3"], "target")
        self.assertEqual(roles["batch-r2"], "version_lineage")
        self.assertEqual(roles["batch-r1"], "version_lineage")
        self.assertEqual(roles["brief-r3"], "source_document")
        self.assertEqual(roles["brief-r2"], "version_lineage")
        self.assertEqual(roles["brief-r1"], "version_lineage")
        # Read-only preview: records nothing, authorizes nothing.
        self.assertFalse(gate["approval_recorded"])
        self.assertFalse(gate["authorizes_execution"])
        self.assertFalse(gate["authorizes_export"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
