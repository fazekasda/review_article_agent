"""Fast-tier tests for the application-boundary screening runner (kickoff 16 §5).

Covers ``src/review_article_agent/screening/runner.py``: one screening case is
run through the direct Jev decision path (plan -> reserve -> intent -> one POST ->
atomic ledger commit), and the committed result is loadable by the screening
workflow as a ``{"decision_result_ref": ...}`` case response with provenance
``verified_dispatch``. A fake transport is always injected, so no test performs a
network call; a real temp store, a real registry activation token, a real
owner-authenticated hosted-use gate, and real rights records are used.

Run:

    PYTHONPATH=src:tests python3 -m unittest tests.test_screening_runner
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from review_article_agent.domain.errors import ReviewAgentError  # noqa: E402
from review_article_agent.generation import hosted_use  # noqa: E402
from review_article_agent.parsing import ParsingRepository  # noqa: E402
from review_article_agent.parsing.core import NORMALIZATION_VERSION, PARSER_VERSION  # noqa: E402
from review_article_agent.runtime import jev, owner_auth  # noqa: E402
from review_article_agent.runtime.dispatch import RuntimeDispatchRepository  # noqa: E402
from review_article_agent.runtime.gates import prepare_gate_snapshot  # noqa: E402
from review_article_agent.runtime.lead import LeadConfig  # noqa: E402
from review_article_agent.runtime.owner_auth import OwnerApproval  # noqa: E402
from review_article_agent.screening import ScreeningError, ScreeningRepository  # noqa: E402
from review_article_agent.screening import runner as screening_runner  # noqa: E402
from review_article_agent.screening.runner import (  # noqa: E402
    ScreeningAuthorizationError,
    execute_screening_case,
)
from review_article_agent.storage import ArtifactRepository, ProjectStore, Registry, paths  # noqa: E402
from review_article_agent.storage.human_approvals import HumanApprovalRepository  # noqa: E402

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")
_MODEL = "deepseek/deepseek-v4.1-flash"
_FIXED_CHALLENGE = "TEST-CHAL-0001"
_OWNER_UID = 1000
_BASE_TIME = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
_API_KEY = "sk-or-v1-screening-canary-secret"
_EVIDENCE = {"ref": "evidence:synthetic", "verified_at": "2026-10-28T00:00:00Z"}


def _jats(pmid: str = "12345678") -> bytes:
    return (
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b"<article><front><journal-meta/><article-meta>"
        b'<article-id pub-id-type="pmid">' + pmid.encode() + b"</article-id>"
        b"</article-meta></front><body><sec><p>Hello scientific world.</p></sec></body>"
        b"</article>"
    )


def _rights(project_id, artifact_id, revision_id):
    subject = {
        "kind": "source_revision",
        "project_id": project_id,
        "artifact_id": artifact_id,
        "revision_id": revision_id,
    }
    return {
        "rights_id": f"RIGHTS-{artifact_id}-{revision_id}",
        "project_id": project_id,
        "record_revision": 1,
        "subject": subject,
        "operations": {op: "allowed" for op in _OPS},
        "basis": {
            op: {
                "kind": "public_domain",
                "evidence": {"ref": "lic:pd-syn", "provenance": "publisher:pd-list"},
            }
            for op in _OPS
        },
        "authorization": {
            "actor": "user",
            "method": "out_of_band_authenticated",
            "authorized_at": "2026-10-28T00:00:00Z",
            "against_subject": subject,
            "against_rights_revision": 1,
            "basis": {
                "kind": "public_domain",
                "evidence": {"ref": "lic:pd-syn", "provenance": "publisher:pd-list"},
            },
        },
        "restrictions": [],
        "privacy": {"personal_data": False, "confidentiality": "public", "denial": False},
        "recorded_by": "user",
        "recorded_at": "2026-10-28T00:00:00Z",
        "derived_status": "valid",
    }


def _config(question_id="eligible"):
    return {
        "questions": [{
            "id": question_id,
            "instructions": "Does the original text meet the protocol eligibility criteria?",
            "purpose": "eligibility",
            "exclude_below": "0.2",
            "retain_above": "0.8",
        }],
        "pilot_size": 1,
        "random_size": 1,
        "risk_size": 1,
        "acceptance_rule": "No important errors; inspect all pilot sources.",
        "model": jev.MODEL,
        "parser": PARSER_VERSION,
        "normalization": NORMALIZATION_VERSION,
    }


def _model_map():
    return LeadConfig.from_mapping({
        "model_id": _MODEL,
        "upstream_provider": "deepseek",
        "max_output_tokens": 1024,
        "context_tokens": 128000,
        "input_price_per_million": "0.5",
        "output_price_per_million": "1.5",
        "pricing_ref": "pricing:synthetic:v1",
        "instructions_revision": "instr.v1",
        "timeout_seconds": 60.0,
        "max_response_bytes": 1048576,
    }).to_mapping()


def _hosted_document(budget="10.00"):
    return {
        "schema_version": 1,
        "document_type": "generation_hosted_use",
        "payload": {
            "role": "screening",
            "config": _model_map(),
            "budget_cap_usd": budget,
            "provider_terms": dict(_EVIDENCE),
            "privacy_review": dict(_EVIDENCE),
            "pricing_evidence": dict(_EVIDENCE),
            "notes": None,
        },
    }


class _FakeTransport:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = 0

    def __call__(self, body, api_key, *, endpoint, timeout):
        self.calls += 1
        if not self.responses:
            raise RuntimeError("synthetic transport has no queued response")
        return self.responses.pop(0)

    @property
    def call_count(self):
        return self.calls


def _jev_response(probability=0.1, *, model=jev.MODEL, cost=0.0001, question_id="eligible"):
    body = {
        "id": "dec-1",
        "model": model,
        "provider": "TypeSafe",
        "answers": {question_id: {"type": "noul", "noul": probability}},
        "usage": {"input_tokens": 10, "output_tokens": 0, "cost": cost},
    }
    return jev.TransportResponse(200, json.dumps(body))


class RunnerTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-screening-runner-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.registry = Registry(str(self.tmp / "app"))
        info = self.registry.create_project(str(self.tmp / "proj"))
        self.addCleanup(self._close_store)
        self.app_dir = paths.app_dir_for(info.root_path)
        self.store = ProjectStore.open_at(self.app_dir)
        self.project_id = self.store.project_id
        self.artifacts = ArtifactRepository(self.store)
        self._patches = []
        self.addCleanup(self._stop_patches)
        self._install_owner_auth()
        self.token = self.registry.activate_execution(self.project_id).activation_token
        self.transport = _FakeTransport()
        self.screening = ScreeningRepository(self.store)

        # Source (with hosted-transfer rights) -> parsed report.
        source_ref = self._put_source("src-1", "rev-1")
        self.report = ParsingRepository(self.store).parse_stored_document(
            source_ref,
            report_artifact_id="parsed",
            report_revision_id="parsed-r1",
            rights=_rights(self.project_id, "parsed", "parsed-r1"),
        )["report_ref"]

        self.config_ref = self._put_screening("config", _config())
        self.hosted_ref = self._hosted("a-hosted", "rev-hosted")
        self.approve(self.hosted_ref, hosted_use.hosted_use_gate_id("screening"))

    # -- harness ------------------------------------------------------------- #
    def _close_store(self):
        try:
            self.store.close()
        except Exception:  # pragma: no cover
            pass

    def _stop_patches(self):
        for patch in self._patches:
            patch.stop()

    def _install_owner_auth(self):
        class _FakeTty:
            def is_tty(self):
                return True

            def write(self, text, *, deadline=None):
                return None

            def read_line(self, *, max_chars, deadline):
                return _FIXED_CHALLENGE

            def close(self):
                return None

        for target, value in (
            ("_new_challenge", lambda: _FIXED_CHALLENGE),
            ("_current_owner_uid", lambda: _OWNER_UID),
            ("_real_uid", lambda: _OWNER_UID),
            ("_utcnow", lambda: _BASE_TIME),
            ("_monotonic", lambda: 1000.0),
            ("_open_controlling_tty", lambda: _FakeTty()),
        ):
            patch = mock.patch.object(owner_auth, target, value)
            patch.start()
            self._patches.append(patch)

    def ref(self, artifact_id, revision_id):
        return {"project_id": self.project_id, "artifact_id": artifact_id, "revision_id": revision_id}

    def _put_source(self, artifact_id, revision_id, *, ops=None):
        operations = {op: "allowed" for op in _OPS}
        if ops:
            operations.update(ops)
        record = _rights(self.project_id, artifact_id, revision_id)
        record["operations"] = operations
        self.artifacts.create_artifact(artifact_id, kind="document")
        self.artifacts.import_revision(
            artifact_id=artifact_id,
            revision_id=revision_id,
            content=_jats(),
            origin_locator="import:synthetic-source",
            rights=record,
        )
        return self.ref(artifact_id, revision_id)

    def _put_screening(self, kind, payload, *, artifact_id=None, revision_id=None):
        artifact_id = artifact_id or kind
        revision_id = revision_id or (kind + "-r1")
        return self.screening.propose(
            kind, payload, artifact_id=artifact_id, revision_id=revision_id,
            rights=_rights(self.project_id, artifact_id, revision_id))

    def _hosted(self, artifact_id, revision_id, *, budget="10.00"):
        return hosted_use.propose_hosted_use(
            self.store, _hosted_document(budget),
            artifact_id=artifact_id, revision_id=revision_id,
            rights=_rights(self.project_id, artifact_id, revision_id))

    def approve(self, ref, gate_id, *, gate_kind="batch", decision="approved"):
        prepared = prepare_gate_snapshot(
            self.store, ref, gate_kind=gate_kind, gate_id=gate_id,
            decision=decision, rationale="synthetic screening gate")
        capability = owner_auth.request_owner_confirmation(prepared.owner_action())
        assert isinstance(capability, OwnerApproval)
        HumanApprovalRepository(self.store).record_decision(prepared, capability)

    # -- runner helper ------------------------------------------------------- #
    def run_case(self, *, request_id="screen-1", hosted_ref=None, api_key=_API_KEY,
                 transport=None, config_ref=None, report_ref=None, passage_id=None,
                 token=None):
        return execute_screening_case(
            self.store,
            registry=self.registry,
            activation_token=self.token if token is None else token,
            config_ref=self.config_ref if config_ref is None else config_ref,
            report_ref=self.report if report_ref is None else report_ref,
            passage_id=passage_id,
            request_id=request_id,
            hosted_ref=self.hosted_ref if hosted_ref is None else hosted_ref,
            api_key=api_key,
            transport=self.transport if transport is None else transport,
        )

    def verified_case(self, *, request_id="screen-1", passage_id=None):
        self.transport.responses.append(_jev_response(0.1))
        result = self.run_case(request_id=request_id, passage_id=passage_id)
        case = {
            "id": "paper1",
            "report_ref": self.report,
            "passage_id": passage_id,
            "response": {"decision_result_ref": result["decision_result_ref"]},
        }
        return result, case


# --------------------------------------------------------------------------- #
# Execute -> committed result
# --------------------------------------------------------------------------- #
class ExecuteTest(RunnerTestBase):
    def test_execute_commits_accepted_result(self):
        self.transport.responses.append(_jev_response(0.1))
        result = self.run_case()
        self.assertEqual(result["status"], "completed")
        self.assertIsNotNone(result["dispatch_id"])
        self.assertIsNotNone(result["result_id"])
        self.assertEqual(self.transport.call_count, 1)

        dispatch = RuntimeDispatchRepository(self.store).get_dispatch(result["dispatch_id"])
        self.assertEqual(dispatch.state, "completed")
        self.assertEqual(dispatch.result_kind, "jev_decision_set")
        self.assertEqual(dispatch.route, "direct_call")
        self.assertEqual(dispatch.stage, "screening")

    def test_ambiguous_result_is_unknown_with_reserve_retained(self):
        self.transport.responses.append(jev.TransportResponse(500, ""))
        result = self.run_case()
        self.assertEqual(self.transport.call_count, 1)
        dispatch = RuntimeDispatchRepository(self.store).get_dispatch(result["dispatch_id"])
        self.assertEqual(dispatch.state, "unknown")
        self.assertIsNone(dispatch.result_ref)
        self.assertIsNone(result["decision_result_ref"])

    def test_replay_never_resends(self):
        self.transport.responses.append(_jev_response(0.1))
        first = self.run_case()
        replay = self.run_case()
        self.assertEqual(self.transport.call_count, 1)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["dispatch_id"], first["dispatch_id"])

    def test_missing_authorization_refused_before_post(self):
        unapproved = self._hosted("a-hosted-nogate", "rev-nogate")
        with self.assertRaises(ScreeningAuthorizationError):
            self.run_case(hosted_ref=unapproved)
        self.assertEqual(self.transport.call_count, 0)

    def test_inactive_token_refused_before_post(self):
        with self.assertRaises(ScreeningAuthorizationError):
            self.run_case(token="not-the-active-token")
        self.assertEqual(self.transport.call_count, 0)


# --------------------------------------------------------------------------- #
# Verified dispatch provenance
# --------------------------------------------------------------------------- #
class VerifiedProvenanceTest(RunnerTestBase):
    def _pilot(self, case):
        return {
            "config_ref": self.config_ref,
            "cases": [case],
            "adjudications": {"paper1": {
                "label": "exclude", "inspected_source": True, "important_failure": False,
                "rationale": "Checked original source", "corrective_action": None}},
        }

    def test_pilot_from_verified_result_reports_verified_dispatch(self):
        result, case = self.verified_case()
        pilot_ref = self._put_screening("pilot", self._pilot(case))
        pilot = self.screening.read(pilot_ref, "pilot")
        observed = pilot["cases"][0]
        self.assertEqual(observed["response_provenance"], "verified_dispatch")
        self.assertEqual(observed["observed_model"], jev.MODEL)
        self.assertEqual(observed["model"], jev.MODEL)
        self.assertEqual(observed["request_sha256"], pilot["cases"][0]["request_sha256"])
        self.assertFalse(pilot["authorizes_extraction"])

    def test_imported_response_keeps_imported_provenance(self):
        case = {
            "id": "paper1", "report_ref": self.report, "passage_id": None,
            "response": {"model": jev.MODEL,
                         "answers": {"eligible": {"type": "noul", "noul": 0.1}},
                         "usage": {"input_tokens": 10, "output_tokens": 0}},
        }
        pilot_ref = self._put_screening("pilot", self._pilot(case))
        pilot = self.screening.read(pilot_ref, "pilot")
        self.assertEqual(pilot["cases"][0]["response_provenance"], "imported_unverified_dispatch")

    def test_no_auto_exclusion_from_verified_result(self):
        # A low probability maps to a *proposed* exclusion, which still needs the
        # human pilot/audit gates; it is never an automatic exclusion.
        _, case = self.verified_case()
        pilot_ref = self._put_screening("pilot", self._pilot(case))
        pilot = self.screening.read(pilot_ref, "pilot")
        self.assertEqual(pilot["cases"][0]["machine"]["label"], "proposed_exclusion")
        self.assertFalse(pilot["authorizes_extraction"])

    def test_foreign_or_missing_result_ref_refused(self):
        _, case = self.verified_case()
        # A result ref for a dispatch that does not exist.
        bad = dict(case)
        bad["response"] = {"decision_result_ref": {
            "project_id": self.project_id, "dispatch_id": "disp-does-not-exist"}}
        with self.assertRaises(ScreeningError):
            self._put_screening("pilot", self._pilot(bad), artifact_id="pilot-bad1",
                                revision_id="pilot-bad1-r1")
        # A result ref for a foreign project.
        foreign = dict(case)
        foreign["response"] = {"decision_result_ref": {
            "project_id": "other-project", "dispatch_id": "disp-x"}}
        with self.assertRaises(ScreeningError):
            self._put_screening("pilot", self._pilot(foreign), artifact_id="pilot-bad2",
                                revision_id="pilot-bad2-r1")

    def test_tampered_request_binding_refused(self):
        # A verified result committed for a *different* config (different request
        # digest) cannot be bound to this config's case.
        _, case = self.verified_case()
        other_config = _config(question_id="other")
        other_ref = self._put_screening("config", other_config, artifact_id="config-other",
                                        revision_id="config-other-r1")
        # Build a pilot whose config is `other` but whose result was for `eligible`.
        payload = self._pilot(case)
        payload["config_ref"] = other_ref
        with self.assertRaises(ScreeningError):
            self._put_screening("pilot", payload, artifact_id="pilot-bad3",
                                revision_id="pilot-bad3-r1")


if __name__ == "__main__":
    unittest.main()
