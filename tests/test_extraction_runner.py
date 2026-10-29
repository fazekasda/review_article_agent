"""Focused S9-RUNNER tests: real runner + workflow config/pilot authority.

No ``_require_run`` seam patch: every path goes through the real runner and the
real :class:`ExtractionWorkflowRepository`. A fake transport is always injected,
so no network is touched.
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

from review_article_agent.extraction import runner  # noqa: E402
from review_article_agent.extraction import workflow  # noqa: E402
from review_article_agent.extraction.runner import (  # noqa: E402
    ExtractionAuthorizationError,
    ExtractionHostedUseError,
    ExtractionReceiptError,
    ExtractionRunnerError,
    execute_run,
    require_run,
    validate_hosted_use_document,
)
from review_article_agent.parsing.core import (  # noqa: E402
    NORMALIZATION_VERSION,
    PARSER_VERSION,
)
from review_article_agent.parsing.repository import ParsingRepository  # noqa: E402
from review_article_agent.runtime import lead, owner_auth  # noqa: E402
from review_article_agent.runtime.gates import prepare_gate_snapshot  # noqa: E402
from review_article_agent.runtime.lead import LeadConfig, TransportResponse  # noqa: E402
from review_article_agent.runtime.owner_auth import OwnerApproval  # noqa: E402
from review_article_agent.storage import ArtifactRepository, ProjectStore, Registry, paths  # noqa: E402
from review_article_agent.storage.agent_runtime import RuntimeConflictError  # noqa: E402
from review_article_agent.storage.human_approvals import HumanApprovalRepository  # noqa: E402

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")
_MODEL = "deepseek/deepseek-v4.1-flash"
_QUALITY_MODEL = "deepseek/deepseek-chat-v3.1"
_EVIDENCE = {"ref": "evidence:synthetic", "verified_at": "2026-10-28T00:00:00Z"}
_FIXED_CHALLENGE = "TEST-CHAL-0001"
_OWNER_UID = 1000
_BASE_TIME = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

# One paragraph and one table asset so asset coverage is exercised.
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
    "    <table-wrap id=\"tbl1\"><label>Table 1</label>"
    "<caption><p>Baseline characteristics of the cohort.</p></caption></table-wrap>\n"
    "  </sec></body>\n"
    "</article>\n"
)
JATS_BYTES = JATS.encode("utf-8")
PLAIN_BYTES = b"not a structured article; unreadable"


def _rights(project_id, artifact_id, revision_id, *, ops=None, privacy=None):
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
        "kind": "source_revision", "project_id": project_id,
        "artifact_id": artifact_id, "revision_id": revision_id,
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
        "authorization": {
            "actor": "user", "method": "out_of_band_authenticated",
            "authorized_at": "2026-10-28T00:00:00Z", "against_subject": subject,
            "against_rights_revision": 1,
            "basis": {"kind": "license", "evidence": {"ref": "lic:syn", "provenance": "publisher:syn"}},
        },
        "restrictions": [],
        "privacy": privacy_value,
        "recorded_by": "user",
        "recorded_at": "2026-10-28T00:00:00Z",
        "derived_status": "valid",
    }


def _model_map(model, provider="deepseek"):
    return LeadConfig.from_mapping(
        {
            "model_id": model, "upstream_provider": provider,
            "max_output_tokens": 1024, "context_tokens": 128000,
            "input_price_per_million": "0.5", "output_price_per_million": "1.5",
            "pricing_ref": "pricing:synthetic:v1", "instructions_revision": "instr.v1",
            "timeout_seconds": 60.0, "max_response_bytes": 1048576,
        }
    ).to_mapping()


def _config_payload(pilot_size=1):
    return {
        "config_schema_version": 1,
        "appraisal_dimensions": [],
        "pilot_size": pilot_size,
        "random_size": 1,
        "risk_size": 1,
        "acceptance_rule": "human review of the pilot",
        "extraction_model": _model_map(_MODEL),
        "quality_model": _model_map(_QUALITY_MODEL),
        "extraction_instructions": "Extract grounded findings from the original passages.",
        "extraction_instructions_revision": "extraction.instr.v1",
        "quality_instructions": "Check the extraction against the original passages.",
        "quality_instructions_revision": "quality.instr.v1",
        "parser": PARSER_VERSION,
        "normalization": NORMALIZATION_VERSION,
    }


def _hosted_document(config_ref, report_refs, *, purpose="extraction", extraction_ref=None,
                     acquisition_ref=None):
    return {
        "schema_version": 1,
        "document_type": "extraction_hosted_use",
        "payload": {
            "config_ref": config_ref,
            "report_refs": list(report_refs),
            "acquisition_ref": acquisition_ref,
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
        "id": "gen-1", "model": model, "provider": provider,
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(content_obj)}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20, "cost": cost},
    }
    payload.update(overrides)
    return TransportResponse(status=200, body_text=json.dumps(payload), truncated=False)


def _extraction_output(passages, source_keys):
    """Build a schema-valid raw output over the receipt's real passages/assets."""
    findings = []
    coverage = []
    index = 0
    for passage in passages:
        if passage["text"].strip():
            index += 1
            finding_id = f"F{index}"
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
                {"target_kind": "passage", "target_id": passage["passage_id"], "disposition": "finding", "finding_id": finding_id}
            )
        for asset_id in passage.get("asset_ids", []):
            coverage.append({"target_kind": "asset", "target_id": asset_id, "disposition": "no_finding"})
    return {
        "schema_version": 1,
        "summary": "Synthetic grounded extraction.",
        "source_records": [{"source_key": key, "source_kind": "document"} for key in source_keys],
        "studies": [], "datasets": [], "experiments": [],
        "findings": findings, "coverage": coverage,
        "links": [], "appraisal": [], "omissions": [],
        "limitations": {"state": "not_applicable", "value": None},
    }


