"""Fast-tier tests for the application-boundary generation runner.

Covers ``src/review_article_agent/generation/runner.py`` (kickoff 16 §5): the
single-shot, budgeted, ledgered, gate/rights-revalidated generation runner over
the direct OpenRouter adapter. A fake transport is always injected, so no test
performs a network call; real temp stores, a real registry activation token, a
real owner-authenticated human gate, and real rights records are used.

Run:

    PYTHONPATH=src:tests python3 -m unittest tests.test_generation_runner
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

from review_article_agent.generation import hosted_use  # noqa: E402
from review_article_agent.generation import runner  # noqa: E402
from review_article_agent.generation.runner import (  # noqa: E402
    GenerationAuthorizationError,
    GenerationBudgetError,
    GenerationDocumentError,
    GenerationRightsError,
    GenerationRunnerError,
    execute_generation_task,
    get_generation_outcome,
)
from review_article_agent.runtime import generation, owner_auth  # noqa: E402
from review_article_agent.runtime.gates import prepare_gate_snapshot  # noqa: E402
from review_article_agent.runtime.lead import LeadConfig, TransportResponse  # noqa: E402
from review_article_agent.runtime.owner_auth import OwnerApproval  # noqa: E402
from review_article_agent.runtime.dispatch import RuntimeDispatchRepository  # noqa: E402
from review_article_agent.storage import ArtifactRepository, ProjectStore, Registry, paths  # noqa: E402
from review_article_agent.storage.agent_runtime import RuntimeRepository  # noqa: E402
from review_article_agent.storage.human_approvals import HumanApprovalRepository  # noqa: E402

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")
_MODEL = "deepseek/deepseek-v4.1-flash"
_ROLE = "section_writer"
_PROMPT_VERSION = "section_writer.generation.v1"
_INSTRUCTIONS = "Write the requested section from the untrusted material."
_EVIDENCE = {"ref": "evidence:synthetic", "verified_at": "2026-10-28T00:00:00Z"}
_FIXED_CHALLENGE = "TEST-CHAL-0001"
_OWNER_UID = 1000
_BASE_TIME = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
_API_KEY = "sk-or-v1-generation-canary-secret"


def _rights(project_id, artifact_id, revision_id, *, ops=None):
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
    return {
        "rights_id": f"RIGHTS-{artifact_id}-{revision_id}",
        "project_id": project_id,
        "record_revision": 1,
        "subject": subject,
        "operations": operations,
        "basis": basis,
        "authorization": {
            "actor": "user",
            "method": "out_of_band_authenticated",
            "authorized_at": "2026-10-28T00:00:00Z",
            "against_subject": subject,
            "against_rights_revision": 1,
            "basis": {"kind": "license", "evidence": {"ref": "lic:syn", "provenance": "publisher:syn"}},
        },
        "restrictions": [],
        "privacy": {"personal_data": False, "confidentiality": "public", "denial": False},
        "recorded_by": "user",
        "recorded_at": "2026-10-28T00:00:00Z",
        "derived_status": "valid",
    }


def _model_map(model=_MODEL, provider="deepseek"):
    return LeadConfig.from_mapping(
        {
            "model_id": model,
            "upstream_provider": provider,
            "max_output_tokens": 1024,
            "context_tokens": 128000,
            "input_price_per_million": "0.5",
            "output_price_per_million": "1.5",
            "pricing_ref": "pricing:synthetic:v1",
            "instructions_revision": "instr.v1",
            "timeout_seconds": 60.0,
            "max_response_bytes": 1048576,
        }
    ).to_mapping()


def _document(role=_ROLE, *, model=_MODEL, budget="10.00", notes=None):
    return {
        "schema_version": 1,
        "document_type": "generation_hosted_use",
        "payload": {
            "role": role,
            "config": _model_map(model),
            "budget_cap_usd": budget,
            "provider_terms": dict(_EVIDENCE),
            "privacy_review": dict(_EVIDENCE),
            "pricing_evidence": dict(_EVIDENCE),
            "notes": notes,
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
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(content_obj)},
            }
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20, "cost": cost},
    }
    payload.update(overrides)
    return TransportResponse(status=200, body_text=json.dumps(payload), truncated=False)


def _proposal():
    return {"section": "results", "text": "The trial reduced the outcome.", "citations": []}


class RunnerTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-generation-runner-")
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

        self.source_ref = self._put("a-source", "rev-source", {"kind": "article", "text": "original text"})
        self.input_ref = self._put("a-input", "rev-input", {"kind": "material", "text": "derived material"})
        self.hosted_ref = self._hosted("a-hosted", "rev-hosted", budget="10.00")
        self.approve(self.hosted_ref, hosted_use.hosted_use_gate_id(_ROLE))

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

    def _put(self, artifact_id, revision_id, content, *, ops=None, kind="report"):
        if not isinstance(content, (bytes, bytearray)):
            content = json.dumps(content).encode("utf-8")
        self.artifacts.create_artifact(artifact_id, kind=kind, created_by="import")
        self.artifacts.import_revision(
            artifact_id=artifact_id,
            revision_id=revision_id,
            content=bytes(content),
            origin_locator="import:synthetic",
            rights=_rights(self.project_id, artifact_id, revision_id, ops=ops),
            acquired_by="import",
        )
        return self.ref(artifact_id, revision_id)

    def _hosted(self, artifact_id, revision_id, *, role=_ROLE, budget="10.00", model=_MODEL):
        return hosted_use.propose_hosted_use(
            self.store,
            _document(role, model=model, budget=budget),
            artifact_id=artifact_id,
            revision_id=revision_id,
            rights=_rights(self.project_id, artifact_id, revision_id),
        )

    def approve(self, ref, gate_id, *, gate_kind="batch", decision="approved"):
        prepared = prepare_gate_snapshot(
            self.store,
            ref,
            gate_kind=gate_kind,
            gate_id=gate_id,
            decision=decision,
            rationale="synthetic generation gate",
        )
        capability = owner_auth.request_owner_confirmation(prepared.owner_action())
        assert isinstance(capability, OwnerApproval)
        HumanApprovalRepository(self.store).record_decision(prepared, capability)

    # -- runner helper ------------------------------------------------------- #
    def run_generation(
        self,
        *,
        request_id="gen-1",
        role=_ROLE,
        hosted_ref=None,
        input_refs=None,
        source_refs=None,
        token=None,
        api_key=_API_KEY,
        transport=None,
        material=None,
    ):
        return execute_generation_task(
            self.store,
            registry=self.registry,
            activation_token=self.token if token is None else token,
            role=role,
            request_id=request_id,
            hosted_ref=self.hosted_ref if hosted_ref is None else hosted_ref,
            prompt_version=_PROMPT_VERSION,
            instructions=_INSTRUCTIONS,
            material={"material": "derived material"} if material is None else material,
            input_refs=[self.input_ref] if input_refs is None else input_refs,
            source_refs=[self.source_ref] if source_refs is None else source_refs,
            api_key=api_key,
            transport=self.transport if transport is None else transport,
        )


# --------------------------------------------------------------------------- #
# Completed commit
# --------------------------------------------------------------------------- #
class CompletedTest(RunnerTestBase):
    def test_completed_commits_proposal_result_cost_atomically(self):
        self.transport.responses.append(_completion(_proposal()))
        result = self.run_generation()
        self.assertEqual(result["outcome"], "completed")
        self.assertEqual(result["role"], _ROLE)
        self.assertEqual(result["prompt_version"], _PROMPT_VERSION)
        self.assertEqual(result["proposal"], _proposal())
        self.assertIsNotNone(result["dispatch_id"])
        self.assertEqual(result["cost"], {"state": "known", "amount_usd": "0.0002"})
        self.assertIsNotNone(result["proposal_ref"])
        self.assertFalse(result["replayed"])
        self.assertEqual(self.transport.call_count, 1)

        dispatch = RuntimeDispatchRepository(self.store).get_dispatch_by_request("gen-1")
        self.assertEqual(dispatch.state, "completed")
        self.assertEqual(dispatch.result_kind, "generation_proposal")
        self.assertEqual(dispatch.cost_state, "known")
        self.assertEqual(dispatch.cost_amount_usd, "0.0002")
        self.assertEqual(dispatch.route, "direct_call")
        self.assertEqual(dispatch.stage, "generation")

        # role + prompt_version are recorded on the stored proposal/version set.
        runtime = RuntimeRepository(self.store)
        task = runtime.get_task_by_request("gen-1")
        self.assertEqual(task.role, _ROLE)
        self.assertEqual(task.prompt_version, _PROMPT_VERSION)
        proposal = runtime.get_proposal(result["proposal_ref"]["record_id"])
        self.assertEqual(proposal.role, _ROLE)

    def test_completed_without_reported_cost_is_unknown_retained(self):
        body = _completion(_proposal())
        payload = json.loads(body.body_text)
        payload["usage"].pop("cost", None)
        self.transport.responses.append(TransportResponse(status=200, body_text=json.dumps(payload), truncated=False))
        result = self.run_generation()
        self.assertEqual(result["outcome"], "completed")
        self.assertEqual(result["proposal"], _proposal())
        self.assertEqual(result["cost"]["state"], "unknown")
        self.assertIsNone(result["cost"]["amount_usd"])

    def test_readback_matches_returned_mapping(self):
        self.transport.responses.append(_completion(_proposal()))
        result = self.run_generation()
        readback = get_generation_outcome(self.store, "gen-1")
        self.assertEqual(readback["outcome"], "completed")
        self.assertEqual(readback["proposal"], _proposal())
        self.assertEqual(readback["dispatch_id"], result["dispatch_id"])
        self.assertIsNone(get_generation_outcome(self.store, "nope"))


# --------------------------------------------------------------------------- #
# Replay / single shot
# --------------------------------------------------------------------------- #
class ReplayTest(RunnerTestBase):
    def test_replay_returns_stored_outcome_zero_posts(self):
        self.transport.responses.append(_completion(_proposal()))
        first = self.run_generation()
        replay = self.run_generation()
        self.assertEqual(self.transport.call_count, 1)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["outcome"], "completed")
        self.assertEqual(replay["proposal"], _proposal())
        self.assertEqual(replay["dispatch_id"], first["dispatch_id"])


# --------------------------------------------------------------------------- #
# Refusals before any POST
# --------------------------------------------------------------------------- #
class RefusalTest(RunnerTestBase):
    def test_gate_missing_refused_before_post(self):
        hosted = self._hosted("a-hosted-nogate", "rev-nogate")
        with self.assertRaises(GenerationAuthorizationError):
            self.run_generation(hosted_ref=hosted)
        self.assertEqual(self.transport.call_count, 0)

    def test_superseded_hosted_revision_refused_before_post(self):
        self.artifacts.import_revision(
            artifact_id="a-hosted",
            revision_id="rev-hosted-2",
            content=_document(budget="11.00"),
            origin_locator="generation:local",
            rights=_rights(self.project_id, "a-hosted", "rev-hosted-2"),
            parent_revision_id="rev-hosted",
            acquired_by="user",
        )
        with self.assertRaises(GenerationDocumentError):
            self.run_generation()
        self.assertEqual(self.transport.call_count, 0)

    def test_role_mismatch_refused_before_post(self):
        with self.assertRaises(GenerationDocumentError):
            self.run_generation(role="section_critic")
        self.assertEqual(self.transport.call_count, 0)

    def test_inactive_token_refused_before_post(self):
        with self.assertRaises(GenerationAuthorizationError):
            self.run_generation(token="not-the-active-token")
        self.assertEqual(self.transport.call_count, 0)

    def test_source_without_hosted_rights_refused_before_post(self):
        bad_source = self._put(
            "a-source-bad", "rev-source-bad", {"kind": "article", "text": "x"},
            ops={"hosted_transfer": "not_allowed"},
        )
        with self.assertRaises(GenerationRightsError):
            self.run_generation(source_refs=[bad_source])
        self.assertEqual(self.transport.call_count, 0)

    def test_budget_exceeded_refused_before_post(self):
        hosted = self._hosted("a-hosted-small", "rev-small", budget="0.001")
        self.approve(hosted, hosted_use.hosted_use_gate_id(_ROLE))
        with self.assertRaises(GenerationBudgetError):
            self.run_generation(hosted_ref=hosted)
        self.assertEqual(self.transport.call_count, 0)

    def test_second_call_after_settled_one_refused(self):
        # First call: reserve 0.065536 fits under the cap 0.0656.
        hosted = self._hosted("a-hosted-tight", "rev-tight", budget="0.0656")
        self.approve(hosted, hosted_use.hosted_use_gate_id(_ROLE))
        self.transport.responses.append(_completion(_proposal()))
        first = self.run_generation(request_id="gen-a", hosted_ref=hosted)
        self.assertEqual(first["outcome"], "completed")
        self.assertEqual(self.transport.call_count, 1)
        # Second call: 0.0002 settled + 0.065536 reserve > 0.0656 -> refused.
        with self.assertRaises(GenerationBudgetError):
            self.run_generation(request_id="gen-b", hosted_ref=hosted)
        self.assertEqual(self.transport.call_count, 1)


# --------------------------------------------------------------------------- #
# Ambiguous / malformed outcomes
# --------------------------------------------------------------------------- #
class OutcomeTest(RunnerTestBase):
    def test_http_5xx_unknown_reserve_retained_counted(self):
        self.transport.responses.append(
            TransportResponse(status=503, body_text=json.dumps({"error": {"code": 503}}), truncated=False)
        )
        result = self.run_generation()
        self.assertEqual(result["outcome"], "unknown")
        self.assertEqual(result["cost"]["state"], "unknown")
        self.assertIsNone(result["cost"]["amount_usd"])
        dispatch = RuntimeDispatchRepository(self.store).get_dispatch_by_request("gen-1")
        self.assertEqual(dispatch.state, "unknown")
        self.assertEqual(dispatch.reserve_usd, "0.065536")
        # The retained reserve is counted against the budget.
        self.assertEqual(
            runner._role_exposure(self.store, role=_ROLE, hosted_ref=self.hosted_ref),
            __import__("decimal").Decimal("0.065536"),
        )

    def test_malformed_json_not_completed_proposal_none(self):
        choice = {"finish_reason": "stop", "message": {"role": "assistant", "content": "not json"}}
        payload = {
            "id": "gen-1",
            "model": _MODEL,
            "provider": "deepseek",
            "choices": [choice],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": 0.0001},
        }
        self.transport.responses.append(
            TransportResponse(status=200, body_text=json.dumps(payload), truncated=False)
        )
        result = self.run_generation()
        self.assertEqual(result["outcome"], "protocol_error")
        self.assertIsNone(result["proposal"])
        self.assertEqual(self.transport.call_count, 1)


# --------------------------------------------------------------------------- #
# No secret echo
# --------------------------------------------------------------------------- #
class NoSecretEchoTest(RunnerTestBase):
    def test_key_never_in_errors_or_records(self):
        hosted = self._hosted("a-hosted-tiny", "rev-tiny", budget="0.001")
        self.approve(hosted, hosted_use.hosted_use_gate_id(_ROLE))
        with self.assertRaises(GenerationRunnerError) as ctx:
            self.run_generation(hosted_ref=hosted, api_key=_API_KEY)
        self.assertNotIn(_API_KEY, str(ctx.exception))

    def test_key_never_in_stored_proposal(self):
        self.transport.responses.append(_completion(_proposal()))
        result = self.run_generation()
        proposal = RuntimeRepository(self.store).get_proposal(result["proposal_ref"]["record_id"])
        self.assertNotIn(_API_KEY.encode("utf-8"), bytes(proposal.canonical))


# --------------------------------------------------------------------------- #
# Vocabulary coverage
# --------------------------------------------------------------------------- #
class VocabularyTest(unittest.TestCase):
    def test_every_generation_role_is_a_scientific_role(self):
        from review_article_agent.runtime import roles

        self.assertTrue(set(generation.ROLES) <= set(roles.SCIENTIFIC_ROLES))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
