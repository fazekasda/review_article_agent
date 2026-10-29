"""Focused S10-WORKFLOW tests: durable synthesis/framework/outline workflow.

The accepted Step 09 receipt seam (``extraction.workflow._require_run``) is
inherited from :mod:`test_extraction_workflow` (which patches it with synthetic
*verified receipts*); one integration case at the end instead extends the real
runner / fake-transport end-to-end fixture in :mod:`test_extraction_runner`, so
no fixture is replicated and the real Step 09 outputs gate is consumed.

Every human approval uses the real owner-capability flow (the controlling-tty
seam is mocked exactly as the accepted freeze tests do); there are no fake
booleans.  No model, network, or provider call is made.

Run::

    PYTHONPATH=src:tests python3 -m unittest test_synthesis_workflow -v
"""

from __future__ import annotations

from test_extraction_workflow import (  # noqa: E402
    GATE_KIND,
    MANDATORY_FAILURE_LABELS,
    WorkflowBase,
    _extraction_output,
)

from review_article_agent.domain.records import RecordValidationError  # noqa: E402
from review_article_agent.synthesis.workflow import (  # noqa: E402
    GATE_FRAMEWORK,
    GATE_FRAMEWORK_KIND,
    GATE_OUTLINE,
    GATE_OUTLINE_KIND,
    GATE_SYNTHESIS,
    GATE_SYNTHESIS_KIND,
    SynthesisWorkflowError,
    SynthesisWorkflowRepository,
)

_CASE = "c1"
_FINDING = _CASE + ":F1"


def _known(value):
    return {"state": "known", "value": value}


def _context():
    return {
        "species": _known("mouse"),
        "population": _known("adult mice"),
        "assay": _known("tumor volume"),
        "outcome": _known("tumor volume"),
        "conditions": _known("vehicle"),
    }


