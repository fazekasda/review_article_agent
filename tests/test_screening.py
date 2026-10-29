"""Small synthetic screening integration suite; no provider calls."""

import copy
import unittest
from types import SimpleNamespace

from test_acquisition_manifest import ManifestBase
from review_article_agent.parsing import ParsingRepository
from review_article_agent.acquisition.manifest import ManifestNotAcquiredError
from review_article_agent.cli.screening import dispatch_screening
from review_article_agent.parsing.core import NORMALIZATION_VERSION, PARSER_VERSION
from review_article_agent.runtime.jev import MODEL
from review_article_agent.screening import ScreeningError, ScreeningRepository
from review_article_agent.screening.workflow import classify, questions


def config():
    return {"questions": [{"id": "eligible", "instructions": "Does the original text meet the protocol eligibility criteria?",
                           "purpose": "eligibility", "exclude_below": "0.2", "retain_above": "0.8"}],
            "pilot_size": 1, "random_size": 1, "risk_size": 1,
            "acceptance_rule": "No important errors; inspect all pilot sources.",
            "model": MODEL, "parser": PARSER_VERSION, "normalization": NORMALIZATION_VERSION}


def response(p):
    return {"model": MODEL, "answers": {"eligible": {"type": "noul", "noul": p}},
            "usage": {"input_tokens": 10, "output_tokens": 0}}


class MappingTests(unittest.TestCase):
    def test_conservative_mapping_and_failure_retention(self):
        for p, label in [(0.1, "proposed_exclusion"), (0.2, "uncertain"), (0.5, "uncertain"), (0.8, "retain")]:
            out = classify(config(), response(p))
            self.assertEqual(out["label"], label)
            self.assertEqual(float(out["decisions"][0]["probability"]), p)
        for bad in (None, {}, response(2), {**response(0.1), "answers": {}}, {**response(0.1), "model": "other"}):
            self.assertEqual(classify(config(), bad)["label"], "uncertain")
        self.assertEqual(classify(config(), response(0.1), context_ready=False)["label"], "uncertain")

    def test_no_implicit_threshold_or_framework_eligibility(self):
        for change in ({"purpose": "hypothesis"}, {"exclude_below": "NaN"}, {"retain_above": "0.1"}):
            cfg = config()
            cfg["questions"][0].update(change)
            with self.assertRaises(ScreeningError):
                questions(cfg)


