"""Step 15 end-to-end (front half) integrated demonstration — AC1 / AC7.

Authority: ``docs/implementation/15-release.md`` AC1 (brief -> seed approval ->
plan -> freeze -> acquisition -> pilot -> screening/exclusion audits ->
extraction/output audits) and AC7 (required visual/supplement evidence is
actually interpreted or escalated, not silently dropped).

One disposable ``ProjectStore`` (tempdir) drives the *actual* named pipeline
modules that each predecessor step already implements:

    brief/seed/plan (real ``build_plan``) -> human freeze gate
      -> acquisition manifest over the exact frozen roster (real named gate)
      -> parsing report of the acquired source (real bytes)
      -> screening pilot -> screening batch -> exclusion audit
      -> extraction pilot -> extraction batch -> output audit -> synthesis gate.

Fixtures come from the existing suites (``test_discovery_freeze``,
``test_acquisition_manifest``, ``test_screening``, ``test_extraction_workflow``)
rather than being copied. Fully synthetic, self-authored content only; no
network, provider, or live model call (fake responses / synthetic receipts).

See ``BRIDGES`` for every place a stage cannot consume the previous stage's
literal output and the existing fixture is used instead.

Run::

    PYTHONPATH=src python3 -m unittest tests.test_step15_e2e_front -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

import test_screening as sc  # noqa: E402  (screening config / fake response)
from test_extraction_workflow import (  # noqa: E402
    GATE_BATCH,
    GATE_KIND,
    GATE_OUTPUTS,
    GATE_PILOT,
    MANDATORY_FAILURE_LABELS,
    WorkflowBase,
)

from review_article_agent.acquisition.manifest import ManifestNotAcquiredError  # noqa: E402
from review_article_agent.discovery.context import (  # noqa: E402
    ARTIFACT_REVISION_GATE_KIND,
    PLAN_GATE_ID,
    PROTOCOL_GATE_KIND,
    SEED_GATE_PREFIX,
)
from review_article_agent.parsing import ParsingRepository  # noqa: E402
from review_article_agent.screening import ScreeningRepository  # noqa: E402
from review_article_agent.screening.workflow import classify  # noqa: E402
from review_article_agent.storage.human_approvals import HumanApprovalRepository  # noqa: E402

# BRIDGES records every stage seam that cannot consume the previous stage's
# literal output and uses the existing fixture instead, with the reason.
BRIDGES = [
    "screening: the case `response` is an imported, unverified Jev response "
    "object (test_screening.response) rather than a live dispatch; the "
    "screening MVP has no live-dispatch binding yet (handoff 08).",
    "extraction: the dispatch receipts are synthetic verified receipts injected "
    "by patching `workflow._require_run` (test_extraction_workflow.WorkflowBase) "
    "because the workflow under test is the comparison/gating logic, not the "
    "durable runner (handoff 09).",
    "model outputs: both screening and extraction consume recorded model-shaped "
    "outputs, not live model calls, so identity/quality pinning is exercised "
    "without any network or spend.",
]


class FrontHalfEndToEndTest(WorkflowBase):
    """Chains the real front-half stages inside one disposable store."""

    def _screening_put(self, kind, payload, *, artifact=None, revision=None):
        repo = ScreeningRepository(self.store)
        artifact = artifact or ("s-" + kind)
        revision = revision or (artifact + "-r1")
        return repo.propose(
            kind,
            payload,
            artifact_id=artifact,
            revision_id=revision,
            rights=self.rights(artifact, revision),
        )

    def _screen_report_for(self, source_ref):
        return ParsingRepository(self.store).parse_stored_document(
            source_ref,
            report_artifact_id="s-parsed",
            report_revision_id="s-parsed-r1",
            rights=self.rights("s-parsed", "s-parsed-r1"),
        )["report_ref"]

    def test_front_half_chain_and_visual_escalation(self):
        # -- brief -> seed approval -> plan -> corpus -> freeze (real gates) -- #
        a, b, source, entries = self.standard()
        approvals = HumanApprovalRepository(self.store)
        # The seed and the exact plan revision carry real owner approvals.
        self.assertTrue(
            approvals.named_gate_satisfied(
                "a-seed0",
                gate_kind=ARTIFACT_REVISION_GATE_KIND,
                gate_id=SEED_GATE_PREFIX + "a-seed0",
            )
        )
        self.assertTrue(
            approvals.named_gate_satisfied(
                "a-plan", gate_kind=PROTOCOL_GATE_KIND, gate_id=PLAN_GATE_ID
            )
        )
        frozen = self.freeze.require_frozen()
        self.assertEqual(
            sorted(frozen["included_candidate_ids"]),
            sorted([a, b]),
        )

        # -- acquisition over the exact frozen roster (named gate) ----------- #
        manifest = self.make(entries)
        self.assertIsNone(self.acq.get(self.ref_of(manifest))["approval"])
        self.gate(manifest)
        acquired = self.acq.require_acquired()
        self.assertTrue(acquired["acquired"])
        self.assertEqual(acquired["acquired_candidate_ids"], [a])
        self.assertFalse(acquired["authorizes_scientific_processing"])

        # -- parsing of the acquired source (real bytes) --------------------- #
        report = self._screen_report_for(source)

        # -- pilot -> screening batch -> exclusion audit --------------------- #
        s_config = self._screening_put("config", sc.config(), artifact="s-config")
        pilot_case = {
            "id": "paper1",
            "report_ref": report,
            "passage_id": None,
            "response": sc.response(0.1),
        }
        # The machine label is the recorded classification (never the human label).
        self.assertEqual(classify(sc.config(), pilot_case["response"])["label"],
                         "proposed_exclusion")
        s_pilot = self._screening_put(
            "pilot",
            {
                "config_ref": s_config,
                "cases": [pilot_case],
                "adjudications": {
                    "paper1": {
                        "label": "exclude",
                        "inspected_source": True,
                        "important_failure": False,
                        "rationale": "Checked the exact original source.",
                        "corrective_action": None,
                    }
                },
            },
            artifact="s-pilot",
        )
        self.approve(
            s_pilot["artifact_id"], s_pilot["revision_id"], "pilot", "screening.pilot"
        )
        approved_pilot = ScreeningRepository(self.store).require_pilot(s_pilot)
        self.assertEqual(approved_pilot["blocking_failures"], [])
        self.assertFalse(approved_pilot["authorizes_extraction"])

        s_batch = self._screening_put(
            "batch",
            {
                "config_ref": s_config,
                "pilot_ref": s_pilot,
                "acquisition_ref": self.ref_of(manifest),
                "cases": [pilot_case],
                "seed": "e2e-front",
            },
            artifact="s-batch",
        )
        s_audit = self._screening_put(
            "audit",
            {
                "batch_ref": s_batch,
                "checks": {
                    "paper1": {
                        "inspected_source": True,
                        "false_exclusion": False,
                        "rationale": "Read the exact original source.",
                        "corrective_action": None,
                    }
                },
            },
            artifact="s-audit",
        )
        self.approve(
            s_audit["artifact_id"],
            s_audit["revision_id"],
            "exclusion_set",
            "screening.exclusions",
        )
        self.assertEqual(
            ScreeningRepository(self.store).require_exclusions(s_audit), ["paper1"]
        )

        # -- extraction pilot -> batch -> output audit -> synthesis gate ---- #
        case = self.case("c1", source_refs=[source])
        pilot_ref = self.build_pilot(
            [case], {"c1": self.adjudication(MANDATORY_FAILURE_LABELS)}
        )
        self.approve_ref(pilot_ref, GATE_KIND, GATE_PILOT)
        self.assertEqual(self.workflow.require_pilot(pilot_ref)["blocking_failures"], [])

        batch_ref = self.put(
            "batch",
            {
                "config_ref": self.config_ref,
                "pilot_ref": pilot_ref,
                "acquisition_ref": self.ref_of(manifest),
                "cases": [case],
                "seed": "e2e-front",
            },
        )
        self.approve_ref(batch_ref, GATE_KIND, GATE_BATCH)
        self.assertIn("c1", self.workflow.require_batch(batch_ref)["random_ids"])

        audit_ref = self.put(
            "audit", {"batch_ref": batch_ref, "checks": {"c1": self.audit_check()}}
        )
        self.approve_ref(audit_ref, GATE_KIND, GATE_OUTPUTS)
        evidence = self.workflow.require_for_synthesis(audit_ref)
        self.assertTrue(evidence["accepted"])
        self.assertEqual(evidence["case_ids"], ["c1"])
        self.assertEqual(evidence["authority"], "named_gate_only")
        self.assertEqual(evidence["guarantee"], "sampled_audit_not_completeness")

        # -- AC7: a required figure/supplement is escalated, not dropped ----- #
        visual = self.make(
            [
                self.entry(
                    a,
                    source=source,
                    assets=[
                        self.asset("f1", "figure"),  # available but no bytes
                        self.asset("s1", "supplement", status="unavailable"),
                    ],
                ),
                self.entry(b, "exclude", rationale="out of scope"),
            ],
            artifact_id="acq.visual",
            request_id="visual-1",
        )
        blockers = self.acq.get(self.ref_of(visual))["payload"]["blockers"]
        self.assertEqual(
            {(x["category"], x["code"], x["asset_id"]) for x in blockers},
            {
                ("visual", "required_asset_bytes_missing", "f1"),
                ("supplement", "required_asset_unavailable", "s1"),
            },
        )
        self.gate(visual)
        with self.assertRaises(ManifestNotAcquiredError) as caught:
            self.acq.require_acquired("acq.visual")
        self.assertIn("blockers_present", caught.exception.reasons)
        queued = self.acq.queue(self.freeze_ref, artifact_id="acq.visual")
        self.assertEqual(
            {x["category"] for x in queued}, {"visual", "supplement"}
        )
        # The clean manifest is untouched by the blocked visual one.
        self.acq.require_acquired()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
