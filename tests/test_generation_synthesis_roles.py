"""Fast-tier tests for the model-backed synthesis/framework/outline proposers.

Covers ``src/review_article_agent/generation/synthesis_roles.py`` (kickoff 16 §5):
the versioned prompt templates, the durable material builders, and the
``propose_<kind>`` functions that run one model call through the tested
generation runner and store the schema-validated result as an **inert** proposal.

A fake transport is always injected, so no test performs a network call. The
fixture reuses the synthetic accepted Step 09 evidence chain from
:mod:`test_synthesis_workflow` (real extraction/synthesis workflow gates) and the
fake transport / hosted-use helpers from :mod:`test_generation_runner`, and adds
a real registry activation token over the same store.

Run::

    PYTHONPATH=src:tests python3 -m unittest test_generation_synthesis_roles
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_generation_runner as gen_runner  # noqa: E402
from test_synthesis_workflow import SynthesisBase  # noqa: E402

from review_article_agent.generation import hosted_use  # noqa: E402
from review_article_agent.generation.synthesis_roles import (  # noqa: E402
    INSTRUCTIONS_FRAMEWORK,
    INSTRUCTIONS_OUTLINE,
    INSTRUCTIONS_SYNTHESIS,
    PROMPT_VERSION_FRAMEWORK,
    PROMPT_VERSION_OUTLINE,
    PROMPT_VERSION_SYNTHESIS,
    propose_framework,
    propose_outline,
    propose_synthesis,
)
from review_article_agent.runtime.lead import TransportResponse  # noqa: E402
from review_article_agent.storage import Registry  # noqa: E402
from review_article_agent.synthesis.workflow import (  # noqa: E402
    GATE_FRAMEWORK,
    GATE_FRAMEWORK_KIND,
    GATE_SYNTHESIS,
    GATE_SYNTHESIS_KIND,
    SynthesisWorkflowError,
)

_ROLES = ("synthesis", "framework", "outline")
_API_KEY = gen_runner._API_KEY


class RoleProposerBase(SynthesisBase):
    def setUp(self):
        super().setUp()
        self.registry = Registry(str(self.tmp / "reg"))
        self.registry.register_restored(str(self.tmp / "project"))
        self.token = self.registry.activate_execution(self.project_id).activation_token
        self.transport = gen_runner._FakeTransport()
        self.hosted = {}
        for role in _ROLES:
            artifact_id = f"hosted-{role}"
            ref = hosted_use.propose_hosted_use(
                self.store,
                gen_runner._document(role=role, model=gen_runner._MODEL, budget="10.00"),
                artifact_id=artifact_id,
                revision_id="r1",
                rights=self.rights(artifact_id, "r1"),
            )
            self.approve(artifact_id, "r1", "batch", hosted_use.hosted_use_gate_id(role))
            self.hosted[role] = ref

    def rights(self, artifact_id, revision_id, **overrides):
        # The extraction/synthesis fixture's rights carry no authorization binding;
        # the generation runner's hosted-transfer check requires one, so bind it.
        record = super().rights(artifact_id, revision_id, **overrides)
        record["authorization"] = {
            "actor": "user",
            "method": "out_of_band_authenticated",
            "authorized_at": "2026-10-29T00:00:00Z",
            "against_subject": dict(record["subject"]),
            "against_rights_revision": 1,
            "basis": {
                "kind": "public_domain",
                "evidence": {"ref": "lic:pd-syn", "provenance": "publisher:pd-list"},
            },
        }
        return record

    # -- accepted upstream objects ------------------------------------------- #
    def accepted_synthesis(self):
        audit_ref, _batch = self.accepted_audit()
        ref = self.put_synthesis(audit_ref)
        self.approve_ref(ref, GATE_SYNTHESIS_KIND, GATE_SYNTHESIS)
        return ref

    def accepted_framework(self):
        synthesis_ref = self.accepted_synthesis()
        framework_ref = self.put_framework(synthesis_ref)
        self.approve_ref(framework_ref, GATE_FRAMEWORK_KIND, GATE_FRAMEWORK)
        return synthesis_ref, framework_ref

    @staticmethod
    def _model_framework_content():
        """The framework content as a compliant model emits it (aliased keys)."""

        def alias(value):
            if isinstance(value, dict):
                renamed = {"concept_role" if k == "role" else
                           "relationship_scope" if k == "scope" else k: alias(v)
                           for k, v in value.items()}
                return renamed
            if isinstance(value, list):
                return [alias(item) for item in value]
            return value

        return alias(RoleProposerBase.framework_content())

    # -- runner helpers ------------------------------------------------------ #
    def run_synthesis(self, *, audit_ref, request_id="syn-req", artifact_id="syn-model",
                      revision_id="r1"):
        return propose_synthesis(
            self.store,
            registry=self.registry,
            activation_token=self.token,
            request_id=request_id,
            hosted_ref=self.hosted["synthesis"],
            api_key=_API_KEY,
            transport=self.transport,
            audit_ref=audit_ref,
            artifact_id=artifact_id,
            revision_id=revision_id,
            rights=self.rights(artifact_id, revision_id),
        )

    def run_framework(self, *, synthesis_ref, request_id="fw-req", artifact_id="fw-model",
                      revision_id="r1"):
        return propose_framework(
            self.store,
            registry=self.registry,
            activation_token=self.token,
            request_id=request_id,
            hosted_ref=self.hosted["framework"],
            api_key=_API_KEY,
            transport=self.transport,
            synthesis_ref=synthesis_ref,
            artifact_id=artifact_id,
            revision_id=revision_id,
            rights=self.rights(artifact_id, revision_id),
        )

    def run_outline(self, *, synthesis_ref, framework_ref, request_id="out-req",
                    artifact_id="out-model", revision_id="r1"):
        return propose_outline(
            self.store,
            registry=self.registry,
            activation_token=self.token,
            request_id=request_id,
            hosted_ref=self.hosted["outline"],
            api_key=_API_KEY,
            transport=self.transport,
            synthesis_ref=synthesis_ref,
            framework_ref=framework_ref,
            artifact_id=artifact_id,
            revision_id=revision_id,
            rights=self.rights(artifact_id, revision_id),
        )


class SynthesisProposerTest(RoleProposerBase):
    def test_valid_proposal_stored_readable_not_approved(self):
        audit_ref, _batch = self.accepted_audit()
        self.transport.responses.append(gen_runner._completion(self.synthesis_content()))
        result = self.run_synthesis(audit_ref=audit_ref)
        self.assertEqual(result["status"], "proposed")
        self.assertEqual(self.transport.call_count, 1)
        ref = result["ref"]
        stored = self.synth.read(ref, "synthesis")
        self.assertEqual(stored["result"]["kind"], "synthesis")
        # Not approved: the named gate is still required.
        with self.assertRaises(SynthesisWorkflowError):
            self.synth.require_synthesis(ref)
        body = self.transport.calls[0]["body"].decode("utf-8")
        self.assertIn(PROMPT_VERSION_SYNTHESIS, body)
        self.assertIn(INSTRUCTIONS_SYNTHESIS[:40], body)
        self.assertIn("allowed_finding_keys", body)

    def test_unknown_finding_is_invalid_and_no_write(self):
        audit_ref, _batch = self.accepted_audit()
        content = self.synthesis_content()
        content["statements"][0]["evidence"] = ["c1:F9"]
        self.transport.responses.append(gen_runner._completion(content))
        result = self.run_synthesis(audit_ref=audit_ref, artifact_id="syn-bad")
        self.assertEqual(result["status"], "invalid_proposal")
        self.assertEqual(result["reason"], "unknown_reference")
        self.assertIsNone(self.artifacts.get_revision("syn-bad", "r1"))
        self.assertEqual(self.transport.call_count, 1)

    def test_non_completed_outcome_no_write(self):
        audit_ref, _batch = self.accepted_audit()
        self.transport.responses.append(
            TransportResponse(
                status=503,
                body_text=json.dumps({"error": {"code": 503}}),
                truncated=False,
            )
        )
        result = self.run_synthesis(audit_ref=audit_ref, artifact_id="syn-fail")
        self.assertEqual(result["status"], "model_not_completed")
        self.assertIsNone(self.artifacts.get_revision("syn-fail", "r1"))
        self.assertEqual(self.transport.call_count, 1)

    def test_replay_no_second_post_same_ref(self):
        audit_ref, _batch = self.accepted_audit()
        self.transport.responses.append(gen_runner._completion(self.synthesis_content()))
        first = self.run_synthesis(audit_ref=audit_ref, request_id="syn-replay",
                                   artifact_id="syn-rep")
        second = self.run_synthesis(audit_ref=audit_ref, request_id="syn-replay",
                                    artifact_id="syn-rep")
        self.assertEqual(self.transport.call_count, 1)
        self.assertEqual(second["status"], "proposed")
        self.assertEqual(second["ref"], first["ref"])
        self.assertTrue(second["replayed"])


class FrameworkProposerTest(RoleProposerBase):
    def test_valid_proposal_stored_readable_not_approved(self):
        synthesis_ref = self.accepted_synthesis()
        self.transport.responses.append(
            gen_runner._completion(self._model_framework_content())
        )
        result = self.run_framework(synthesis_ref=synthesis_ref)
        self.assertEqual(result["status"], "proposed")
        self.assertEqual(self.transport.call_count, 1)
        stored = self.synth.read(result["ref"], "framework")
        self.assertEqual(stored["result"]["kind"], "framework")
        with self.assertRaises(SynthesisWorkflowError):
            self.synth.require_framework(result["ref"])
        body = self.transport.calls[0]["body"].decode("utf-8")
        self.assertIn(PROMPT_VERSION_FRAMEWORK, body)
        self.assertIn(INSTRUCTIONS_FRAMEWORK[:40], body)
        self.assertIn("allowed_finding_keys", body)

    def test_unknown_finding_is_invalid_and_no_write(self):
        synthesis_ref = self.accepted_synthesis()
        content = self._model_framework_content()
        content["concepts"][0]["original_support"] = ["c1:F9"]
        self.transport.responses.append(gen_runner._completion(content))
        result = self.run_framework(synthesis_ref=synthesis_ref, artifact_id="fw-bad")
        self.assertEqual(result["status"], "invalid_proposal")
        self.assertEqual(result["reason"], "unknown_reference")
        self.assertIsNone(self.artifacts.get_revision("fw-bad", "r1"))
        self.assertEqual(self.transport.call_count, 1)

    def test_non_completed_outcome_no_write(self):
        synthesis_ref = self.accepted_synthesis()
        self.transport.responses.append(
            TransportResponse(
                status=503,
                body_text=json.dumps({"error": {"code": 503}}),
                truncated=False,
            )
        )
        result = self.run_framework(synthesis_ref=synthesis_ref, artifact_id="fw-fail")
        self.assertEqual(result["status"], "model_not_completed")
        self.assertIsNone(self.artifacts.get_revision("fw-fail", "r1"))
        self.assertEqual(self.transport.call_count, 1)

    def test_literal_reserved_key_is_refused_no_write(self):
        synthesis_ref = self.accepted_synthesis()
        # A non-compliant model emits the literal ``role`` key the runner reserves.
        self.transport.responses.append(gen_runner._completion(self.framework_content()))
        result = self.run_framework(synthesis_ref=synthesis_ref, artifact_id="fw-literal")
        self.assertEqual(result["status"], "invalid_proposal")
        self.assertEqual(result["reason"], "disallowed_content_key")
        self.assertIsNone(self.artifacts.get_revision("fw-literal", "r1"))

    def test_replay_no_second_post_same_ref(self):
        synthesis_ref = self.accepted_synthesis()
        self.transport.responses.append(
            gen_runner._completion(self._model_framework_content())
        )
        first = self.run_framework(synthesis_ref=synthesis_ref, request_id="fw-replay",
                                   artifact_id="fw-rep")
        second = self.run_framework(synthesis_ref=synthesis_ref, request_id="fw-replay",
                                    artifact_id="fw-rep")
        self.assertEqual(self.transport.call_count, 1)
        self.assertEqual(second["status"], "proposed")
        self.assertEqual(second["ref"], first["ref"])
        self.assertTrue(second["replayed"])


class OutlineProposerTest(RoleProposerBase):
    def test_valid_proposal_stored_readable_not_approved(self):
        synthesis_ref, framework_ref = self.accepted_framework()
        self.transport.responses.append(gen_runner._completion(self.outline_content()))
        result = self.run_outline(synthesis_ref=synthesis_ref, framework_ref=framework_ref)
        self.assertEqual(result["status"], "proposed")
        self.assertEqual(self.transport.call_count, 1)
        stored = self.synth.read(result["ref"], "outline")
        self.assertEqual(stored["result"]["kind"], "outline")
        with self.assertRaises(SynthesisWorkflowError):
            self.synth.require_outline(result["ref"])
        body = self.transport.calls[0]["body"].decode("utf-8")
        self.assertIn(PROMPT_VERSION_OUTLINE, body)
        self.assertIn(INSTRUCTIONS_OUTLINE[:40], body)
        self.assertIn("concept_ids", body)

    def test_unknown_concept_is_invalid_and_no_write(self):
        synthesis_ref, framework_ref = self.accepted_framework()
        content = self.outline_content()
        content["sections"][0]["concept_dependencies"] = ["C9"]
        self.transport.responses.append(gen_runner._completion(content))
        result = self.run_outline(synthesis_ref=synthesis_ref, framework_ref=framework_ref,
                                  artifact_id="out-bad")
        self.assertEqual(result["status"], "invalid_proposal")
        self.assertEqual(result["reason"], "unknown_reference")
        self.assertIsNone(self.artifacts.get_revision("out-bad", "r1"))
        self.assertEqual(self.transport.call_count, 1)

    def test_non_completed_outcome_no_write(self):
        synthesis_ref, framework_ref = self.accepted_framework()
        self.transport.responses.append(
            TransportResponse(
                status=503,
                body_text=json.dumps({"error": {"code": 503}}),
                truncated=False,
            )
        )
        result = self.run_outline(synthesis_ref=synthesis_ref, framework_ref=framework_ref,
                                  artifact_id="out-fail")
        self.assertEqual(result["status"], "model_not_completed")
        self.assertIsNone(self.artifacts.get_revision("out-fail", "r1"))
        self.assertEqual(self.transport.call_count, 1)

    def test_replay_no_second_post_same_ref(self):
        synthesis_ref, framework_ref = self.accepted_framework()
        self.transport.responses.append(gen_runner._completion(self.outline_content()))
        first = self.run_outline(synthesis_ref=synthesis_ref, framework_ref=framework_ref,
                                 request_id="out-replay", artifact_id="out-rep")
        second = self.run_outline(synthesis_ref=synthesis_ref, framework_ref=framework_ref,
                                  request_id="out-replay", artifact_id="out-rep")
        self.assertEqual(self.transport.call_count, 1)
        self.assertEqual(second["status"], "proposed")
        self.assertEqual(second["ref"], first["ref"])
        self.assertTrue(second["replayed"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
