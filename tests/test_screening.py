"""Small synthetic screening integration suite; no provider calls."""

import copy
import unittest
from types import SimpleNamespace
from unittest import mock

from test_acquisition_manifest import ManifestBase
from review_article_agent.parsing import ParsingRepository
from review_article_agent.acquisition.manifest import ManifestNotAcquiredError
from review_article_agent.cli.screening import dispatch_screening
from review_article_agent.domain.errors import StoreError
from review_article_agent.parsing.core import NORMALIZATION_VERSION, PARSER_VERSION
from review_article_agent.runtime.jev import MODEL
from review_article_agent.screening import ScreeningError, ScreeningRepository
from review_article_agent.screening.workflow import classify, questions
from review_article_agent.storage.store import ProjectStore


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

    def test_exclusion_validation_is_one_read_transaction(self):
        """A late failed audit cannot commit between the scan and the return.

        The hook fires on the second batch read (the coordinator's repro point:
        the first batch read happens while validating the audit, the second
        after the permanent-pause scan). With a single BEGIN IMMEDIATE read
        transaction the independent connection is locked out; without it the
        late audit commits and the stale decision is returned.
        """
        batch = self.make_batch()
        audit = self.put("audit", self.audit_input(batch))
        self.approve_ref(audit, "exclusion_set", "screening.exclusions")
        late = ProjectStore.open_at(self.app_dir, expected_project_id=self.project_id)
        self.addCleanup(late.close)
        late.connection.execute("PRAGMA busy_timeout = 0")
        late_screening = ScreeningRepository(late)
        original = ScreeningRepository._read
        reads = {"batch": 0}
        outcome = {}

        def hooked(repo, ref, kind):
            value = original(repo, ref, kind)
            if repo is self.screening and kind == "batch":
                reads["batch"] += 1
                if reads["batch"] == 2:
                    try:
                        late_screening.propose(
                            "audit", self.audit_input(batch, True),
                            artifact_id="late-audit", revision_id="late-audit-r1",
                            rights=self.rights("late-audit", "late-audit-r1"))
                    except StoreError as exc:  # BEGIN IMMEDIATE was locked out
                        outcome["error"] = exc
                    else:
                        outcome["error"] = None
            return value

        with mock.patch.object(ScreeningRepository, "_read", hooked):
            self.assertEqual(self.screening.require_exclusions(audit), ["paper1"])
        # The independent writer could not commit inside the validation unit.
        self.assertIsNotNone(outcome.get("error"))
        # Once the read transaction ended, the same writer persists the audit and
        # the next validation sees it (no caching, no mixed snapshot).
        late_screening.propose(
            "audit", self.audit_input(batch, True), artifact_id="late-audit",
            revision_id="late-audit-r1", rights=self.rights("late-audit", "late-audit-r1"))
        with self.assertRaises(ScreeningError):
            self.screening.require_exclusions(audit)

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

    # -- S8-R1: validated response model identity is pinned by the pilot ------ #
    def _second_case(self):
        passages = ParsingRepository(self.store).read_report(self.report)["payload"]["passages"]
        return {"id": "paper2", "report_ref": self.report,
                "passage_id": passages[0]["passage_id"], "response": response(0.1)}

    def test_pilot_pins_observed_model_and_same_model_batch_passes(self):
        batch = self.make_batch()
        pilot = self.screening.read(self.pilot, "pilot")
        self.assertEqual(pilot["cases"][0]["observed_model"], MODEL)
        result = self.screening.read(batch, "batch")
        self.assertEqual(result["cases"][0]["observed_model"], MODEL)

    def test_dated_snapshot_drift_fails_closed(self):
        scenarios = [
            (MODEL, MODEL + "-20261014"),                     # base pilot -> dated batch
            (MODEL + "-20261014", MODEL),                     # dated pilot -> base batch
            (MODEL + "-20261014", MODEL + "-20261015"),       # dated snapshot drift
        ]
        for index, (pilot_model, batch_model) in enumerate(scenarios):
            with self.subTest(pilot=pilot_model, batch=batch_model):
                payload = copy.deepcopy(self.pilot_input)
                payload["cases"][0]["response"]["model"] = pilot_model
                pilot = self.put("pilot", payload, artifact="drift-pilot-%d" % index,
                                 revision="drift-pilot-%d-r1" % index)
                self.approve_ref(pilot, "pilot", "screening.pilot")
                batch_input = copy.deepcopy(self.batch_input())
                batch_input["pilot_ref"] = pilot
                batch_input["cases"][0]["response"]["model"] = batch_model
                with self.assertRaises(ScreeningError):
                    self.put("batch", batch_input, artifact="drift-batch-%d" % index,
                             revision="drift-batch-%d-r1" % index)

    def test_mixed_pilot_models_cannot_authorize_batch(self):
        payload = copy.deepcopy(self.pilot_input)
        payload["cases"][0]["response"]["model"] = MODEL + "-20261014"
        second = self._second_case()
        second["response"]["model"] = MODEL + "-20261015"
        payload["cases"].append(second)
        payload["adjudications"]["paper2"] = dict(payload["adjudications"]["paper1"])
        pilot = self.put("pilot", payload, revision="pilot-r2", parent="pilot-r1")
        self.approve_ref(pilot, "pilot", "screening.pilot")
        batch_input = copy.deepcopy(self.batch_input())
        batch_input["pilot_ref"] = pilot
        batch_input["cases"][0]["response"]["model"] = MODEL + "-20261014"
        with self.assertRaises(ScreeningError):
            self.put("batch", batch_input, revision="batch-r2")

    def test_pilot_without_usable_model_cannot_authorize_batch(self):
        payload = copy.deepcopy(self.pilot_input)
        payload["cases"][0]["response"] = {**response(0.1), "model": "other/model"}
        payload["adjudications"]["paper1"]["label"] = "retain"
        pilot = self.put("pilot", payload, revision="pilot-r2", parent="pilot-r1")
        self.approve_ref(pilot, "pilot", "screening.pilot")
        batch_input = copy.deepcopy(self.batch_input())
        batch_input["pilot_ref"] = pilot
        with self.assertRaises(ScreeningError):
            self.put("batch", batch_input, revision="batch-r2")
        self.assertIsNone(self.artifacts.get_artifact("batch"))

    def test_failure_observation_is_retained_in_batch(self):
        second = self._second_case()
        second["response"] = {**response(0.1), "model": "not-jev"}
        self.approve_ref(self.pilot, "pilot", "screening.pilot")
        batch_input = copy.deepcopy(self.batch_input())
        batch_input["cases"].append(second)
        batch = self.put("batch", batch_input)
        result = self.screening.read(batch, "batch")
        failed = next(c for c in result["cases"] if c["id"] == "paper2")
        self.assertEqual(failed["machine"]["label"], "uncertain")
        self.assertIsNone(failed["observed_model"])


if __name__ == "__main__":
    unittest.main()
