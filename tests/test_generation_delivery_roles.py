"""Fast-tier tests for the real global reviewer / global writer / notes planner.

Covers ``src/review_article_agent/generation/delivery_roles.py`` (kickoff 16 §5):
a full ``DeliveryRepository.run_global_review`` driven by the real ports over the
single authorized/budgeted/ledgered generation runner, and ``plan_note_edits``
proposing (never applying) an author-note edit batch. Real temp stores, a real
registry activation token, real owner-authenticated hosted-use gates, and real
rights records are used; **no network call** is made (a fake transport is
injected).

Run::

    PYTHONPATH=src:tests python3 -m unittest tests.test_generation_delivery_roles
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

from review_article_agent.delivery.schema import (  # noqa: E402
    GLOBAL_ROUND_LIMIT,
)
from review_article_agent.delivery.workflow import (  # noqa: E402
    DeliveryRepository,
)
from review_article_agent.generation import hosted_use  # noqa: E402
from review_article_agent.generation.delivery_roles import (  # noqa: E402
    make_global_review_ports,
    plan_note_edits,
)
from review_article_agent.generation.hosted_use import (  # noqa: E402
    GenerationRoleModelError,
    propose_hosted_use,
)
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
_REVIEWER_MODEL = "deepseek/deepseek-v4.1-flash"
_WRITER_MODEL = "anthropic/claude-sonnet-5-5"
_PLANNER_MODEL = "openai/gpt-5-mini"
_EVIDENCE = {"ref": "evidence:synthetic", "verified_at": "2026-10-28T00:00:00Z"}
_FIXED_CHALLENGE = "TEST-CHAL-0001"
_OWNER_UID = 1000
_BASE_TIME = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
_API_KEY = "sk-or-v1-delivery-roles-canary-secret"

SECTIONS_OK = [
    {"section_id": "s1", "review_status": "complete", "origin_complete": True}
]
ORIGIN_OK = {"trace_id": "trace-1", "claims": []}


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


def _finding(paragraph_id="p2", *, severity="major", status="open"):
    return {
        "finding_id": "f1",
        "paragraph_id": paragraph_id,
        "kind": "inconsistency",
        "severity": severity,
        "status": status,
        "objection": "The assembled manuscript is internally inconsistent here.",
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


class _RoleTransport:
    """A fake transport whose response is derived from the request material."""

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


# --------------------------------------------------------------------------- #
# Base
# --------------------------------------------------------------------------- #
class DeliveryRolesBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-delivery-roles-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.registry = Registry(str(self.tmp / "app"))
        info = self.registry.create_project(str(self.tmp / "proj"))
        self.addCleanup(self._close_store)
        self.app_dir = paths.app_dir_for(info.root_path)
        self.store = ProjectStore.open_at(self.app_dir)
        self.project_id = self.store.project_id
        self.artifacts = ArtifactRepository(self.store)
        self.repo = DeliveryRepository(self.store)
        self._patches = []
        self.addCleanup(self._stop_patches)
        self._install_owner_auth()
        self.token = self.registry.activate_execution(self.project_id).activation_token

        self.source_ref = self._put("a-source", "rev-source", {"kind": "article"})
        self.reviewer_ref = self._hosted(
            "a-reviewer", "rev-reviewer", role="global_reviewer",
            model=_REVIEWER_MODEL, provider="deepseek",
        )
        self.writer_ref = self._hosted(
            "a-writer", "rev-writer", role="global_writer",
            model=_WRITER_MODEL, provider="anthropic",
        )
        self.planner_ref = self._hosted(
            "a-planner", "rev-planner", role="notes_planner",
            model=_PLANNER_MODEL, provider="openai",
        )
        for ref, role in (
            (self.reviewer_ref, "global_reviewer"),
            (self.writer_ref, "global_writer"),
            (self.planner_ref, "notes_planner"),
        ):
            self.approve(ref, hosted_use.hosted_use_gate_id(role))

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
            rationale="synthetic delivery-roles gate",
        )
        capability = owner_auth.request_owner_confirmation(prepared.owner_action())
        assert isinstance(capability, OwnerApproval)
        HumanApprovalRepository(self.store).record_decision(prepared, capability)

    def rights(self, artifact_id, revision_id):
        return _rights(self.project_id, artifact_id, revision_id)

    def ports(self, transport, delivery_id="delivery-1"):
        return make_global_review_ports(
            self.store,
            registry=self.registry,
            activation_token=self.token,
            delivery_id=delivery_id,
            reviewer_hosted_ref=self.reviewer_ref,
            writer_hosted_ref=self.writer_ref,
            api_key=_API_KEY,
            transport=transport,
            source_refs=[self.source_ref],
        )

    def start(self, delivery_id="delivery-1"):
        return self.repo.start_global_review(
            delivery_id, SECTIONS_OK, ORIGIN_OK, rights=self.rights
        )

    def run_review(self, transport, manuscript, evidence=None, delivery_id="delivery-1"):
        reviewer, writer = self.ports(transport, delivery_id)
        return self.repo.run_global_review(
            delivery_id,
            manuscript,
            evidence or [],
            reviewer,
            writer,
            rights=self.rights,
        )


def _two_paragraph_manuscript():
    text = "Intro [@k1].\n\nBody [@k1]. PROBLEM detail."
    return {
        "revision_id": "m1",
        "text": text,
        "paragraphs": {
            "p1": {"text": "Intro [@k1].", "checks": "valid"},
            "p2": {"text": "Body [@k1]. PROBLEM detail.", "checks": "valid"},
        },
    }


# --------------------------------------------------------------------------- #
# Clean reviewer
# --------------------------------------------------------------------------- #
class CleanReviewerTests(DeliveryRolesBase):
    def test_clean_reviewer_completes_with_one_post(self):
        self.start()
        transport = _RoleTransport(lambda payload: {"findings": []})
        state = self.run_review(transport, _two_paragraph_manuscript())

        self.assertEqual(state["global_review"]["status"], "complete")
        self.assertEqual(state["global_review"]["reviewer_calls"], 1)
        self.assertEqual(transport.call_count, 1)
        # The reviewer material is exactly the manuscript + its paragraphs.
        material = _material_of(transport.calls[0])
        self.assertEqual(set(material), {"manuscript", "paragraphs"})


# --------------------------------------------------------------------------- #
# Writer application
# --------------------------------------------------------------------------- #
class WriterApplicationTests(DeliveryRolesBase):
    def test_writer_revises_only_targeted_paragraph(self):
        self.start()

        def responder(payload):
            material = _material_of(payload)
            if "findings" in material:
                # Global writer: replace only the targeted paragraph's text.
                text = material["manuscript"].replace("PROBLEM", "RESOLVED")
                return {"paragraphs": [{"paragraph_id": "p2", "text": text.split("\n\n")[1]}]}
            if "PROBLEM" in material["manuscript"]:
                return {"findings": [_finding("p2")]}
            return {"findings": []}

        transport = _RoleTransport(responder)
        state = self.run_review(transport, _two_paragraph_manuscript())

        gr = state["global_review"]
        self.assertEqual(gr["status"], "complete")
        self.assertEqual(gr["rounds_used"], 1)
        self.assertEqual(gr["reviewer_calls"], 2)
        final = state["manuscript"]["text"]
        self.assertIn("Intro [@k1].", final)
        self.assertIn("Body [@k1]. RESOLVED detail.", final)
        self.assertNotIn("PROBLEM", final)

    def test_writer_adding_new_citation_key_is_rejected(self):
        self.start()

        def responder(payload):
            material = _material_of(payload)
            if "findings" in material:
                # The replacement smuggles in a citation key absent from the
                # manuscript; the port must reject it and keep the old text.
                return {
                    "paragraphs": [
                        {"paragraph_id": "p2", "text": "Body [@k1; @k9]. RESOLVED detail."}
                    ]
                }
            if "PROBLEM" in material["manuscript"]:
                return {"findings": [_finding("p2")]}
            return {"findings": []}

        transport = _RoleTransport(responder)
        state = self.run_review(transport, _two_paragraph_manuscript())

        gr = state["global_review"]
        self.assertEqual(gr["status"], "blocked")
        self.assertEqual(gr["rounds_used"], GLOBAL_ROUND_LIMIT)
        final = state["manuscript"]["text"]
        self.assertIn("PROBLEM", final)
        self.assertNotIn("k9", final)


# --------------------------------------------------------------------------- #
# Model identity
# --------------------------------------------------------------------------- #
class ModelIdentityTests(DeliveryRolesBase):
    def test_same_model_reviewer_and_writer_refused(self):
        same_writer = self._hosted(
            "a-writer-same", "rev-writer-same", role="global_writer",
            model=_REVIEWER_MODEL, provider="deepseek",
        )
        with self.assertRaises(GenerationRoleModelError):
            make_global_review_ports(
                self.store,
                registry=self.registry,
                activation_token=self.token,
                delivery_id="delivery-same",
                reviewer_hosted_ref=self.reviewer_ref,
                writer_hosted_ref=same_writer,
                api_key=_API_KEY,
                transport=_RoleTransport(lambda payload: {"findings": []}),
                source_refs=[self.source_ref],
            )


# --------------------------------------------------------------------------- #
# Resume / replay
# --------------------------------------------------------------------------- #
class ResumeTests(DeliveryRolesBase):
    def test_resume_replays_without_extra_posts(self):
        self.start()
        manuscript = _two_paragraph_manuscript()

        first_transport = _RoleTransport(lambda payload: {"findings": []})
        reviewer, writer = self.ports(first_transport)

        def crashing_reviewer(text):
            # The model call commits its dispatch, then the worker "crashes"
            # before the review state is persisted.
            reviewer(text)
            raise RuntimeError("simulated crash after the reviewer call")

        with self.assertRaises(RuntimeError):
            self.repo.run_global_review(
                "delivery-1", manuscript, [], crashing_reviewer, writer,
                rights=self.rights,
            )
        self.assertEqual(first_transport.call_count, 1)

        def fail_responder(payload):  # pragma: no cover - must never run
            raise AssertionError("a resumed review must not issue a new POST")

        resume_transport = _RoleTransport(fail_responder)
        state = self.run_review(resume_transport, manuscript)

        self.assertEqual(resume_transport.call_count, 0)
        self.assertEqual(state["global_review"]["status"], "complete")
        self.assertEqual(state["global_review"]["reviewer_calls"], 1)


# --------------------------------------------------------------------------- #
# Bounded rounds
# --------------------------------------------------------------------------- #
class BoundedRoundsTests(DeliveryRolesBase):
    def test_global_rounds_are_bounded(self):
        self.start()

        def responder(payload):
            material = _material_of(payload)
            if "findings" in material:
                return {"paragraphs": []}
            return {"findings": [_finding("p2")]}

        transport = _RoleTransport(responder)
        state = self.run_review(transport, _two_paragraph_manuscript())

        gr = state["global_review"]
        self.assertEqual(gr["rounds_used"], GLOBAL_ROUND_LIMIT)
        self.assertEqual(gr["reviewer_calls"], GLOBAL_ROUND_LIMIT + 1)
        self.assertEqual(gr["status"], "blocked")
        self.assertIsNotNone(gr["required_human_action"])


# --------------------------------------------------------------------------- #
# Notes planner
# --------------------------------------------------------------------------- #
class NotesPlannerTests(DeliveryRolesBase):
    def _reviewed_delivery(self):
        self.start()
        transport = _RoleTransport(lambda payload: {"findings": []})
        self.run_review(transport, _two_paragraph_manuscript())
        self.repo.notes(
            "delivery-1",
            [
                {"note_id": "n1", "text": "tighten intro", "category": "wording"},
                {
                    "note_id": "n2",
                    "text": "adjust claim",
                    "category": "claim",
                    "target": {"paragraph_id": "p2"},
                },
                {"note_id": "n3", "text": "add a section", "category": "scope"},
            ],
            rights=self.rights,
        )

    def _plan(self, responder):
        transport = _RoleTransport(responder)
        result = plan_note_edits(
            self.store,
            registry=self.registry,
            activation_token=self.token,
            delivery_id="delivery-1",
            hosted_ref=self.planner_ref,
            api_key=_API_KEY,
            transport=transport,
            source_refs=[self.source_ref],
        )
        return result, transport

    def test_proposes_valid_edits_and_drops_invalid_ones(self):
        self._reviewed_delivery()

        def responder(payload):
            return {
                "edits": [
                    # valid wording edit that keeps the existing citation
                    {
                        "note_id": "n1",
                        "paragraph_id": "p1",
                        "new_text": "Intro [@k1] tightened.",
                        "kind": "wording",
                    },
                    # unknown note
                    {"note_id": "nX", "paragraph_id": "p1", "new_text": "x", "kind": "wording"},
                    # unknown paragraph
                    {"note_id": "n1", "paragraph_id": "pX", "new_text": "x", "kind": "wording"},
                    # bad kind
                    {"note_id": "n1", "paragraph_id": "p1", "new_text": "x", "kind": "weird"},
                    # new citation key
                    {
                        "note_id": "n1",
                        "paragraph_id": "p1",
                        "new_text": "Intro [@k1; @k9].",
                        "kind": "wording",
                    },
                    # note whose plan action is not editable (scope)
                    {"note_id": "n3", "paragraph_id": "p1", "new_text": "x", "kind": "wording"},
                ]
            }

        result, transport = self._plan(responder)

        self.assertEqual(result["status"], "proposed")
        self.assertEqual(len(result["edits"]), 1)
        self.assertEqual(result["edits"][0]["note_id"], "n1")
        self.assertEqual(
            sorted(item["reason"] for item in result["dropped"]),
            [
                "bad_kind",
                "new_citation_key",
                "note_not_editable",
                "unknown_note",
                "unknown_paragraph",
            ],
        )
        # The material carries the recorded notes and the current manuscript.
        material = _material_of(transport.calls[0])
        self.assertEqual(set(material), {"manuscript", "paragraphs", "notes"})
        self.assertEqual(
            sorted(note["note_id"] for note in material["notes"]), ["n1", "n2", "n3"]
        )

    def test_never_applies_the_proposed_batch(self):
        self._reviewed_delivery()

        def responder(payload):
            return {
                "edits": [
                    {
                        "note_id": "n1",
                        "paragraph_id": "p1",
                        "new_text": "Intro [@k1] tightened.",
                        "kind": "wording",
                    }
                ]
            }

        result, _ = self._plan(responder)
        self.assertEqual(result["status"], "proposed")
        self.assertEqual(len(result["edits"]), 1)

        # Nothing was applied: the delivery still has no applied batch and the
        # paragraph text is unchanged.
        state = self.repo.read("delivery-1")
        self.assertEqual(state["applied_batches"], [])
        self.assertEqual(state["paragraphs"]["p1"]["text"], "Intro [@k1].")


if __name__ == "__main__":
    unittest.main()
