#!/usr/bin/env python3
"""Offline unit regressions for ``scripts/check_contract_docs.py``.

Stdlib-only, offline, no network, no paid calls, no credentials or source text.
These tests exercise the *existing* validator functions and the finalized
2026-10-27 ratification/default evidence checks directly, and they pin the
fail-closed behavior on malformed records. They complement (do not replace)
``check_contract_docs.py --self-test``.

Run (from anywhere):

    python3 -m unittest discover -s scripts -p 'test_*.py'
"""

import copy
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import check_contract_docs as ccd  # noqa: E402


# --------------------------------------------------------------------------- #
# synthetic valid fixtures (structural only; no secrets, no source text)
# --------------------------------------------------------------------------- #
def rights_valid():
    ops = ("acquisition", "storage", "redistribution", "hosted_transfer")
    return {
        "rights_id": "RIGHTS-syn-A",
        "project_id": "proj-syn-1",
        "record_revision": 1,
        "subject": {"kind": "source_revision", "project_id": "proj-syn-1",
                    "artifact_id": "art-syn-A", "revision_id": "rev-syn-A"},
        "operations": {op: "allowed" for op in ops},
        "basis": {op: {"kind": "public_domain",
                       "evidence": {"ref": "lic:pd-syn-A",
                                    "provenance": "publisher:pd-list"}}
                  for op in ops},
        "authorization": {
            "actor": "human:user",
            "method": "out_of_band_authenticated",
            "authorized_at": "2026-10-27T00:00:00Z",
            "against_subject": {"kind": "source_revision",
                                "project_id": "proj-syn-1",
                                "artifact_id": "art-syn-A",
                                "revision_id": "rev-syn-A"},
            "against_rights_revision": 1,
            "basis": {"kind": "public_domain",
                      "evidence": {"ref": "lic:pd-syn-A",
                                   "provenance": "publisher:pd-list"}},
        },
        "restrictions": [],
        "privacy": {"personal_data": False, "confidentiality": "public",
                    "denial": False},
        "recorded_by": "agent:recorder",
        "recorded_at": "2026-10-27T00:00:00Z",
        "derived_status": "valid",
    }


def approval_valid():
    return {
        "approval_id": "appr_0001",
        "granted_by": {"actor_kind": "human", "actor_id": "user_owner",
                       "authority": "owner_authenticated",
                       "owner_auth_ref": "authrec_0001"},
        "project_id": "proj_A",
        "target": {"project_id": "proj_A", "artifact_id": "jev_rubric",
                   "revision_id": "rev_3"},
        "decision": "approved",
        "gate_kind": "pilot",
        "gate_id": "gate_pilot_1",
        "dependency_snapshot": {
            "snapshot_id": "snap_7",
            "project_id": "proj_A",
            "entries": [{"project_id": "proj_A", "artifact_id": "jev_rubric",
                         "revision_id": "rev_3",
                         "digest": "00112233445566778899aabbccddeeff"
                                   "00112233445566778899aabbccddeeff",
                         "role": "rubric"}],
            "versions": {
                "context_kind": "human_authored",
                "input_digest": "00112233445566778899aabbccddeeff"
                                "00112233445566778899aabbccddeeff",
                "model": {"state": "not_applicable"},
                "prompt": {"state": "not_applicable"},
                "rubric": {"state": "known",
                           "ref": {"version": "jev-rubric/1",
                                   "digest": "ffeeddccbbaa99887766554433221100"
                                             "ffeeddccbbaa99887766554433221100"}},
                "parser": {"state": "not_applicable"},
                "adapter": {"state": "not_applicable"},
                "route": "not_applicable",
            },
        },
        "granted_at": "2026-10-27T12:00:00Z",
        "status": "active",
        "rationale": "Pilot rubric meets the confirmed workflow requirements.",
    }


