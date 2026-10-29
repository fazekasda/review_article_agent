"""Focused S11-WORKSPACE tests: durable per-section planner/writer workspace identity.

The accepted Step 10 outline seam (``SynthesisWorkflowRepository.require_for_blueprints``)
is inherited from :mod:`test_synthesis_workflow` (which itself inherits the accepted
Step 09 evidence fixture); the runtime session mappings are recorded through the
real :class:`~review_article_agent.storage.agent_runtime.RuntimeRepository` over the
same store. No model, network, provider, or OpenCode session call is made.

**Fixture disclosure:** the planner/writer mappings are *synthetic* app-owned rows
recorded locally; this suite does **not** create a live OpenCode session (the
production route is out of scope here). It proves the durable workspace binding,
its pins, and its fail-closed revalidation only.

Run::

    PYTHONPATH=src:tests python3 -m unittest test_drafting_workspace -v
"""

from __future__ import annotations

import copy
import json

from test_synthesis_workflow import SynthesisBase  # noqa: E402

from review_article_agent.domain.records import RecordValidationError  # noqa: E402
from review_article_agent.drafting.workspace import (  # noqa: E402
    DOCUMENT_TYPE,
    PLANNER_ROLE,
    WRITER_ROLE,
    SectionWorkspaceError,
    SectionWorkspaceRepository,
)
from review_article_agent.runtime.roles import RoleConfiguration  # noqa: E402
from review_article_agent.storage.agent_runtime import RuntimeRepository  # noqa: E402
from review_article_agent.synthesis.workflow import (  # noqa: E402
    GATE_FRAMEWORK,
    GATE_FRAMEWORK_KIND,
    GATE_SYNTHESIS,
    GATE_SYNTHESIS_KIND,
)

_PLANNER_MODEL = "openrouter/deepseek/v4.1-flash#high"
_WRITER_MODEL = "openrouter/anthropic/claude-sonnet-5-5#high"


def _role_config():
    return RoleConfiguration.from_mapping(
        {
            "schema_version": 1,
            "roles": {
                "synthesizer": {
                    "provider_id": "openrouter",
                    "model_id": "deepseek/v4.1-flash",
                    "variant": "high",
                    "steps": 12,
                },
                "writer": {
                    "provider_id": "openrouter",
                    "model_id": "anthropic/claude-sonnet-5-5",
                    "variant": "high",
                    "steps": 16,
                },
            },
        }
    )


class WorkspaceBase(SynthesisBase):
    def setUp(self):
        super().setUp()
        self.workspace = SectionWorkspaceRepository(self.store)
        self._role_config = _role_config()

    def _reopen(self):
        super()._reopen()
        self.workspace = SectionWorkspaceRepository(self.store)

    # -- runtime mapping fixtures -------------------------------------------- #
    def map_session(self, session_id, role, *, model):
        return RuntimeRepository(self.store).record_session_mapping(
            service_instance_id="svc_1",
            opencode_project_id="oc_proj_1",
            clean_location=str(self.tmp / "clean"),
            session_id=session_id,
            role=role,
            model=model,
            configuration=self._role_config,
        )

    def planner_mapping(self):
        return self.map_session("ses_planner_1", PLANNER_ROLE, model=_PLANNER_MODEL)

    def writer_mapping(self, n=1):
        return self.map_session(f"ses_writer_{n}", WRITER_ROLE, model=_WRITER_MODEL)

    def payload(self, outline_ref, *, section_id="sec1", planner, writer):
        return {
            "outline_ref": outline_ref,
            "section_id": section_id,
            "planner_mapping_id": planner.mapping_id,
            "writer_mapping_id": writer.mapping_id,
        }

    def propose_ws(self, payload, *, artifact_id="ws", revision="ws-r1", parent=None):
        return self.workspace.propose(
            payload,
            artifact_id=artifact_id,
            revision_id=revision,
            rights=self.rights(artifact_id, revision),
            parent_revision_id=parent,
        )