def _quality_output(escalation="none"):
    return {
        "checks": {name: True for name in workflow.QUALITY_CHECKS},
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
        self.workflow = workflow.ExtractionWorkflowRepository(self.store)
        self._patches = []
        self.addCleanup(self._stop_patches)
        self._install_owner_auth()
        self.token = self.registry.activate_execution(self.project_id).activation_token

        self.source_ref, self.report_ref, self.passages = self._store_report("a-src", "a-rep")
        self.source_keys = ["a-rep"]
        self.config_ref = self.workflow.propose(
            "config", _config_payload(), artifact_id="a-config", revision_id="rev-config",
            rights=_rights(self.project_id, "a-config", "rev-config"),
        )
        self.approve(self.config_ref, workflow.GATE_CONFIG)
        self.hosted_ref = self._put_document(
            "a-hosted", "rev-hosted", _hosted_document(self.config_ref, [self.report_ref]),
            dependencies=[self.config_ref, self.report_ref, self.source_ref],
        )
        self.approve(self.hosted_ref, runner.hosted_use_gate_id("extraction"))
        self.transport = _FakeTransport()

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

    def _store_report(self, source_id, report_id):
        self.artifacts.create_artifact(source_id, kind="document", created_by="import")
        self.artifacts.import_revision(
            artifact_id=source_id, revision_id="rev-" + source_id, content=JATS_BYTES,
            origin_locator="import:synthetic",
            rights=_rights(self.project_id, source_id, "rev-" + source_id), acquired_by="import",
        )
        source_ref = self.ref(source_id, "rev-" + source_id)
        parsed = ParsingRepository(self.store).parse_stored_document(
            source_ref, report_artifact_id=report_id, report_revision_id="rev-" + report_id,
            rights=_rights(self.project_id, report_id, "rev-" + report_id),
        )
        record = ParsingRepository(self.store).read_report(parsed["report_ref"])
        artifact_id = parsed["report_ref"]["artifact_id"]
        passages = []
        for passage in record["payload"]["passages"]:
            passages.append(
                {"passage_id": f"{artifact_id}:{passage['passage_id']}", "source_key": artifact_id, "text": passage["text"]}
            )
        for index, asset in enumerate(record["payload"].get("assets", [])):
            asset_id = asset.get("id") or f"asset-{index}"
            caption = asset.get("caption") or ""
            passages.append(
                {"passage_id": f"{artifact_id}:asset-{index}", "source_key": artifact_id, "text": caption,
                 "asset_ids": [f"{artifact_id}:{asset_id}:{index}"]}
            )
        return source_ref, parsed["report_ref"], passages

    def _put_document(self, artifact_id, revision_id, document, dependencies=()):
        self.artifacts.create_artifact(artifact_id, kind="report", created_by="import")
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

    # -- runner helpers ------------------------------------------------------ #
    def run_extraction(self, *, request_id="run-1", transport=None, mode="pilot", pilot_ref=None,
                       hosted_ref=None, report_refs=None, artifact_id="a-run", revision_id="rev-run",
                       config_ref=None, purpose="extraction", extraction_ref=None):
        return execute_run(
            self.store, registry=self.registry, activation_token=self.token,
            config_ref=self.config_ref if config_ref is None else config_ref,
            report_refs=[self.report_ref] if report_refs is None else report_refs,
            purpose=purpose, request_id=request_id,
            hosted_ref=self.hosted_ref if hosted_ref is None else hosted_ref,
            api_key="sk-or-test", transport=self.transport if transport is None else transport,
            extraction_ref=extraction_ref, mode=mode, pilot_ref=pilot_ref,
            artifact_id=artifact_id, revision_id=revision_id,
            rights=_rights(self.project_id, artifact_id, revision_id),
        )

    def pilot_extraction(self, *, request_id="run-x", artifact_id="a-run-x", revision_id="rev-run-x"):
        self.transport.responses.append(_completion(_extraction_output(self.passages, self.source_keys)))
        return self.run_extraction(request_id=request_id, artifact_id=artifact_id, revision_id=revision_id)

    def _hosted_quality(self, extraction_ref):
        return self._put_document(
            "a-hosted-q", "rev-hosted-q",
            _hosted_document(self.config_ref, [self.report_ref], purpose="quality", extraction_ref=extraction_ref),
            dependencies=[self.config_ref, self.report_ref, extraction_ref, self.source_ref],
        )

    def run_quality(self, *, extraction_ref, request_id="run-q", hosted_ref=None,
                    artifact_id="a-run-q", revision_id="rev-run-q"):
        return execute_run(
            self.store, registry=self.registry, activation_token=self.token,
            config_ref=self.config_ref, report_refs=[self.report_ref], purpose="quality",
            request_id=request_id, hosted_ref=hosted_ref, api_key="sk-or-test",
            extraction_ref=extraction_ref, transport=self.transport,
            artifact_id=artifact_id, revision_id=revision_id,
            rights=_rights(self.project_id, artifact_id, revision_id),
        )


class ConfigAuthorityTest(RunnerTestBase):
    def test_unapproved_config_blocks_before_send(self):
        # Re-create a second config WITHOUT its gate, and a hosted doc that points
        # to it; execution must fail closed with zero posts.
        other = self.workflow.propose(
            "config", _config_payload(), artifact_id="a-config2", revision_id="rev-config2",
            rights=_rights(self.project_id, "a-config2", "rev-config2"),
        )
        hosted = self._put_document(
            "a-hosted2", "rev-hosted2", _hosted_document(other, [self.report_ref])
        )
        self.approve(hosted, runner.hosted_use_gate_id("extraction"))
        self.transport.responses.append(_completion(_extraction_output(self.passages, self.source_keys)))
        with self.assertRaises(ExtractionAuthorizationError):
            self.run_extraction(request_id="run-c0", config_ref=other, hosted_ref=hosted)
        self.assertEqual(self.transport.call_count, 0)

    def test_hosted_use_document_acquisition_shape(self):
        document = _hosted_document(self.config_ref, [self.report_ref])
        document["payload"]["acquisition_ref"] = self.ref("a-manifest", "rev-m")
        validate_hosted_use_document(document)
        document["payload"]["acquisition_ref"] = "not-a-ref"
        with self.assertRaises(ExtractionHostedUseError):
            validate_hosted_use_document(document)


class PilotRunTest(RunnerTestBase):
    def test_pilot_success_public_receipt_keys_and_assets(self):
        result = self.pilot_extraction()
        self.assertEqual(result["outcome"], "completed")
        receipt = require_run(self.store, result["run_ref"], purpose="extraction")
        self.assertEqual(set(receipt), set(workflow.RECEIPT_KEYS))
        self.assertEqual(receipt["observed_model"], _MODEL)
        self.assertEqual(receipt["source_refs"], [self.source_ref])
        self.assertEqual(receipt["raw_output"]["schema_version"], 1)
        # The table asset handle is surfaced as required coverage material.
        asset_ids = [aid for p in receipt["passages"] for aid in p.get("asset_ids", [])]
        self.assertTrue(asset_ids)

    def test_require_pilot_workflow_authority(self):
        # Mandatory failure labels are required to propose a pilot at all.
        with self.assertRaises(workflow.ExtractionWorkflowError):
            self.workflow.propose(
                "pilot",
                {"config_ref": self.config_ref, "cases": [], "adjudications": {}},
                artifact_id="a-pilot-bad", revision_id="rev-pilot-bad",
                rights=_rights(self.project_id, "a-pilot-bad", "rev-pilot-bad"),
            )


class QualityRunTest(RunnerTestBase):
    def test_quality_binds_extraction_and_original_sources(self):
        run = self.pilot_extraction()
        hosted_q = self._hosted_quality(run["run_ref"])
        self.approve(hosted_q, runner.hosted_use_gate_id("quality"))
        self.transport.responses.append(_completion(_quality_output(), model=_QUALITY_MODEL))
        quality = self.run_quality(extraction_ref=run["run_ref"], hosted_ref=hosted_q)
        self.assertEqual(quality["outcome"], "completed")
        receipt = require_run(self.store, quality["run_ref"], purpose="quality")
        self.assertEqual(set(receipt), set(workflow.RECEIPT_KEYS))
        self.assertEqual(receipt["extraction_ref"], run["run_ref"])
        self.assertEqual(receipt["source_refs"], [self.source_ref])
        self.assertEqual(receipt["raw_output"]["escalation"], "none")


class BulkGateTest(RunnerTestBase):
    def _make_pilot(self):
        run = self.pilot_extraction(request_id="run-px", artifact_id="a-run-px", revision_id="rev-run-px")
        hosted_q = self._hosted_quality(run["run_ref"])
        self.approve(hosted_q, runner.hosted_use_gate_id("quality"))
        self.transport.responses.append(_completion(_quality_output(), model=_QUALITY_MODEL))
        quality = self.run_quality(
            extraction_ref=run["run_ref"], request_id="run-qx", hosted_ref=hosted_q,
            artifact_id="a-run-qx", revision_id="rev-run-qx",
        )
        adjudication = {
            "labels": list(workflow.MANDATORY_FAILURE_LABELS),
            "inspected_original": True,
            "checked_omissions": True,
            "checked_qualifications": True,
            "checked_fields": True,
            "important_failure": False,
            "severity": "none",
            "rationale": "synthetic human adjudication",
            "corrective_action": None,
        }
        pilot_ref = self.workflow.propose(
            "pilot",
            {
                "config_ref": self.config_ref,
                "cases": [{"id": "case-1", "extraction_ref": run["run_ref"], "quality_ref": quality["run_ref"]}],
                "adjudications": {"case-1": adjudication},
            },
            artifact_id="a-pilot", revision_id="rev-pilot",
            rights=_rights(self.project_id, "a-pilot", "rev-pilot"),
        )
        return pilot_ref

    def test_bulk_requires_approved_pilot(self):
        self.transport.responses.append(_completion(_extraction_output(self.passages, self.source_keys)))
        before = self.transport.call_count
        with self.assertRaises(ExtractionAuthorizationError):
            self.run_extraction(request_id="run-b0", mode="bulk", pilot_ref=None)
        self.assertEqual(self.transport.call_count, before)

    def test_bulk_requires_workflow_pilot_gate(self):
        pilot_ref = self._make_pilot()
        # Without the extraction.pilot human gate the workflow refuses the pilot.
        with self.assertRaises(workflow.ExtractionWorkflowError):
            workflow.ExtractionWorkflowRepository(self.store).require_pilot(pilot_ref)
        before = self.transport.call_count
        # And the runner refuses bulk work with no new posts.
        with self.assertRaises(ExtractionAuthorizationError):
            self.run_extraction(request_id="run-b1", mode="bulk", pilot_ref=pilot_ref)
        self.assertEqual(self.transport.call_count, before)

    def test_bulk_with_approved_pilot_requires_acquisition(self):
        pilot_ref = self._make_pilot()
        self.approve(pilot_ref, workflow.GATE_PILOT)
        before = self.transport.call_count
        # Pilot + config match, but bulk has no acquired manifest: blocked, no new posts.
        with self.assertRaises(ExtractionAuthorizationError):
            self.run_extraction(request_id="run-b2", mode="bulk", pilot_ref=pilot_ref)
        self.assertEqual(self.transport.call_count, before)


class ReceiptAuthorityTest(RunnerTestBase):
    def test_forged_receipt_swapping_sources_rejected(self):
        result = self.pilot_extraction()
        from review_article_agent.extraction.runner import _load_run_receipt  # noqa: E402

        _, _, original = _load_run_receipt(self.store, result["run_ref"])
        tampered = dict(original)
        tampered["source_refs"] = [self.config_ref]  # not the reports' origin
        self.artifacts.import_revision(
            artifact_id="a-run-x", revision_id="rev-run-2",
            content={"schema_version": 1, "document_type": "extraction_run", "payload": tampered},
            origin_locator="import:forged",
            rights=_rights(self.project_id, "a-run-x", "rev-run-2"),
            parent_revision_id="rev-run-x", acquired_by="import",
        )
        with self.assertRaises(ExtractionReceiptError):
            require_run(self.store, self.ref("a-run-x", "rev-run-2"), purpose="extraction")

    def test_diagnostic_result_forged_complete_receipt_rejected(self):
        # An escalation is an accepted diagnostic, never a completed extraction.
        self.transport.responses.append(_completion({"schema_version": 1, "summary": "incomplete"}))
        result = self.run_extraction(request_id="run-esc")
        self.assertEqual(result["outcome"], "escalation")
        from review_article_agent.extraction.runner import _load_run_receipt  # noqa: E402

        _, _, original = _load_run_receipt(self.store, result["run_ref"])
        forged = dict(original)
        forged["outcome"] = "completed"
        forged["raw_output_sha256"] = None
        self.artifacts.import_revision(
            artifact_id="a-run", revision_id="rev-run-2",
            content={"schema_version": 1, "document_type": "extraction_run", "payload": forged},
            origin_locator="import:forged",
            rights=_rights(self.project_id, "a-run", "rev-run-2"),
            parent_revision_id="rev-run", acquired_by="import",
        )
        with self.assertRaises(ExtractionReceiptError):
            require_run(self.store, self.ref("a-run", "rev-run-2"), purpose="extraction")

    def test_generic_imported_receipt_not_authority(self):
        forged = {
            "schema_version": 1, "document_type": "extraction_run",
            "payload": {
                "purpose": "extraction", "config_ref": self.config_ref, "report_refs": [self.report_ref],
                "source_refs": [self.source_ref], "extraction_ref": None, "acquisition_ref": None,
                "request_id": "run-forged", "dispatch_id": "disp-forged", "task_id": "rtask-forged",
                "proposal_record_id": "prop-forged", "proposal_digest": "0" * 64,
                "observed_model": _MODEL, "request_sha256": "1" * 64, "input_digest": "2" * 64,
                "version_digest": "3" * 64, "instructions_sha256": "4" * 64,
                "mode": "pilot", "pilot_ref": None, "outcome": "completed", "raw_output_sha256": None,
            },
        }
        ref = self._put_document("a-forged", "rev-forged", forged)
        with self.assertRaises(ExtractionReceiptError):
            require_run(self.store, ref, purpose="extraction")


class ReplayTest(RunnerTestBase):
    def test_replay_observes_without_post(self):
        first = self.pilot_extraction()
        self.assertEqual(self.transport.call_count, 1)
        replay = self.run_extraction(request_id="run-x", artifact_id="a-run-x", revision_id="rev-run-x")
        self.assertEqual(replay["outcome"], "completed")
        self.assertEqual(self.transport.call_count, 1)
        self.assertEqual(replay["dispatch_id"], first["dispatch_id"])

    def test_divergent_receipt_ids_conflict(self):
        self.pilot_extraction()
        with self.assertRaises(RuntimeConflictError):
            self.run_extraction(request_id="run-x", artifact_id="a-run-other", revision_id="rev-run-other")


class ReceiptIntegrityFixesTest(RunnerTestBase):
    def test_unknown_source_key_escalates_even_without_findings(self):
        output = _extraction_output(self.passages, self.source_keys)
        output["source_records"].append({"source_key": "a-not-a-read-report", "source_kind": "document"})
        self.transport.responses.append(_completion(output))
        result = self.run_extraction(request_id="run-uk")
        self.assertEqual(result["outcome"], "escalation")

    def test_receipt_write_failure_rolls_back_atomic_commit(self):
        from review_article_agent.extraction import runner as runner_mod  # noqa: E402

        self.transport.responses.append(_completion(_extraction_output(self.passages, self.source_keys)))
        with mock.patch.object(runner_mod, "_write_receipt_in_transaction", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.run_extraction(request_id="run-rb")
        from review_article_agent.runtime.dispatch import RuntimeDispatchRepository  # noqa: E402

        dispatch = RuntimeDispatchRepository(self.store).get_dispatch_by_request("run-rb")
        # The whole unit of work rolled back: the dispatch is not completed, the
        # settled cost was reverted (submitted-state ambiguity only), and no
        # accepted result/receipt survives -- so no receipt/result/cost disagree.
        self.assertNotEqual(dispatch.state, "completed")
        self.assertNotEqual(dispatch.cost_state, "known")
        self.assertIsNone(dispatch.result_ref)
        self.assertIsNone(self.artifacts.get_artifact("a-run"))

    def test_new_request_existing_revision_rejected_before_send(self):
        self.pilot_extraction()
        before = self.transport.call_count
        with self.assertRaises(ExtractionRunnerError):
            self.run_extraction(request_id="run-new", artifact_id="a-run-x", revision_id="rev-run-x")
        self.assertEqual(self.transport.call_count, before)

    def test_tampered_version_digest_rejected(self):
        result = self.pilot_extraction()
        from review_article_agent.extraction.runner import _load_run_receipt  # noqa: E402

        _, _, original = _load_run_receipt(self.store, result["run_ref"])
        tampered = dict(original)
        tampered["version_digest"] = "0" * 64
        self.artifacts.import_revision(
            artifact_id="a-run-x", revision_id="rev-run-2",
            content={"schema_version": 1, "document_type": "extraction_run", "payload": tampered},
            origin_locator="import:forged",
            rights=_rights(self.project_id, "a-run-x", "rev-run-2"),
            parent_revision_id="rev-run-x", acquired_by="import",
        )
        with self.assertRaises(ExtractionReceiptError):
            require_run(self.store, self.ref("a-run-x", "rev-run-2"), purpose="extraction")


class HumanBlockTest(RunnerTestBase):
    def test_unreadable_report_blocks(self):
        # A source that does not parse to a usable report is a human block.
        self.artifacts.create_artifact("a-plain", kind="document", created_by="import")
        self.artifacts.import_revision(
            artifact_id="a-plain", revision_id="rev-a-plain", content=PLAIN_BYTES,
            origin_locator="import:synthetic",
            rights=_rights(self.project_id, "a-plain", "rev-a-plain"), acquired_by="import",
        )
        parsed = ParsingRepository(self.store).parse_stored_document(
            self.ref("a-plain", "rev-a-plain"), report_artifact_id="a-plain-rep",
            report_revision_id="rev-a-plain-rep",
            rights=_rights(self.project_id, "a-plain-rep", "rev-a-plain-rep"),
        )
        hosted = self._put_document(
            "a-hosted-plain", "rev-hosted-plain", _hosted_document(self.config_ref, [parsed["report_ref"]])
        )
        self.approve(hosted, runner.hosted_use_gate_id("extraction"))
        self.transport.responses.append(_completion(_extraction_output(self.passages, self.source_keys)))
        with self.assertRaises(ExtractionRunnerError):
            self.run_extraction(request_id="run-plain", hosted_ref=hosted, report_refs=[parsed["report_ref"]])
        self.assertEqual(self.transport.call_count, 0)


# --------------------------------------------------------------------------- #
# Real end-to-end flow: acquisition manifest -> pilot -> bulk -> audit -> outputs
# --------------------------------------------------------------------------- #
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

import test_acquisition_manifest as acq_manifest  # noqa: E402

from review_article_agent.acquisition.manifest import AcquisitionRepository  # noqa: E402


class FullFlowIntegrationTest(acq_manifest.ManifestBase):
    """Positive full flow over a REAL frozen corpus + acquired manifest.

    No ``_require_run`` seam patch and no fake acquisition: the runner reads real
    receipts, the workflow reads the real pilot/batch/audit, and the manifest is
    the real ``acquisition.complete`` gate.
    """

    def setUp(self) -> None:
        super().setUp()
        self.registry = Registry(str(self.tmp / "registry"))
        self.registry.register_restored(str(self.tmp / "project"), project_id=self.project_id)
        self.token = self.registry.activate_execution(self.project_id).activation_token
        self.workflow = workflow.ExtractionWorkflowRepository(self.store)
        self.transport = _FakeTransport()

    def rights(self, artifact_id, revision_id, **overrides):  # type: ignore[override]
        return _rights(self.project_id, artifact_id, revision_id)

    # -- fixtures ------------------------------------------------------------ #
    def _acquire(self):
        _a, _b, src, entries = self.standard()
        rev = self.make(entries)
        self.gate(rev)
        self.acq.require_acquired()
        return src, self.ref_of(rev)

    def _config_and_report(self, src):
        repo = ParsingRepository(self.store)
        parsed = repo.parse_stored_document(
            src, report_artifact_id="art-rep", report_revision_id="rev-rep",
            rights=self.rights("art-rep", "rev-rep"),
        )
        self.report_ref = parsed["report_ref"]
        record = repo.read_report(self.report_ref)
        self.passages = [
            {"passage_id": f"art-rep:{p['passage_id']}", "source_key": "art-rep", "text": p["text"]}
            for p in record["payload"]["passages"]
        ]
        for index, asset in enumerate(record["payload"].get("assets", [])):
            aid = asset.get("id") or f"asset-{index}"
            self.passages.append(
                {"passage_id": f"art-rep:asset-{index}", "source_key": "art-rep",
                 "text": asset.get("caption") or "", "asset_ids": [f"art-rep:{aid}:{index}"]}
            )
        self.source_ref = src
        self.config_ref = self.workflow.propose(
            "config", _config_payload(), artifact_id="art-cfg", revision_id="rev-cfg",
            rights=self.rights("art-cfg", "rev-cfg"),
        )
        self.approve("art-cfg", "rev-cfg", workflow.GATE_KIND, workflow.GATE_CONFIG)

    def _hosted(self, *, purpose, artifact_id, revision_id, extraction_ref=None, acquisition_ref=None):
        deps = [self.config_ref, self.report_ref, self.source_ref]
        if extraction_ref is not None:
            deps.append(extraction_ref)
        if acquisition_ref is not None:
            deps.append(acquisition_ref)
        document = _hosted_document(
            self.config_ref, [self.report_ref], purpose=purpose,
            extraction_ref=extraction_ref, acquisition_ref=acquisition_ref,
        )
        revision = self._put(artifact_id, "report", revision_id, document, dependencies=deps)
        ref = self.ref_of(revision)
        self.approve(artifact_id, revision_id, workflow.GATE_KIND, runner.hosted_use_gate_id(purpose))
        return ref

    def _execute(self, *, purpose, mode, request_id, artifact_id, revision_id, hosted_ref,
                 extraction_ref=None, pilot_ref=None, report_refs=None):
        return execute_run(
            self.store, registry=self.registry, activation_token=self.token,
            config_ref=self.config_ref,
            report_refs=[self.report_ref] if report_refs is None else report_refs, purpose=purpose,
            request_id=request_id, hosted_ref=hosted_ref, api_key="sk-or-test",
            extraction_ref=extraction_ref, transport=self.transport, mode=mode, pilot_ref=pilot_ref,
            artifact_id=artifact_id, revision_id=revision_id,
            rights=self.rights(artifact_id, revision_id),
        )

    def _pilot(self):
        """Pilot extraction + quality + adjudicated, gate-approved pilot document."""
        self.transport.responses.append(_completion(_extraction_output(self.passages, ["art-rep"])))
        pilot_hosted = self._hosted(purpose="extraction", artifact_id="art-hp", revision_id="rev-hp")
        extraction = self._execute(
            purpose="extraction", mode="pilot", request_id="p-run", artifact_id="art-pr", revision_id="rev-pr",
            hosted_ref=pilot_hosted,
        )
        quality_hosted = self._hosted(
            purpose="quality", artifact_id="art-hq", revision_id="rev-hq", extraction_ref=extraction["run_ref"]
        )
        self.transport.responses.append(_completion(_quality_output(), model=_QUALITY_MODEL))
        quality = self._execute(
            purpose="quality", mode="pilot", request_id="q-run", artifact_id="art-qr", revision_id="rev-qr",
            hosted_ref=quality_hosted, extraction_ref=extraction["run_ref"],
        )
        adjudication = {
            "labels": list(workflow.MANDATORY_FAILURE_LABELS),
            "inspected_original": True, "checked_omissions": True, "checked_qualifications": True,
            "checked_fields": True, "important_failure": False, "severity": "none",
            "rationale": "human read the original", "corrective_action": None,
        }
        pilot_ref = self.workflow.propose(
            "pilot",
            {"config_ref": self.config_ref,
             "cases": [{"id": "case-1", "extraction_ref": extraction["run_ref"], "quality_ref": quality["run_ref"]}],
             "adjudications": {"case-1": adjudication}},
            artifact_id="art-pilot", revision_id="rev-pilot", rights=self.rights("art-pilot", "rev-pilot"),
        )
        self.approve("art-pilot", "rev-pilot", workflow.GATE_KIND, workflow.GATE_PILOT)
        return pilot_ref

    def _bulk_case(self, pilot_ref, acquisition_ref, *, seed="seed-1"):
        bulk_hosted = self._hosted(
            purpose="extraction", artifact_id="art-hb", revision_id="rev-hb", acquisition_ref=acquisition_ref
        )
        self.transport.responses.append(_completion(_extraction_output(self.passages, ["art-rep"])))
        extraction = self._execute(
            purpose="extraction", mode="bulk", request_id="b-run", artifact_id="art-br", revision_id="rev-br",
            hosted_ref=bulk_hosted, pilot_ref=pilot_ref,
        )
        quality_hosted = self._hosted(
            purpose="quality", artifact_id="art-hbq", revision_id="rev-hbq",
            extraction_ref=extraction["run_ref"], acquisition_ref=acquisition_ref,
        )
        self.transport.responses.append(_completion(_quality_output(), model=_QUALITY_MODEL))
        quality = self._execute(
            purpose="quality", mode="bulk", request_id="bq-run", artifact_id="art-bqr", revision_id="rev-bqr",
            hosted_ref=quality_hosted, extraction_ref=extraction["run_ref"], pilot_ref=pilot_ref,
        )
        cases = [{"id": "case-1", "extraction_ref": extraction["run_ref"], "quality_ref": quality["run_ref"]}]
        batch = self.workflow.propose(
            "batch",
            {"config_ref": self.config_ref, "pilot_ref": pilot_ref, "acquisition_ref": acquisition_ref,
             "cases": cases, "seed": seed},
            artifact_id="art-batch", revision_id="rev-batch", rights=self.rights("art-batch", "rev-batch"),
        )
        self.approve("art-batch", "rev-batch", workflow.GATE_KIND, workflow.GATE_BATCH)
        return batch, cases

    @staticmethod
    def _check(*, important_error):
        return {
            "inspected_original": True, "omissions": True, "qualifications": True, "fields": True,
            "important_error": important_error, "rationale": "human checked a sample",
            "corrective_action": "re-run with correction" if important_error else None,
        }

    # -- tests ---------------------------------------------------------------- #
    def test_positive_full_flow_to_synthesis(self):
        src, acquisition_ref = self._acquire()
        self._config_and_report(src)
        pilot_ref = self._pilot()
        batch_ref, _cases = self._bulk_case(pilot_ref, acquisition_ref)
        audit_ref = self.workflow.propose(
            "audit", {"batch_ref": batch_ref, "checks": {"case-1": self._check(important_error=False)}},
            artifact_id="art-audit", revision_id="rev-audit", rights=self.rights("art-audit", "rev-audit"),
        )
        self.approve("art-audit", "rev-audit", workflow.GATE_KIND, workflow.GATE_OUTPUTS)
        synthesis = self.workflow.require_for_synthesis(audit_ref)
        self.assertTrue(synthesis["accepted"])
        self.assertEqual(synthesis["case_ids"], ["case-1"])
        # The bulk receipt is bound to the dispatched pilot and acquisition.
        bulk_receipt = require_run(self.store, self.ref("art-br", "rev-br"), purpose="extraction")
        self.assertEqual(set(bulk_receipt), set(workflow.RECEIPT_KEYS))

    def test_important_audit_pauses_exact_batch_until_new_batch(self):
        src, acquisition_ref = self._acquire()
        self._config_and_report(src)
        pilot_ref = self._pilot()
        batch_ref, cases = self._bulk_case(pilot_ref, acquisition_ref)
        # A paused (important-error) audit of this exact batch permanently pauses it.
        self.workflow.propose(
            "audit", {"batch_ref": batch_ref, "checks": {"case-1": self._check(important_error=True)}},
            artifact_id="art-audit-bad", revision_id="rev-audit-bad",
            rights=self.rights("art-audit-bad", "rev-audit-bad"),
        )
        clean = self.workflow.propose(
            "audit", {"batch_ref": batch_ref, "checks": {"case-1": self._check(important_error=False)}},
            artifact_id="art-audit-clean", revision_id="rev-audit-clean",
            rights=self.rights("art-audit-clean", "rev-audit-clean"),
        )
        self.approve("art-audit-clean", "rev-audit-clean", workflow.GATE_KIND, workflow.GATE_OUTPUTS)
        with self.assertRaises(workflow.ExtractionWorkflowError):
            self.workflow.require_for_synthesis(clean)
        # A corrected NEW batch with a clean audit is accepted.
        batch2 = self.workflow.propose(
            "batch",
            {"config_ref": self.config_ref, "pilot_ref": pilot_ref, "acquisition_ref": acquisition_ref,
             "cases": cases, "seed": "seed-2"},
            artifact_id="art-batch2", revision_id="rev-batch2", rights=self.rights("art-batch2", "rev-batch2"),
        )
        self.approve("art-batch2", "rev-batch2", workflow.GATE_KIND, workflow.GATE_BATCH)
        audit2 = self.workflow.propose(
            "audit", {"batch_ref": batch2, "checks": {"case-1": self._check(important_error=False)}},
            artifact_id="art-audit2", revision_id="rev-audit2", rights=self.rights("art-audit2", "rev-audit2"),
        )
        self.approve("art-audit2", "rev-audit2", workflow.GATE_KIND, workflow.GATE_OUTPUTS)
        synthesis = self.workflow.require_for_synthesis(audit2)
        self.assertTrue(synthesis["accepted"])

    def test_presend_revalidates_revoked_pilot(self):
        src, acquisition_ref = self._acquire()
        self._config_and_report(src)
        pilot_ref = self._pilot()
        bulk_hosted = self._hosted(
            purpose="extraction", artifact_id="art-hb", revision_id="rev-hb", acquisition_ref=acquisition_ref
        )
        kwargs = dict(
            store=self.store, registry=self.registry, activation_token=self.token,
            hosted_ref=bulk_hosted, purpose="extraction", mode="bulk", config_ref=self.config_ref,
            report_refs=[self.report_ref], extraction_ref=None,
            acquisition_ref=acquisition_ref, pilot_ref=pilot_ref,
        )
        runner._revalidate_before_send(**kwargs)  # approved pilot passes
        self.approve("art-pilot", "rev-pilot", workflow.GATE_KIND, workflow.GATE_PILOT, decision="rejected")
        with self.assertRaises(ExtractionAuthorizationError):
            runner._revalidate_before_send(**kwargs)  # revoked pilot is caught pre-send

    def test_bulk_source_not_in_manifest_membership_blocked(self):
        src, acquisition_ref = self._acquire()
        self._config_and_report(src)
        pilot_ref = self._pilot()
        # A second, unrelated report whose original source is NOT in the manifest.
        other = self.put_source(acq_manifest._jats("999"))
        parsed = ParsingRepository(self.store).parse_stored_document(
            other, report_artifact_id="art-other", report_revision_id="rev-other",
            rights=self.rights("art-other", "rev-other"),
        )
        hosted = self._put(
            "art-hother", "report", "rev-hother",
            _hosted_document(self.config_ref, [parsed["report_ref"]], acquisition_ref=acquisition_ref),
            dependencies=[self.config_ref, parsed["report_ref"], other, acquisition_ref],
        )
        self.approve("art-hother", "rev-hother", workflow.GATE_KIND, runner.hosted_use_gate_id("extraction"))
        before = self.transport.call_count
        with self.assertRaises(ExtractionAuthorizationError):
            self._execute(
                purpose="extraction", mode="bulk", request_id="b-run-other",
                artifact_id="art-bor", revision_id="rev-bor", hosted_ref=self.ref_of(hosted),
                pilot_ref=pilot_ref, report_refs=[parsed["report_ref"]],
            )
        self.assertEqual(self.transport.call_count, before)


if __name__ == "__main__":
    unittest.main()