def decision_pred_valid():
    return {
        "decision_id": "dec_0001",
        "question_id": "is_original_report",
        "question_def_ref": {"project_id": "proj_A",
                             "artifact_id": "question_def",
                             "revision_id": "qd_rev_1"},
        "target": {"project_id": "proj_A", "artifact_id": "candidate_17",
                   "revision_id": "rev_3"},
        "answer": {"answer_kind": "predicate", "wire_type": "noul",
                   "probability": 0.83},
        "outcome": "answered",
        "versions": {
            "context_kind": "model_derived",
            "input_digest": "00112233445566778899aabbccddeeff"
                            "00112233445566778899aabbccddeeff",
            "model": {"state": "known",
                      "ref": {"version": "jev-1.14-20261014",
                              "digest": "ffeeddccbbaa99887766554433221100"
                                        "ffeeddccbbaa99887766554433221100"}},
            "prompt": {"state": "known",
                       "ref": {"version": "screen/1",
                               "digest": "ffeeddccbbaa99887766554433221100"
                                         "ffeeddccbbaa99887766554433221100"}},
            "rubric": {"state": "known",
                       "ref": {"version": "jev-rubric/1",
                               "digest": "ffeeddccbbaa99887766554433221100"
                                         "ffeeddccbbaa99887766554433221100"}},
            "parser": {"state": "not_applicable"},
            "adapter": {"state": "known",
                        "ref": {"version": "jev-adapter/1",
                                "digest": "ffeeddccbbaa99887766554433221100"
                                          "ffeeddccbbaa99887766554433221100"}},
            "route": "direct_call",
        },
        "route": "direct_call",
        "cost": {"state": "known", "amount_usd": "0.00002037",
                 "currency": "USD", "route": "direct_call",
                 "accounting_bucket": "direct_call", "reserve_usd": "0.25",
                 "dispatch_ref": "disp_0001"},
    }


def decision_score_valid():
    d = decision_pred_valid()
    d["decision_id"] = "dec_0002"
    d["question_id"] = "confidence_level"
    d["answer"] = {"answer_kind": "score", "wire_type": "score", "score": 1.09,
                   "legend": {"0": "low", "1": "moderate", "2": "high"},
                   "confidence": 0.36}
    return d


# --------------------------------------------------------------------------- #
class TestValidFixtures(unittest.TestCase):
    def test_valid_rights_passes(self):
        self.assertEqual(ccd.rights_violations(rights_valid(), "t"), [])

    def test_valid_approval_passes(self):
        self.assertEqual(ccd.approval_violations(approval_valid(), "t"), [])

    def test_valid_predicate_passes(self):
        self.assertEqual(ccd.decision_c02_violations(decision_pred_valid(), "t"), [])

    def test_valid_score_passes(self):
        self.assertEqual(ccd.decision_c02_violations(decision_score_valid(), "t"), [])


class TestNegativeMutations(unittest.TestCase):
    def test_bad_digest_rejected(self):
        d = copy.deepcopy(approval_valid())
        d["dependency_snapshot"]["entries"][0]["digest"] = "not-hex"
        self.assertTrue(ccd.approval_violations(d, "t"))

    def test_cross_project_approval_rejected(self):
        d = copy.deepcopy(approval_valid())
        d["target"]["project_id"] = "proj_other"
        self.assertTrue(ccd.approval_violations(d, "t"))

        d2 = copy.deepcopy(approval_valid())
        d2["dependency_snapshot"]["entries"][0]["project_id"] = "proj_other"
        self.assertTrue(ccd.approval_violations(d2, "t"))

    def test_cross_project_rights_rejected(self):
        d = copy.deepcopy(rights_valid())
        d["subject"]["project_id"] = "proj-other"
        self.assertTrue(ccd.rights_violations(d, "t"))

    def test_source_and_rights_revision_binding(self):
        # authorization must bind the exact rights record_revision ...
        d = copy.deepcopy(rights_valid())
        d["authorization"]["against_rights_revision"] = 2
        self.assertTrue(ccd.rights_violations(d, "t"))
        # ... and the exact source subject.
        d = copy.deepcopy(rights_valid())
        d["authorization"]["against_subject"] = {
            "kind": "source_revision", "project_id": "proj-syn-1",
            "artifact_id": "art-syn-A", "revision_id": "rev-other"}
        self.assertTrue(ccd.rights_violations(d, "t"))

    def test_unknown_cost_cannot_be_zero(self):
        c = {"state": "unknown", "amount_usd": "0", "currency": "USD",
             "route": "direct_call", "accounting_bucket": "direct_call",
             "reserve_usd": "0.25"}
        self.assertTrue(ccd.cost_violations(c, "t"))
        d = copy.deepcopy(decision_pred_valid())
        d["cost"] = c
        self.assertTrue(ccd.decision_c02_violations(d, "t"))

    def test_wrong_typed_answers(self):
        d = copy.deepcopy(decision_pred_valid())
        d["answer"]["probability"] = 1.5
        self.assertTrue(ccd.decision_c02_violations(d, "t"))

        d = copy.deepcopy(decision_pred_valid())
        d["answer"]["choice"] = "x"  # predicate carries a foreign field
        self.assertTrue(ccd.decision_c02_violations(d, "t"))

        d = copy.deepcopy(decision_score_valid())
        d["answer"]["score"] = 9.0  # outside declared levels 0..2
        self.assertTrue(ccd.decision_c02_violations(d, "t"))

        d = copy.deepcopy(decision_pred_valid())
        d["answer"]["wire_type"] = "choice"  # inconsistent with predicate
        self.assertTrue(ccd.decision_c02_violations(d, "t"))

    def test_missing_state_field_rejected(self):
        # a version slot that omits its state enum
        vers = copy.deepcopy(decision_pred_valid()["versions"])
        del vers["model"]["state"]
        self.assertTrue(ccd.version_violations(vers, "t"))

        # a rights record that omits a mandatory field
        d = copy.deepcopy(rights_valid())
        del d["privacy"]
        self.assertTrue(ccd.rights_violations(d, "t"))

        # an approval that omits a mandatory field
        d = copy.deepcopy(approval_valid())
        del d["rationale"]
        self.assertTrue(ccd.approval_violations(d, "t"))


