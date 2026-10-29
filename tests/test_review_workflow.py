"""Focused S12-WORKFLOW tests: durable, resumable section review.

Covers ``src/review_article_agent/review/workflow.py`` against
``docs/implementation/12-review.md``: separate-context different-model critique,
bounded critique/fact-check repair rounds, repair-regression rejection, a
read-only final validation that never auto-passes, change-triggered rechecks, and
a persisted review state that survives an interrupted worker call.

**Fixture disclosure:** every draft/package is the synthetic Step 11 fixture and
the critic/writer are local fakes that count calls.  No real model, network,
provider, or OpenCode session call is made.

Run::

    PYTHONPATH=src python3 -m unittest tests.test_review_workflow -v
"""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.drafting.schema import (  # noqa: E402
    draft_example,
    drafting_example_package,
)
from review_article_agent.review.schema import ROUND_LIMITS  # noqa: E402
from review_article_agent.review.workflow import (  # noqa: E402
    SectionReviewError,
    SectionReviewRepository,
)
from review_article_agent.storage import ProjectStore, paths  # noqa: E402

_CRITIC = "openrouter/deepseek/v4.1-flash#high"
_WRITER = "openrouter/anthropic/claude-sonnet-5-5#high"

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")


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


def _sentence(draft, paragraph_id, intent_id):
    for paragraph in draft["paragraphs"]:
        if paragraph["paragraph_id"] != paragraph_id:
            continue
        for sentence in paragraph["sentences"]:
            if sentence["intent_id"] == intent_id:
                return sentence
    raise AssertionError("sentence not found")


def _clean_draft():
    """A draft whose sentences carry verified citations and support flags."""
    package = drafting_example_package()
    draft = draft_example(package=package)
    _sentence(draft, "P1", "I1")["citation_keys"] = ["CIT-A10"]
    secondary = _sentence(draft, "P2", "I2")
    secondary["citation_keys"] = ["CIT-C3"]
    secondary["secondary"] = True
    _sentence(draft, "P2", "I3")["proposed_synthesis"] = True
    return draft, package


def _major(target=("P1", "I1"), finding_key="A10:F1",
           quote="tumor volume was reduced in the treated cohort",
           objection="The claim is not supported by the cited source."):
    return {
        "target": {"paragraph_id": target[0], "intent_id": target[1]},
        "kind": "unsupported_claim",
        "severity": "major",
        "objection": objection,
        "basis": {"finding_key": finding_key, "quote": quote},
    }


class FakeCritic:
    """Returns a fixed list of raw findings; counts calls."""

    def __init__(self, findings):
        self._findings = copy.deepcopy(findings)
        self.calls = 0

    def __call__(self, draft, package):
        self.calls += 1
        return copy.deepcopy(self._findings)


class FakeWriter:
    """Replaces a sentence text per target, or rejects a target; counts calls."""

    def __init__(self, texts=None, reject=()):
        self._texts = dict(texts or {})
        self._reject = set(reject)
        self.calls = 0

    def __call__(self, draft, findings):
        self.calls += 1
        new_draft = copy.deepcopy(draft)
        responses = {}
        for finding in findings:
            target = finding["target"]
            key = (target["paragraph_id"], target["intent_id"])
            if key in self._reject:
                responses[finding["finding_id"]] = {
                    "accept": False,
                    "reason": "The cited source does support the claim.",
                }
            elif key in self._texts:
                _sentence(new_draft, key[0], key[1])["text"] = self._texts[key]
                responses[finding["finding_id"]] = {"accept": True, "reason": "rewritten"}
            else:
                responses[finding["finding_id"]] = {
                    "accept": False,
                    "reason": "no change required",
                }
        return new_draft, responses


