"""Focused tests for the bounded direct extraction/quality runner (S9-RUNNER).

Covers gates, hosted rights, activation, pilot/bulk, replay, unknown, receipt
tamper/generic-import rejection, quality exact-source binding, and a successful
``require_run`` + schema round-trip. Deliberately small; no network is touched
(a fake transport is always injected).
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from review_article_agent.extraction import runner  # noqa: E402
from review_article_agent.extraction.runner import (  # noqa: E402
    ExtractionAuthorizationError,
    ExtractionConfigDocumentError,
    ExtractionHostedUseError,
    ExtractionReceiptError,
    execute_run,
    require_run,
    validate_extraction_config_document,
    validate_hosted_use_document,
)
from review_article_agent.parsing.core import (  # noqa: E402
    NORMALIZATION_VERSION,
    PARSER_VERSION,
)
from review_article_agent.parsing.repository import ParsingRepository  # noqa: E402
from review_article_agent.runtime import owner_auth  # noqa: E402
from review_article_agent.runtime.gates import prepare_gate_snapshot  # noqa: E402
from review_article_agent.runtime.lead import LeadConfig  # noqa: E402
from review_article_agent.runtime.owner_auth import OwnerApproval  # noqa: E402
from review_article_agent.storage import ArtifactRepository, ProjectStore, Registry, paths  # noqa: E402
from review_article_agent.storage.human_approvals import HumanApprovalRepository  # noqa: E402

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")
_MODEL = "deepseek/deepseek-v4.1-flash"
_QUALITY_MODEL = "deepseek/deepseek-chat-v3.1"
_EVIDENCE = {"ref": "evidence:synthetic", "verified_at": "2026-10-28T00:00:00Z"}
_FIXED_CHALLENGE = "TEST-CHAL-0001"
_OWNER_UID = 1000
_BASE_TIME = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

JATS = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<!DOCTYPE article PUBLIC "-//NLM//DTD JATS (Z39.96) Journal Archiving '
    'and Interchange DTD v1.3 20210610//EN" "JATS-archivearticle1-3.dtd">\n'
    '<article xmlns:xlink="http://www.w3.org/1999/xlink">\n'
    "  <front><article-meta><abstract>\n"
    "    <p>Treatment did not reduce tumor volume in mice.</p>\n"
    "  </abstract></article-meta></front>\n"
    "  <body><sec>\n"
    "    <title>Results</title>\n"
    "    <p>Clearance was 5 mg.</p>\n"
    "  </sec></body>\n"
    "</article>\n"
)
JATS_BYTES = JATS.encode("utf-8")


def _rights(project_id, artifact_id, revision_id, *, ops=None, privacy=None, authorization=True):
    operations = {op: "allowed" for op in _OPS}
    if ops:
        operations.update(ops)
    basis = {}
    for op in _OPS:
        if operations[op] == "allowed":
            basis[op] = {"kind": "license", "evidence": {"ref": "lic:syn", "provenance": "publisher:syn"}}
        else:
            basis[op] = {"kind": "unknown", "evidence": {"ref": "", "provenance": ""}}
    subject = {
        "kind": "source_revision",
        "project_id": project_id,
        "artifact_id": artifact_id,
        "revision_id": revision_id,
    }
    auth = None
    if authorization:
        auth = {
            "actor": "user",
            "method": "out_of_band_authenticated",
            "authorized_at": "2026-10-28T00:00:00Z",
            "against_subject": subject,
            "against_rights_revision": 1,
            "basis": {"kind": "license", "evidence": {"ref": "lic:syn", "provenance": "publisher:syn"}},
        }
    privacy_value = {"personal_data": False, "confidentiality": "public", "denial": False}
    if privacy:
        privacy_value.update(privacy)
    return {
        "rights_id": f"RIGHTS-{artifact_id}-{revision_id}",
        "project_id": project_id,
        "record_revision": 1,
        "subject": subject,
        "operations": operations,
        "basis": basis,
        "authorization": auth,
        "restrictions": [],
        "privacy": privacy_value,
        "recorded_by": "user",
        "recorded_at": "2026-10-28T00:00:00Z",
        "derived_status": "valid",
    }


def _config_map(model=_MODEL, provider="deepseek"):
    return LeadConfig.from_mapping(
        {
            "model_id": model,
            "upstream_provider": provider,
            "max_output_tokens": 1024,
            "context_tokens": 128000,
            "input_price_per_million": "0.5",
            "output_price_per_million": "1.5",
            "pricing_ref": "pricing:synthetic:v1",
            "instructions_revision": "extraction.instr.v1",
            "timeout_seconds": 60.0,
            "max_response_bytes": 1048576,
        }
    ).to_mapping()


def _config_document(**overrides):
    payload = {
        "extraction_model": _config_map(),
        "quality_model": _config_map(model=_QUALITY_MODEL),
        "instructions": "Extract grounded findings from the original passages.",
        "quality_instructions": "Check the extraction against the original passages.",
        "appraisal_dimensions": [],
        "parser": PARSER_VERSION,
        "normalization": NORMALIZATION_VERSION,
        "schema_version": 1,
        "pilot_size": 5,
        "random_size": 5,
        "risk_size": 5,
        "acceptance_rule": "human review of the pilot",
    }
    payload.update(overrides)
    return {"schema_version": 1, "document_type": "extraction_config", "payload": payload}


def _hosted_document(config_ref, report_refs, *, purpose="extraction", extraction_ref=None):
    return {
        "schema_version": 1,
        "document_type": "extraction_hosted_use",
        "payload": {
            "config_ref": config_ref,
            "report_refs": list(report_refs),
            "purpose": purpose,
            "extraction_ref": extraction_ref,
            "provider_terms": dict(_EVIDENCE),
            "privacy_review": dict(_EVIDENCE),
            "pricing_evidence": dict(_EVIDENCE),
        },
    }


class _FakeTransport:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, body, api_key, *, endpoint, timeout, max_response_bytes):
        self.calls.append({"body": body, "api_key": api_key})
        if not self.responses:
            raise RuntimeError("synthetic transport has no queued response")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    @property
    def call_count(self):
        return len(self.calls)


def _completion(content_obj, *, model=_MODEL, provider="deepseek", cost=0.0002, **overrides):
    payload = {
        "id": "gen-1",
        "model": model,
        "provider": provider,
        "choices": [
            {"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(content_obj)}}
        ],
        "usage": {
            "prompt_tokens": 12,
            "completion_tokens": 8,
            "total_tokens": 20,
            "cost": cost,
        },
    }
    payload.update(overrides)
    from review_article_agent.runtime.lead import TransportResponse

    return TransportResponse(status=200, body_text=json.dumps(payload), truncated=False)


def _extraction_output(passages, source_keys):
    findings = []
    coverage = []
    for index, passage in enumerate(passages):
        finding_id = f"F{index + 1}"
        findings.append(
            {
                "finding_id": finding_id,
                "source_key": passage["source_key"],
                "passage_id": passage["passage_id"],
                "snippet": passage["text"],
                "statement": f"Reported: {passage['text'][:60]}",
                "attribution": "direct_observation",
                "citation_flag": "own_result",
                "context": {
                    key: {"state": "not_applicable", "value": None}
                    for key in ("species", "population", "assay", "outcome", "conditions")
                },
                "secondary_warning": {"state": "not_applicable", "value": None},
                "limitations": {"state": "not_applicable", "value": None},
                "numeric_details": [],
            }
        )
        coverage.append(
            {
                "target_kind": "passage",
                "target_id": passage["passage_id"],
                "disposition": "finding",
                "finding_id": finding_id,
            }
        )
    return {
        "schema_version": 1,
        "summary": "Synthetic grounded extraction.",
        "source_records": [{"source_key": key, "source_kind": "document"} for key in source_keys],
        "studies": [],
        "datasets": [],
        "experiments": [],
        "findings": findings,
        "coverage": coverage,
        "links": [],
        "appraisal": [],
        "omissions": [],
        "limitations": {"state": "not_applicable", "value": None},
    }


def _quality_output(escalation="none"):
    return {
        "checks": {
            "omissions": True,
            "qualifications": True,
            "field_correctness": True,
            "negations": True,
            "nulls": True,
            "numerical_units_denominators": True,
            "species_models": True,
            "author_spin": True,
            "tables_supplements": True,
        },
        "issues": [],
        "escalation": escalation,
    }


class RunnerTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-extraction-runner-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.registry = Registry(str(self.tmp / "app"))
        info = self.registry.create_project(str(self.tmp / "proj"))
        self.addCleanup(self._close_store)
        self.app_dir = paths.app_dir_for(info.root_path)
        self.store = ProjectStore.open_at(self.app_dir)
        self.project_id = self.store.project_id
        self.artifacts = ArtifactRepository(self.store)
        self.addCleanup(self._stop_patches)
        self._patches = []
        self._install_owner_auth()
        self.token = self.registry.activate_execution(self.project_id).activation_token

        # A stored original document -> parsed report.
        self.artifacts.create_artifact("a-src", kind="document", created_by="import")
        self.artifacts.import_revision(
            artifact_id="a-src", revision_id="rev-src", content=JATS_BYTES,
            origin_locator="import:synthetic",
            rights=_rights(self.project_id, "a-src", "rev-src"), acquired_by="import",
        )
        self.source_ref = self.ref("a-src", "rev-src")
        parsed = ParsingRepository(self.store).parse_stored_document(
            self.source_ref, report_artifact_id="a-rep", report_revision_id="rev-rep",
            rights=_rights(self.project_id, "a-rep", "rev-rep"),
        )
        self.report_ref = parsed["report_ref"]
        record = ParsingRepository(self.store).read_report(self.report_ref)
        self.passages = [
            {
                "passage_id": f"a-rep:{p['passage_id']}",
                "source_key": "a-rep",
                "text": p["text"],
            }
            for p in record["payload"]["passages"]
        ]
        self.source_keys = ["a-rep"]

        self.config_ref = self._put_document(
            "a-config", "rev-config", _config_document(), kind="document"
        )
        self.hosted_ref = self._put_document(
            "a-hosted", "rev-hosted",
            _hosted_document(self.config_ref, [self.report_ref]),
            kind="document",
            dependencies=[self.config_ref, self.report_ref, self.source_ref],
        )
        self.receipt_rights = _rights(self.project_id, "a-run", "rev-run")
        self.transport = _FakeTransport()
        self.extraction_receipt = None

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

    # -- fixtures ------------------------------------------------------------ #
    def ref(self, artifact_id, revision_id):
        return {"project_id": self.project_id, "artifact_id": artifact_id, "revision_id": revision_id}

    def _put_document(self, artifact_id, revision_id, document, *, kind, dependencies=()):
        self.artifacts.create_artifact(artifact_id, kind=kind, created_by="import")
        self.artifacts.import_revision(
            artifact_id=artifact_id, revision_id=revision_id, content=document,
            origin_locator="import:synthetic",
            rights=_rights(self.project_id, artifact_id, revision_id),
            dependencies=list(dependencies), acquired_by="import",
        )
        return self.ref(artifact_id, revision_id)

    def approve(self, ref, gate_id, *, gate_kind="batch"):
        prepared = prepare_gate_snapshot(
            self.store, ref, gate_kind=gate_kind, gate_id=gate_id,
            decision="approved", rationale="synthetic gate",
        )
        capability = owner_auth.request_owner_confirmation(prepared.owner_action())
        assert isinstance(capability, OwnerApproval)
        HumanApprovalRepository(self.store).record_decision(prepared, capability)

    def approve_all(self, *, config=True, hosted=True):
        if config:
            self.approve(self.config_ref, runner.CONFIG_GATE_ID)
        if hosted:
            self.approve(self.hosted_ref, runner.hosted_use_gate_id("extraction"))

    def run_extraction(self, *, request_id="run-1", transport=None, mode="pilot",
                       pilot_ref=None, report_refs=None, config_ref=None, hosted_ref=None,
                       artifact_id="a-run", revision_id="rev-run"):
        rights = _rights(self.project_id, artifact_id, revision_id)
        return execute_run(
            self.store, registry=self.registry, activation_token=self.token,
            config_ref=self.config_ref if config_ref is None else config_ref,
            report_refs=[self.report_ref] if report_refs is None else report_refs,
            purpose="extraction", request_id=request_id,
            hosted_ref=self.hosted_ref if hosted_ref is None else hosted_ref,
            api_key="sk-or-test", transport=self.transport if transport is None else transport,
            mode=mode, pilot_ref=pilot_ref,
            artifact_id=artifact_id, revision_id=revision_id, rights=rights,
        )

    def _extraction_body(self):
        return _completion(_extraction_output(self.passages, self.source_keys))

    def do_pilot(self, *, request_id="run-1"):
        self.transport.responses.append(self._extraction_body())
        return self.run_extraction(request_id=request_id)


class ConfigDocumentTest(RunnerTestBase):
    def test_valid_config_returns_independent_copy(self):
        source = _config_document()
        validated = validate_extraction_config_document(source)
        source["payload"]["instructions"] = "changed"
        self.assertEqual(validated["payload"]["instructions"], "Extract grounded findings from the original passages.")
        self.assertEqual(validated["payload"]["schema_version"], 1)

    def test_rejects_unknown_and_wrong_version(self):
        for bad in (
            _config_document() | {"extra": 1},
            _config_document() | {"document_type": "extraction_hosted_use"},
            _config_document(pilot_size=0),
            _config_document(appraisal_dimensions=[{"dimension_id": "risk", "score": 1}]),
            _config_document(extraction_model={**_config_map(), "model_id": ""}),
        ):
            with self.assertRaises(ExtractionConfigDocumentError):
                validate_extraction_config_document(bad)


class HostedDocumentTest(RunnerTestBase):
    def test_quality_requires_extraction_ref(self):
        with self.assertRaises(ExtractionHostedUseError):
            validate_hosted_use_document(
                _hosted_document(self.config_ref, [self.report_ref], purpose="quality")
            )

    def test_extraction_ref_forbidden_for_extraction(self):
        with self.assertRaises(ExtractionHostedUseError):
            validate_hosted_use_document(
                _hosted_document(
                    self.config_ref, [self.report_ref], purpose="extraction",
                    extraction_ref=self.ref("a-x", "r-x"),
                )
            )


class ExecuteRunTest(RunnerTestBase):
    def test_success_require_run_and_schema(self):
        self.approve_all()
        result = self.do_pilot()
        self.assertEqual(result["outcome"], "completed")
        self.assertIsNotNone(result["run_ref"])
        run = require_run(self.store, result["run_ref"], purpose="extraction")
        self.assertEqual(run["purpose"], "extraction")
        self.assertEqual(run["observed_model"], _MODEL)
        self.assertEqual(run["config_ref"], self.config_ref)
        self.assertEqual(run["source_refs"], [self.source_ref])
        self.assertEqual(len(run["passages"]), len(self.passages))
        self.assertEqual(run["raw_output"]["schema_version"], 1)
        self.assertEqual(run["dispatch_id"], result["dispatch_id"])

    def test_missing_config_gate_zero_post(self):
        self.approve_all(config=False)
        with self.assertRaises(ExtractionAuthorizationError):
            self.do_pilot()
        self.assertEqual(self.transport.call_count, 0)

    def test_missing_hosted_gate_zero_post(self):
        self.approve_all(hosted=False)
        with self.assertRaises(ExtractionAuthorizationError):
            self.do_pilot()
        self.assertEqual(self.transport.call_count, 0)

    def test_denied_hosted_rights_zero_post(self):
        # Re-create the source with a rights record that denies hosted transfer.
        self.artifacts.create_artifact("a-src2", kind="document", created_by="import")
        self.artifacts.import_revision(
            artifact_id="a-src2", revision_id="rev-src2", content=JATS_BYTES,
            origin_locator="import:synthetic",
            rights=_rights(self.project_id, "a-src2", "rev-src2", ops={"hosted_transfer": "not_allowed"}),
            acquired_by="import",
        )
        parsed = ParsingRepository(self.store).parse_stored_document(
            self.ref("a-src2", "rev-src2"), report_artifact_id="a-rep2", report_revision_id="rev-rep2",
            rights=_rights(self.project_id, "a-rep2", "rev-rep2"),
        )
        hosted = self._put_document(
            "a-hosted2", "rev-hosted2",
            _hosted_document(self.config_ref, [parsed["report_ref"]]),
            kind="document",
            dependencies=[self.config_ref, parsed["report_ref"], self.ref("a-src2", "rev-src2")],
        )
        self.approve(self.config_ref, runner.CONFIG_GATE_ID)
        self.approve(hosted, runner.hosted_use_gate_id("extraction"))
        with self.assertRaises(runner.ExtractionRightsError):
            self.run_extraction(request_id="run-2", hosted_ref=hosted, report_refs=[parsed["report_ref"]])
        self.assertEqual(self.transport.call_count, 0)

    def test_foreign_token_zero_post(self):
        self.approve_all()
        with self.assertRaises(ExtractionAuthorizationError):
            execute_run(
                self.store, registry=self.registry, activation_token="wrong",
                config_ref=self.config_ref, report_refs=[self.report_ref],
                purpose="extraction", request_id="run-3", hosted_ref=self.hosted_ref,
                api_key="sk-or-test", transport=self.transport,
                artifact_id="a-run", revision_id="rev-run", rights=self.receipt_rights,
            )
        self.assertEqual(self.transport.call_count, 0)

    def test_unknown_retains_reserve_and_never_resends(self):
        self.approve_all()
        self.transport.responses.append(RuntimeError("network down"))
        result = self.run_extraction(request_id="run-4")
        self.assertEqual(result["outcome"], "unknown")
        self.assertEqual(result["cost_state"], "unknown")
        self.assertEqual(self.transport.call_count, 1)
        # A replay observes the stored unknown outcome; no second POST and no receipt.
        replay = self.run_extraction(request_id="run-4")
        self.assertEqual(replay["outcome"], "unknown")
        self.assertEqual(self.transport.call_count, 1)

    def test_replay_returns_stored_without_post(self):
        self.approve_all()
        first = self.do_pilot()
        self.assertEqual(self.transport.call_count, 1)
        # Re-running the same request must observe, not re-send, even with no
        # queued transport response.
        replay = self.run_extraction()
        self.assertEqual(replay["outcome"], "completed")
        self.assertEqual(self.transport.call_count, 1)
        self.assertEqual(replay["dispatch_id"], first["dispatch_id"])


class QualityRunTest(RunnerTestBase):
    def _hosted_quality(self, extraction_ref):
        return self._put_document(
            "a-hosted-q", "rev-hosted-q",
            _hosted_document(self.config_ref, [self.report_ref], purpose="quality", extraction_ref=extraction_ref),
            kind="document",
            dependencies=[self.config_ref, self.report_ref, extraction_ref, self.source_ref],
        )

    def test_quality_binds_extraction_and_includes_original_sources(self):
        self.approve_all()
        pilot = self.do_pilot(request_id="run-1")
        hosted_q = self._hosted_quality(pilot["run_ref"])
        self.approve(hosted_q, runner.hosted_use_gate_id("quality"))
        self.transport.responses.append(_completion(_quality_output(), model=_QUALITY_MODEL))
        quality = execute_run(
            self.store, registry=self.registry, activation_token=self.token,
            config_ref=self.config_ref, report_refs=[self.report_ref], purpose="quality",
            request_id="run-q1", hosted_ref=hosted_q, api_key="sk-or-test",
            extraction_ref=pilot["run_ref"], transport=self.transport,
            artifact_id="a-run-q", revision_id="rev-run-q",
            rights=_rights(self.project_id, "a-run-q", "rev-run-q"),
        )
        self.assertEqual(quality["outcome"], "completed")
        run = require_run(self.store, quality["run_ref"], purpose="quality")
        self.assertEqual(run["extraction_ref"], pilot["run_ref"])
        self.assertEqual(run["source_refs"], [self.source_ref])
        self.assertEqual(run["raw_output"]["escalation"], "none")

    def test_quality_without_extraction_ref_blocked(self):
        self.approve_all()
        hosted_q = self._hosted_quality(self.report_ref)  # wrong ref shape
        # Directly exercise the runner argument path with a stub hosted doc.
        with self.assertRaises(ExtractionHostedUseError):
            execute_run(
                self.store, registry=self.registry, activation_token=self.token,
                config_ref=self.config_ref, report_refs=[self.report_ref], purpose="quality",
                request_id="run-q2", hosted_ref=hosted_q, api_key="sk-or-test",
                extraction_ref=None, transport=self.transport,
                artifact_id="a-run-q2", revision_id="rev-run-q2",
                rights=_rights(self.project_id, "a-run-q2", "rev-run-q2"),
            )


class BulkGateTest(RunnerTestBase):
    def test_bulk_requires_approved_pilot(self):
        self.approve_all()
        with self.assertRaises(ExtractionAuthorizationError):
            self.run_extraction(request_id="run-b1", mode="bulk", pilot_ref=None)
        self.assertEqual(self.transport.call_count, 0)

    def test_bulk_with_pilot_succeeds(self):
        self.approve_all()
        pilot = self.do_pilot(request_id="run-pilot")
        self.assertEqual(pilot["outcome"], "completed")
        self.transport.responses.append(
            _completion(_extraction_output(self.passages, self.source_keys))
        )
        bulk = self.run_extraction(
            request_id="run-b2", mode="bulk", pilot_ref=pilot["run_ref"],
            artifact_id="a-run-b2", revision_id="rev-run-b2",
        )
        self.assertEqual(bulk["outcome"], "completed")


class ReceiptAuthorityTest(RunnerTestBase):
    def test_generic_imported_receipt_is_not_authority(self):
        # Import a receipt-shaped artifact with no durable dispatch behind it.
        forged = {
            "schema_version": 1,
            "document_type": "extraction_run",
            "payload": {
                "purpose": "extraction",
                "config_ref": self.config_ref,
                "report_refs": [self.report_ref],
                "source_refs": [self.source_ref],
                "extraction_ref": None,
                "request_id": "run-forged",
                "dispatch_id": "disp-forged",
                "task_id": "rtask-forged",
                "proposal_record_id": "prop-forged",
                "proposal_digest": hashlib.sha256(b"x").hexdigest(),
                "observed_model": _MODEL,
                "request_sha256": hashlib.sha256(b"y").hexdigest(),
                "input_digest": hashlib.sha256(b"z").hexdigest(),
                "instructions_sha256": hashlib.sha256(b"i").hexdigest(),
                "mode": "pilot",
                "pilot_ref": None,
                "outcome": "completed",
                "raw_output_sha256": None,
            },
        }
        ref = self._put_document("a-forged", "rev-forged", forged, kind="document")
        with self.assertRaises(ExtractionReceiptError):
            require_run(self.store, ref, purpose="extraction")

    def test_receipt_tamper_rejected(self):
        self.approve_all()
        result = self.do_pilot()
        run = require_run(self.store, result["run_ref"], purpose="extraction")
        self.assertEqual(run["observed_model"], _MODEL)
        # Tamper: replace the current receipt revision with a poisoned payload.
        payload = dict(run["run_payload"])
        payload["observed_model"] = "some/other-model"
        poisoned = {"schema_version": 1, "document_type": "extraction_run", "payload": payload}
        self.artifacts.import_revision(
            artifact_id="a-run", revision_id="rev-run-2", content=poisoned,
            origin_locator="import:poisoned",
            rights=_rights(self.project_id, "a-run", "rev-run-2"),
            parent_revision_id="rev-run", acquired_by="import",
        )
        with self.assertRaises(ExtractionReceiptError):
            require_run(self.store, self.ref("a-run", "rev-run-2"), purpose="extraction")


class EscalationTest(RunnerTestBase):
    def test_schema_invalid_extraction_escalates(self):
        self.approve_all()
        bad = {"schema_version": 1, "summary": "incomplete"}
        self.transport.responses.append(_completion(bad))
        result = self.run_extraction(request_id="run-esc")
        self.assertEqual(result["outcome"], "escalation")
        run = require_run(self.store, result["run_ref"], purpose="extraction")
        self.assertEqual(run["run_payload"]["outcome"], "escalation")


if __name__ == "__main__":
    unittest.main()
