"""Model-backed blueprint/section-writer drafting-role tests (kickoff 16 §5).

Covers ``src/review_article_agent/generation/drafting_roles.py``: the ``blueprint``
and ``section_writer`` roles run through the one model port (the direct route
authorized by a per-role ``generation_hosted_use`` document + named gate), with a
versioned prompt, and keep the model output a **proposal** that passes the
existing drafting schema validators and workflow (stored, never approved).  A
workspace may bind the planner/writer to direct-route hosted-use refs (the
additive ``direct_hosted_use`` binding kind) while every existing session-binding
check is retained.

The accepted Step 10/Step 09/Step 11 fixtures and the real owner-capability flow
are inherited from :class:`test_drafting_workflow.BlueprintBase`; a real
application ``Registry`` (adopted over the same throwaway store) provides the
activation token, and the fake transport from :mod:`test_generation_runner`
performs no network call.

**Fixture disclosure:** the planner/writer direct bindings, the hosted-use
documents, and the accepted receipts are *synthetic* app-owned rows recorded
locally; no live OpenCode service/session is created and no model, network, or
provider call is made.

Run::

    PYTHONPATH=src:tests python3 -m unittest test_generation_drafting_roles -v
"""

from __future__ import annotations

import json
import unittest

from test_drafting_workflow import BlueprintBase, blueprint_content  # noqa: E402
from test_generation_runner import (  # noqa: E402
    _API_KEY,
    _MODEL,
    _FakeTransport,
    _completion,
    _document,
    _rights as _authorized_rights,
)

from review_article_agent.drafting.package import DraftingPackageRepository  # noqa: E402
from review_article_agent.drafting.workflow import (  # noqa: E402
    DraftingWorkflowError,
    DraftingWorkflowRepository,
)
from review_article_agent.drafting.workspace import (  # noqa: E402
    DIRECT_BINDING_KIND,
    SectionWorkspaceError,
    SectionWorkspaceRepository,
)
from review_article_agent.generation import hosted_use  # noqa: E402
from review_article_agent.generation import drafting_roles as roles  # noqa: E402
from review_article_agent.runtime.lead import TransportResponse  # noqa: E402
from review_article_agent.storage import Registry  # noqa: E402
from _tiers import slow  # noqa: E402

_BLUEPRINT_ROLE = "blueprint"
_WRITER_ROLE = "section_writer"


def _direct_slot(role, hosted_ref):
    return {"kind": DIRECT_BINDING_KIND, "role": role, "hosted_ref": hosted_ref}


def _draft_content(blueprint):
    """Return a valid draft content for a validated blueprint result."""
    paragraphs = []
    for paragraph in blueprint["paragraphs"]:
        sentences = []
        for intent in paragraph["intents"]:
            claim_ids = [claim["claim_id"] for claim in intent["claims"]]
            sentences.append(
                {
                    "intent_id": intent["intent_id"],
                    "claim_ids": claim_ids,
                    "text": " ".join(claim["text"] for claim in intent["claims"]),
                }
            )
        paragraphs.append(
            {"paragraph_id": paragraph["paragraph_id"], "sentences": sentences}
        )
    return {
        "schema_version": 1,
        "section_id": blueprint["section_id"],
        "paragraphs": paragraphs,
    }


