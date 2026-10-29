"""Tests for the read-only source-revision dependency-impact report (Step 06 D1).

Real local store, synthetic data only. Run with::

    PYTHONPATH=src python3 -m unittest discover -s tests -p test_acquisition_impact.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.acquisition.impact import source_impact  # noqa: E402
from review_article_agent.domain.records import RecordValidationError  # noqa: E402
from review_article_agent.runtime import owner_auth  # noqa: E402
from review_article_agent.runtime.gates import prepare_gate_snapshot  # noqa: E402
from review_article_agent.storage.human_approvals import HumanApprovalRepository  # noqa: E402
from review_article_agent.storage import (  # noqa: E402
    ApprovalRepository,
    ArtifactRepository,
    ProjectStore,
    paths,
)

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")
_SECRET = b"SECRET-SOURCE-TEXT-7731"


def _rights(project_id, artifact_id, revision_id):
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


class ImpactBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-impact-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.store = self.new_store("p")
        self.repo = ArtifactRepository(self.store)
        self.approvals = ApprovalRepository(self.store)
        self.pid = self.store.project_id

    def new_store(self, name):
        store = ProjectStore.create_at(
            paths.app_dir_for(str(self.tmp / name)), paths.new_project_id()
        )
        self.addCleanup(store.close)
        return store

    def add(self, kind, content, rid, *, aid=None, parent=None, deps=None, repo=None):
        repo = repo or self.repo
        if aid is None:
            aid = repo.create_artifact(kind=kind, label="TITLE-CANARY").artifact_id
        repo.import_revision(
            artifact_id=aid,
            content=content,
            origin_locator="import:synthetic",
            rights=_rights(repo.project_id, aid, rid),
            revision_id=rid,
            parent_revision_id=parent,
            dependencies=deps or [],
        )
        return aid

    def ref(self, aid, rid, role=None):
        r = {"project_id": self.pid, "artifact_id": aid, "revision_id": rid}
        if role:
            r["role"] = role
        return r

    def approve(self, aid, rid, gate_id):
        return self.approvals.record_fixture_approval(
            target=self.ref(aid, rid),
            gate_kind="fixture_artifact_revision",
            gate_id=gate_id,
            decision="approved",
            rationale="synthetic fixture approval",
        )

    def build(self):
        """S(rev s1) <- A <- B ; C unrelated approved."""
        s = self.add("document", _SECRET, "s1")
        a = self.add("document", b"a", "a1", deps=[self.ref(s, "s1", "derived_from")])
        b = self.add("document", b"b", "b1", deps=[self.ref(a, "a1", "derived_from")])
        c = self.add("document", b"c", "c1")
        return s, a, b, c

    def dump(self):
        return list(self.store.connection.iterdump())


class ImpactTest(ImpactBase):
    def test_old_source_dependent_goes_stale_and_is_flagged(self) -> None:
        s, a, b, c = self.build()
        appr_a = self.approve(a, "a1", "g-a")
        appr_c = self.approve(c, "c1", "g-c")
        self.assertEqual(self.approvals.evaluate(appr_a.approval_id).status, "active")
        self.add("document", b"s2", "s2", aid=s, parent="s1")
        before = self.dump()
        report = source_impact(self.store, self.ref(s, "s2"), approvals=self.approvals)
        self.assertEqual(self.dump(), before)  # no writes
        json.dumps(report)  # JSON-safe
        self.assertTrue(report["claims_change"])
        self.assertEqual(report["changed_source"], self.ref(s, "s2"))
        self.assertEqual(
            [(e["ref"]["artifact_id"], e["relation"]) for e in report["affected"]],
            [(a, "direct"), (b, "transitive")],
        )
        a_entry = report["affected"][0]
        self.assertEqual(a_entry["approvals"][0]["status"], "stale")
        self.assertEqual(
            self.approvals.evaluate(appr_a.approval_id).status, "stale"
        )  # generic freshness is the authority and agrees
        self.assertEqual(self.approvals.evaluate(appr_c.approval_id).status, "active")
        self.assertEqual(report["unaffected"], [self.ref(c, "c1")])
        self.assertEqual(report["unaffected_count"], 1)
        self.assertEqual(report["affected_count"], 2)
        self.assertTrue(report["historical_work_retained"])
        self.assertFalse(report["authorizing"])
        self.assertFalse(report["relatedness_inferred"])
        text = json.dumps(report)
        self.assertNotIn("SECRET-SOURCE-TEXT", text)
        self.assertNotIn("TITLE-CANARY", text)
        # earlier work still present
        self.assertEqual(self.repo.read_content(s, "s1"), _SECRET)
        self.assertIsNotNone(self.approvals.get_approval(appr_a.approval_id))

    def test_historical_dependent_revision_not_flagged(self) -> None:
        s, a, _b, _c = self.build()
        self.add("document", b"a2", "a2", aid=a, parent="a1",
                 deps=[self.ref(a, "a1", "version_of")])
        self.add("document", b"s2", "s2", aid=s, parent="s1")
        report = source_impact(self.store, self.ref(s, "s2"))
        # a's head no longer uses s1 live; only its historical a1 does.
        self.assertEqual(report["affected"], [])
        self.assertEqual(report["affected_count"], 0)

    def test_new_unrelated_source_flags_nothing_and_lineage_not_stale(self) -> None:
        s, a, b, c = self.build()
        c2 = self.add("document", b"c2", "c2", aid=c, parent="c1",
                      deps=[self.ref(c, "c1", "version_of")])
        appr = self.approve(c, "c2", "g-c")
        new = self.add("document", b"new", "n1")
        report = source_impact(self.store, self.ref(new, "n1"))
        self.assertFalse(report["claims_change"])
        self.assertEqual(report["affected"], [])
        self.assertEqual(report["affected_count"], 0)
        self.assertEqual(report["unaffected_count"], 4)
        self.assertEqual(self.approvals.evaluate(appr.approval_id).status, "active")
        self.assertEqual(c2, c)

    def test_non_current_ref_claims_nothing(self) -> None:
        s, a, _b, _c = self.build()
        self.add("document", b"s2", "s2", aid=s, parent="s1")
        report = source_impact(self.store, self.ref(s, "s1"))
        self.assertFalse(report["source_is_current"])
        self.assertFalse(report["claims_change"])
        self.assertEqual(report["affected"], [])
        self.assertEqual(report["current_source"], self.ref(s, "s2"))

    def test_cycle_safe(self) -> None:
        s, a, b, _c = self.build()
        self.add("document", b"s2", "s2", aid=s, parent="s1")
        # Test-only raw edge closing a b -> a cycle (a1 depends on b1).
        self.store.connection.execute(
            "INSERT INTO dependency (project_id, from_artifact_id, from_revision_id, "
            "dependency_id, role, to_project_id, to_artifact_id, to_revision_id, "
            "created_at_utc) VALUES (?,?,?,?,?,?,?,?,?)",
            (self.pid, a, "a1", "dep-cycle", "derived_from", self.pid, b, "b1",
             "2026-10-28T00:00:00Z"),
        )
        report = source_impact(self.store, self.ref(s, "s2"))
        self.assertEqual({e["ref"]["artifact_id"] for e in report["affected"]}, {a, b})

    def test_bounds(self) -> None:
        s, *_ = self.build()
        self.add("document", b"s2", "s2", aid=s, parent="s1")
        with self.assertRaises(RecordValidationError):
            source_impact(self.store, self.ref(s, "s2"), max_nodes=1)
        for bad in (0, -1, True, "5", 10**9):
            with self.assertRaises(RecordValidationError):
                source_impact(self.store, self.ref(s, "s2"), max_nodes=bad)
        self.assertEqual(
            source_impact(self.store, self.ref(s, "s2"), max_nodes=2)["affected_count"], 2
        )

    def test_rejections(self) -> None:
        s, *_ = self.build()
        other = self.new_store("q")
        oaid = self.add("document", b"o", "o1", repo=ArtifactRepository(other))
        bad_refs = [
            {"project_id": other.project_id, "artifact_id": oaid, "revision_id": "o1"},
            {"project_id": self.pid, "artifact_id": s, "revision_id": "missing"},
            {"project_id": self.pid, "artifact_id": s},
            {**self.ref(s, "s1"), "extra": "x"},
            "not-a-mapping",
        ]
        for ref in bad_refs:
            with self.assertRaises(RecordValidationError, msg=repr(ref)):
                source_impact(self.store, ref)
        # a cross-project edge cannot appear in this project's report
        self.assertEqual(
            source_impact(other, {"project_id": other.project_id,
                                  "artifact_id": oaid, "revision_id": "o1"})["affected"],
            [],
        )


class _FakeTty:
    """Low-level terminal double (only terminal operations are mocked)."""

    def __init__(self, replies):
        self._replies = list(replies)

    def is_tty(self):
        return True

    def write(self, text, *, deadline=None):
        pass

    def read_line(self, *, max_chars, deadline):
        return self._replies.pop(0) if self._replies else None

    def close(self):
        pass


class HumanGateImpactTest(ImpactBase):
    """Real owner-capability human approvals via the default approvals source."""

    def human_approve(self, aid, rid, gate_id):
        prepared = prepare_gate_snapshot(
            self.store,
            self.ref(aid, rid),
            gate_kind="pilot",
            gate_id=gate_id,
            decision="approved",
            rationale="human review of the exact revision",
        )
        cap = owner_auth.request_owner_confirmation(prepared.owner_action())
        return self.human.record_decision(prepared, cap)

    def test_default_consults_human_approvals(self) -> None:
        patches = [
            mock.patch.object(owner_auth, "_new_challenge", lambda: "CHAL-1"),
            mock.patch.object(owner_auth, "_current_owner_uid", lambda: 1000),
            mock.patch.object(owner_auth, "_real_uid", lambda: 1000),
            mock.patch.object(
                owner_auth, "_utcnow",
                lambda: datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc),
            ),
            mock.patch.object(owner_auth, "_monotonic", lambda: 1000.0),
            mock.patch.object(
                owner_auth, "_open_controlling_tty", lambda: _FakeTty(["CHAL-1"])
            ),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.human = HumanApprovalRepository(self.store)
        s, a, b, c = self.build()
        c2 = self.add("document", b"c2", "c2", aid=c, parent="c1",
                      deps=[self.ref(c, "c1", "version_of")])
        h_a = self.human_approve(a, "a1", "gate_a")
        h_c = self.human_approve(c2, "c2", "gate_c")
        self.assertEqual(self.human.evaluate(h_a.approval_id).status, "active")
        self.add("document", b"s2", "s2", aid=s, parent="s1")
        before = self.dump()
        report = source_impact(self.store, self.ref(s, "s2"))  # default approvals
        self.assertEqual(self.dump(), before)
        json.dumps(report)
        self.assertEqual(
            [e["ref"]["artifact_id"] for e in report["affected"]], [a, b]
        )
        entry = report["affected"][0]
        self.assertEqual(len(entry["approvals"]), 1)
        self.assertEqual(entry["approvals"][0]["approval_id"], h_a.approval_id)
        self.assertEqual(entry["approvals"][0]["status"], "stale")
        self.assertEqual(self.human.evaluate(h_a.approval_id).status, "stale")
        # unrelated authentic approval (with version_of lineage) stays active
        self.assertEqual(self.human.evaluate(h_c.approval_id).status, "active")
        self.assertEqual(report["unaffected"], [self.ref(c, "c2")])
        self.assertNotIn(c, [e["ref"]["artifact_id"] for e in report["affected"]])
        self.assertFalse(report["authorizing"])


if __name__ == "__main__":
    unittest.main()
