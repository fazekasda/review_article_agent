"""Fast-tier tests for the real section-critic / section-repair model ports.

Covers ``src/review_article_agent/generation/review_roles.py`` (kickoff 16 §5):
a full ``SectionReviewRepository.run`` driven by the real ports over the single
authorized/budgeted/ledgered generation runner, with a fake transport. Real temp
stores, a real registry activation token, real owner-authenticated hosted-use
gates, and real rights records are used; **no network call** is made.

Run:

    PYTHONPATH=src:tests python3 -m unittest tests.test_generation_review_roles
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from review_article_agent.drafting.schema import (  # noqa: E402
    draft_example,
    drafting_example_package,
)
from review_article_agent.generation import hosted_use, review_roles  # noqa: E402
from review_article_agent.generation.hosted_use import (  # noqa: E402
    GenerationRoleModelError,
    propose_hosted_use,
)
from review_article_agent.generation.review_roles import (  # noqa: E402
    make_review_ports,
)
from review_article_agent.review.schema import ROUND_LIMITS  # noqa: E402
from review_article_agent.review.workflow import SectionReviewRepository  # noqa: E402
from review_article_agent.runtime import owner_auth  # noqa: E402
from review_article_agent.runtime.gates import prepare_gate_snapshot  # noqa: E402
from review_article_agent.runtime.lead import LeadConfig, TransportResponse  # noqa: E402
from review_article_agent.runtime.owner_auth import OwnerApproval  # noqa: E402
from review_article_agent.storage import (  # noqa: E402
    ArtifactRepository,
    ProjectStore,
    Registry,
    paths,
)
from review_article_agent.storage.human_approvals import (  # noqa: E402
    HumanApprovalRepository,
)

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")
_WRITER_MODEL = "anthropic/claude-sonnet-5-5"
_CRITIC_MODEL = "deepseek/deepseek-v4.1-flash"
_EVIDENCE = {"ref": "evidence:synthetic", "verified_at": "2026-10-28T00:00:00Z"}
_FIXED_CHALLENGE = "TEST-CHAL-0001"
_OWNER_UID = 1000
_BASE_TIME = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
_API_KEY = "sk-or-v1-review-canary-secret"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _rights(project_id, artifact_id, revision_id, *, ops=None):
    operations = {op: "allowed" for op in _OPS}
    if ops:
        operations.update(ops)
    basis = {}
    for op in _OPS:
        if operations[op] == "allowed":
            basis[op] = {
                "kind": "license",
                "evidence": {"ref": "lic:syn", "provenance": "publisher:syn"},
            }
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
            "basis": {
                "kind": "license",
                "evidence": {"ref": "lic:syn", "provenance": "publisher:syn"},
            },
        },
        "restrictions": [],
        "privacy": {"personal_data": False, "confidentiality": "public", "denial": False},
        "recorded_by": "user",
        "recorded_at": "2026-10-28T00:00:00Z",
        "derived_status": "valid",
    }


def _model_map(model, provider):
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


def _document(role, *, model, provider="deepseek", budget="10.00"):
    return {
        "schema_version": 1,
        "document_type": "generation_hosted_use",
        "payload": {
            "role": role,
            "config": _model_map(model, provider),
            "budget_cap_usd": budget,
            "provider_terms": dict(_EVIDENCE),
            "privacy_review": dict(_EVIDENCE),
            "pricing_evidence": dict(_EVIDENCE),
            "notes": None,
        },
    }


def _sentence(draft, paragraph_id, intent_id):
    for paragraph in draft["paragraphs"]:
        if paragraph["paragraph_id"] != paragraph_id:
            continue
        for sentence in paragraph["sentences"]:
            if sentence["intent_id"] == intent_id:
                return sentence
    raise AssertionError("sentence not found")


def _clean_draft():
    package = drafting_example_package()
    draft = draft_example(package=package)
    _sentence(draft, "P1", "I1")["citation_keys"] = ["CIT-A10"]
    secondary = _sentence(draft, "P2", "I2")
    secondary["citation_keys"] = ["CIT-C3"]
    secondary["secondary"] = True
    _sentence(draft, "P2", "I3")["proposed_synthesis"] = True
    return draft, package


def _major(sentence=("P1", "I1"), finding_key="A10:F1",
           quote="tumor volume was reduced in the treated cohort",
           objection="The claim is not supported by the cited source."):
    # The model output shape: the envelope forbids the key "target", so the
    # critic contract names the sentence "sentence"; the port maps it back.
    return {
        "sentence": {"paragraph_id": sentence[0], "intent_id": sentence[1]},
        "kind": "unsupported_claim",
        "severity": "major",
        "objection": objection,
        "basis": {"finding_key": finding_key, "quote": quote},
    }


# --------------------------------------------------------------------------- #
# Fake transport
# --------------------------------------------------------------------------- #
def _completion(content_obj, *, model, cost=0.0002):
    payload = {
        "id": "gen-1",
        "model": model,
        "provider": "syn",
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(content_obj)},
            }
        ],
        "usage": {
            "prompt_tokens": 12,
            "completion_tokens": 8,
            "total_tokens": 20,
            "cost": cost,
        },
    }
    return TransportResponse(status=200, body_text=json.dumps(payload), truncated=False)


def _material_of(payload):
    return json.loads(payload["messages"][1]["content"])


class _ReviewTransport:
    """A fake transport whose response is derived from the request material.

    ``responder(payload) -> content object``; the completion echoes the requested
    model so the runner's model check passes. ``calls`` counts POSTs.
    """

    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    def __call__(self, body, api_key, *, endpoint, timeout, max_response_bytes):
        payload = json.loads(body)
        self.calls.append(payload)
        return _completion(self.responder(payload), model=payload["model"])

    @property
    def call_count(self):
        return len(self.calls)


def _critic_then_repair(finding, texts):
    """A responder: critic returns ``[finding]``; repair rewrites each target."""

    def responder(payload):
        material = _material_of(payload)
        if "findings" in material:
            sentences = []
            responses = {}
            for item in material["findings"]:
                target = item["target"]
                key = (target["paragraph_id"], target["intent_id"])
                if key in texts:
                    sentences.append(
                        {
                            "paragraph_id": key[0],
                            "intent_id": key[1],
                            "text": texts[key],
                        }
                    )
                responses[item["finding_id"]] = {"accept": True, "reason": "rewritten"}
            return {"sentences": sentences, "responses": responses}
        return {"findings": [copy.deepcopy(finding)]}

    return responder


# --------------------------------------------------------------------------- #
# Base
# --------------------------------------------------------------------------- #
class ReviewRolesBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-review-roles-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.registry = Registry(str(self.tmp / "app"))
        info = self.registry.create_project(str(self.tmp / "proj"))
        self.addCleanup(self._close_store)
        self.app_dir = paths.app_dir_for(info.root_path)
        self.store = ProjectStore.open_at(self.app_dir)
        self.project_id = self.store.project_id
        self.artifacts = ArtifactRepository(self.store)
        self.repo = SectionReviewRepository(self.store)
        self._patches = []
        self.addCleanup(self._stop_patches)
        self._install_owner_auth()
        self.token = self.registry.activate_execution(self.project_id).activation_token

        self.source_ref = self._put("a-source", "rev-source", {"kind": "article"})
        self.writer_ref = self._hosted(
            "a-writer", "rev-writer", role="section_repair", model=_WRITER_MODEL,
            provider="anthropic",
        )
        self.critic_ref = self._hosted(
            "a-critic", "rev-critic", role="section_critic", model=_CRITIC_MODEL,
            provider="deepseek",
        )
        self.approve(self.writer_ref, hosted_use.hosted_use_gate_id("section_repair"))
        self.approve(self.critic_ref, hosted_use.hosted_use_gate_id("section_critic"))

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

    def _put(self, artifact_id, revision_id, content, *, kind="report"):
        if not isinstance(content, (bytes, bytearray)):
            content = json.dumps(content).encode("utf-8")
        self.artifacts.create_artifact(artifact_id, kind=kind, created_by="import")
        self.artifacts.import_revision(
            artifact_id=artifact_id,
            revision_id=revision_id,
            content=bytes(content),
            origin_locator="import:synthetic",
            rights=_rights(self.project_id, artifact_id, revision_id),
            acquired_by="import",
        )
        return {
            "project_id": self.project_id,
            "artifact_id": artifact_id,
            "revision_id": revision_id,
        }

    def _hosted(self, artifact_id, revision_id, *, role, model, provider):
        return propose_hosted_use(
            self.store,
            _document(role, model=model, provider=provider),
            artifact_id=artifact_id,
            revision_id=revision_id,
            rights=_rights(self.project_id, artifact_id, revision_id),
        )

    def approve(self, ref, gate_id, *, gate_kind="batch"):
        prepared = prepare_gate_snapshot(
            self.store,
            ref,
            gate_kind=gate_kind,
            gate_id=gate_id,
            decision="approved",
            rationale="synthetic review gate",
        )
        capability = owner_auth.request_owner_confirmation(prepared.owner_action())
        assert isinstance(capability, OwnerApproval)
        HumanApprovalRepository(self.store).record_decision(prepared, capability)

    def rights(self, artifact_id, revision_id):
        return _rights(self.project_id, artifact_id, revision_id)

    def ports(self, review_id, transport):
        return make_review_ports(
            self.store,
            registry=self.registry,
            activation_token=self.token,
            review_id=review_id,
            critic_hosted_ref=self.critic_ref,
            writer_hosted_ref=self.writer_ref,
            api_key=_API_KEY,
            transport=transport,
            source_refs=[self.source_ref],
        )

    def run_review(self, review_id, draft, package, transport):
        critic, writer, critic_model, writer_model = self.ports(review_id, transport)
        return self.repo.run(
            review_id,
            draft=draft,
            package=package,
            writer_model=writer_model,
            critic_model=critic_model,
            critic=critic,
            writer=writer,
            rights=self.rights,
        )


# --------------------------------------------------------------------------- #
# Clean critic
# --------------------------------------------------------------------------- #
class CleanCriticTests(ReviewRolesBase):
    def test_clean_critic_completes_with_one_post(self):
        draft, package = _clean_draft()
        transport = _ReviewTransport(lambda payload: {"findings": []})
        state = self.run_review("review-clean", draft, package, transport)

        self.assertEqual(state["status"], "complete")
        self.assertEqual(state["critic_calls"], 1)
        self.assertEqual(state["repair_calls"], 0)
        self.assertEqual(transport.call_count, 1)
        self.assertFalse(state["engine"]["independent_human_verification"])


# --------------------------------------------------------------------------- #
# Repair application
# --------------------------------------------------------------------------- #
class RepairApplicationTests(ReviewRolesBase):
    def test_repair_applied_to_targeted_sentence_only(self):
        draft, package = _clean_draft()
        new_text = "Compound Z reduced tumor volume in a treated mouse model."
        transport = _ReviewTransport(_critic_then_repair(_major(), {("P1", "I1"): new_text}))
        state = self.run_review("review-repair", draft, package, transport)

        self.assertEqual(state["status"], "complete")
        self.assertEqual(state["repair_calls"], 1)
        self.assertEqual(
            _sentence(state["draft"], "P1", "I1")["text"], new_text
        )
        # The untargeted sentences are byte-identical to the input draft.
        self.assertEqual(
            _sentence(state["draft"], "P2", "I2")["text"],
            _sentence(draft, "P2", "I2")["text"],
        )
        fixed = [f for f in state["findings"] if f["disposition"] == "fixed"]
        self.assertEqual(len(fixed), 1)

    def test_untargeted_edit_and_citation_change_are_ignored(self):
        draft, package = _clean_draft()
        good = "Compound Z reduced tumor volume in a treated mouse model."
        sneaky = "An entirely different untargeted sentence."

        def responder(payload):
            material = _material_of(payload)
            if "findings" in material:
                item = material["findings"][0]
                target = item["target"]
                return {
                    "sentences": [
                        # A valid edit to the targeted sentence, with a bogus
                        # citation_keys field the port must ignore.
                        {
                            "paragraph_id": target["paragraph_id"],
                            "intent_id": target["intent_id"],
                            "text": good,
                            "citation_keys": ["CIT-EVIL"],
                        },
                        # An edit to an untargeted sentence the port must ignore.
                        {"paragraph_id": "P2", "intent_id": "I2", "text": sneaky},
                    ],
                    "responses": {item["finding_id"]: {"accept": True, "reason": "ok"}},
                }
            return {"findings": [_major()]}

        transport = _ReviewTransport(responder)
        state = self.run_review("review-untargeted", draft, package, transport)

        self.assertEqual(state["status"], "complete")
        self.assertEqual(_sentence(state["draft"], "P1", "I1")["text"], good)
        self.assertEqual(
            _sentence(state["draft"], "P1", "I1")["citation_keys"], ["CIT-A10"]
        )
        self.assertEqual(
            _sentence(state["draft"], "P2", "I2")["text"],
            _sentence(draft, "P2", "I2")["text"],
        )


# --------------------------------------------------------------------------- #
# Model identity
# --------------------------------------------------------------------------- #
class ModelIdentityTests(ReviewRolesBase):
    def test_same_model_critic_and_writer_refused(self):
        same_repair = self._hosted(
            "a-repair-same", "rev-repair-same", role="section_repair",
            model=_CRITIC_MODEL, provider="deepseek",
        )
        with self.assertRaises(GenerationRoleModelError):
            make_review_ports(
                self.store,
                registry=self.registry,
                activation_token=self.token,
                review_id="review-same",
                critic_hosted_ref=self.critic_ref,
                writer_hosted_ref=same_repair,
                api_key=_API_KEY,
                transport=_ReviewTransport(lambda payload: {"findings": []}),
                source_refs=[self.source_ref],
            )


# --------------------------------------------------------------------------- #
# Separate context
# --------------------------------------------------------------------------- #
class SeparateContextTests(ReviewRolesBase):
    def test_critic_material_excludes_writer_history(self):
        draft, package = _clean_draft()
        new_text = "Compound Z reduced tumor volume in a treated mouse model."
        transport = _ReviewTransport(_critic_then_repair(_major(), {("P1", "I1"): new_text}))
        self.run_review("review-context", draft, package, transport)

        self.assertEqual(transport.call_count, 2)
        critic_material = _material_of(transport.calls[0])
        repair_material = _material_of(transport.calls[1])
        self.assertEqual(set(critic_material), {"draft", "package"})
        self.assertEqual(set(repair_material), {"draft", "findings", "package"})
        # The critic request carries no repair responses or writer disposition.
        critic_text = transport.calls[0]["messages"][1]["content"]
        for forbidden in ("responses", "writer_response"):
            self.assertNotIn(forbidden, critic_text)


# --------------------------------------------------------------------------- #
# Resume / replay
# --------------------------------------------------------------------------- #
class ResumeTests(ReviewRolesBase):
    def test_resume_after_interruption_replays_without_extra_posts(self):
        draft, package = _clean_draft()
        new_text = "Compound Z reduced tumor volume in a treated mouse model."

        first_transport = _ReviewTransport(
            _critic_then_repair(_major(), {("P1", "I1"): new_text})
        )
        critic, writer, critic_model, writer_model = self.ports(
            "review-resume", first_transport
        )

        def crashing_writer(current_draft, findings):
            # The model call commits its dispatch, then the worker "crashes"
            # before the review state is persisted.
            writer(current_draft, findings)
            raise RuntimeError("simulated crash after the model call")

        with self.assertRaises(RuntimeError):
            self.repo.run(
                "review-resume",
                draft=draft,
                package=package,
                writer_model=writer_model,
                critic_model=critic_model,
                critic=critic,
                writer=crashing_writer,
                rights=self.rights,
            )

        # One critic POST + one repair POST, then the crash.
        self.assertEqual(first_transport.call_count, 2)
        mid = self.repo.read("review-resume")
        self.assertEqual(mid["phase"], "writer")
        self.assertEqual(mid["repair_calls"], 0)

        def fail_responder(payload):  # pragma: no cover - must never run
            raise AssertionError("a resumed review must not issue a new POST")

        resume_transport = _ReviewTransport(fail_responder)
        state = self.run_review("review-resume", draft, package, resume_transport)

        self.assertEqual(resume_transport.call_count, 0)
        self.assertEqual(state["status"], "complete")
        self.assertEqual(state["repair_calls"], 1)
        self.assertEqual(_sentence(state["draft"], "P1", "I1")["text"], new_text)


# --------------------------------------------------------------------------- #
# Bounded rounds
# --------------------------------------------------------------------------- #
class BoundedRoundsTests(ReviewRolesBase):
    def test_critique_rounds_are_bounded_and_never_auto_pass(self):
        draft, package = _clean_draft()
        _sentence(draft, "P1", "I1")["text"] = (
            "Compound Z may reduce tumor volume in a mouse model."
        )
        # The repair drops the qualifier "may" on every round -> rejected, so the
        # finding stays confirmed and the critique budget is consumed.
        transport = _ReviewTransport(
            _critic_then_repair(
                _major(), {("P1", "I1"): "Compound Z reduced tumor volume in a mouse model."}
            )
        )
        state = self.run_review("review-bounded", draft, package, transport)

        self.assertEqual(state["rounds_used"]["critique"], ROUND_LIMITS["critique"])
        self.assertEqual(state["critic_calls"], ROUND_LIMITS["critique"])
        self.assertEqual(state["status"], "blocked")
        self.assertIsNotNone(state["required_human_action"])
        self.assertTrue(state["violations"])
        self.assertIn("qualifier_removed", state["violations"][0]["violations"])
        # round 0 critic + 3 distinct repair rounds (later critic calls replay).
        self.assertEqual(transport.call_count, 4)


if __name__ == "__main__":
    unittest.main()