class ProposeAndReadTests(WorkspaceBase):
    def test_propose_and_reopen_reconstructs_mapping(self):
        _s, _f, outline_ref = self.approved_chain()
        planner = self.planner_mapping()
        writer = self.writer_mapping()
        ref = self.propose_ws(self.payload(outline_ref, planner=planner, writer=writer))

        stored = self.workspace.read(ref)
        result = stored["result"]
        self.assertEqual(result["project_id"], self.project_id)
        self.assertEqual(result["section_id"], "sec1")
        self.assertEqual(result["outline_ref"], outline_ref)
        self.assertEqual(result["planner"]["role"], PLANNER_ROLE)
        self.assertEqual(result["planner"]["purpose"], "planning")
        self.assertEqual(result["planner"]["session_id"], "ses_planner_1")
        self.assertEqual(result["planner"]["model_ref"], _PLANNER_MODEL)
        self.assertEqual(result["writer"]["role"], WRITER_ROLE)
        self.assertEqual(result["writer"]["session_id"], "ses_writer_1")
        self.assertEqual(result["writer"]["model_ref"], _WRITER_MODEL)
        self.assertTrue(result["distinct_sessions"])
        self.assertFalse(result["grants_execution"])

        # Reopening the store reconstructs the exact same mapping.
        self._reopen()
        again = self.workspace.read(ref)
        self.assertEqual(again, stored)

    def test_require_for_drafting_returns_approved_context(self):
        synthesis_ref, framework_ref, outline_ref = self.approved_chain()
        planner = self.planner_mapping()
        writer = self.writer_mapping()
        ref = self.propose_ws(self.payload(outline_ref, planner=planner, writer=writer))
        context = self.workspace.require_for_drafting(ref)
        self.assertEqual(context["outline_ref"], outline_ref)
        self.assertEqual(context["framework_ref"], framework_ref)
        self.assertEqual(context["synthesis_ref"], synthesis_ref)
        self.assertEqual(context["section_id"], "sec1")
        self.assertFalse(context["grants_execution"])

    def test_dependency_edge_is_the_outline(self):
        _s, _f, outline_ref = self.approved_chain()
        ref = self.propose_ws(
            self.payload(outline_ref, planner=self.planner_mapping(), writer=self.writer_mapping())
        )
        deps = self.artifacts.list_dependencies(ref["artifact_id"], ref["revision_id"])
        live = [d for d in deps if d.role != "version_of"]
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0].role, "derived_from")
        self.assertEqual(live[0].as_ref(), outline_ref)


class FailClosedTests(WorkspaceBase):
    def test_foreign_role_mapping_refused(self):
        _s, _f, outline_ref = self.approved_chain()
        planner = self.planner_mapping()
        writer = self.writer_mapping()
        # A writer mapping supplied for the planner slot is a foreign role.
        bad = self.payload(outline_ref, planner=writer, writer=writer)
        with self.assertRaises(SectionWorkspaceError):
            self.propose_ws(bad)

    def test_reusing_one_mapping_for_both_slots_refused(self):
        _s, _f, outline_ref = self.approved_chain()
        planner = self.planner_mapping()
        same = self.payload(outline_ref, planner=planner, writer=planner)
        with self.assertRaises(SectionWorkspaceError):
            self.propose_ws(same)

    def test_missing_mapping_fails_closed_no_pretend_model(self):
        _s, _f, outline_ref = self.approved_chain()
        writer = self.writer_mapping()
        payload = self.payload(outline_ref, planner=writer, writer=writer)
        payload["planner_mapping_id"] = "rmap_" + "0" * 32
        with self.assertRaises(SectionWorkspaceError):
            self.propose_ws(payload)

    def test_section_must_exist_in_outline(self):
        _s, _f, outline_ref = self.approved_chain()
        payload = self.payload(
            outline_ref, section_id="not-a-section",
            planner=self.planner_mapping(), writer=self.writer_mapping(),
        )
        with self.assertRaises(SectionWorkspaceError):
            self.propose_ws(payload)

    def test_payload_contract_is_exact(self):
        _s, _f, outline_ref = self.approved_chain()
        payload = self.payload(
            outline_ref, planner=self.planner_mapping(), writer=self.writer_mapping()
        )
        payload["extra"] = True
        with self.assertRaises(SectionWorkspaceError):
            self.propose_ws(payload)

    def test_ungated_outline_refused(self):
        # An accepted synthesis + framework, but the outline is not yet gated.
        audit_ref, _batch = self.accepted_audit()
        synthesis_ref = self.put_synthesis(audit_ref)
        self.approve_ref(synthesis_ref, GATE_SYNTHESIS_KIND, GATE_SYNTHESIS)
        framework_ref = self.put_framework(synthesis_ref)
        self.approve_ref(framework_ref, GATE_FRAMEWORK_KIND, GATE_FRAMEWORK)
        outline_ref = self.put_outline(synthesis_ref, framework_ref)  # no gate
        payload = self.payload(
            outline_ref, planner=self.planner_mapping(), writer=self.writer_mapping()
        )
        with self.assertRaises(SectionWorkspaceError):
            self.propose_ws(payload)

    def test_cross_project_outline_ref_refused(self):
        _s, _f, outline_ref = self.approved_chain()
        foreign = {
            "project_id": "OTHER",
            "artifact_id": outline_ref["artifact_id"],
            "revision_id": outline_ref["revision_id"],
        }
        payload = self.payload(
            foreign, planner=self.planner_mapping(), writer=self.writer_mapping()
        )
        with self.assertRaises(RecordValidationError):
            self.propose_ws(payload)


