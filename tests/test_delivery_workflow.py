"""Focused S14-T3 tests: durable Step 14 delivery workflow (MVP).

Covers ``src/review_article_agent/delivery/workflow.py`` against
``docs/implementation/14-delivery.md``: the global-review gate (section + origin
completion), a bounded global review with persisted loop counters, the explicit
freshness decision, the notes action plan and once-per-batch edit application
with revalidation, the exact-revision final approval manifest, the pre-export
hash re-check, and a complete example delivery with backup/verify/restore.

**Fixture disclosure:** the reviewer and writer are local fakes that count
calls; every source is a synthetic in-file document.  No real model, network,
provider, or OpenCode session call is made.

Run::

    PYTHONPATH=src python3 -m unittest tests.test_delivery_workflow -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.delivery.schema import DeliveryError  # noqa: E402
from review_article_agent.delivery.workflow import (  # noqa: E402
    DeliveryRepository,
)
from review_article_agent.storage import (  # noqa: E402
    ProjectStore,
    Registry,
    paths,
)
from review_article_agent.storage.backup import (  # noqa: E402
    create_backup,
    restore_backup,
    verify_backup,
)

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")

SECTIONS_OK = [
    {"section_id": "s1", "review_status": "complete", "origin_complete": True}
]
ORIGIN_OK = {"trace_id": "trace-1", "claims": []}


def _rights(project_id, artifact_id, revision_id):
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
        "recorded_at": "2026-10-30T00:00:00Z",
        "derived_status": "valid",
    }


def _ref(key="k1", doi="10.1/x", version="v1"):
    return {
        "key": key,
        "identity": {"doi": doi},
        "version": version,
        "title": "A synthetic study",
        "authors": ["A. Author", "B. Author"],
        "year": 2020,
        "journal": "Journal of Synthetic Results",
    }


CORPUS = {"k1": {"identity": {"doi": "10.1/x"}, "version": "v1"}}


def _reviewer(find_if_missing=True):
    """A recording fake reviewer: an open major finding while ``MISSING`` is present."""
    calls = []

    def reviewer(text):
        calls.append(text)
        if find_if_missing and "MISSING" in text:
            return [
                {
                    "finding_id": "f1",
                    "severity": "major",
                    "status": "open",
                    "objection": "unresolved consequential gap",
                }
            ]
        return []

    return reviewer, calls


def _writer():
    """A recording fake writer: replaces the ``MISSING`` marker with resolved text."""
    calls = []

    def writer(text, findings):
        calls.append(findings)
        return text.replace("MISSING", "RESOLVED")

    return writer, calls


class DeliveryBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-delivery-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(os.path.realpath(self._tmp.name))
        self.proot = self.tmp / "p"
        self.store = ProjectStore.create_at(
            paths.app_dir_for(str(self.proot)), paths.new_project_id()
        )
        self.addCleanup(self._close)
        self.pid = self.store.project_id
        self.repo = DeliveryRepository(self.store)

    def _close(self):
        try:
            self.store.close()
        except Exception:
            pass

    def rights(self, artifact_id, revision_id):
        return _rights(self.pid, artifact_id, revision_id)

    def start(self, delivery_id="delivery-1", sections=None, origin_state=None):
        return self.repo.start_global_review(
            delivery_id,
            sections if sections is not None else SECTIONS_OK,
            origin_state if origin_state is not None else ORIGIN_OK,
            rights=self.rights,
        )

    def review_once(self, delivery_id="delivery-1", text="Body [@k1].", paragraphs=None):
        reviewer, reviewer_calls = _reviewer(find_if_missing=False)
        writer, _ = _writer()
        state = self.repo.run_global_review(
            delivery_id,
            {
                "revision_id": "m1",
                "text": text,
                "paragraphs": paragraphs
                or {"p1": {"text": text, "checks": "valid"}},
            },
            [],
            reviewer,
            writer,
            rights=self.rights,
        )
        return state, reviewer_calls

    def accept_cutoff(self, delivery_id="delivery-1"):
        return self.repo.freshness(
            delivery_id, "2026-01-01", {"action": "accept_cutoff"}, rights=self.rights
        )


class HappyPathTests(DeliveryBase):
    def test_complete_example_delivery_and_backup_restore(self):
        did = "delivery-happy"

        # 1. start (gate met: sections + origin complete)
        self.start(did)

        # 2. global review: one reviewer finding -> one writer repair -> complete
        reviewer, reviewer_calls = _reviewer(find_if_missing=True)
        writer, writer_calls = _writer()
        state = self.repo.run_global_review(
            did,
            {
                "revision_id": "m1",
                "text": "Body [@k1]. MISSING detail.",
                "paragraphs": {"p1": {"text": "Body [@k1]. MISSING detail.", "checks": "valid"}},
            },
            [],
            reviewer,
            writer,
            rights=self.rights,
        )
        gr = state["global_review"]
        self.assertEqual(gr["status"], "complete")
        self.assertEqual(gr["reviewer_calls"], 2)
        self.assertEqual(gr["rounds_used"], 1)
        self.assertEqual(len(reviewer_calls), 2)
        self.assertEqual(len(writer_calls), 1)

        # 3. accept the freshness cutoff
        state = self.accept_cutoff(did)
        self.assertEqual(state["freshness"]["status"], "cutoff_accepted")

        # 4. notes plan + one approved wording batch (stays valid)
        self.repo.notes(
            did, [{"note_id": "n1", "text": "tighten wording", "category": "wording"}],
            rights=self.rights,
        )
        state = self.repo.apply_notes(
            did,
            batch_id="b1",
            approval_id="appr-1",
            edits=[
                {
                    "note_id": "n1",
                    "paragraph_id": "p1",
                    "new_text": "Body [@k1]. RESOLVED detail here.",
                    "kind": "wording",
                }
            ],
            rights=self.rights,
        )
        self.assertEqual(state["applied_batches"], ["b1"])
        self.assertEqual(state["revalidation_required"], [])

        # 5. final approval with one accepted secondary warning
        state = self.repo.final_approval(
            did,
            artifact_hashes={"art-1": "sha-1"},
            warnings=[{"warning_id": "w1", "claim_id": "c1", "text": "secondary source"}],
            accepted_warning_ids=["w1"],
            unsupported_major_claims=[],
            approver="alice",
            rights=self.rights,
        )
        manifest = state["approval"]
        self.assertTrue(manifest["approved"])
        self.assertEqual(manifest["manuscript_revision"], "m1")
        self.assertEqual(manifest["accepted_warnings"][0]["status"], "secondary_accepted")

        # 6. export: hashes current -> files written and consistent
        out_dir = self.tmp / "export"
        record = self.repo.export(
            did,
            manuscript={"revision_id": "m1", "text": "Body [@k1]. RESOLVED detail here."},
            references=[_ref()],
            corpus=CORPUS,
            current_hashes={"art-1": "sha-1"},
            policy={"companion_accepted": True, "journal_policy_verified": False},
            ai_use=[{"role": "writer", "model": "fake", "version": "1"}],
            out_dir=out_dir,
            rights=self.rights,
        )
        expected = {
            "manuscript.md",
            "references.bib",
            "references.ris",
            "companion.md",
            "summary.md",
            "ai_declaration.md",
            "export_manifest.json",
        }
        self.assertEqual(set(os.listdir(out_dir)), expected)
        bib = (out_dir / "references.bib").read_text()
        ris = (out_dir / "references.ris").read_text()
        self.assertIn("@article{k1,", bib)
        self.assertIn("ID  - k1", ris)
        self.assertTrue(record["checks"]["consistency"]["ok"])
        self.assertEqual(set(record["file_hashes"]), expected)

        # 7. backup / verify / restore evidence
        original = self.repo.read(did)
        dest = self.tmp / "backup"
        create_backup(self.store, dest)
        self.assertTrue(verify_backup(dest)["ok"])

        restored_root = self.tmp / "restored"
        restored_root.mkdir()
        registry = Registry(self.tmp / "state")
        info = restore_backup(dest, restored_root, registry)
        restored = ProjectStore.open_at(
            paths.app_dir_for(info.root_path), expected_project_id=info.project_id
        )
        self.addCleanup(restored.close)
        self.assertEqual(DeliveryRepository(restored).read(did), original)


class GateTests(DeliveryBase):
    def test_incomplete_section_refuses_start_and_persists_nothing(self):
        did = "delivery-incomplete"
        with self.assertRaises(DeliveryError):
            self.repo.start_global_review(
                did,
                [{"section_id": "s1", "review_status": "in_progress", "origin_complete": True}],
                ORIGIN_OK,
                rights=self.rights,
            )
        self.assertIsNone(self.repo.read(did))

    def test_incomplete_origin_refuses_start(self):
        did = "delivery-origin"
        bad_origin = {
            "trace_id": "trace-1",
            "claims": [{"claim_id": "c1", "outcome": "unsupported"}],
        }
        with self.assertRaises(DeliveryError):
            self.start(did, origin_state=bad_origin)
        self.assertIsNone(self.repo.read(did))

    def test_omitted_contrary_evidence_is_surfaced_and_blocks(self):
        did = "delivery-omission"
        self.start(did)
        reviewer, _ = _reviewer(find_if_missing=False)
        writer, _ = _writer()
        evidence = [
            {
                "evidence_id": "e2",
                "source_key": "k2",
                "direction": "contrary",
                "consequential": True,
            }
        ]
        state = self.repo.run_global_review(
            did,
            {"revision_id": "m1", "text": "Body [@k1].", "paragraphs": {}},
            evidence,
            reviewer,
            writer,
            rights=self.rights,
        )
        kinds = [finding.get("kind") for finding in state["global_review"]["findings"]]
        self.assertIn("omitted_contrary", kinds)
        self.assertEqual(state["global_review"]["status"], "blocked")


class GlobalReviewLoopTests(DeliveryBase):
    def test_two_round_exhaustion_blocks(self):
        did = "delivery-blocked"
        self.start(did)
        reviewer, reviewer_calls = _reviewer(find_if_missing=False)
        writer, writer_calls = _writer()

        def stubborn(text):
            reviewer_calls.append(text)
            return [
                {"finding_id": "f1", "severity": "major", "status": "open", "objection": "x"}
            ]

        state = self.repo.run_global_review(
            did,
            {"revision_id": "m1", "text": "Body [@k1].", "paragraphs": {}},
            [],
            stubborn,
            writer,
            rights=self.rights,
        )
        gr = state["global_review"]
        self.assertEqual(gr["status"], "blocked")
        self.assertEqual(gr["rounds_used"], 2)
        self.assertEqual(gr["reviewer_calls"], 3)
        self.assertTrue(gr["required_human_action"])
        self.assertEqual(len(writer_calls), 2)

    def test_restart_resumes_counters_without_extra_reviewer_calls(self):
        did = "delivery-resume"
        self.start(did)
        state, _ = self.review_once(did, text="Body [@k1]. MISSING detail.")
        self.assertEqual(state["global_review"]["reviewer_calls"], 1)

        reviewer2, reviewer_calls2 = _reviewer(find_if_missing=True)
        writer2, _ = _writer()
        resumed = self.repo.run_global_review(
            did,
            {"revision_id": "m1", "text": "Body [@k1]. MISSING detail.", "paragraphs": {}},
            [],
            reviewer2,
            writer2,
            rights=self.rights,
        )
        self.assertEqual(len(reviewer_calls2), 0)
        self.assertEqual(resumed["global_review"]["reviewer_calls"], 1)
        self.assertEqual(resumed["global_review"]["status"], "complete")


class FreshnessTests(DeliveryBase):
    def test_second_topup_refused(self):
        did = "delivery-topup"
        self.start(did)
        state = self.repo.freshness(
            did,
            "2026-01-01",
            {"action": "authorize_topup", "approval_id": "a1"},
            rights=self.rights,
        )
        self.assertEqual(state["freshness"]["status"], "topup_authorized")
        self.assertEqual(state["freshness"]["topups_used"], 1)
        with self.assertRaises(DeliveryError):
            self.repo.freshness(
                did,
                "2026-01-01",
                {"action": "authorize_topup", "approval_id": "a2"},
                rights=self.rights,
            )


class NotesTests(DeliveryBase):
    def test_scope_note_cannot_be_applied_as_edit(self):
        did = "delivery-scope"
        self.start(did)
        self.review_once(did)
        self.accept_cutoff(did)
        self.repo.notes(
            did, [{"note_id": "n2", "text": "add a section", "category": "scope"}],
            rights=self.rights,
        )
        with self.assertRaises(DeliveryError):
            self.repo.apply_notes(
                did,
                batch_id="b-scope",
                approval_id="appr-2",
                edits=[
                    {
                        "note_id": "n2",
                        "paragraph_id": "p1",
                        "new_text": "body",
                        "kind": "wording",
                    }
                ],
                rights=self.rights,
            )

    def test_citation_edit_blocks_final_approval_until_revalidated(self):
        did = "delivery-revalidate"
        self.start(did)
        self.review_once(
            did,
            text="Body [@k1].",
            paragraphs={"p1": {"text": "Body [@k1].", "checks": "valid"}},
        )
        self.accept_cutoff(did)
        self.repo.notes(
            did,
            [
                {
                    "note_id": "n3",
                    "text": "swap citation",
                    "category": "claim",
                    "target": {"paragraph_id": "p1", "intent_id": "I1"},
                }
            ],
            rights=self.rights,
        )
        state = self.repo.apply_notes(
            did,
            batch_id="b-cite",
            approval_id="appr-3",
            edits=[
                {
                    "note_id": "n3",
                    "paragraph_id": "p1",
                    "new_text": "Body [@k2].",
                    "kind": "citation",
                }
            ],
            rights=self.rights,
        )
        self.assertIn("p1", state["revalidation_required"])
        self.assertEqual(state["paragraphs"]["p1"]["checks"], "stale")

        with self.assertRaises(DeliveryError):
            self.repo.final_approval(
                did,
                artifact_hashes={"art-1": "sha-1"},
                warnings=[],
                accepted_warning_ids=[],
                unsupported_major_claims=[],
                approver="alice",
                rights=self.rights,
            )

        self.repo.revalidated(did, ["p1"], rights=self.rights)
        state = self.repo.final_approval(
            did,
            artifact_hashes={"art-1": "sha-1"},
            warnings=[],
            accepted_warning_ids=[],
            unsupported_major_claims=[],
            approver="alice",
            rights=self.rights,
        )
        self.assertTrue(state["approval"]["approved"])


class ApprovalAndExportTests(DeliveryBase):
    def _approved(self, did="delivery-export"):
        self.start(did)
        self.review_once(did)
        self.accept_cutoff(did)
        return self.repo.final_approval(
            did,
            artifact_hashes={"art-1": "sha-1"},
            warnings=[],
            accepted_warning_ids=[],
            unsupported_major_claims=[],
            approver="alice",
            rights=self.rights,
        )

    def test_unsupported_major_claim_cannot_be_waived(self):
        did = "delivery-unsupported"
        self.start(did)
        self.review_once(did)
        self.accept_cutoff(did)
        with self.assertRaises(DeliveryError):
            self.repo.final_approval(
                did,
                artifact_hashes={"art-1": "sha-1"},
                warnings=[],
                accepted_warning_ids=[],
                unsupported_major_claims=[{"claim_id": "c1", "detail": "unsupported"}],
                approver="alice",
                rights=self.rights,
            )

    def test_hash_change_after_approval_refuses_export(self):
        did = "delivery-hash"
        self._approved(did)
        out_dir = self.tmp / "export-refused"
        with self.assertRaises(DeliveryError):
            self.repo.export(
                did,
                manuscript={"revision_id": "m1", "text": "Body [@k1]."},
                references=[_ref()],
                corpus=CORPUS,
                current_hashes={"art-1": "CHANGED"},
                policy={"companion_accepted": True, "journal_policy_verified": False},
                ai_use=[],
                out_dir=out_dir,
                rights=self.rights,
            )
        self.assertFalse(os.path.exists(out_dir))


if __name__ == "__main__":
    unittest.main()