class TestFailClosedOnMalformed(unittest.TestCase):
    """Malformed records must fail closed (violations / non-dict), never pass."""

    def test_empty_dicts_rejected(self):
        for fn in (ccd.rights_violations, ccd.approval_violations,
                   ccd.decision_c02_violations, ccd.cost_violations):
            self.assertTrue(fn({}, "t"), msg=fn.__name__)

    def test_non_dict_rejected(self):
        for fn in (ccd.rights_violations, ccd.approval_violations,
                   ccd.decision_c02_violations, ccd.cost_violations):
            for bad in ([], "x", 3, None):
                v = fn(bad, "t")
                self.assertTrue(v, msg=f"{fn.__name__}({bad!r})")
                self.assertIn("not a JSON object", v[0])


class TestRatificationAndDefaults(unittest.TestCase):
    def test_live_docs_ratified(self):
        self.assertEqual(ccd.ratification_violations(), [])
        self.assertEqual(ccd.defaults_violations(), [])
        self.assertEqual(ccd.no_invented_cutoff_violations(), [])

    def test_reviewed_revision_drift_rejected(self):
        adr = ccd.read(ccd.ADR002)
        reg = ccd.read(ccd.DOCS["c03"])
        v = ccd.ratification_violations(
            adr_text=adr.replace(ccd.REVIEWED_REVISION, "0" * 40), reg_text=reg)
        self.assertTrue(v)

    def test_accepted_status_drift_rejected(self):
        adr = ccd.read(ccd.ADR002)
        reg = ccd.read(ccd.DOCS["c03"])
        mutated = ccd.mutate_summary_cell(reg, "POL-P1", 3, "proposed")
        self.assertTrue(ccd.ratification_violations(adr_text=adr, reg_text=mutated))

    def test_cost_default_drift_rejected(self):
        reg = ccd.read(ccd.DOCS["c03"])
        c02 = ccd.read(ccd.DOCS["c02"])
        self.assertTrue(ccd.defaults_violations(
            reg_text=reg.replace("project_cap_usd: 25", "project_cap_usd: 30"),
            c02_text=c02))
        self.assertTrue(ccd.defaults_violations(
            reg_text=reg, c02_text=c02.replace("USD 10", "USD 20")))
        self.assertTrue(ccd.defaults_violations(
            reg_text=reg.replace("25 / 10 / 2", "25 / 10 / 3"),
            c02_text=c02))

    def test_invented_cutoff_drift_rejected(self):
        reg = ccd.read(ccd.DOCS["c03"])
        mutated = ccd.mutate_summary_cell(reg, "POL-OPS-PILOT", 4, "0.95")
        self.assertTrue(ccd.no_invented_cutoff_violations(reg_text=mutated))

    def test_summary_row_parsing(self):
        reg = ccd.read(ccd.DOCS["c03"])
        rows = ccd.summary_rows(reg)
        for rid in ccd.ACCEPTED_IDS:
            self.assertIn(rid, rows)
            self.assertIn("accepted", " ".join(rows[rid]).lower())


class TestLinksAndCwdIndependence(unittest.TestCase):
    def test_local_links_resolve(self):
        # The checker computes ROOT from __file__, so it works from any cwd.
        for path in ccd.DOCS.values():
            self.assertEqual(ccd.check_links(path), [], msg=path)
        for path in (ccd.ADR002, ccd.BASELINE, ccd.HANDOFF, ccd.KICKOFF,
                     ccd.STEP, ccd.ROOT_README):
            self.assertEqual(ccd.check_links(path), [], msg=path)


if __name__ == "__main__":
    unittest.main()