class DirectRolesBase(BlueprintBase):
    def setUp(self):
        super().setUp()
        # Adopt the throwaway store into a real registry and activate execution.
        self.registry = Registry(str(self.tmp / "reg"))
        self.registry.register_restored(
            str(self.tmp / "project"), project_id=self.project_id
        )
        self.token = self.registry.activate_execution(self.project_id).activation_token
        self.transport = _FakeTransport()

        self.blueprint_hosted_ref = self._hosted(
            "hu-blueprint", "hu-blueprint-r1", role=_BLUEPRINT_ROLE
        )
        self.writer_hosted_ref = self._hosted(
            "hu-writer", "hu-writer-r1", role=_WRITER_ROLE
        )
        self.approve_ref(
            self.blueprint_hosted_ref,
            hosted_use.HOSTED_USE_GATE_KIND,
            hosted_use.hosted_use_gate_id(_BLUEPRINT_ROLE),
        )
        self.approve_ref(
            self.writer_hosted_ref,
            hosted_use.HOSTED_USE_GATE_KIND,
            hosted_use.hosted_use_gate_id(_WRITER_ROLE),
        )

    def rights(self, artifact_id, revision_id, **overrides):
        # The package's original sources are transferred to the provider, so the
        # fixture's rights records carry an authorization binding (the drafting
        # chain otherwise uses authorization-less records).
        return _authorized_rights(
            self.project_id, artifact_id, revision_id, **overrides
        )

    def _hosted(self, artifact_id, revision_id, *, role):
        return hosted_use.propose_hosted_use(
            self.store,
            _document(role),
            artifact_id=artifact_id,
            revision_id=revision_id,
            rights=self.rights(artifact_id, revision_id),
        )

    # -- direct-bound workspace --------------------------------------------- #
    def make_direct_workspace(
        self, outline_ref, *, section_id="sec1", artifact_id="ws", revision="ws-r1",
        planner=None, writer=None, parent=None,
    ):
        payload = {
            "outline_ref": outline_ref,
            "section_id": section_id,
            "planner": planner
            or _direct_slot(_BLUEPRINT_ROLE, self.blueprint_hosted_ref),
            "writer": writer
            or _direct_slot(_WRITER_ROLE, self.writer_hosted_ref),
        }
        return self.workspace.propose(
            payload,
            artifact_id=artifact_id,
            revision_id=revision,
            rights=self.rights(artifact_id, revision),
            parent_revision_id=parent,
        )

    # -- model-driven proposals --------------------------------------------- #
    def propose_blueprint_model(
        self, workspace_ref, *, request_id="gen-bp-1", artifact_id="bp",
        revision="bp-r1", content=None, transport=None,
    ):
        if content is None:
            ws = self.workspace.read(workspace_ref)["result"]
            package = DraftingPackageRepository(self.store).build(
                ws["outline_ref"], ws["section_id"]
            )
            content = blueprint_content(
                section_id=ws["section_id"],
                finding_key=package["evidence"][0]["finding_key"],
            )
        self.transport.responses.append(_completion(content))
        return roles.propose_blueprint(
            self.store,
            registry=self.registry,
            activation_token=self.token,
            request_id=request_id,
            workspace_ref=workspace_ref,
            hosted_ref=self.blueprint_hosted_ref,
            api_key=_API_KEY,
            artifact_id=artifact_id,
            revision_id=revision,
            rights=self.rights(artifact_id, revision),
            transport=self.transport if transport is None else transport,
        )

    def propose_draft_model(
        self, blueprint_ref, *, request_id="gen-dr-1", artifact_id="draft",
        revision="draft-r1", content=None, transport=None,
    ):
        blueprint = DraftingWorkflowRepository(self.store).read(blueprint_ref)["result"]
        if content is None:
            content = _draft_content(blueprint)
        self.transport.responses.append(_completion(content))
        return roles.propose_draft(
            self.store,
            registry=self.registry,
            activation_token=self.token,
            request_id=request_id,
            blueprint_ref=blueprint_ref,
            hosted_ref=self.writer_hosted_ref,
            api_key=_API_KEY,
            artifact_id=artifact_id,
            revision_id=revision,
            rights=self.rights(artifact_id, revision),
            transport=self.transport if transport is None else transport,
        )


# --------------------------------------------------------------------------- #
# Fast tier: fixed contracts + material builders (no store, no model)
# --------------------------------------------------------------------------- #
class _StubStore:
    project_id = "P-0001"


