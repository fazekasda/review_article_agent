"""Synthetic extraction-workflow suite: pilots, batches, audits, synthesis gate.

The durable-runner seam (``workflow._require_run``) is patched here with synthetic
**verified receipts**: this suite tests the workflow's own comparison, gating,
selection, escalation, and permanent-pause logic only.  Real runner/dispatch
verification is exercised by the later integration tests, not here.  No network,
provider, or model call is made.

Run::

    PYTHONPATH=src python3 -m unittest tests.test_extraction_workflow -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock
from _tiers import slow

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from test_acquisition_manifest import ManifestBase  # noqa: E402

from review_article_agent.domain.records import canonical_json  # noqa: E402
from review_article_agent.extraction import workflow  # noqa: E402
from review_article_agent.extraction.workflow import (  # noqa: E402
    GATE_BATCH,
    GATE_CONFIG,
    GATE_OUTPUTS,
    GATE_PILOT,
    MANDATORY_FAILURE_LABELS,
    QUALITY_CHECKS,
    ExtractionWorkflowError,
    ExtractionWorkflowRepository,
    validate_quality,
)
from review_article_agent.parsing.core import NORMALIZATION_VERSION, PARSER_VERSION  # noqa: E402

PASSAGES = [
    {
        "passage_id": "p1",
        "source_key": "docA",
        "text": "In mice, compound Z did not reduce tumor volume (12.5 mm3) versus vehicle.",
        "asset_ids": ["tbl1"],
    }
]

EXTRACTION_MODEL = {
    "model_id": "deepseek/v4-flash",
    "upstream_provider": "openrouter",
    "max_output_tokens": 4096,
    "context_tokens": 128000,
    "input_price_per_million": "0.10",
    "output_price_per_million": "0.40",
    "pricing_ref": "pricing:openrouter:v1",
    "instructions_revision": "extract-v1",
    "timeout_seconds": 60.0,
    "max_response_bytes": 1000000,
}
QUALITY_MODEL = dict(EXTRACTION_MODEL, instructions_revision="quality-v1")


def _context(*, unknown_population=False):
    context = {
        "species": {"state": "known", "value": "mouse"},
        "population": {"state": "known", "value": "adult mice"},
        "assay": {"state": "known", "value": "tumor volume"},
        "outcome": {"state": "known", "value": "tumor volume"},
        "conditions": {"state": "known", "value": "vehicle"},
    }
    if unknown_population:
        context["population"] = {"state": "unknown", "value": None}
    return context


def _extraction_output(*, omitted=False, secondary=False, unknown_population=False, appraisal=None):
    asset = (
        {"target_kind": "asset", "target_id": "tbl1", "disposition": "omitted", "reason": "table_not_acquired"}
        if omitted
        else {"target_kind": "asset", "target_id": "tbl1", "disposition": "no_finding"}
    )
    return {
        "schema_version": 1,
        "summary": "Synthetic summary.",
        "source_records": [{"source_key": "docA", "source_kind": "document"}],
        "findings": [
            {
                "finding_id": "F1",
                "source_key": "docA",
                "passage_id": "p1",
                "snippet": "did not reduce tumor volume",
                "statement": "Compound Z did not reduce tumor volume versus vehicle.",
                "attribution": "secondary_account" if secondary else "direct_observation",
                "citation_flag": "own_result",
                "context": _context(unknown_population=unknown_population),
                "secondary_warning": {"state": "known", "value": True}
                if secondary
                else {"state": "not_applicable", "value": None},
                "limitations": {"state": "known", "value": "single model"},
                "numeric_details": [
                    {
                        "name": "tumor volume",
                        "value": {"state": "known", "value": 12.5},
                        "unit": {"state": "known", "value": "mm3"},
                        "denominator": {"state": "missing", "value": None},
                    }
                ],
            }
        ],
        "coverage": [
            {"target_kind": "passage", "target_id": "p1", "disposition": "finding", "finding_id": "F1"},
            asset,
        ],
        "appraisal": appraisal
        if appraisal is not None
        else [
            {"dimension_id": "risk_of_bias", "state": "assessed", "judgement": "low", "rationale": "randomised"}
        ],
    }


def _quality_output(*, checks=None, escalation="none", issues=None):
    resolved = {name: True for name in QUALITY_CHECKS}
    if checks:
        resolved.update(checks)
    return {"checks": resolved, "issues": issues or [], "escalation": escalation}


class WorkflowBase(ManifestBase):
    def setUp(self):
        super().setUp()
        self.workflow = ExtractionWorkflowRepository(self.store)
        self.receipts = {}
        patcher = mock.patch.object(workflow, "_require_run", self._fake_require_run)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._n = 0
        self._cfgrev = 1
        self.config_ref = self.put("config", self.config())
        self.approve_ref(self.config_ref, GATE_KIND, GATE_CONFIG)

    def _rebind(self):
        self.workflow = ExtractionWorkflowRepository(self.store)

    def reconfigure(self, **overrides):
        """Supersede the approved config with a new revision + fresh gate."""
        parent = self.config_ref["revision_id"]
        self._cfgrev += 1
        self.config_ref = self.put(
            "config", self.config(**overrides), revision=f"config-r{self._cfgrev}", parent=parent
        )
        self.approve_ref(self.config_ref, GATE_KIND, GATE_CONFIG)
        return self.config_ref

    def _fake_require_run(self, store, ref, *, purpose):
        return self.receipts[(ref["artifact_id"], ref["revision_id"])]

    def config(self, **overrides):
        cfg = {
            "config_schema_version": 1,
            "appraisal_dimensions": [{"dimension_id": "risk_of_bias"}],
            "pilot_size": 1,
            "random_size": 1,
            "risk_size": 1,
            "acceptance_rule": "No important errors; inspect all pilot sources.",
            "extraction_model": dict(EXTRACTION_MODEL),
            "quality_model": dict(QUALITY_MODEL),
            "extraction_instructions": "Extract grounded findings.",
            "extraction_instructions_revision": "extract-v1",
            "quality_instructions": "Audit omissions and fields.",
            "quality_instructions_revision": "quality-v1",
            "parser": PARSER_VERSION,
            "normalization": NORMALIZATION_VERSION,
        }
        cfg.update(overrides)
        return cfg

    def put(self, kind, payload, *, revision=None, parent=None):
        revision = revision or kind + "-r1"
        return self.workflow.propose(
            kind, payload, artifact_id=kind, revision_id=revision,
            rights=self.rights(kind, revision), parent_revision_id=parent,
        )

    def approve_ref(self, ref, gate_kind, gate_id):
        self.approve(ref["artifact_id"], ref["revision_id"], gate_kind, gate_id)

    def dispatch(self, label, *, config_ref, observed_model, raw_output, purpose="extraction",
                 extraction_ref=None, source_refs=None, passages=None):
        self._n += 1
        artifact = f"dispatch-{self._n}-{label}"
        revision = artifact + "-r1"
        self.artifacts.create_artifact(artifact, kind="report")
        self.artifacts.import_revision(
            artifact_id=artifact, content=b"dispatch", origin_locator="extraction:dispatch",
            rights=self.rights(artifact, revision), revision_id=revision,
        )
        ref = self.ref(artifact, revision)
        self.receipts[(artifact, revision)] = {
            "purpose": purpose,
            "config_ref": config_ref,
            "report_refs": [],
            "source_refs": source_refs or [],
            "passages": PASSAGES if passages is None else passages,
            "raw_output": raw_output,
            "observed_model": observed_model,
            "dispatch_id": artifact,
            "extraction_ref": extraction_ref,
        }
        return ref

    def case(self, name, *, source_refs=None, raw_output=None, quality=None, extraction_model=None):
        extraction = self.dispatch(
            name + "-ext", config_ref=self.config_ref,
            observed_model=extraction_model or EXTRACTION_MODEL["model_id"],
            raw_output=raw_output if raw_output is not None else _extraction_output(),
            source_refs=source_refs,
        )
        quality_ref = self.dispatch(
            name + "-qual", config_ref=self.config_ref, observed_model=QUALITY_MODEL["model_id"],
            purpose="quality", extraction_ref=extraction,
            raw_output=quality if quality is not None else _quality_output(),
        )
        return {"id": name, "extraction_ref": extraction, "quality_ref": quality_ref}

    @staticmethod
    def adjudication(labels, *, important_failure=False, severity="none", corrective_action=None, **overrides):
        record = {
            "labels": list(labels),
            "inspected_original": True,
            "checked_omissions": True,
            "checked_qualifications": True,
            "checked_fields": True,
            "important_failure": important_failure,
            "severity": severity,
            "rationale": "Inspected the original source, omissions, qualifications, and returned fields.",
            "corrective_action": corrective_action,
        }
        record.update(overrides)
        return record

    def build_pilot(self, cases, adjudications):
        pilot_ref = self.put(
            "pilot", {"config_ref": self.config_ref, "cases": cases, "adjudications": adjudications}
        )
        return pilot_ref

    def audit_check(self, *, important_error=False, corrective_action=None):
        return {
            "inspected_original": True,
            "omissions": True,
            "qualifications": True,
            "fields": True,
            "important_error": important_error,
            "rationale": "Compared the extracted fields against the original source.",
            "corrective_action": corrective_action,
        }


GATE_KIND = workflow.GATE_KIND


class PilotTests(WorkflowBase):
    def test_mandatory_labels_required(self):
        case = self.case("c1")
        with self.assertRaises(ExtractionWorkflowError):
            self.build_pilot([case], {"c1": self.adjudication(["negation"])})

    def test_successful_pilot_has_no_blocking_failures(self):
        case = self.case("c1")
        pilot_ref = self.build_pilot([case], {"c1": self.adjudication(MANDATORY_FAILURE_LABELS)})
        self.approve_ref(pilot_ref, GATE_KIND, GATE_PILOT)
        pilot = self.workflow.require_pilot(pilot_ref)
        self.assertEqual(pilot["blocking_failures"], [])
        self.assertEqual(set(pilot["mandatory_labels"]), set(MANDATORY_FAILURE_LABELS))
        self.assertFalse(pilot["authorizes_extraction"])

    def test_important_pilot_failure_blocks(self):
        case = self.case("c1")
        pilot_ref = self.build_pilot(
            [case],
            {"c1": self.adjudication(
                MANDATORY_FAILURE_LABELS, important_failure=True, severity="major",
                corrective_action="Correct the instruction and re-run the pilot.",
            )},
        )
        self.approve_ref(pilot_ref, GATE_KIND, GATE_PILOT)
        with self.assertRaises(ExtractionWorkflowError):
            self.workflow.require_pilot(pilot_ref)

    def test_bad_quality_marks_case_and_pilot_blocked(self):
        blocked = validate_quality(_quality_output(checks={"omissions": False}))
        self.assertTrue(blocked["blocked"])
        self.assertIn("omissions", blocked["failed_checks"])
        case = self.case("c1", quality=_quality_output(escalation="human"))
        pilot_ref = self.build_pilot(
            [case],
            {"c1": self.adjudication(
                MANDATORY_FAILURE_LABELS, corrective_action="Escalate to a human/stronger re-run."
            )},
        )
        self.assertTrue(self.workflow._pilot(self.workflow._read(pilot_ref, "pilot")["input"])["blocking_failures"])
        self.approve_ref(pilot_ref, GATE_KIND, GATE_PILOT)
        with self.assertRaises(ExtractionWorkflowError):
            self.workflow.require_pilot(pilot_ref)

    def test_omitted_coverage_must_escalate_not_auto_accept(self):
        case = self.case("c1", raw_output=_extraction_output(omitted=True))
        # The gate is "none" though the extraction omitted a required asset.
        pilot_ref = self.build_pilot(
            [case],
            {"c1": self.adjudication(
                MANDATORY_FAILURE_LABELS, corrective_action="Escalate the omission to a human."
            )},
        )
        derived = self.workflow._pilot(self.workflow._read(pilot_ref, "pilot")["input"])
        self.assertIn("omitted_coverage", derived["cases"][0]["attention"])
        self.assertTrue(derived["cases"][0]["requires_escalation"])
        self.assertTrue(derived["cases"][0]["blocked"])

    def test_material_config_change_stales_the_pilot(self):
        case = self.case("c1")
        pilot_ref = self.build_pilot([case], {"c1": self.adjudication(MANDATORY_FAILURE_LABELS)})
        self.approve_ref(pilot_ref, GATE_KIND, GATE_PILOT)
        self.workflow.require_pilot(pilot_ref)
        # A material config edit supersedes the pinned revision and its gate.
        self.put("config", self.config(pilot_size=2), revision="config-r2", parent="config-r1")
        with self.assertRaises(ExtractionWorkflowError):
            self.workflow.require_pilot(pilot_ref)

    def test_model_identity_mismatch_is_rejected(self):
        case = self.case("c1", extraction_model="some/other-model")
        with self.assertRaises(ExtractionWorkflowError):
            self.build_pilot([case], {"c1": self.adjudication(MANDATORY_FAILURE_LABELS)})

    def test_config_schema_version_type_is_exactly_int(self):
        for bad in (True, "1", 1.0):
            with self.assertRaises(ExtractionWorkflowError):
                self.workflow._validate_config(self.config(config_schema_version=bad))

    def test_approved_formal_tool_only(self):
        # A dimension may carry one human-approved formal tool + applicability.
        self.reconfigure(
            appraisal_dimensions=[
                {"dimension_id": "risk_of_bias", "formal_tool": "RoB2", "applicability": "randomised trials"}
            ]
        )
        config = self.workflow._validate_config(self.workflow._read(self.config_ref, "config"))
        config_id = canonical_json(self.workflow._ref(self.config_ref))

        def appraisal(tool):
            entry = {"dimension_id": "risk_of_bias", "state": "assessed", "judgement": "low", "rationale": "x"}
            if tool is not None:
                entry["tool_applied"] = {"state": "known", "value": tool}
            return [entry]

        allowed = self.case("c1", raw_output=_extraction_output(appraisal=appraisal("RoB2")))
        self.assertEqual(self.workflow._case(config, config_id, allowed)["id"], "c1")
        denied_tool = self.case("c2", raw_output=_extraction_output(appraisal=appraisal("OtherTool")))
        with self.assertRaises(ExtractionWorkflowError):
            self.workflow._case(config, config_id, denied_tool)

        # A bare dimension must not report any known tool.
        self.reconfigure(appraisal_dimensions=[{"dimension_id": "risk_of_bias"}])
        bare_config = self.workflow._validate_config(self.workflow._read(self.config_ref, "config"))
        bare_id = canonical_json(self.workflow._ref(self.config_ref))
        bare_tool = self.case("c3", raw_output=_extraction_output(appraisal=appraisal("RoB2")))
        with self.assertRaises(ExtractionWorkflowError):
            self.workflow._case(bare_config, bare_id, bare_tool)

        # A formal_tool without an explicit applicability is rejected outright.
        with self.assertRaises(ExtractionWorkflowError):
            self.workflow._validate_config(
                self.config(appraisal_dimensions=[{"dimension_id": "risk_of_bias", "formal_tool": "RoB2"}])
            )

    def test_restart_preserves_gated_pilot(self):
        case = self.case("c1")
        pilot_ref = self.build_pilot([case], {"c1": self.adjudication(MANDATORY_FAILURE_LABELS)})
        self.approve_ref(pilot_ref, GATE_KIND, GATE_PILOT)
        self._reopen()
        self._rebind()
        pilot = self.workflow.require_pilot(pilot_ref)
        self.assertEqual(pilot["blocking_failures"], [])


@slow
class BatchAndAuditTests(WorkflowBase):
    def _gated_pipeline(self, cases, *, adjudications=None):
        pilot_ref = self.put(
            "pilot",
            {"config_ref": self.config_ref, "cases": cases,
             "adjudications": adjudications or {c["id"]: self.adjudication(MANDATORY_FAILURE_LABELS) for c in cases}},
        )
        self.approve_ref(pilot_ref, GATE_KIND, GATE_PILOT)
        return pilot_ref

    def test_batch_selection_is_deterministic_and_risk_weighted(self):
        _a, _b, source, entries = self.standard()
        manifest = self.make(entries)
        self.gate(manifest)
        cases = [
            self.case("clean", source_refs=[source]),
            self.case("risky", source_refs=[source],
                      raw_output=_extraction_output(secondary=True, unknown_population=True)),
            self.case("secondary", source_refs=[source], raw_output=_extraction_output(secondary=True)),
        ]
        pilot_ref = self._gated_pipeline(cases)
        batch_input = {
            "config_ref": self.config_ref, "pilot_ref": pilot_ref,
            "acquisition_ref": self.ref_of(manifest), "cases": cases, "seed": "s-1",
        }
        batch_ref = self.put("batch", batch_input)
        self.approve_ref(batch_ref, GATE_KIND, GATE_BATCH)
        batch = self.workflow.require_batch(batch_ref)
        self.assertEqual(batch["random_ids"], self.workflow.require_batch(batch_ref)["random_ids"])
        self.assertIn("risky", batch["risk_ids"])
        self.assertIn("secondary", batch["risk_signals"]["risky"])
        self.assertIn("missing_context", batch["risk_signals"]["risky"])
        self.assertGreater(
            len(batch["risk_signals"]["risky"]), len(batch["risk_signals"]["clean"])
        )

    def test_important_audit_error_permanently_pauses_the_batch(self):
        _a, _b, source, entries = self.standard()
        manifest = self.make(entries)
        self.gate(manifest)
        case = self.case("c1", source_refs=[source])
        pilot_ref = self._gated_pipeline([case])
        batch_ref = self.put(
            "batch",
            {"config_ref": self.config_ref, "pilot_ref": pilot_ref,
             "acquisition_ref": self.ref_of(manifest), "cases": [case], "seed": "s-1"},
        )
        self.approve_ref(batch_ref, GATE_KIND, GATE_BATCH)
        bad_audit = self.put(
            "audit",
            {"batch_ref": batch_ref,
             "checks": {"c1": self.audit_check(important_error=True, corrective_action="Re-extract the source.")}},
        )
        bad = self.workflow._read(bad_audit, "audit")
        self.assertTrue(bad["paused"])
        # A later clean audit revision cannot erase the permanent pause.
        clean_audit = self.put(
            "audit",
            {"batch_ref": batch_ref, "checks": {"c1": self.audit_check()}},
            revision="audit-r2", parent="audit-r1",
        )
        self.approve_ref(clean_audit, GATE_KIND, GATE_OUTPUTS)
        with self.assertRaises(ExtractionWorkflowError):
            self.workflow.require_for_synthesis(clean_audit)

    def test_synthesis_gate_success_returns_accepted_evidence(self):
        _a, _b, source, entries = self.standard()
        manifest = self.make(entries)
        self.gate(manifest)
        case = self.case("c1", source_refs=[source])
        pilot_ref = self._gated_pipeline([case])
        batch_ref = self.put(
            "batch",
            {"config_ref": self.config_ref, "pilot_ref": pilot_ref,
             "acquisition_ref": self.ref_of(manifest), "cases": [case], "seed": "s-1"},
        )
        self.approve_ref(batch_ref, GATE_KIND, GATE_BATCH)
        audit_ref = self.put("audit", {"batch_ref": batch_ref, "checks": {"c1": self.audit_check()}})
        self.approve_ref(audit_ref, GATE_KIND, GATE_OUTPUTS)
        evidence = self.workflow.require_for_synthesis(audit_ref)
        self.assertTrue(evidence["accepted"])
        self.assertEqual(evidence["case_ids"], ["c1"])
        self.assertEqual(evidence["audited_ids"], ["c1"])
        self.assertEqual(evidence["authority"], "named_gate_only")
        self.assertEqual(evidence["guarantee"], "sampled_audit_not_completeness")

    def test_synthesis_requires_the_outputs_gate(self):
        _a, _b, source, entries = self.standard()
        manifest = self.make(entries)
        self.gate(manifest)
        case = self.case("c1", source_refs=[source])
        pilot_ref = self._gated_pipeline([case])
        batch_ref = self.put(
            "batch",
            {"config_ref": self.config_ref, "pilot_ref": pilot_ref,
             "acquisition_ref": self.ref_of(manifest), "cases": [case], "seed": "s-1"},
        )
        self.approve_ref(batch_ref, GATE_KIND, GATE_BATCH)
        audit_ref = self.put("audit", {"batch_ref": batch_ref, "checks": {"c1": self.audit_check()}})
        with self.assertRaises(ExtractionWorkflowError):
            self.workflow.require_for_synthesis(audit_ref)


if __name__ == "__main__":
    unittest.main()