class WorkflowTests(ManifestBase):
    def setUp(self):
        super().setUp()
        self.screening = ScreeningRepository(self.store)
        _, _, self.source, entries = self.standard()
        self.manifest = self.make(entries)
        self.gate(self.manifest)
        # Preserve real source identity while adding explicit paragraph context.
        self.report = ParsingRepository(self.store).parse_stored_document(
            self.source, report_artifact_id="parsed", report_revision_id="parsed-r1",
            rights=self.rights("parsed", "parsed-r1"))["report_ref"]
        self.config_ref = self.put("config", config())
        self.case = {"id": "paper1", "report_ref": self.report, "passage_id": None, "response": response(0.1)}
        self.pilot_input = {"config_ref": self.config_ref, "cases": [self.case],
                            "adjudications": {"paper1": {"label": "exclude", "inspected_source": True,
                             "important_failure": False, "rationale": "Checked original source", "corrective_action": None}}}
        self.pilot = self.put("pilot", self.pilot_input)

    def put(self, kind, payload, *, artifact=None, revision=None, parent=None):
        artifact = artifact or kind
        revision = revision or artifact + "-r1"
        return self.screening.propose(kind, payload, artifact_id=artifact, revision_id=revision,
                                      rights=self.rights(artifact, revision), parent_revision_id=parent)

    def approve_ref(self, ref, kind, gate):
        self.approve(ref["artifact_id"], ref["revision_id"], kind, gate)

    def batch_input(self):
        return {"config_ref": self.config_ref, "pilot_ref": self.pilot,
                "acquisition_ref": self.ref_of(self.manifest), "cases": [self.case], "seed": "fixed"}

    def make_batch(self):
        self.approve_ref(self.pilot, "pilot", "screening.pilot")
        return self.put("batch", self.batch_input())

    def audit_input(self, batch, false=False):
        return {"batch_ref": batch, "checks": {"paper1": {"inspected_source": True,
                "false_exclusion": false, "rationale": "Read exact original source",
                "corrective_action": "Revise rules and re-pilot" if false else None}}}

    def test_real_gates_audit_replay_and_restart(self):
        batch = self.make_batch()
        result = self.screening.read(batch, "batch")
        self.assertEqual(result["random_ids"], ["paper1"])
        self.assertEqual(result["risk_ids"], ["paper1"])
        self.assertEqual(self.screening.read(batch, "batch"), result)
        audit = self.put("audit", self.audit_input(batch))
        with self.assertRaises(ScreeningError):
            self.screening.require_exclusions(audit)
        self.approve_ref(audit, "exclusion_set", "screening.exclusions")
        self.assertEqual(self.screening.require_exclusions(audit), ["paper1"])
        self._reopen()
        self.assertEqual(ScreeningRepository(self.store).require_exclusions(audit), ["paper1"])

    def test_missing_or_wrong_pilot_gate_blocks_batch(self):
        with self.assertRaises(ScreeningError):
            self.put("batch", self.batch_input())
        self.approve_ref(self.pilot, "pilot", "other.pilot")
        with self.assertRaises(ScreeningError):
            self.put("batch", self.batch_input())
        self.assertIsNone(self.artifacts.get_artifact("batch"))

    def test_changed_config_invalidates_pilot(self):
        self.make_batch()
        cfg = config()
        cfg["questions"][0]["instructions"] = "New eligibility criteria"
        self.put("config", cfg, revision="config-r2", parent="config-r1")
        with self.assertRaises(ScreeningError):
            self.screening.require_pilot(self.pilot)

    def test_acquisition_rejection_blocks_batch(self):
        self.approve_ref(self.pilot, "pilot", "screening.pilot")
        self.gate(self.manifest, decision="rejected")
        with self.assertRaises(ManifestNotAcquiredError):
            self.put("batch", self.batch_input())
        self.assertIsNone(self.artifacts.get_artifact("batch"))

    def test_cli_show_defaults_to_summary(self):
        args = SimpleNamespace(project=self.project_id, artifact_id="pilot", revision_id="pilot-r1",
                               kind="pilot", screening_command="show", content=False)
        ctx = SimpleNamespace(registry=SimpleNamespace(get=lambda pid: SimpleNamespace(
            root_path=str(self.tmp / "project"), project_id=self.project_id)))
        result = dispatch_screening(args, ctx)
        self.assertEqual(result["case_count"], 1)
        self.assertNotIn("payload", result)
        args.content = True
        self.assertIn("adjudications", dispatch_screening(args, ctx)["payload"]["input"])

    def test_source_change_stales_screening(self):
        self.make_batch()
        data = self.artifacts.read_content(self.source["artifact_id"], self.source["revision_id"])
        self.append_source(self.source, data + b"\n")
        with self.assertRaises(ScreeningError):
            self.screening.require_pilot(self.pilot)

    def test_pilot_false_exclusion_blocks_even_when_approved(self):
        payload = copy.deepcopy(self.pilot_input)
        payload["adjudications"]["paper1"].update(label="retain", corrective_action="Revise rubric")
        pilot = self.put("pilot", payload, revision="pilot-r2", parent="pilot-r1")
        self.approve_ref(pilot, "pilot", "screening.pilot")
        with self.assertRaises(ScreeningError):
            self.screening.require_pilot(pilot)

    def test_false_exclusion_permanently_pauses_exact_batch(self):
        batch = self.make_batch()
        audit = self.put("audit", self.audit_input(batch, True))
        self.assertTrue(self.screening.read(audit, "audit")["paused"])
        replacement = self.put("audit", self.audit_input(batch), revision="audit-r2", parent="audit-r1")
        self.approve_ref(replacement, "exclusion_set", "screening.exclusions")
        with self.assertRaises(ScreeningError):
            self.screening.require_exclusions(replacement)

    def test_incomplete_source_checks_refused(self):
        batch = self.make_batch()
        with self.assertRaises(ScreeningError):
            self.put("audit", {"batch_ref": batch, "checks": {}})
        bad = copy.deepcopy(self.pilot_input)
        bad["cases"].append({**self.case, "id": "duplicate"})
        with self.assertRaises(ScreeningError):
            self.put("pilot", bad, revision="pilot-r2", parent="pilot-r1")


if __name__ == "__main__":
    unittest.main()