class HistoryAndDivergenceTests(WorkspaceBase):
    def test_old_revision_inspectable_after_global_change(self):
        synthesis_ref, _framework_ref, outline_ref = self.approved_chain()
        ref = self.propose_ws(
            self.payload(outline_ref, planner=self.planner_mapping(), writer=self.writer_mapping())
        )
        before = self.workspace.read(ref)

        # A global framework change conservatively stales the outline gate.
        self.put_framework(synthesis_ref, revision="fw-r2", parent="fw-r1",
                           c1_definition="A revised Compound Z definition.")

        # The current read now fails closed (the outline is no longer approved)...
        with self.assertRaises(SectionWorkspaceError):
            self.workspace.read(ref, current=True)
        # ...but the historical revision stays inspectable and unchanged.
        after = self.workspace.read(ref, current=False)
        self.assertEqual(after, before)

    def test_stale_outline_on_new_proposal_refused(self):
        synthesis_ref, _framework_ref, outline_ref = self.approved_chain()
        self.put_framework(synthesis_ref, revision="fw-r2", parent="fw-r1",
                           c1_definition="A revised Compound Z definition.")
        payload = self.payload(
            outline_ref, planner=self.planner_mapping(), writer=self.writer_mapping()
        )
        with self.assertRaises(SectionWorkspaceError):
            self.propose_ws(payload)

    def test_edit_may_not_diverge_mapping_pins(self):
        _s, _f, outline_ref = self.approved_chain()
        planner = self.planner_mapping()
        writer = self.writer_mapping(1)
        writer2 = self.writer_mapping(2)
        self.propose_ws(
            self.payload(outline_ref, planner=planner, writer=writer),
            revision="ws-r1",
        )
        # Retargeting the writer session on an edit is a divergent reuse.
        with self.assertRaises(SectionWorkspaceError):
            self.propose_ws(
                self.payload(outline_ref, planner=planner, writer=writer2),
                revision="ws-r2", parent="ws-r1",
            )

    def test_edit_with_same_pins_is_allowed(self):
        _s, _f, outline_ref = self.approved_chain()
        planner = self.planner_mapping()
        writer = self.writer_mapping()
        payload = self.payload(outline_ref, planner=planner, writer=writer)
        self.propose_ws(payload, revision="ws-r1")
        ref2 = self.propose_ws(payload, revision="ws-r2", parent="ws-r1")
        self.assertEqual(self.workspace.read(ref2)["result"]["writer"]["mapping_id"], writer.mapping_id)

    def test_forged_stored_model_refused_on_read(self):
        _s, _f, outline_ref = self.approved_chain()
        payload = self.payload(
            outline_ref, planner=self.planner_mapping(), writer=self.writer_mapping()
        )
        ref = self.propose_ws(payload, artifact_id="ws", revision="ws-r1")
        stored = self.workspace.read(ref)

        # A generic forged document claiming a different planner model.
        forged = copy.deepcopy(stored["result"])
        forged["planner"]["model"]["model_id"] = "pretend/model"
        forged["planner"]["model_ref"] = "openrouter/pretend/model"
        self.artifacts.create_artifact("forged-ws", kind="report", created_by="import")
        self.artifacts.import_revision(
            artifact_id="forged-ws",
            revision_id="forged-ws-r1",
            content={
                "schema_version": 1,
                "document_type": DOCUMENT_TYPE,
                "payload": {"input": payload, "result": forged},
            },
            origin_locator="import:synthetic",
            rights=self.rights("forged-ws", "forged-ws-r1"),
            dependencies=[outline_ref],
            acquired_by="import",
        )
        forged_ref = {
            "project_id": self.project_id,
            "artifact_id": "forged-ws",
            "revision_id": "forged-ws-r1",
        }
        with self.assertRaises(SectionWorkspaceError):
            self.workspace.read(forged_ref)


if __name__ == "__main__":
    import unittest

    unittest.main()