def _ref(artifact_id, revision_id="r1", project_id="P-0001"):
    return {"project_id": project_id, "artifact_id": artifact_id, "revision_id": revision_id}


def _synthetic_package():
    return {
        "section_id": "sec2",
        "section": {"section_id": "sec2", "title": "Evidence"},
        "outline_ref": _ref("outline"),
        "framework_ref": _ref("framework"),
        "synthesis_ref": _ref("synthesis"),
        "audit_ref": _ref("audit"),
        "acquisition_ref": _ref("acquisition"),
        "freeze_ref": _ref("freeze"),
        "corpus_ref": _ref("corpus"),
        "evidence": [
            {
                "finding_key": "A10:F1",
                "finding": {"finding_id": "F1"},
                "summary": "summary",
                "extraction_ref": _ref("A10", "e1"),
                "source_refs": [_ref("A10", "a1")],
                "citation_keys": ["cit_x"],
            }
        ],
        "citations": {"cit_x": {"citation_key": "cit_x"}},
    }


class InstructionContractTest(unittest.TestCase):
    def test_instructions_state_the_exact_contract_and_illustrative_example(self):
        for text in (roles.BLUEPRINT_INSTRUCTIONS, roles.SECTION_WRITER_INSTRUCTIONS):
            self.assertIn("OUTPUT CONTRACT", text)
            self.assertIn("illustrative only", text)
            self.assertIn("schema_version", text)
        self.assertIn("paragraphs", roles.BLUEPRINT_INSTRUCTIONS)
        self.assertIn("finding_keys", roles.BLUEPRINT_INSTRUCTIONS)
        self.assertIn("claim_ids", roles.SECTION_WRITER_INSTRUCTIONS)

    def test_prompt_versions_are_fixed_constants(self):
        self.assertEqual(roles.BLUEPRINT_PROMPT_VERSION, "drafting.blueprint.v1")
        self.assertEqual(roles.SECTION_WRITER_PROMPT_VERSION, "drafting.section_writer.v1")


class MaterialBuilderTest(unittest.TestCase):
    def test_blueprint_material_pins_refs_and_authorized_keys(self):
        store = _StubStore()
        package = _synthetic_package()
        workspace = {"section_id": "sec2", "outline_ref": _ref("outline")}
        material, input_refs, source_refs = roles.blueprint_material(
            store, _ref("ws"), workspace, package
        )
        self.assertEqual(material["section_id"], "sec2")
        self.assertEqual(material["authorized_finding_keys"], ["A10:F1"])
        self.assertEqual(material["authorized_citation_keys"], ["cit_x"])
        self.assertEqual(material["illustrative_example"]["label"], "illustrative only")
        # input refs pin the workspace + every package global ref (deduplicated).
        pinned = {(r["artifact_id"], r["revision_id"]) for r in input_refs}
        self.assertIn(("ws", "r1"), pinned)
        self.assertIn(("outline", "r1"), pinned)
        self.assertIn(("corpus", "r1"), pinned)
        # source refs are the original sources behind the evidence.
        self.assertEqual(
            [(r["artifact_id"], r["revision_id"]) for r in source_refs],
            [("A10", "a1")],
        )

    def test_draft_material_pins_blueprint_and_package(self):
        store = _StubStore()
        package = _synthetic_package()
        blueprint = {"section_id": "sec2", "paragraphs": []}
        material, input_refs, source_refs = roles.draft_material(
            store, _ref("bp"), blueprint, package
        )
        self.assertEqual(material["section_id"], "sec2")
        pinned = {(r["artifact_id"], r["revision_id"]) for r in input_refs}
        self.assertIn(("bp", "r1"), pinned)
        self.assertIn(("outline", "r1"), pinned)
        self.assertEqual(len(source_refs), 1)