class SynthesisBase(WorkflowBase):
    def setUp(self):
        super().setUp()
        self.synth = SynthesisWorkflowRepository(self.store)

    def _reopen(self):
        super()._reopen()
        self.synth = SynthesisWorkflowRepository(self.store)

    # -- Step 09 accepted evidence ------------------------------------------- #
    def accepted_audit(self, *, unknown_population=False):
        _a, _b, source, entries = self.standard()
        manifest = self.make(entries)
        self.gate(manifest)
        case = self.case(
            _CASE, source_refs=[source],
            raw_output=_extraction_output(unknown_population=unknown_population),
        )
        pilot_ref = self.build_pilot([case], {_CASE: self.adjudication(MANDATORY_FAILURE_LABELS)})
        self.approve_ref(pilot_ref, GATE_KIND, "extraction.pilot")
        batch_ref = self.put(
            "batch",
            {"config_ref": self.config_ref, "pilot_ref": pilot_ref,
             "acquisition_ref": self.ref_of(manifest), "cases": [case], "seed": "s1"},
        )
        self.approve_ref(batch_ref, GATE_KIND, "extraction.batch")
        audit_ref = self.put("audit", {"batch_ref": batch_ref, "checks": {_CASE: self.audit_check()}})
        self.approve_ref(audit_ref, GATE_KIND, "extraction.outputs")
        return audit_ref, batch_ref

    # -- payload builders ----------------------------------------------------- #
    @staticmethod
    def synthesis_content(*, statement_status="proposed", statement_id="S-1"):
        return {
            "schema_version": 1,
            "memo": "Compound Z effect from the accepted evidence.",
            "statements": [
                {
                    "statement_id": statement_id,
                    "text": "Compound Z reduced tumor volume in mice.",
                    "classification": "reported_evidence",
                    "evidence": [_FINDING],
                    "applicability": _context(),
                    "causal_limitations": _known("association in one model; no mechanism"),
                    "uncertainty": _known("limited to the reported model"),
                    "status": statement_status,
                }
            ],
            "dispositions": [
                {"finding": _FINDING, "disposition": "incorporated", "rationale": "used in the memo"}
            ],
        }

    @staticmethod
    def framework_content(*, c1_definition="Compound Z tumor-volume effect.",
                          r1_directionality="shared-study reports constrain the effect"):
        return {
            "schema_version": 1,
            "title": "Compound Z framework",
            "contribution": "Organizes the accepted evidence without forcing a novel framework.",
            "criteria": {
                "contribution": "adds an organizing relationship",
                "admission": "definition, role, support, counterevidence, limits",
                "support": "accepted finding keys",
                "revision": "changed definitions reopen dependent sections",
            },
            "concepts": [
                {
                    "concept_id": "C1", "definition": c1_definition,
                    "role": "central measured outcome", "original_support": [_FINDING],
                    "counterevidence_note": "no counterevidence identified in the accepted cases",
                    "applicability_limits": "mouse tumor models only",
                    "testable_implication": "a dose-response experiment would show a monotone effect",
                    "status": "proposed", "novelty": "existing",
                },
                {
                    "concept_id": "C2", "definition": "Report correlation by shared underlying study.",
                    "role": "explains why reports are not independent", "original_support": [_FINDING],
                    "counterevidence_note": "no counterevidence identified",
                    "applicability_limits": "only for reports declaring a shared study",
                    "testable_implication": {"state": "not_applicable", "reason": "methodological"},
                    "status": "proposed", "novelty": "existing",
                },
            ],
            "relationships": [
                {
                    "relationship_id": "R1", "from_concept": "C2", "to_concept": "C1",
                    "directionality": r1_directionality, "scope": "reviewed corpus only",
                    "evidence": [_FINDING], "status": "proposed",
                }
            ],
        }

    @staticmethod
    def outline_content():
        return {
            "schema_version": 1,
            "title": "Compound Z outline",
            "sections": [
                {"section_id": "sec1", "order": 1, "title": "Background",
                 "concept_dependencies": ["C1"]},
                {"section_id": "sec2", "order": 2, "title": "Evidence and correlation",
                 "concept_dependencies": ["C2"], "relationship_dependencies": ["R1"],
                 "statement_dependencies": ["S-1"]},
            ],
        }

    # -- proposal helpers ----------------------------------------------------- #
    def put_synthesis(self, audit_ref, *, revision="syn-r1", parent=None, **content_kw):
        return self.synth.propose(
            "synthesis", {"audit_ref": audit_ref, "content": self.synthesis_content(**content_kw)},
            artifact_id="syn", revision_id=revision, rights=self.rights("syn", revision),
            parent_revision_id=parent,
        )

    def put_framework(self, synthesis_ref, *, revision="fw-r1", parent=None, **content_kw):
        return self.synth.propose(
            "framework",
            {"synthesis_ref": synthesis_ref, "content": self.framework_content(**content_kw)},
            artifact_id="fw", revision_id=revision, rights=self.rights("fw", revision),
            parent_revision_id=parent,
        )

    def put_outline(self, synthesis_ref, framework_ref, *, revision="out-r1", parent=None):
        return self.synth.propose(
            "outline",
            {"synthesis_ref": synthesis_ref, "framework_ref": framework_ref,
             "content": self.outline_content()},
            artifact_id="out", revision_id=revision, rights=self.rights("out", revision),
            parent_revision_id=parent,
        )

    def approved_chain(self, *, unknown_population=False):
        audit_ref, _batch = self.accepted_audit(unknown_population=unknown_population)
        synthesis_ref = self.put_synthesis(audit_ref)
        self.approve_ref(synthesis_ref, GATE_SYNTHESIS_KIND, GATE_SYNTHESIS)
        framework_ref = self.put_framework(synthesis_ref)
        self.approve_ref(framework_ref, GATE_FRAMEWORK_KIND, GATE_FRAMEWORK)
        outline_ref = self.put_outline(synthesis_ref, framework_ref)
        self.approve_ref(outline_ref, GATE_OUTLINE_KIND, GATE_OUTLINE)
        return synthesis_ref, framework_ref, outline_ref


