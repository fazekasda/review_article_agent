"""Tests for the revision-scoped hosted lead proposal runner (Step 05, M2).

Covers ``src/review_article_agent/discovery/lead_runner.py`` against the M2
acceptance criteria and the C02/C03 runtime and rights boundaries:

* static hosted-use document validation (exact envelope/payload, purpose, a
  strict :class:`LeadConfig`, exact source refs, evidence shape/time);
* a real store + registry with **injected fake terminal capabilities and a fake
  transport** (no network, no provider, no paid call, no upload);
* zero-POST fail-closed behaviour for a missing/stale/cross-project/malformed
  batch, an unsatisfied (or later-stale) gate, denied hosted-transfer rights,
  unknown confidentiality, a missing authorization binding, and bad evidence;
* an individually unapproved seed blocking the run;
* the exact request body scope (only the deterministic lead prompt);
* the reserve + submission committed **before** the single POST;
* shared cumulative caps (a stage cap blocks before any send);
* replay / reopen zero-POST, a divergent request conflict, and a drained token;
* malformed-200 / refusal outcomes that preserve the reserve; an ambiguous
  transport error is retried under a new deterministic id (ADR 006) and never
  re-POSTs the same identity;
* a valid, immutable, non-authorizing proposal on success.

Synthetic data only. Run::

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_discovery_lead_runner.py'
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.discovery import context as ctx  # noqa: E402
from review_article_agent.discovery import lead_runner as runner  # noqa: E402
from review_article_agent.discovery.lead_runner import (  # noqa: E402
    HostedLeadError,
    HostedUseAuthorizationError,
    HostedUseDocumentError,
    HostedUseRightsError,
    execute_lead_proposal,
    get_lead_outcome,
    validate_hosted_use_document,
)
from review_article_agent.discovery.records import (  # noqa: E402
    MetadataRecord,
    SearchPage,
    SearchResult,
)
from review_article_agent.runtime import owner_auth  # noqa: E402
from review_article_agent.runtime.dispatch import (  # noqa: E402
    RuntimeDispatchRepository,
    is_retry_resolved,
    runtime_dispatch_remote_state,
)
from review_article_agent.runtime.gates import prepare_gate_snapshot  # noqa: E402
from review_article_agent.runtime.jobs import (  # noqa: E402
    BudgetExceededError,
    JobRepository,
)
from review_article_agent.runtime.lead import LeadConfig, TransportResponse  # noqa: E402
from review_article_agent.runtime.owner_auth import OwnerApproval  # noqa: E402
from review_article_agent.storage import ArtifactRepository, ProjectStore, paths  # noqa: E402
from review_article_agent.storage import Registry  # noqa: E402
from review_article_agent.storage.agent_runtime import (  # noqa: E402
    RuntimeConflictError,
    RuntimeRepository,
)
from review_article_agent.storage.discovery import DiscoveryRepository  # noqa: E402
from review_article_agent.storage.human_approvals import HumanApprovalRepository  # noqa: E402

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")
_BASE_TIME = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
_FIXED_CHALLENGE = "TEST-CHAL-0001"
_OWNER_UID = 1000
_MODEL_ID = "anthropic/claude-sonnet-5-5"
_EVIDENCE = {"ref": "evidence:synthetic", "verified_at": "2026-10-28T00:00:00Z"}


def _config_map(**overrides):
    config = {
        "model_id": _MODEL_ID,
        "upstream_provider": "anthropic",
        "max_output_tokens": 1024,
        "context_tokens": 128000,
        "input_price_per_million": "0.5",
        "output_price_per_million": "1.5",
        "pricing_ref": "pricing:synthetic:v1",
        "instructions_revision": "lead.instr.v1",
        "timeout_seconds": 60.0,
        "max_response_bytes": 1048576,
    }
    config.update(overrides)
    return LeadConfig.from_mapping(config).to_mapping()


def _rights(
    project_id,
    artifact_id,
    revision_id,
    *,
    ops_override=None,
    privacy=None,
    authorization=True,
):
    ops = {op: "allowed" for op in _OPS}
    if ops_override:
        ops.update(ops_override)
    basis = {}
    for op in _OPS:
        if ops[op] == "allowed":
            basis[op] = {
                "kind": "license",
                "evidence": {"ref": "lic:synthetic", "provenance": "publisher:syn"},
            }
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
            "basis": {
                "kind": "license",
                "evidence": {"ref": "lic:synthetic", "provenance": "publisher:syn"},
            },
        }
    privacy_value = {"personal_data": False, "confidentiality": "public", "denial": False}
    if privacy:
        privacy_value.update(privacy)
    return {
        "rights_id": f"RIGHTS-{artifact_id}-{revision_id}",
        "project_id": project_id,
        "record_revision": 1,
        "subject": subject,
        "operations": ops,
        "basis": basis,
        "authorization": auth,
        "restrictions": [],
        "privacy": privacy_value,
        "recorded_by": "user",
        "recorded_at": "2026-10-28T00:00:00Z",
        "derived_status": "valid",
    }


def _brief_envelope(objective="synthetic objective"):
    return {
        "schema_version": 1,
        "document_type": "discovery_brief",
        "payload": {
            "objective": objective,
            "starting_hypothesis": "synthetic hypothesis",
            "questions": ["q1"],
        },
    }


def _hosted_use_document(purpose, config, source_refs, **overrides):
    payload = {
        "purpose": purpose,
        "config": config,
        "source_refs": source_refs,
        "provider_terms": dict(_EVIDENCE),
        "privacy_review": dict(_EVIDENCE),
        "pricing_evidence": dict(_EVIDENCE),
    }
    payload.update(overrides)
    return {
        "schema_version": 1,
        "document_type": "discovery_hosted_use",
        "payload": payload,
    }


def _completion_body(text="synthetic lead proposal", *, cost=0.0002, model=_MODEL_ID):
    # ``cost`` is a JSON number so the adapter's strict decimal parser reads it as
    # an exact amount (a JSON string would not be a reported cost at all).
    return json.dumps(
        {
            "id": "gen-synthetic-1",
            "model": model,
            "provider": "anthropic",
            "choices": [
                {"finish_reason": "stop", "message": {"role": "assistant", "content": text}}
            ],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 8,
                "total_tokens": 20,
                "cost": cost,
            },
        }
    )


class _FakeTty:
    def __init__(self, *, replies=(), is_tty=True):
        self._replies = list(replies)
        self._is_tty = is_tty

    def is_tty(self):
        return self._is_tty

    def write(self, text, *, deadline=None):
        return None

    def read_line(self, *, max_chars, deadline):
        return self._replies.pop(0) if self._replies else None

    def close(self):
        return None


class _FakeTransport:
    """A deterministic transport double. Never touches the network."""

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []
        self.observer = None

    def __call__(self, body, api_key, *, endpoint, timeout, max_response_bytes):
        self.calls.append(
            {
                "body": body,
                "api_key": api_key,
                "endpoint": endpoint,
                "timeout": timeout,
                "max_response_bytes": max_response_bytes,
            }
        )
        if self.observer is not None:
            self.observer()
        if not self.responses:
            raise RuntimeError("synthetic transport has no queued response")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    @property
    def call_count(self):
        return len(self.calls)


class _BlockingTransport(_FakeTransport):
    """A transport double that signals entry then blocks until released."""

    def __init__(self, entered, release, response):
        super().__init__([response])
        self._entered = entered
        self._release = release

    def __call__(self, body, api_key, *, endpoint, timeout, max_response_bytes):
        self._entered.set()
        if not self._release.wait(timeout=10):
            raise RuntimeError("synthetic blocking transport was never released")
        return super().__call__(
            body,
            api_key,
            endpoint=endpoint,
            timeout=timeout,
            max_response_bytes=max_response_bytes,
        )


class RunnerTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-lead-runner-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.registry = Registry(str(self.tmp / "app"))
        info = self.registry.create_project(str(self.tmp / "proj"))
        self.addCleanup(self._close_store)
        self.app_dir = paths.app_dir_for(info.root_path)
        self.store = ProjectStore.open_at(self.app_dir)
        self.project_id = self.store.project_id
        self.artifacts = ArtifactRepository(self.store)
        self.runtime = RuntimeRepository(self.store)
        self.dispatch = RuntimeDispatchRepository(self.store)
        self.jobs = JobRepository(self.store)
        self.transport = _FakeTransport()
        self._install_owner_auth()

        self.brief_ref = self.put_source(
            "a-brief", "rev-brief", _brief_envelope(), kind="report"
        )
        self.interview_batch = self.put_batch(
            "a-batch-int", "rev-batch-int", "interview", [self.brief_ref]
        )
        self.framing_batch = self.put_batch(
            "a-batch-fra", "rev-batch-fra", "framing", [self.brief_ref]
        )
        self.token = self.registry.activate_execution(self.project_id).activation_token

    def _close_store(self) -> None:
        try:
            self.store.close()
        except Exception:  # pragma: no cover - defensive
            pass

    # -- owner-auth seams ---------------------------------------------------- #
    def _install_owner_auth(self) -> None:
        patches = [
            mock.patch.object(owner_auth, "_new_challenge", lambda: _FIXED_CHALLENGE),
            mock.patch.object(owner_auth, "_current_owner_uid", lambda: _OWNER_UID),
            mock.patch.object(owner_auth, "_real_uid", lambda: _OWNER_UID),
            mock.patch.object(owner_auth, "_utcnow", lambda: _BASE_TIME),
            mock.patch.object(owner_auth, "_monotonic", lambda: 1000.0),
            mock.patch.object(
                owner_auth,
                "_open_controlling_tty",
                lambda: _FakeTty(replies=[_FIXED_CHALLENGE]),
            ),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    # -- fixtures ------------------------------------------------------------ #
    def ref(self, artifact_id, revision_id):
        return {
            "project_id": self.project_id,
            "artifact_id": artifact_id,
            "revision_id": revision_id,
        }

    def put_source(
        self,
        artifact_id,
        revision_id,
        document,
        *,
        kind="report",
        ops_override=None,
        privacy=None,
        authorization=True,
        dependencies=(),
        parent=None,
    ):
        self.artifacts.create_artifact(artifact_id, kind=kind)
        self.artifacts.import_revision(
            artifact_id=artifact_id,
            content=document,
            origin_locator="import:synthetic",
            rights=_rights(
                self.project_id,
                artifact_id,
                revision_id,
                ops_override=ops_override,
                privacy=privacy,
                authorization=authorization,
            ),
            revision_id=revision_id,
            dependencies=list(dependencies),
            parent_revision_id=parent,
        )
        return self.ref(artifact_id, revision_id)

    def put_batch(
        self,
        artifact_id,
        revision_id,
        purpose,
        source_refs,
        *,
        config=None,
        document=None,
        parent=None,
        extra_dependencies=(),
    ):
        document = document if document is not None else _hosted_use_document(
            purpose, config or _config_map(), source_refs
        )
        self.artifacts.create_artifact(artifact_id, kind="report")
        dependencies = [
            {
                "project_id": ref["project_id"],
                "artifact_id": ref["artifact_id"],
                "revision_id": ref["revision_id"],
            }
            for ref in source_refs
        ]
        dependencies.extend(extra_dependencies)
        self.artifacts.import_revision(
            artifact_id=artifact_id,
            content=document,
            origin_locator="import:synthetic",
            rights=_rights(self.project_id, artifact_id, revision_id),
            revision_id=revision_id,
            dependencies=dependencies,
            parent_revision_id=parent,
        )
        return self.ref(artifact_id, revision_id)

    def approve(
        self, artifact_id, revision_id, gate_id, *, gate_kind="batch", decision="approved"
    ):
        prepared = prepare_gate_snapshot(
            self.store,
            self.ref(artifact_id, revision_id),
            gate_kind=gate_kind,
            gate_id=gate_id,
            decision=decision,
            rationale="synthetic hosted-use gate",
        )
        capability = owner_auth.request_owner_confirmation(prepared.owner_action())
        assert isinstance(capability, OwnerApproval)
        return HumanApprovalRepository(self.store).record_decision(prepared, capability)

    def approve_batch(self, batch_ref, purpose):
        return self.approve(
            batch_ref["artifact_id"],
            batch_ref["revision_id"],
            runner.hosted_use_gate_id(purpose),
        )

    def put_run(self, request_id="dsr-1", n_records=1):
        records = tuple(
            MetadataRecord(
                source="pubmed",
                source_id=f"3145210{index}",
                pmid=f"3145210{index}",
                title=f"fixture {index}",
                publication_year=2020,
            )
            for index in range(n_records)
        )
        page = SearchPage(
            source="pubmed",
            query="q",
            requested_url="https://eutils.ncbi.nlm.nih.gov/fixture",
            retrieved_at="2026-01-01T00:00:00+00:00",
            page_index=0,
            records=records,
            page_size=20,
            total_count=n_records,
            complete=True,
            stop_reason="completed",
        )
        result = SearchResult.from_pages(
            source="pubmed",
            query="q",
            pages=[page],
            started_at="2026-01-01T00:00:00+00:00",
            completed_at="2026-01-01T00:00:01+00:00",
            stop_reason="completed",
            total_count=n_records,
        )
        return DiscoveryRepository(self.store).record_search(
            request_id=request_id, result=result
        )

    def put_seed(self, artifact_id="a-seed0", revision_id="rev-seed0", *, run_id):
        document = {
            "schema_version": 1,
            "document_type": "discovery_seed",
            "payload": {
                "run_id": run_id,
                "record_index": 0,
                "abstract": "synthetic abstract",
                "abstract_context": "provisional",
                "limitations": ["synthetic"],
            },
        }
        self.artifacts.create_artifact(artifact_id, kind="document")
        self.artifacts.import_revision(
            artifact_id=artifact_id,
            content=document,
            origin_locator="import:synthetic",
            rights=_rights(self.project_id, artifact_id, revision_id),
            revision_id=revision_id,
        )
        return self.ref(artifact_id, revision_id)

    # -- runner helper ------------------------------------------------------- #
    def run_lead(self, *, request_id="req-1", batch_ref=None, transport=None,
                 token=None, api_key="sk-or-test-key", sleep=None):
        return execute_lead_proposal(
            self.store,
            registry=self.registry,
            activation_token=self.token if token is None else token,
            batch_ref=self.interview_batch if batch_ref is None else batch_ref,
            request_id=request_id,
            api_key=api_key,
            transport=self.transport if transport is None else transport,
            sleep=(lambda _delay: None) if sleep is None else sleep,
        )

    def assert_no_dispatch(self):
        self.assertEqual(self.dispatch.list_dispatches(), [])


# --------------------------------------------------------------------------- #
# Static validation
# --------------------------------------------------------------------------- #
class HostedUseDocumentTest(RunnerTestBase):
    def _document(self, **overrides):
        payload = {
            "purpose": "interview",
            "config": _config_map(),
            "source_refs": [self.ref("a1", "r1")],
            "provider_terms": dict(_EVIDENCE),
            "privacy_review": dict(_EVIDENCE),
            "pricing_evidence": dict(_EVIDENCE),
        }
        payload.update(overrides)
        return {
            "schema_version": 1,
            "document_type": "discovery_hosted_use",
            "payload": payload,
        }

    def test_valid_document_returns_independent_copy(self) -> None:
        source = self._document()
        validated = validate_hosted_use_document(source)
        source["payload"]["source_refs"].append(self.ref("a2", "r2"))
        self.assertEqual(len(validated["payload"]["source_refs"]), 1)
        self.assertEqual(validated["payload"]["purpose"], "interview")

    def test_rejects_unknown_missing_envelope_and_payload_fields(self) -> None:
        for bad in (
            self._document() | {"extra": 1},
            {"schema_version": 1, "document_type": "discovery_hosted_use"},
            self._document() | {"document_type": "discovery_brief"},
            {
                **self._document(),
                "payload": {
                    **self._document()["payload"],
                    "approved": True,
                },
            },
            {
                **self._document(),
                "payload": {
                    k: v
                    for k, v in self._document()["payload"].items()
                    if k != "privacy_review"
                },
            },
        ):
            with self.assertRaises(HostedUseDocumentError):
                validate_hosted_use_document(bad)

    def test_rejects_bad_purpose_config_and_evidence(self) -> None:
        with self.assertRaises(HostedUseDocumentError):
            validate_hosted_use_document(self._document(purpose="screening"))
        with self.assertRaises(HostedUseDocumentError):
            validate_hosted_use_document(
                self._document(config={**_config_map(), "model_id": ""})
            )
        with self.assertRaises(HostedUseDocumentError):
            validate_hosted_use_document(
                self._document(provider_terms={"ref": "", "verified_at": "2026-10-28T00:00:00Z"})
            )
        with self.assertRaises(HostedUseDocumentError):
            validate_hosted_use_document(
                self._document(
                    privacy_review={"ref": "x", "verified_at": "2026-10-28T00:00:00"}
                )
            )
        with self.assertRaises(HostedUseDocumentError):
            validate_hosted_use_document(
                self._document(pricing_evidence={"ref": "x", "verified_at": "2026-10-28T00:00:00Z", "extra": 1})
            )

    def test_rejects_duplicate_and_crossproject_source_refs(self) -> None:
        with self.assertRaises(HostedUseDocumentError):
            validate_hosted_use_document(
                self._document(source_refs=[self.ref("a1", "r1"), self.ref("a1", "r1")])
            )
        with self.assertRaises(HostedUseDocumentError):
            validate_hosted_use_document(
                self._document(
                    source_refs=[
                        self.ref("a1", "r1"),
                        {"project_id": "other", "artifact_id": "a2", "revision_id": "r2"},
                    ]
                )
            )

    def test_rejects_bool_schema_version(self) -> None:
        with self.assertRaises(HostedUseDocumentError):
            validate_hosted_use_document(self._document() | {"schema_version": True})


# --------------------------------------------------------------------------- #
# Run / zero-POST guards
# --------------------------------------------------------------------------- #
class ExecuteLeadProposalTest(RunnerTestBase):
    def test_success_records_proposal_and_exact_body(self) -> None:
        self.approve_batch(self.interview_batch, "interview")
        self.transport.responses.append(
            TransportResponse(status=200, body_text=_completion_body())
        )
        outcome = self.run_lead()
        self.assertEqual(outcome["status"], "completed")
        self.assertEqual(outcome["dispatch_state"], "completed")
        self.assertEqual(outcome["cost_state"], "known")
        self.assertEqual(outcome["cost_amount_usd"], "0.0002")
        self.assertTrue(outcome["posted"])
        self.assertEqual(self.transport.call_count, 1)

        # The POST body carries only the deterministic lead prompt.
        body = json.loads(self.transport.calls[0]["body"].decode("utf-8"))
        self.assertEqual(body["model"], _MODEL_ID)
        self.assertEqual(body["provider"]["only"], ["anthropic"])
        self.assertFalse(body["provider"]["allow_fallbacks"])
        self.assertEqual(body["provider"]["data_collection"], "deny")
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        self.assertIn("synthetic objective", body["messages"][1]["content"])
        self.assertEqual(
            body["messages"][1]["content"],
            "Draft the next interview question(s) for this brief:\n\n"
            "[source 0] artifact=a-brief revision=rev-brief\n" + json.dumps(
                _brief_envelope("synthetic objective"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ),
        )

        # The proposal is stored, non-authoritative, and immutable.
        task = self.runtime.get_task_by_request("req-1")
        proposals = self.runtime.proposals_for_task(task.task_id)
        self.assertEqual(len(proposals), 1)
        proposal = proposals[0]
        mapping = proposal.to_mapping()
        self.assertEqual(mapping["kind"], "proposal")
        self.assertEqual(proposal.model_ref, f"openrouter/{_MODEL_ID}#direct")
        for forbidden in ("authority", "approval", "authorize"):
            self.assertNotIn(forbidden, mapping)
        payload = json.loads(proposal.canonical.decode("utf-8"))
        self.assertEqual(payload["content"]["purpose"], "interview")
        self.assertEqual(payload["content"]["proposal_text"], "synthetic lead proposal")

    def test_reserve_committed_before_send(self) -> None:
        self.approve_batch(self.interview_batch, "interview")
        observed = {}

        def observe():
            record = self.dispatch.get_dispatch_by_request("req-1")
            observed["state"] = record.state
            observed["cost_state"] = record.cost_state
            observed["reserve"] = record.reserve_usd

        self.transport.observer = observe
        self.transport.responses.append(
            TransportResponse(status=200, body_text=_completion_body())
        )
        self.run_lead()
        self.assertEqual(observed["state"], "submitted")
        self.assertEqual(observed["cost_state"], "unknown")
        self.assertEqual(observed["reserve"], "0.065536")

    def test_replay_returns_stored_outcome_without_post(self) -> None:
        self.approve_batch(self.interview_batch, "interview")
        self.transport.responses.append(
            TransportResponse(status=200, body_text=_completion_body())
        )
        first = self.run_lead()
        self.assertTrue(first["posted"])
        second_transport = _FakeTransport()
        second = self.run_lead(transport=second_transport)
        self.assertEqual(second["status"], "completed")
        self.assertFalse(second["posted"])
        self.assertEqual(second_transport.call_count, 0)
        self.assertEqual(first["proposal_id"], second["proposal_id"])
        self.assertEqual(len(self.dispatch.list_dispatches()), 1)

    def test_missing_batch_zero_post(self) -> None:
        with self.assertRaises(HostedLeadError):
            self.run_lead(batch_ref=self.ref("a-batch-int", "rev-missing"))
        self.assert_no_dispatch()
        self.assertEqual(self.transport.call_count, 0)

    def test_stale_batch_zero_post(self) -> None:
        # A newer revision of the same batch artifact makes rev-batch-int stale.
        document = _hosted_use_document("interview", _config_map(), [self.brief_ref])
        self.artifacts.import_revision(
            artifact_id="a-batch-int",
            content=document,
            origin_locator="import:synthetic",
            rights=_rights(self.project_id, "a-batch-int", "rev-batch-int2"),
            revision_id="rev-batch-int2",
            dependencies=[self.brief_ref],
            parent_revision_id="rev-batch-int",
        )
        with self.assertRaises(HostedLeadError):
            self.run_lead()
        self.assert_no_dispatch()
        self.assertEqual(self.transport.call_count, 0)

    def test_crossproject_batch_zero_post(self) -> None:
        bad = {"project_id": "other", "artifact_id": "a-batch-int", "revision_id": "rev-batch-int"}
        with self.assertRaises(HostedLeadError):
            self.run_lead(batch_ref=bad)
        self.assert_no_dispatch()
        self.assertEqual(self.transport.call_count, 0)

    def test_malformed_batch_zero_post(self) -> None:
        bad_document = {
            "schema_version": 1,
            "document_type": "discovery_hosted_use",
            "payload": {
                "purpose": "interview",
                "config": _config_map(),
                "source_refs": [self.brief_ref],
                # provider_terms/privacy_review/pricing_evidence missing
            },
        }
        bad_batch = self.put_batch(
            "a-batch-bad", "rev-batch-bad", "interview", [self.brief_ref], document=bad_document
        )
        with self.assertRaises(HostedUseDocumentError):
            self.run_lead(batch_ref=bad_batch)
        self.assert_no_dispatch()
        self.assertEqual(self.transport.call_count, 0)

    def test_unapproved_gate_zero_post(self) -> None:
        with self.assertRaises(HostedUseAuthorizationError):
            self.run_lead()
        self.assert_no_dispatch()
        self.assertEqual(self.transport.call_count, 0)

    def test_gate_stale_after_source_edit_zero_post(self) -> None:
        self.approve_batch(self.interview_batch, "interview")
        # A new revision of the live brief input makes the approval stale.
        self.artifacts.import_revision(
            artifact_id="a-brief",
            content=_brief_envelope("changed objective"),
            origin_locator="import:synthetic",
            rights=_rights(self.project_id, "a-brief", "rev-brief2"),
            revision_id="rev-brief2",
            parent_revision_id="rev-brief",
        )
        with self.assertRaises(HostedUseAuthorizationError):
            self.run_lead()
        self.assert_no_dispatch()
        self.assertEqual(self.transport.call_count, 0)

    def test_denied_hosted_transfer_rights_zero_post(self) -> None:
        source = self.put_source(
            "a-deny", "rev-deny", _brief_envelope(),
            ops_override={"hosted_transfer": "not_allowed"},
        )
        batch = self.put_batch("a-batch-deny", "rev-batch-deny", "interview", [source])
        self.approve_batch(batch, "interview")
        with self.assertRaises(HostedUseRightsError):
            self.run_lead(batch_ref=batch)
        self.assert_no_dispatch()
        self.assertEqual(self.transport.call_count, 0)

    def test_unknown_confidentiality_zero_post(self) -> None:
        source = self.put_source(
            "a-conf", "rev-conf", _brief_envelope(),
            privacy={"confidentiality": "unknown"},
        )
        batch = self.put_batch("a-batch-conf", "rev-batch-conf", "interview", [source])
        self.approve_batch(batch, "interview")
        with self.assertRaises(HostedUseRightsError):
            self.run_lead(batch_ref=batch)
        self.assert_no_dispatch()
        self.assertEqual(self.transport.call_count, 0)

    def test_missing_authorization_zero_post(self) -> None:
        source = self.put_source(
            "a-noauth", "rev-noauth", _brief_envelope(), authorization=False
        )
        batch = self.put_batch("a-batch-noauth", "rev-batch-noauth", "interview", [source])
        self.approve_batch(batch, "interview")
        with self.assertRaises(HostedUseRightsError):
            self.run_lead(batch_ref=batch)
        self.assert_no_dispatch()
        self.assertEqual(self.transport.call_count, 0)

    def test_bad_provider_evidence_zero_post(self) -> None:
        document = _hosted_use_document(
            "interview",
            _config_map(),
            [self.brief_ref],
            provider_terms={"ref": "terms", "verified_at": "not-a-time"},
        )
        bad_batch = self.put_batch(
            "a-batch-ev", "rev-batch-ev", "interview", [self.brief_ref], document=document
        )
        self.approve_batch(bad_batch, "interview")
        with self.assertRaises(HostedUseDocumentError):
            self.run_lead(batch_ref=bad_batch)
        self.assert_no_dispatch()
        self.assertEqual(self.transport.call_count, 0)

    def test_shared_stage_cap_zero_post(self) -> None:
        self.approve_batch(self.interview_batch, "interview")
        self.jobs.set_budget_policy(project_cap_usd="25", stage_cap_usd="0.01")
        with self.assertRaises(BudgetExceededError):
            self.run_lead()
        self.assert_no_dispatch()
        self.assertEqual(self.transport.call_count, 0)

    def test_drained_token_zero_post(self) -> None:
        self.approve_batch(self.interview_batch, "interview")
        self.registry.begin_drain(self.token)
        with self.assertRaises(HostedLeadError):
            self.run_lead()
        self.assert_no_dispatch()
        self.assertEqual(self.transport.call_count, 0)

    def test_divergent_request_conflict(self) -> None:
        self.approve_batch(self.interview_batch, "interview")
        self.approve_batch(self.framing_batch, "framing")
        self.transport.responses.append(
            TransportResponse(status=200, body_text=_completion_body())
        )
        first = self.run_lead(batch_ref=self.interview_batch)
        self.assertEqual(first["status"], "completed")
        with self.assertRaises(Exception) as caught:
            self.run_lead(batch_ref=self.framing_batch)
        self.assertIsInstance(caught.exception, (RuntimeConflictError, HostedLeadError))
        self.assertEqual(len(self.dispatch.list_dispatches()), 1)
        self.assertEqual(self.transport.call_count, 1)

    def test_malformed_200_preserves_cost_no_retry(self) -> None:
        self.approve_batch(self.interview_batch, "interview")
        self.transport.responses.append(
            TransportResponse(status=200, body_text="{not-json")
        )
        outcome = self.run_lead()
        self.assertEqual(outcome["status"], "protocol_error")
        self.assertEqual(outcome["dispatch_state"], "completed")
        self.assertEqual(outcome["cost_state"], "unknown")
        # A replay never re-sends.
        replay_transport = _FakeTransport()
        replay = self.run_lead(transport=replay_transport)
        self.assertEqual(replay["status"], "protocol_error")
        self.assertEqual(replay_transport.call_count, 0)
        self.assertIsNone(replay["proposal_id"])

    def test_refusal_is_not_acceptance(self) -> None:
        self.approve_batch(self.interview_batch, "interview")
        refusal = json.dumps(
            {
                "id": "gen-refuse",
                "model": _MODEL_ID,
                "choices": [
                    {"finish_reason": "content_filter", "message": {"role": "assistant", "content": ""}}
                ],
            }
        )
        self.transport.responses.append(TransportResponse(status=200, body_text=refusal))
        outcome = self.run_lead()
        self.assertEqual(outcome["status"], "refused")
        self.assertEqual(outcome["cost_state"], "unknown")
        # A diagnostic machine record is persisted, but it is NOT a successful
        # lead proposal: the public proposal id is null and no proposal text is
        # attributed.
        self.assertIsNone(outcome["proposal_id"])
        self.assertIsNone(outcome["proposal_record_id"])
        self.assertIsNotNone(outcome["diagnostic_record_id"])
        task = self.runtime.get_task_by_request("req-1")
        proposals = self.runtime.proposals_for_task(task.task_id)
        self.assertEqual(len(proposals), 1)
        content = json.loads(proposals[0].canonical.decode("utf-8"))["content"]
        self.assertEqual(content["outcome"], "refused")
        self.assertIsNone(content["proposal_text"])

    def test_transport_error_then_success_two_posts(self) -> None:
        # ADR 006: an ambiguous transport error is retried under a new id; the base
        # becomes a conservative terminal failure and the retried success is found
        # by the base request id.
        self.approve_batch(self.interview_batch, "interview")
        self.transport.responses.append(RuntimeError("synthetic network failure"))
        self.transport.responses.append(
            TransportResponse(status=200, body_text=_completion_body())
        )
        outcome = self.run_lead()
        self.assertEqual(self.transport.call_count, 2)
        self.assertEqual(outcome["status"], "completed")
        self.assertEqual(outcome["outcome"], "completed")
        base = self.dispatch.get_dispatch_by_request("req-1")
        self.assertTrue(is_retry_resolved(base))
        self.assertEqual(base.state, "failed")
        self.assertEqual(base.cost_state, "estimated")
        self.assertEqual(base.cost_amount_usd, "0.065536")
        retry = self.dispatch.get_dispatch_by_request("req-1-r1")
        self.assertEqual(retry.state, "completed")
        # Readback by the base id resolves the chain to the completed member.
        readback = get_lead_outcome(self.store, "req-1")
        self.assertEqual(readback["status"], "completed")
        self.assertEqual(readback["dispatch_id"], retry.dispatch_id)

    def test_three_ambiguous_exhausts_and_frees_slot(self) -> None:
        self.approve_batch(self.interview_batch, "interview")
        # A large non-chain spend in the stage lets the retries clear the overhead
        # cap, so all three attempts are sent and the last is abandoned.
        self._seed_non_chain_stage_spend(reserve="0.50")
        for _ in range(3):
            self.transport.responses.append(RuntimeError("synthetic network failure"))
        outcome = self.run_lead()
        self.assertEqual(self.transport.call_count, 3)
        self.assertEqual(outcome["status"], "failed")
        for rid in ("req-1", "req-1-r1", "req-1-r2"):
            self.assertTrue(is_retry_resolved(self.dispatch.get_dispatch_by_request(rid)), rid)
        # The serial remote slot is released and readback reports a non-completion.
        self.assertEqual(
            runtime_dispatch_remote_state(self.store.connection, self.project_id), "none"
        )
        self.assertEqual(get_lead_outcome(self.store, "req-1")["status"], "failed")

    def _seed_non_chain_stage_spend(self, *, reserve="0.50", role="section_writer"):
        """Seed a non-chain ``discovery``-stage dispatch to clear the overhead cap."""
        from review_article_agent.runtime.roles import (
            BoundedTask,
            ModelRef,
            RoleConfig,
            RoleConfiguration,
        )

        model = ModelRef(provider_id="openrouter", model_id=_MODEL_ID, variant="direct")
        role_config = RoleConfig(role=role, model=model, steps=1)
        role_set = RoleConfiguration(roles=(role_config,))
        bounded = BoundedTask(
            project_id=self.project_id,
            role=role,
            target={
                "project_id": self.project_id,
                "artifact_id": self.brief_ref["artifact_id"],
                "revision_id": self.brief_ref["revision_id"],
            },
            input_refs=[],
            scope=["discovery", f"role={role}", "non-chain-spend"],
            prompt_version="section_writer.generation.v1",
            size_limit=262_144,
            step_limit=1,
            role_config=role_config,
        )
        task = self.runtime.record_task(
            bounded, request_id="non-chain-spend", configuration=role_set
        )
        self.dispatch.reserve_dispatch(
            task,
            request_id="non-chain-spend",
            route="direct_call",
            stage="discovery",
            input_digest="a" * 64,
            version_digest="b" * 64,
            reserve_usd=reserve,
            bound_evidence_digest="c" * 64,
        )

    def test_outcome_has_no_secret_or_source_text(self) -> None:
        self.approve_batch(self.interview_batch, "interview")
        self.transport.responses.append(
            TransportResponse(status=200, body_text=_completion_body())
        )
        outcome = self.run_lead()
        serialized = json.dumps(outcome)
        self.assertNotIn("sk-or-test-key", serialized)
        self.assertNotIn("synthetic objective", serialized)
        self.assertNotIn("synthetic lead proposal", serialized)

    def test_get_lead_outcome_readback(self) -> None:
        self.assertIsNone(get_lead_outcome(self.store, "req-unknown"))
        self.approve_batch(self.interview_batch, "interview")
        self.transport.responses.append(
            TransportResponse(status=200, body_text=_completion_body())
        )
        self.run_lead()
        readback = get_lead_outcome(self.store, "req-1")
        self.assertEqual(readback["status"], "completed")
        self.assertIsNotNone(readback["proposal_id"])


# --------------------------------------------------------------------------- #
# Concurrency, amendments, drain, bounded output
# --------------------------------------------------------------------------- #
class ConcurrencyAndLifecycleTest(RunnerTestBase):
    def test_concurrent_duplicate_send_exactly_one_post(self) -> None:
        # Two callers, two ProjectStore connections (each owned by its thread),
        # one shared Registry. The new-send decision is made inside the registry
        # write guard, so at most one caller can ever reach the POST.
        self.approve_batch(self.interview_batch, "interview")
        entered = threading.Event()
        release = threading.Event()
        transport_a = _BlockingTransport(
            entered, release, TransportResponse(status=200, body_text=_completion_body())
        )
        transport_b = _FakeTransport()
        results: dict[str, object] = {}

        def worker(name, transport):
            store = ProjectStore.open_at(self.app_dir)
            try:
                results[name] = execute_lead_proposal(
                    store,
                    registry=self.registry,
                    activation_token=self.token,
                    batch_ref=self.interview_batch,
                    request_id="race-1",
                    api_key="sk-or-test-key",
                    transport=transport,
                )
            except BaseException as exc:  # recorded so the assertion can report it
                results[name] = exc
            finally:
                store.close()

        thread_a = threading.Thread(target=worker, args=("a", transport_a))
        thread_a.start()
        self.assertTrue(entered.wait(timeout=10), "first caller never reached the POST")
        thread_b = threading.Thread(target=worker, args=("b", transport_b))
        thread_b.start()
        thread_b.join(timeout=20)
        self.assertFalse(thread_b.is_alive(), "second caller did not finish")
        release.set()
        thread_a.join(timeout=20)
        self.assertFalse(thread_a.is_alive(), "first caller did not finish")

        result_a = results.get("a")
        result_b = results.get("b")
        self.assertIsInstance(result_a, dict, msg=f"first caller failed: {result_a!r}")
        self.assertEqual(result_a["status"], "completed")
        self.assertIsInstance(result_b, dict, msg=f"second caller failed: {result_b!r}")
        # The loser observes the in-flight/stored state without mutating remote
        # execution and without a second POST.
        self.assertIn(result_b["status"], ("submitted", "completed"))
        self.assertEqual(transport_a.call_count, 1)
        self.assertEqual(transport_b.call_count, 0)

        self.assertEqual(len(self.dispatch.list_dispatches()), 1)
        record = self.dispatch.get_dispatch_by_request("race-1")
        self.assertEqual(record.state, "completed")
        self.assertEqual(record.result_kind, "lead_proposal")
        task = self.runtime.get_task_by_request("race-1")
        self.assertEqual(len(self.runtime.proposals_for_task(task.task_id)), 1)

    def test_amended_batch_lineage_edge_and_fresh_gate_permits(self) -> None:
        v1 = self.put_batch("a-batch-am", "rev-batch-am", "interview", [self.brief_ref])
        self.approve_batch(v1, "interview")
        lineage = {**self.ref("a-batch-am", "rev-batch-am"), "role": "version_of"}
        v2 = self.put_batch(
            "a-batch-am",
            "rev-batch-am2",
            "interview",
            [self.brief_ref],
            parent="rev-batch-am",
            extra_dependencies=[lineage],
        )
        # The old (v1) gate is stale: its target is no longer the current head.
        with self.assertRaises(HostedLeadError):
            self.run_lead(batch_ref=v1)
        self.assert_no_dispatch()
        self.assertEqual(self.transport.call_count, 0)
        # A fresh exact gate on the amended revision permits the run; the
        # same-artifact version_of lineage edge is not treated as a source.
        self.approve_batch(v2, "interview")
        self.transport.responses.append(
            TransportResponse(status=200, body_text=_completion_body())
        )
        outcome = self.run_lead(batch_ref=v2)
        self.assertEqual(outcome["status"], "completed")
        self.assertEqual(self.transport.call_count, 1)

    def test_extra_undeclared_dependency_rejected(self) -> None:
        extra = self.put_source("a-extra", "rev-extra", _brief_envelope())
        batch = self.put_batch(
            "a-batch-extra",
            "rev-batch-extra",
            "interview",
            [self.brief_ref],
            extra_dependencies=[extra],
        )
        self.approve_batch(batch, "interview")
        with self.assertRaises(HostedLeadError):
            self.run_lead(batch_ref=batch)
        self.assert_no_dispatch()
        self.assertEqual(self.transport.call_count, 0)

    def test_drain_during_post_commits_and_allows_safe_switch(self) -> None:
        self.approve_batch(self.interview_batch, "interview")

        class _DrainTransport(_FakeTransport):
            def __init__(self, registry, token, response):
                super().__init__([response])
                self._registry = registry
                self._token = token
                self._drained = False

            def __call__(self, body, api_key, *, endpoint, timeout, max_response_bytes):
                if not self._drained:
                    self._registry.begin_drain(self._token)
                    self._drained = True
                return super().__call__(
                    body,
                    api_key,
                    endpoint=endpoint,
                    timeout=timeout,
                    max_response_bytes=max_response_bytes,
                )

        transport = _DrainTransport(
            self.registry, self.token, TransportResponse(status=200, body_text=_completion_body())
        )
        outcome = self.run_lead(transport=transport)
        self.assertEqual(outcome["status"], "completed")
        self.assertEqual(outcome["cost_state"], "known")
        self.assertIsNotNone(outcome["actual_model"])
        self.assertEqual(transport.call_count, 1)
        record = self.dispatch.get_dispatch_by_request("req-1")
        self.assertEqual(record.state, "completed")
        task = self.runtime.get_task_by_request("req-1")
        self.assertEqual(len(self.runtime.proposals_for_task(task.task_id)), 1)

        # A later safe switch is permitted: the drained activation releases, then
        # re-activates, and a replay returns the stored outcome with no re-POST.
        self.registry.release_execution(self.token)
        new_token = self.registry.activate_execution(self.project_id).activation_token
        spy = _FakeTransport()
        replay = self.run_lead(transport=spy, token=new_token)
        self.assertEqual(replay["status"], "completed")
        self.assertEqual(spy.call_count, 0)
        self.assertEqual(len(self.dispatch.list_dispatches()), 1)

    def test_foreign_token_rejected(self) -> None:
        self.approve_batch(self.interview_batch, "interview")
        with self.assertRaises(HostedLeadError):
            self.run_lead(token="act_ffffffffffffffffffffffffffffffff")
        self.assert_no_dispatch()
        self.assertEqual(self.transport.call_count, 0)

    def test_oversized_completion_records_diagnostic(self) -> None:
        self.approve_batch(self.interview_batch, "interview")
        self.transport.responses.append(
            TransportResponse(status=200, body_text=_completion_body(text="x" * 300_000))
        )
        outcome = self.run_lead()
        # A completed output too large to store as a bounded proposal becomes a
        # diagnostic with the received metadata and the settled cost preserved.
        self.assertEqual(outcome["status"], "oversized_output")
        self.assertIsNone(outcome["proposal_id"])
        self.assertIsNone(outcome["proposal_record_id"])
        self.assertIsNotNone(outcome["diagnostic_record_id"])
        self.assertEqual(outcome["cost_state"], "known")
        self.assertEqual(outcome["cost_amount_usd"], "0.0002")
        self.assertEqual(outcome["actual_model"], _MODEL_ID)
        self.assertIsNotNone(outcome["request_sha256"])
        record = self.dispatch.get_dispatch_by_request("req-1")
        self.assertEqual(record.state, "completed")
        self.assertEqual(record.result_kind, "lead_oversized_output")
        task = self.runtime.get_task_by_request("req-1")
        proposals = self.runtime.proposals_for_task(task.task_id)
        self.assertEqual(len(proposals), 1)
        content = json.loads(proposals[0].canonical.decode("utf-8"))["content"]
        self.assertEqual(content["outcome"], "oversized_output")
        self.assertIsNone(content["proposal_text"])
        # The persisted envelope stays bounded.
        self.assertLessEqual(len(proposals[0].canonical), 262_144)
        # The metadata survives a reopen (no in-memory result).
        reopened = get_lead_outcome(self.store, "req-1")
        self.assertEqual(reopened["status"], "oversized_output")
        self.assertEqual(reopened["actual_model"], _MODEL_ID)
        self.assertEqual(reopened["request_sha256"], outcome["request_sha256"])

    def test_source_edit_after_send_stays_proposal_and_creates_no_approval(self) -> None:
        self.approve_batch(self.interview_batch, "interview")
        self.transport.responses.append(
            TransportResponse(status=200, body_text=_completion_body())
        )
        self.run_lead()
        approvals = HumanApprovalRepository(self.store)
        before = len(approvals.list_approvals())
        # A post-send scope edit: a new revision of the live brief input.
        self.artifacts.import_revision(
            artifact_id="a-brief",
            content=_brief_envelope("edited objective"),
            origin_locator="import:synthetic",
            rights=_rights(self.project_id, "a-brief", "rev-brief-edit"),
            revision_id="rev-brief-edit",
            parent_revision_id="rev-brief",
        )
        after = get_lead_outcome(self.store, "req-1")
        self.assertEqual(after["status"], "completed")
        self.assertIsNotNone(after["proposal_id"])
        # The runner created no authoritative approval; the outcome stays a
        # (still non-authorizing) proposal.
        self.assertEqual(len(approvals.list_approvals()), before)
        task = self.runtime.get_task_by_request("req-1")
        proposal = self.runtime.proposals_for_task(task.task_id)[0]
        self.assertEqual(proposal.to_mapping()["kind"], "proposal")
        for forbidden in ("authority", "approval", "authorize"):
            self.assertNotIn(forbidden, proposal.to_mapping())


# --------------------------------------------------------------------------- #
# Durable execution-metadata provenance
# --------------------------------------------------------------------------- #
class MetadataPersistenceTest(RunnerTestBase):
    def _run_success(self, **kwargs):
        self.approve_batch(self.interview_batch, "interview")
        self.transport.responses.append(
            TransportResponse(status=200, body_text=_completion_body())
        )
        return self.run_lead(**kwargs)

    def test_success_metadata_survives_reopen(self) -> None:
        first = self._run_success()
        expected = {
            "actual_model": _MODEL_ID,
            "provider": "anthropic",
            "response_id": "gen-synthetic-1",
            "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
        }
        for key, value in expected.items():
            self.assertEqual(first[key], value)
        self.assertIsNotNone(first["versions"])
        self.assertEqual(first["http_status"], 200)
        self.assertEqual(self.transport.call_count, 1)

        # A fresh readback (no in-memory result) and a POST-free replay agree.
        reopened = get_lead_outcome(self.store, "req-1")
        for key, value in expected.items():
            self.assertEqual(reopened[key], value)
        self.assertEqual(reopened["versions"], first["versions"])
        self.assertEqual(reopened["request_sha256"], first["request_sha256"])
        self.assertEqual(reopened["proposal_id"], first["proposal_id"])

        replay_transport = _FakeTransport()
        replayed = self.run_lead(transport=replay_transport)
        self.assertEqual(replay_transport.call_count, 0)
        for key, value in expected.items():
            self.assertEqual(replayed[key], value)
        self.assertEqual(replayed["versions"], first["versions"])
        self.assertTrue(first["posted"])
        self.assertFalse(replayed["posted"])

    def test_refusal_metadata_survives_reopen(self) -> None:
        self.approve_batch(self.interview_batch, "interview")
        refusal = json.dumps(
            {
                "id": "gen-refuse",
                "model": _MODEL_ID,
                "choices": [
                    {"finish_reason": "content_filter", "message": {"role": "assistant", "content": ""}}
                ],
                "usage": {"prompt_tokens": 7, "completion_tokens": 0, "total_tokens": 7},
            }
        )
        self.transport.responses.append(TransportResponse(status=200, body_text=refusal))
        first = self.run_lead()
        self.assertEqual(first["status"], "refused")
        self.assertEqual(first["actual_model"], _MODEL_ID)
        self.assertEqual(first["response_id"], "gen-refuse")
        self.assertEqual(
            first["usage"],
            {"prompt_tokens": 7, "completion_tokens": 0, "total_tokens": 7},
        )
        self.assertIsNone(first["proposal_id"])
        self.assertIsNotNone(first["diagnostic_record_id"])
        reopened = get_lead_outcome(self.store, "req-1")
        self.assertEqual(reopened["actual_model"], first["actual_model"])
        self.assertEqual(reopened["usage"], first["usage"])
        self.assertEqual(reopened["response_id"], first["response_id"])
        self.assertEqual(reopened["diagnostic_record_id"], first["diagnostic_record_id"])
        self.assertEqual(self.transport.call_count, 1)

    def test_protocol_metadata_survives_reopen(self) -> None:
        self.approve_batch(self.interview_batch, "interview")
        self.transport.responses.append(TransportResponse(status=200, body_text="{not-json"))
        first = self.run_lead()
        self.assertEqual(first["status"], "protocol_error")
        self.assertEqual(first["http_status"], 200)
        self.assertIsNotNone(first["request_sha256"])
        self.assertIsNone(first["proposal_id"])
        self.assertIsNotNone(first["diagnostic_record_id"])
        reopened = get_lead_outcome(self.store, "req-1")
        for key in (
            "actual_model",
            "provider",
            "response_id",
            "usage",
            "versions",
            "http_status",
            "request_sha256",
            "diagnostic_record_id",
        ):
            self.assertEqual(reopened[key], first[key])

    def test_exhausted_readback_survives_reopen(self) -> None:
        # ADR 006: a lone ambiguous transport error is retried; with no stage base
        # spend the second retry is refused, so the chain is exhausted and the
        # readback reports a definitive non-completion (never "unknown").
        self.approve_batch(self.interview_batch, "interview")
        self.transport.responses.append(RuntimeError("synthetic network failure"))
        first = self.run_lead()
        self.assertEqual(first["status"], "failed")
        self.assertEqual(first["cost_state"], "estimated")
        self.assertEqual(first["reserve_usd"], "0.065536")
        self.assertIsNone(first["proposal_id"])
        reopened = get_lead_outcome(self.store, "req-1")
        for key in (
            "status",
            "outcome",
            "dispatch_state",
            "cost_state",
            "cost_amount_usd",
            "reserve_usd",
        ):
            self.assertEqual(reopened[key], first[key])
        # The base error and one retry were sent; the base is a resolved failure.
        self.assertEqual(self.transport.call_count, 2)
        self.assertTrue(is_retry_resolved(self.dispatch.get_dispatch_by_request("req-1")))

    def test_cost_is_dispatch_authoritative_not_copied(self) -> None:
        self.approve_batch(self.interview_batch, "interview")
        self.transport.responses.append(
            TransportResponse(status=200, body_text=_completion_body(cost=0.0002))
        )
        outcome = self.run_lead()
        dispatch_record = self.dispatch.get_dispatch_by_request("req-1")
        self.assertEqual(outcome["cost_amount_usd"], dispatch_record.cost_amount_usd)
        self.assertEqual(outcome["cost_state"], dispatch_record.cost_state)
        self.assertEqual(outcome["cost_amount_usd"], "0.0002")
        # The persisted envelope never carries a monetary cost (nothing to copy
        # unchecked); the direct-dispatch ledger stays the single authority.
        task = self.runtime.get_task_by_request("req-1")
        for record in self.runtime.proposals_for_task(task.task_id):
            text = record.canonical.decode("utf-8").lower()
            self.assertNotIn("cost_amount", text)
            self.assertNotIn('"cost"', text)

    def test_tampered_dispatch_result_digest_rejected(self) -> None:
        self._run_success()
        dispatch_record = self.dispatch.get_dispatch_by_request("req-1")
        self.store.connection.execute(
            "UPDATE runtime_dispatch_state SET result_digest = ? "
            "WHERE project_id = ? AND dispatch_id = ?",
            ("b" * 64, self.project_id, dispatch_record.dispatch_id),
        )
        self.store.connection.commit()
        with self.assertRaises(HostedLeadError):
            get_lead_outcome(self.store, "req-1")


# --------------------------------------------------------------------------- #
# Seed context
# --------------------------------------------------------------------------- #
class SeedContextTest(RunnerTestBase):
    def _seed_batch(self, artifact_id, approve_seed):
        run = self.put_run(request_id="dsr-1")
        seed_ref = self.put_seed(artifact_id="a-seed0", revision_id="rev-seed0", run_id=run.run_id)
        if approve_seed:
            self.approve(
                seed_ref["artifact_id"],
                seed_ref["revision_id"],
                ctx.SEED_GATE_PREFIX + seed_ref["artifact_id"],
                gate_kind=ctx.ARTIFACT_REVISION_GATE_KIND,
            )
        batch = self.put_batch(
            artifact_id, "rev-" + artifact_id, "interview", [seed_ref]
        )
        self.approve_batch(batch, "interview")
        return batch

    def test_unapproved_seed_zero_post(self) -> None:
        batch = self._seed_batch("a-batch-seed", approve_seed=False)
        with self.assertRaises(HostedUseAuthorizationError):
            self.run_lead(batch_ref=batch)
        self.assert_no_dispatch()
        self.assertEqual(self.transport.call_count, 0)

    def test_approved_seed_succeeds_without_promoting_abstract(self) -> None:
        batch = self._seed_batch("a-batch-seed2", approve_seed=True)
        self.transport.responses.append(
            TransportResponse(status=200, body_text=_completion_body())
        )
        outcome = self.run_lead(batch_ref=batch)
        self.assertEqual(outcome["status"], "completed")
        # The seed's provisional label is preserved in the prompt bytes.
        body = json.loads(self.transport.calls[0]["body"].decode("utf-8"))
        self.assertIn("provisional", body["messages"][1]["content"])


if __name__ == "__main__":
    unittest.main()