# --------------------------------------------------------------------------- #
# Slow tier: end-to-end through the runner (fake transport)
# --------------------------------------------------------------------------- #
@slow
class DirectBindingWorkspaceTest(DirectRolesBase):
    def test_direct_bound_workspace_records_provenance(self):
        _s, _f, outline_ref = self.approved_chain()
        ws_ref = self.make_direct_workspace(outline_ref)
        result = SectionWorkspaceRepository(self.store).read(ws_ref)["result"]
        self.assertEqual(result["planner"]["binding_kind"], DIRECT_BINDING_KIND)
        self.assertEqual(result["planner"]["role"], _BLUEPRINT_ROLE)
        self.assertEqual(result["planner"]["model"]["model_id"], _MODEL)
        self.assertEqual(result["writer"]["binding_kind"], DIRECT_BINDING_KIND)
        self.assertEqual(result["writer"]["role"], _WRITER_ROLE)
        self.assertEqual(result["section_id"], "sec1")
        self.assertFalse(result["grants_execution"])

    def test_direct_binding_missing_hosted_use_fails_closed(self):
        _s, _f, outline_ref = self.approved_chain()
        bogus = _direct_slot(_BLUEPRINT_ROLE, self.ref("no-such-hosted", "r1"))
        with self.assertRaises(SectionWorkspaceError):
            self.make_direct_workspace(outline_ref, planner=bogus)

    def test_direct_binding_wrong_role_fails_closed(self):
        _s, _f, outline_ref = self.approved_chain()
        # A writer-role document supplied for the planner slot is a foreign role.
        bad = _direct_slot(_WRITER_ROLE, self.writer_hosted_ref)
        with self.assertRaises(SectionWorkspaceError):
            self.make_direct_workspace(outline_ref, planner=bad)


@slow
class ProposeBlueprintTest(DirectRolesBase):
    def test_valid_blueprint_stored_not_approved(self):
        _s, _f, outline_ref = self.approved_chain()
        ws_ref = self.make_direct_workspace(outline_ref)
        out = self.propose_blueprint_model(ws_ref)
        self.assertEqual(out["status"], "proposed")
        self.assertEqual(self.transport.call_count, 1)
        envelope = DraftingWorkflowRepository(self.store).read(out["ref"])
        self.assertEqual(envelope["result"]["kind"], "drafting_blueprint")
        self.assertEqual(envelope["result"]["section_id"], "sec1")
        self.assertEqual(envelope["result"]["status"], "provisional")
        self.assertFalse(envelope["result"]["authorizes_export"])
        # Stored, never approved: its own named gate is not active.
        with self.assertRaises(DraftingWorkflowError):
            DraftingWorkflowRepository(self.store).require_blueprint(out["ref"])

    def test_prompt_contains_prompt_version(self):
        _s, _f, outline_ref = self.approved_chain()
        ws_ref = self.make_direct_workspace(outline_ref)
        self.propose_blueprint_model(ws_ref)
        body = json.loads(self.transport.calls[0]["body"].decode("utf-8"))
        system = body["messages"][0]["content"]
        self.assertIn(roles.BLUEPRINT_PROMPT_VERSION, system)
        self.assertIn("OUTPUT CONTRACT", system)

    def test_blueprint_citing_unknown_finding_key_invalid_no_write(self):
        _s, _f, outline_ref = self.approved_chain()
        ws_ref = self.make_direct_workspace(outline_ref)
        bad = blueprint_content(section_id="sec1", finding_key="c1:F999")
        out = self.propose_blueprint_model(
            ws_ref, artifact_id="bp-bad", revision="bp-bad-r1", content=bad
        )
        self.assertEqual(out["status"], "invalid_proposal")
        self.assertEqual(out["reason"], roles.INVALID_PROPOSAL_REASON)
        self.assertEqual(self.transport.call_count, 1)
        self.assertIsNone(self.artifacts.get_artifact("bp-bad"))

    def test_non_completed_writes_nothing(self):
        _s, _f, outline_ref = self.approved_chain()
        ws_ref = self.make_direct_workspace(outline_ref)
        # A 5xx is an ambiguous (non-completed) outcome.
        self.transport.responses.append(
            TransportResponse(status=503, body_text=json.dumps({"error": {}}), truncated=False)
        )
        out = self.propose_blueprint_model(
            ws_ref, artifact_id="bp-nc", revision="bp-nc-r1"
        )
        self.assertEqual(out["status"], "model_not_completed")
        self.assertNotEqual(out["outcome"], "completed")
        self.assertIsNone(self.artifacts.get_artifact("bp-nc"))

    def test_replay_no_second_post(self):
        _s, _f, outline_ref = self.approved_chain()
        ws_ref = self.make_direct_workspace(outline_ref)
        first = self.propose_blueprint_model(ws_ref, request_id="gen-bp-replay")
        self.assertEqual(first["status"], "proposed")
        # Same logical request id: the runner observes it and issues no second POST.
        second = self.propose_blueprint_model(
            ws_ref, request_id="gen-bp-replay",
            artifact_id="bp2", revision="bp2-r1",
        )
        self.assertEqual(second["status"], "proposed")
        self.assertTrue(second["generation"]["replayed"])
        self.assertEqual(self.transport.call_count, 1)