class ChainAndGateTests(SynthesisBase):
    def test_approved_chain_and_blueprints_after_reopen(self):
        synthesis_ref, framework_ref, outline_ref = self.approved_chain()
        self.assertEqual(self.synth.require_synthesis(synthesis_ref)["kind"], "synthesis")
        self.assertEqual(self.synth.require_framework(framework_ref)["kind"], "framework")
        self.assertEqual(self.synth.require_outline(outline_ref)["kind"], "outline")
        self._reopen()
        blueprints = self.synth.require_for_blueprints(outline_ref)
        self.assertTrue(blueprints["accepted"])
        self.assertEqual(blueprints["outline_ref"], outline_ref)
        self.assertEqual(blueprints["framework_ref"], framework_ref)
        self.assertEqual(blueprints["synthesis_ref"], synthesis_ref)
        self.assertEqual(blueprints["authority"], "named_gate_only")
        self.assertEqual(blueprints["synthesis"]["statements"][0]["statement_id"], "S-1")
        self.assertEqual(blueprints["outline"]["sections"][0]["section_id"], "sec1")

    def test_synthesis_requires_extraction_outputs_gate(self):
        _a, _b, source, entries = self.standard()
        manifest = self.make(entries)
        self.gate(manifest)
        case = self.case(_CASE, source_refs=[source])
        pilot_ref = self.build_pilot([case], {_CASE: self.adjudication(MANDATORY_FAILURE_LABELS)})
        self.approve_ref(pilot_ref, GATE_KIND, "extraction.pilot")
        batch_ref = self.put(
            "batch",
            {"config_ref": self.config_ref, "pilot_ref": pilot_ref,
             "acquisition_ref": self.ref_of(manifest), "cases": [case], "seed": "s1"},
        )
        self.approve_ref(batch_ref, GATE_KIND, "extraction.batch")
        # No extraction.outputs gate yet: the synthesis proposal must fail closed.
        audit_ref = self.put("audit", {"batch_ref": batch_ref, "checks": {_CASE: self.audit_check()}})
        with self.assertRaises(Exception):
            self.put_synthesis(audit_ref)

    def test_synthesis_gate_required_and_inband_forgery_refused(self):
        audit_ref, _batch = self.accepted_audit()
        # An in-band approved statement status is not authority.
        ref = self.put_synthesis(audit_ref, statement_status="approved")
        with self.assertRaises(SynthesisWorkflowError):
            self.synth.require_synthesis(ref)
        self.approve_ref(ref, GATE_SYNTHESIS_KIND, GATE_SYNTHESIS)
        result = self.synth.require_synthesis(ref)
        self.assertEqual(result["statements"][0]["status"], "approved")

    def test_forged_top_level_approval_field_is_rejected(self):
        audit_ref, _batch = self.accepted_audit()
        content = self.synthesis_content()
        content["approved"] = True
        with self.assertRaises(SynthesisWorkflowError):
            self.synth.propose(
                "synthesis", {"audit_ref": audit_ref, "content": content},
                artifact_id="syn", revision_id="syn-r1", rights=self.rights("syn", "syn-r1"),
            )

    def test_framework_cannot_precede_accepted_synthesis(self):
        audit_ref, _batch = self.accepted_audit()
        synthesis_ref = self.put_synthesis(audit_ref)
        # Synthesis exists but is not gated: the framework proposal is refused.
        with self.assertRaises(SynthesisWorkflowError):
            self.put_framework(synthesis_ref)
        self.approve_ref(synthesis_ref, GATE_SYNTHESIS_KIND, GATE_SYNTHESIS)
        framework_ref = self.put_framework(synthesis_ref)
        # The framework proposal exists but its own gate is not satisfied.
        with self.assertRaises(SynthesisWorkflowError):
            self.synth.require_framework(framework_ref)

    def test_outline_requires_framework_gate(self):
        synthesis_ref, framework_ref, _outline = self.approved_chain()
        # A framework edit supersedes the framework gate, refusing the outline.
        self.put_framework(synthesis_ref, revision="fw-r2", parent="fw-r1")
        with self.assertRaises(Exception):
            self.synth.require_outline(_outline)

    def test_cross_project_ref_is_rejected(self):
        audit_ref, _batch = self.accepted_audit()
        synthesis_ref = self.put_synthesis(audit_ref)
        self.approve_ref(synthesis_ref, GATE_SYNTHESIS_KIND, GATE_SYNTHESIS)
        foreign = {"project_id": "OTHER", "artifact_id": synthesis_ref["artifact_id"],
                   "revision_id": synthesis_ref["revision_id"]}
        with self.assertRaises(RecordValidationError):
            self.put_framework(foreign)


