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
* malformed-200 / refusal / ambiguous-transport outcomes that preserve the
  reserve and never blindly retry;
* a valid, immutable, non-authorizing proposal on success.

Synthetic data only. Run::

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_discovery_lead_runner.py'
"""

from __future__ import annotations

import json
import sys
import tempfile
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
from review_article_agent.runtime.dispatch import RuntimeDispatchRepository  # noqa: E402
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


class RunnerTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-lead-runner-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.registry = Registry(str(self.tmp / "app"))
        info = self.registry.create_project(str(self.tmp / "proj"))
        self.addCleanup(self._close_store)
        self.store = ProjectStore.open_at(paths.app_dir_for(info.root_path))
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
    ):
        document = document if document is not None else _hosted_use_document(
            purpose, config or _config_map(), source_refs
        )
        self.artifacts.create_artifact(artifact_id, kind="report")
        self.artifacts.import_revision(
            artifact_id=artifact_id,
            content=document,
            origin_locator="import:synthetic",
            rights=_rights(self.project_id, artifact_id, revision_id),
            revision_id=revision_id,
            dependencies=[
                {
                    "project_id": ref["project_id"],
                    "artifact_id": ref["artifact_id"],
                    "revision_id": ref["revision_id"],
                }
                for ref in source_refs
            ],
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
                 token=None, api_key="sk-or-test-key"):
        return execute_lead_proposal(
            self.store,
            registry=self.registry,
            activation_token=self.token if token is None else token,
            batch_ref=self.interview_batch if batch_ref is None else batch_ref,
            request_id=request_id,
            api_key=api_key,
            transport=self.transport if transport is None else transport,
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

    def test_transport_unknown_retains_reserve_and_never_resends(self) -> None:
        self.approve_batch(self.interview_batch, "interview")
        self.transport.responses.append(RuntimeError("synthetic network failure"))
        first = self.run_lead()
        self.assertEqual(first["status"], "unknown")
        self.assertEqual(first["dispatch_state"], "unknown")
        self.assertEqual(first["cost_state"], "unknown")
        self.assertEqual(first["reserve_usd"], "0.065536")
        # The reserve and remote occupancy are retained; a re-run never re-sends.
        self.assertEqual(len(self.dispatch.list_dispatches()), 1)
        replay_transport = _FakeTransport()
        second = self.run_lead(transport=replay_transport)
        self.assertEqual(second["status"], "unknown")
        self.assertEqual(replay_transport.call_count, 0)
        self.assertEqual(self.transport.call_count, 1)

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

    def test_unknown_metadata_survives_reopen(self) -> None:
        self.approve_batch(self.interview_batch, "interview")
        self.transport.responses.append(RuntimeError("synthetic network failure"))
        first = self.run_lead()
        self.assertEqual(first["status"], "unknown")
        self.assertEqual(first["cost_state"], "unknown")
        self.assertEqual(first["reserve_usd"], "0.065536")
        self.assertIsNotNone(first["request_sha256"])
        self.assertIsNotNone(first["versions"])
        self.assertIsNone(first["proposal_id"])
        self.assertIsNotNone(first["diagnostic_record_id"])
        reopened = get_lead_outcome(self.store, "req-1")
        for key in (
            "status",
            "actual_model",
            "response_id",
            "usage",
            "versions",
            "request_sha256",
            "diagnostic_record_id",
            "reserve_usd",
        ):
            self.assertEqual(reopened[key], first[key])
        # No rerun and the reserve is retained.
        self.assertEqual(self.transport.call_count, 1)

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