@slow
class ProposeDraftTest(DirectRolesBase):
    def _approved_blueprint(self):
        _s, _f, outline_ref = self.approved_chain()
        ws_ref = self.make_direct_workspace(outline_ref)
        bp = self.propose_blueprint_model(ws_ref)
        self.approve_bp(bp["ref"])
        return bp["ref"]

    def test_valid_draft_stored_not_approved(self):
        bp_ref = self._approved_blueprint()
        out = self.propose_draft_model(bp_ref)
        self.assertEqual(out["status"], "proposed")
        envelope = DraftingWorkflowRepository(self.store).read(out["ref"], "draft")
        self.assertEqual(envelope["result"]["kind"], "section_draft")
        self.assertEqual(envelope["result"]["status"], "provisional")
        self.assertFalse(envelope["result"]["authorizes_export"])
        self.assertNotIn("final", envelope["result"])
        # The writer prompt version is recorded on the durable request.
        self.assertEqual(out["generation"]["prompt_version"], roles.SECTION_WRITER_PROMPT_VERSION)
        # One POST for the blueprint and one for the draft.
        self.assertEqual(self.transport.call_count, 2)

    def test_draft_with_unknown_claim_id_invalid_no_write(self):
        bp_ref = self._approved_blueprint()
        blueprint = DraftingWorkflowRepository(self.store).read(bp_ref)["result"]
        bad = _draft_content(blueprint)
        bad["paragraphs"][0]["sentences"][0]["claim_ids"] = ["K-DOES-NOT-EXIST"]
        out = self.propose_draft_model(
            bp_ref, artifact_id="draft-bad", revision="draft-bad-r1", content=bad
        )
        self.assertEqual(out["status"], "invalid_proposal")
        self.assertEqual(out["reason"], roles.INVALID_PROPOSAL_REASON)
        self.assertIsNone(self.artifacts.get_artifact("draft-bad"))

    def test_draft_requires_approved_blueprint(self):
        _s, _f, outline_ref = self.approved_chain()
        ws_ref = self.make_direct_workspace(outline_ref)
        bp = self.propose_blueprint_model(ws_ref, artifact_id="bp-un", revision="bp-un-r1")
        # The blueprint is stored but not approved; a draft may not consume it.
        self.transport.responses.append(_completion({"schema_version": 1}))
        with self.assertRaises(DraftingWorkflowError):
            self.propose_draft_model(
                bp["ref"], artifact_id="draft-un", revision="draft-un-r1"
            )
        self.assertIsNone(self.artifacts.get_artifact("draft-un"))


if __name__ == "__main__":  # pragma: no cover
    import unittest

    unittest.main()