class BlockingAndClassificationTests(SynthesisBase):
    def test_blocked_statement_refused_regardless_of_status(self):
        audit_ref, _batch = self.accepted_audit(unknown_population=True)
        ref = self.put_synthesis(audit_ref)
        # The statement is marked blocked by missing original context...
        stored = self.synth.read(ref, "synthesis")
        self.assertTrue(stored["result"]["statements"][0]["blocked"])
        # ...even after the human gate approves the artifact.
        self.approve_ref(ref, GATE_SYNTHESIS_KIND, GATE_SYNTHESIS)
        with self.assertRaises(SynthesisWorkflowError):
            self.synth.require_synthesis(ref)

    def test_unclassified_concept_refuses_frame_and_establish(self):
        audit_ref, _batch = self.accepted_audit()
        synthesis_ref = self.put_synthesis(audit_ref)
        self.approve_ref(synthesis_ref, GATE_SYNTHESIS_KIND, GATE_SYNTHESIS)
        # A concept without its counterevidence safeguard exists but is unclassified.
        content = self.framework_content()
        del content["concepts"][1]["counterevidence_note"]
        framework_ref = self.synth.propose(
            "framework", {"synthesis_ref": synthesis_ref, "content": content},
            artifact_id="fw", revision_id="fw-r1", rights=self.rights("fw", "fw-r1"),
        )
        self.assertEqual(
            self.synth.read(framework_ref, "framework")["result"]["unclassified_concepts"], ["C2"]
        )
        self.approve_ref(framework_ref, GATE_FRAMEWORK_KIND, GATE_FRAMEWORK)
        with self.assertRaises(SynthesisWorkflowError):
            self.synth.require_framework(framework_ref)

    def test_requested_established_without_safeguards_rejected_at_proposal(self):
        audit_ref, _batch = self.accepted_audit()
        synthesis_ref = self.put_synthesis(audit_ref)
        self.approve_ref(synthesis_ref, GATE_SYNTHESIS_KIND, GATE_SYNTHESIS)
        content = self.framework_content()
        content["concepts"][0]["status"] = "established"
        with self.assertRaises(SynthesisWorkflowError):
            self.synth.propose(
                "framework", {"synthesis_ref": synthesis_ref, "content": content},
                artifact_id="fw", revision_id="fw-r1", rights=self.rights("fw", "fw-r1"),
            )


class ImpactTests(SynthesisBase):
    def _impact_of(self, ref, kind):
        return self.synth.read_impact(ref, kind)

    def test_definition_edit_flags_dependent_section_and_stales_gate(self):
        synthesis_ref, framework_ref, outline_ref = self.approved_chain()
        self.synth.require_outline(outline_ref)
        edited = self.put_framework(
            synthesis_ref, revision="fw-r2", parent="fw-r1",
            c1_definition="A revised Compound Z definition.",
        )
        impact = self._impact_of(edited, "framework")
        self.assertEqual(impact["parent_ref"], framework_ref)
        self.assertIn("C1", impact["changed"]["concepts"])
        self.assertEqual(len(impact["dependent_outlines"]), 1)
        entry = impact["dependent_outlines"][0]
        self.assertEqual(entry["outline_ref"], outline_ref)
        self.assertIn("sec1", entry["affected_sections"])
        self.assertNotIn("sec2", entry["affected_sections"])
        self.assertIn("sec2", entry["unaffected_sections"])
        self.assertEqual(impact["stale_gates"], [GATE_FRAMEWORK, GATE_OUTLINE])
        # The framework change stales the dependent outline gate.
        with self.assertRaises(Exception):
            self.synth.require_outline(outline_ref)
        # The impact stays readable after the old outline is stale, and the old
        # revision remains immutable.
        self.assertEqual(self._impact_of(edited, "framework")["kind"], "framework")
        self.assertEqual(
            self.synth.read(framework_ref, "framework", current=False)["result"]["concepts"][0]["definition"],
            "Compound Z tumor-volume effect.",
        )

    def test_relationship_edit_flags_indirect_concept_endpoints(self):
        synthesis_ref, framework_ref, outline_ref = self.approved_chain()
        edited = self.put_framework(
            synthesis_ref, revision="fw-r2", parent="fw-r1",
            r1_directionality="reversed: the effect constrains shared-study reports",
        )
        impact = self._impact_of(edited, "framework")
        self.assertIn("R1", impact["changed"]["relationships"])
        entry = impact["dependent_outlines"][0]
        self.assertEqual(entry["outline_ref"], outline_ref)
        # sec2 depends directly on R1; sec1 depends on the C1 endpoint (indirect).
        self.assertIn("relationship:R1", entry["affected_sections"]["sec2"])
        self.assertIn("concept:C2", entry["affected_sections"]["sec2"])
        self.assertIn("concept:C1", entry["affected_sections"]["sec1"])

    def test_statement_edit_flags_synthesis_statement_sections(self):
        synthesis_ref, framework_ref, outline_ref = self.approved_chain()
        content = self.synthesis_content()
        content["statements"][0]["text"] = "A materially revised statement."
        edited = self.synth.propose(
            "synthesis", {"audit_ref": self.synth.read(synthesis_ref, "synthesis")["input"]["audit_ref"],
                          "content": content},
            artifact_id="syn", revision_id="syn-r2", rights=self.rights("syn", "syn-r2"),
            parent_revision_id="syn-r1",
        )
        impact = self._impact_of(edited, "synthesis")
        self.assertIn("S-1", impact["changed"]["statements"])
        entry = impact["dependent_outlines"][0]
        self.assertEqual(entry["outline_ref"], outline_ref)
        self.assertIn("statement:S-1", entry["affected_sections"]["sec2"])