class ScriptedWriter:
    """A writer whose per-call behaviour is scripted (dict, or ``"raise"``)."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = 0

    def __call__(self, draft, findings):
        index = self.calls
        self.calls += 1
        action = self._script[index]
        if action == "raise":
            raise RuntimeError("simulated worker crash")
        new_draft = copy.deepcopy(draft)
        responses = {}
        for finding in findings:
            target = finding["target"]
            key = (target["paragraph_id"], target["intent_id"])
            if key in action:
                _sentence(new_draft, key[0], key[1])["text"] = action[key]
            responses[finding["finding_id"]] = {"accept": True, "reason": "rewritten"}
        return new_draft, responses


class ReviewBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-review-")
        self.addCleanup(self._tmp.cleanup)
        self.proot = Path(os.path.realpath(self._tmp.name)) / "p"
        self.store = ProjectStore.create_at(
            paths.app_dir_for(str(self.proot)), paths.new_project_id()
        )
        self.addCleanup(self._close)
        self.pid = self.store.project_id
        self.repo = SectionReviewRepository(self.store)

    def _close(self):
        try:
            self.store.close()
        except Exception:
            pass

    def rights(self, artifact_id, revision_id):
        return _rights(self.pid, artifact_id, revision_id)

    def run_review(self, review_id, draft, package, critic, writer):
        return self.repo.run(
            review_id,
            draft=draft,
            package=package,
            writer_model=_WRITER,
            critic_model=_CRITIC,
            critic=critic,
            writer=writer,
            rights=self.rights,
        )


class EarlySuccessTests(ReviewBase):
    def test_clean_draft_completes_with_one_critic_and_no_repair(self):
        draft, package = _clean_draft()
        critic = FakeCritic([])
        writer = FakeWriter()
        state = self.run_review("review-clean", draft, package, critic, writer)

        self.assertEqual(state["status"], "complete")
        self.assertEqual(state["critic_calls"], 1)
        self.assertEqual(state["repair_calls"], 0)
        self.assertEqual(critic.calls, 1)
        self.assertEqual(writer.calls, 0)
        self.assertFalse(state["engine"]["independent_human_verification"])
        self.assertTrue(state["validation"]["read_only"])


class FalseCriticTests(ReviewBase):
    def test_assertion_and_fabricated_objections_rejected_without_repair(self):
        draft, package = _clean_draft()
        assertion = _major()
        assertion.pop("basis")
        assertion["objection"] = "I simply do not believe this claim."
        fabricated = _major(
            objection="The source says something else entirely.",
        )
        fabricated["basis"] = {
            "finding_key": "A10:F1",
            "quote": "text that appears nowhere in the source",
        }
        critic = FakeCritic([assertion, fabricated])
        writer = FakeWriter()
        state = self.run_review("review-false", draft, package, critic, writer)

        self.assertEqual(state["repair_calls"], 0)
        self.assertEqual(writer.calls, 0)
        rejected = [f for f in state["findings"] if f["disposition"] == "rejected"]
        self.assertEqual(len(rejected), 2)
        reasons = " ".join(f["reason"] for f in rejected)
        self.assertIn("assertion", reasons)
        self.assertIn("fabricated", reasons)
        self.assertEqual(state["status"], "complete")


class RepairTests(ReviewBase):
    def test_one_major_repaired_in_round_one_completes(self):
        draft, package = _clean_draft()
        critic = FakeCritic([_major()])
        writer = FakeWriter({("P1", "I1"): "Compound Z reduced tumor volume in a treated mouse model."})
        state = self.run_review("review-repair", draft, package, critic, writer)

        self.assertEqual(state["status"], "complete")
        self.assertEqual(state["critic_calls"], 1)
        self.assertEqual(state["repair_calls"], 1)
        fixed = [f for f in state["findings"] if f["disposition"] == "fixed"]
        self.assertEqual(len(fixed), 1)

    def test_repair_regression_rejected_and_blocked_after_three_rounds(self):
        draft, package = _clean_draft()
        _sentence(draft, "P1", "I1")["text"] = "Compound Z may reduce tumor volume in a mouse model."
        critic = FakeCritic([_major()])
        # Drops the qualifier "may" on every round -> every repair is rejected.
        writer = FakeWriter({("P1", "I1"): "Compound Z reduces tumor volume in a mouse model."})
        state = self.run_review("review-regression", draft, package, critic, writer)

        self.assertEqual(state["critic_calls"], ROUND_LIMITS["critique"])
        self.assertEqual(critic.calls, ROUND_LIMITS["critique"])
        self.assertIn("qualifier_removed", state["violations"][0]["violations"])
        confirmed = [f for f in state["findings"] if f["disposition"] == "confirmed"]
        self.assertTrue(confirmed)
        self.assertEqual(state["status"], "blocked")
        self.assertNotEqual(state["status"], "complete")

    def test_disputed_major_blocks_for_human(self):
        draft, package = _clean_draft()
        critic = FakeCritic([_major()])
        writer = FakeWriter(reject=[("P1", "I1")])
        state = self.run_review("review-dispute", draft, package, critic, writer)

        self.assertEqual(state["status"], "blocked")
        self.assertIsNotNone(state["required_human_action"])
        self.assertIn("disputed", [f["disposition"] for f in state["findings"]])


class OriginHandoffTests(ReviewBase):
    def test_secondary_origin_flag_preserved_and_never_primary_verified(self):
        draft, package = _clean_draft()
        state = self.run_review(
            "review-origin", draft, package, FakeCritic([]), FakeWriter()
        )

        checks = state["checks"]
        secondary = [c for c in checks if c["intent_id"] == "I2"][0]
        primary = [c for c in checks if c["intent_id"] == "I1"][0]
        self.assertEqual(secondary["support_label"], "secondary_support")
        self.assertEqual(secondary["origin_flag"], "unresolved_secondary_origin")
        self.assertEqual(primary["origin_status"], "pending_origin_tracing")
        self.assertNotIn("primary_verified", json.dumps(state))


class RestartTests(ReviewBase):
    def test_resume_after_crash_does_not_duplicate_repairs(self):
        draft, package = _clean_draft()
        _sentence(draft, "P1", "I1")["text"] = "Compound Z may reduce tumor volume in a mouse model."
        findings = [
            _major(),
            _major(
                target=("P2", "I2"),
                finding_key="C3:F1",
                quote="A review article repeats the reported reduction.",
                objection="The secondary account is misattributed.",
            ),
        ]
        # Round 0: repair P1:I1 validly, but P2:I2 with an invented number.
        check_first = {
            ("P1", "I1"): "Compound Z may reduce tumor volume in a treated mouse model.",
            ("P2", "I2"): "A secondary account repeats the reduction by a factor of 40.",
        }
        # Resume of round 1: repair both validly.
        round_one = {
            ("P1", "I1"): "Compound Z may reduce tumor volume in a treated mouse model.",
            ("P2", "I2"): "A secondary account repeats the reported reduction.",
        }
        writer = ScriptedWriter([check_first, "raise", round_one])
        critic = FakeCritic(findings)

        with self.assertRaises(RuntimeError):
            self.run_review("review-restart", draft, package, critic, writer)

        mid = self.repo.read("review-restart")
        self.assertEqual(mid["rounds_used"]["critique"], 1)
        self.assertEqual(mid["critic_calls"], 2)
        self.assertEqual(mid["repair_calls"], 1)
        self.assertEqual(mid["phase"], "writer")

        state = self.run_review("review-restart", draft, package, critic, writer)

        self.assertEqual(state["rounds_used"]["critique"], 2)
        self.assertEqual(state["critic_calls"], 2)
        self.assertEqual(state["repair_calls"], 2)
        self.assertEqual(state["status"], "complete")
        # The round-0 accepted repair is neither duplicated nor reverted.
        self.assertEqual(
            _sentence(state["draft"], "P1", "I1")["text"],
            "Compound Z may reduce tumor volume in a treated mouse model.",
        )


class ChangedPackageTests(ReviewBase):
    def test_changed_evidence_passage_invalidates_and_rechecks(self):
        draft, package = _clean_draft()
        state = self.run_review(
            "review-change", draft, package, FakeCritic([]), FakeWriter()
        )
        target_id = "fc:P1:I1"
        old_fingerprint = [c for c in state["checks"] if c["check_id"] == target_id][0][
            "fingerprint"
        ]

        changed = copy.deepcopy(package)
        for entry in changed["evidence"]:
            if entry["finding_key"] == "A10:F1":
                entry["original_passage"]["text"] = "a substantially rewritten passage"
        state2 = self.run_review(
            "review-change", draft, changed, FakeCritic([]), FakeWriter()
        )

        self.assertIn(target_id, state2["invalidated_checks"])
        new_fingerprint = [c for c in state2["checks"] if c["check_id"] == target_id][0][
            "fingerprint"
        ]
        self.assertNotEqual(old_fingerprint, new_fingerprint)


class ModelIdentityTests(ReviewBase):
    def test_same_model_is_refused(self):
        draft, package = _clean_draft()
        with self.assertRaises(SectionReviewError):
            self.repo.run(
                "review-same",
                draft=draft,
                package=package,
                writer_model=_WRITER,
                critic_model=_WRITER,
                critic=FakeCritic([]),
                writer=FakeWriter(),
                rights=self.rights,
            )


if __name__ == "__main__":
    unittest.main()