# --------------------------------------------------------------------------- #
# Integration: extend the real runner / fake-transport end-to-end fixture.
# --------------------------------------------------------------------------- #
from test_extraction_runner import (  # noqa: E402
    FullFlowIntegrationTest,
    _FakeTransport,
    workflow as extraction_workflow,
)


class SynthesisOverRealRunnerTest(FullFlowIntegrationTest):
    """Prove the real Step 09 receipt/outputs gate is consumed by synthesis.

    This class *extends* ``FullFlowIntegrationTest`` (it does not replicate the
    frozen-corpus/acquisition fixture).  Its inherited full-flow cases are
    deselected here so the giant fixture runs once, in its own module.
    """

    test_positive_full_flow_to_synthesis = None
    test_important_audit_pauses_exact_batch_until_new_batch = None
    test_presend_revalidates_revoked_pilot = None
    test_bulk_source_not_in_manifest_membership_blocked = None

    def setUp(self):
        super().setUp()
        self.synth = SynthesisWorkflowRepository(self.store)

    def test_step09_outputs_gate_consumed(self):
        src, acquisition_ref = self._acquire()
        self._config_and_report(src)
        pilot_ref = self._pilot()
        batch_ref, _cases = self._bulk_case(pilot_ref, acquisition_ref)
        audit_ref = self.workflow.propose(
            "audit", {"batch_ref": batch_ref, "checks": {"case-1": self._check(important_error=False)}},
            artifact_id="art-audit", revision_id="rev-audit",
            rights=self.rights("art-audit", "rev-audit"),
        )
        # A minimal synthesis that explicitly dispositions every accepted finding
        # (none incorporated), so it validates over the unknown-size case set.
        accepted_cases = self.workflow.require_batch(batch_ref)["cases"]
        dispositions = [
            {"finding": f"{case['id']}:{finding['finding_id']}", "disposition": "not_used",
             "rationale": "integration smoke; not used in the memo"}
            for case in accepted_cases
            for finding in case["extraction"]["findings"]
        ]
        content = {"schema_version": 1, "memo": "integration smoke", "statements": [],
                   "dispositions": dispositions}
        payload = {"audit_ref": audit_ref, "content": content}

        # Before the extraction.outputs gate: the synthesis proposal fails closed.
        with self.assertRaises(extraction_workflow.ExtractionWorkflowError):
            self.synth.propose("synthesis", payload, artifact_id="art-syn",
                               revision_id="rev-syn", rights=self.rights("art-syn", "rev-syn"))

        self.approve("art-audit", "rev-audit", extraction_workflow.GATE_KIND,
                     extraction_workflow.GATE_OUTPUTS)
        ref = self.synth.propose("synthesis", payload, artifact_id="art-syn",
                                 revision_id="rev-syn", rights=self.rights("art-syn", "rev-syn"))
        # The exact named synthesis gate is still required for authority.
        with self.assertRaises(SynthesisWorkflowError):
            self.synth.require_synthesis(ref)
        self.approve("art-syn", "rev-syn", GATE_SYNTHESIS_KIND, GATE_SYNTHESIS)
        result = self.synth.require_synthesis(ref)
        self.assertEqual(result["kind"], "synthesis")
        self.assertTrue(result["findings"])


if __name__ == "__main__":
    import unittest

    unittest.main()
