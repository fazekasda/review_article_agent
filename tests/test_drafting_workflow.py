"""Focused S11-BLUEPRINT tests: durable, owner-gated section blueprints.

Covers ``src/review_article_agent/drafting/workflow.py`` against
``docs/implementation/11-drafting.md`` AC1/AC2/AC6/AC7: a section blueprint may
be created only over the exact current approved global chain, the exact current
per-section workspace, and the reconstructible approved drafting package; it is
consumed only through its **own** exact named owner gate
(``artifact_revision`` / ``drafting.blueprint``); a changed shared argument
conservatively stales it; and every historical revision stays inspectable.

The accepted Step 10 outline seam and the Step 09 synthetic verified-receipt seam
are inherited from :class:`test_drafting_workspace.WorkspaceBase` (which extends
``test_synthesis_workflow.SynthesisBase``); the per-section workspace identity and
the approved drafting package are the real integrated modules.  Every human
approval uses the real owner-capability flow (the controlling-tty seam is mocked
as the accepted freeze tests do).

**Fixture disclosure:** the planner/writer runtime mappings and the accepted
Step 09 receipts are *synthetic* app-owned rows recorded locally; this suite does
**not** create a live OpenCode session and makes no model, network, or provider
call.  It proves the durable owner-gated blueprint binding and its fail-closed
revalidation only.

Run::

    PYTHONPATH=src:tests python3 -m unittest test_drafting_workflow -v
"""

from __future__ import annotations

import json

from test_drafting_workspace import (  # noqa: E402
    _PLANNER_MODEL,
    _WRITER_MODEL,
    WorkspaceBase,
)
from test_synthesis_workflow import (  # noqa: E402
    GATE_FRAMEWORK,
    GATE_FRAMEWORK_KIND,
    GATE_OUTLINE,
    GATE_OUTLINE_KIND,
    GATE_SYNTHESIS,
    GATE_SYNTHESIS_KIND,
)

from review_article_agent.drafting.package import DraftingPackageRepository  # noqa: E402
from review_article_agent.drafting.workflow import (  # noqa: E402
    DOCUMENT_TYPE,
    GATE_ID,
    GATE_KIND,
    DraftingWorkflowError,
    DraftingWorkflowRepository,
)
from review_article_agent.drafting.workspace import (  # noqa: E402
    PLANNER_ROLE,
    WRITER_ROLE,
)
from _tiers import slow

_FINDING = "c1:F1"


def _known(value):
    return {"state": "known", "value": value}


def _applicability():
    return {
        "species": _known("mouse"),
        "population": _known("adult mice"),
        "assay": _known("tumor volume"),
        "outcome": _known("tumor volume"),
        "conditions": _known("vehicle"),
    }


def blueprint_content(*, section_id="sec1", finding_key=_FINDING):
    return {
        "schema_version": 1,
        "section_id": section_id,
        "paragraphs": [
            {
                "paragraph_id": "P1",
                "intents": [
                    {
                        "intent_id": "I1",
                        "intent": "State the reported effect with its limits.",
                        "claims": [
                            {
                                "claim_id": "K1",
                                "text": "Compound Z did not reduce tumor volume in mice.",
                                "finding_keys": [finding_key],
                                "attribution": "direct_observation",
                                "classification": "reported_evidence",
                                "applicability": _applicability(),
                                "causal_limitations": _known(
                                    "association only; no mechanism established"
                                ),
                            }
                        ],
                    }
                ],
            }
        ],
    }


class BlueprintBase(WorkspaceBase):
    def setUp(self):
        super().setUp()
        self.wf = DraftingWorkflowRepository(self.store)

    def _reopen(self):
        super()._reopen()
        self.wf = DraftingWorkflowRepository(self.store)

    # -- fixtures ------------------------------------------------------------ #
    def make_workspace(self, outline_ref, *, section_id="sec1", artifact_id="ws",
                       revision="ws-r1", planner=None, writer=None, parent=None):
        planner = planner or self.planner_mapping()
        writer = writer or self.writer_mapping()
        return self.workspace.propose(
            self.payload(outline_ref, section_id=section_id, planner=planner, writer=writer),
            artifact_id=artifact_id, revision_id=revision,
            rights=self.rights(artifact_id, revision), parent_revision_id=parent,
        )

    def blueprint_payload(self, workspace_ref, *, section_id="sec1"):
        ws = self.workspace.read(workspace_ref)["result"]
        package = DraftingPackageRepository(self.store).build(ws["outline_ref"], section_id)
        finding_key = package["evidence"][0]["finding_key"]
        return {
            "workspace_ref": workspace_ref,
            "content": blueprint_content(section_id=section_id, finding_key=finding_key),
        }

    def propose_bp(self, payload, *, artifact_id="bp", revision="bp-r1", parent=None):
        return self.wf.propose(
            "blueprint", payload, artifact_id=artifact_id, revision_id=revision,
            rights=self.rights(artifact_id, revision), parent_revision_id=parent,
        )

    def approve_bp(self, ref):
        return self.approve_ref(ref, GATE_KIND, GATE_ID)


@slow
class ProposeReadAndGateTests(BlueprintBase):
    def test_propose_read_reopen_and_own_gate(self):
        _s, _f, outline_ref = self.approved_chain()
        ws_ref = self.make_workspace(outline_ref)
        bp_ref = self.propose_bp(self.blueprint_payload(ws_ref))

        envelope = self.wf.read(bp_ref)
        self.assertEqual(set(envelope), {"input", "workspace", "package", "result"})
        self.assertEqual(envelope["input"]["workspace_ref"], ws_ref)
        self.assertEqual(envelope["result"]["kind"], "drafting_blueprint")
        self.assertEqual(envelope["result"]["section_id"], "sec1")
        self.assertEqual(envelope["result"]["status"], "provisional")
        self.assertFalse(envelope["result"]["authorizes_export"])
        self.assertEqual(envelope["package"]["section"]["section_id"], "sec1")

        # The gate is required to *consume* the blueprint.
        with self.assertRaises(DraftingWorkflowError):
            self.wf.require_blueprint(bp_ref)

        self.approve_bp(bp_ref)
        approved = self.wf.require_blueprint(bp_ref)
        self.assertEqual(approved["blueprint_ref"], bp_ref)
        self.assertEqual(approved["gate_kind"], GATE_KIND)
        self.assertEqual(approved["gate_id"], GATE_ID)
        self.assertTrue(approved["approved"])
        self.assertFalse(approved["authorizes_export"])
        self.assertEqual(approved["result"], envelope["result"])

    def test_reopened_context_equality(self):
        _s, _f, outline_ref = self.approved_chain()
        ws_ref = self.make_workspace(outline_ref)
        bp_ref = self.propose_bp(self.blueprint_payload(ws_ref))
        before = self.wf.read(bp_ref)
        self._reopen()
        self.assertEqual(self.wf.read(bp_ref), before)

    def test_dependency_set_is_exact_and_deduplicated(self):
        _s, _f, outline_ref = self.approved_chain()
        ws_ref = self.make_workspace(outline_ref)
        bp_ref = self.propose_bp(self.blueprint_payload(ws_ref))
        deps = self.artifacts.list_dependencies(bp_ref["artifact_id"], bp_ref["revision_id"])
        live = [d for d in deps if d.role != "version_of"]
        self.assertTrue(live)
        self.assertTrue(all(d.role == "derived_from" for d in live))
        refs = {json.dumps(d.as_ref(), sort_keys=True) for d in live}
        self.assertEqual(len(refs), len(live))  # deduplicated
        self.assertIn(json.dumps(ws_ref, sort_keys=True), refs)
        self.assertIn(json.dumps(outline_ref, sort_keys=True), refs)


@slow
class OwnerGateIsolationTests(BlueprintBase):
    def test_unapproved_other_section_does_not_block(self):
        _s, _f, outline_ref = self.approved_chain()
        ws1 = self.make_workspace(outline_ref, section_id="sec1",
                                  artifact_id="ws-sec1", revision="ws-sec1-r1")
        # A distinct section with distinct mappings (per-section exclusivity).
        planner2 = self.map_session("ses_planner_2", PLANNER_ROLE, model=_PLANNER_MODEL)
        writer2 = self.map_session("ses_writer_2", WRITER_ROLE, model=_WRITER_MODEL)
        ws3 = self.make_workspace(outline_ref, section_id="sec3", artifact_id="ws-sec3",
                                  revision="ws-sec3-r1", planner=planner2, writer=writer2)
        bp1 = self.propose_bp(self.blueprint_payload(ws1, section_id="sec1"),
                              artifact_id="bp-sec1", revision="bp-sec1-r1")
        bp3 = self.propose_bp(self.blueprint_payload(ws3, section_id="sec3"),
                              artifact_id="bp-sec3", revision="bp-sec3-r1")
        # Only sec1's blueprint is approved; sec3's stays unapproved.
        self.approve_bp(bp1)
        self.assertTrue(self.wf.require_blueprint(bp1)["approved"])
        with self.assertRaises(DraftingWorkflowError):
            self.wf.require_blueprint(bp3)

    def test_generic_artifact_approval_cannot_bypass_own_gate(self):
        _s, _f, outline_ref = self.approved_chain()
        ws_ref = self.make_workspace(outline_ref)
        bp_ref = self.propose_bp(self.blueprint_payload(ws_ref))
        # A different named gate on the same artifact is not the blueprint gate.
        self.approve_ref(bp_ref, "artifact_revision", "synthesis.memo")
        with self.assertRaises(DraftingWorkflowError):
            self.wf.require_blueprint(bp_ref)

    def test_inband_status_is_not_authority(self):
        _s, _f, outline_ref = self.approved_chain()
        ws_ref = self.make_workspace(outline_ref)
        payload = self.blueprint_payload(ws_ref)
        payload["content"]["status"] = "approved"
        with self.assertRaises(DraftingWorkflowError):
            self.propose_bp(payload)


@slow
class StalenessAndHistoryTests(BlueprintBase):
    def test_changing_shared_framework_flags_stale_and_refuses_require(self):
        synthesis_ref, _f, outline_ref = self.approved_chain()
        ws_ref = self.make_workspace(outline_ref)
        bp_ref = self.propose_bp(self.blueprint_payload(ws_ref))
        self.approve_bp(bp_ref)
        before = self.wf.read(bp_ref)

        # A global framework change conservatively stales the outline/blueprint.
        self.put_framework(synthesis_ref, revision="fw-r2", parent="fw-r1",
                           c1_definition="A revised Compound Z definition.")

        with self.assertRaises(DraftingWorkflowError):
            self.wf.read(bp_ref, current=True)
        with self.assertRaises(DraftingWorkflowError):
            self.wf.require_blueprint(bp_ref)

        report = self.wf.status(bp_ref)
        self.assertTrue(report["stale"])
        self.assertTrue(report["reasons"])
        self.assertTrue(report["provisional"])
        self.assertFalse(report["authorizes_export"])
        self.assertEqual(report["pending"], ["source_checks", "origin_tracing"])

        # The historical revision stays inspectable and unchanged.
        self.assertEqual(self.wf.read(bp_ref, current=False), before)

    def test_new_blueprint_revision_invalidates_old_approval(self):
        _s, _f, outline_ref = self.approved_chain()
        ws_ref = self.make_workspace(outline_ref)
        bp1 = self.propose_bp(self.blueprint_payload(ws_ref), revision="bp-r1")
        self.approve_bp(bp1)
        self.assertTrue(self.wf.require_blueprint(bp1)["approved"])

        # A new revision of the same blueprint artifact (an edit).
        bp2 = self.propose_bp(self.blueprint_payload(ws_ref), revision="bp-r2", parent="bp-r1")
        # The old approval does not carry forward to the new revision.
        with self.assertRaises(DraftingWorkflowError):
            self.wf.require_blueprint(bp2)
        # The old revision is no longer the current head.
        with self.assertRaises(DraftingWorkflowError):
            self.wf.read(bp1, current=True)
        # Re-approving the new revision restores consumption.
        self.approve_bp(bp2)
        self.assertTrue(self.wf.require_blueprint(bp2)["approved"])
        # The old revision stays inspectable historically.
        self.assertEqual(self.wf.read(bp1, current=False)["result"]["section_id"], "sec1")

    def test_edit_may_not_switch_section_or_workspace_identity(self):
        _s, _f, outline_ref = self.approved_chain()
        ws_ref = self.make_workspace(outline_ref, section_id="sec1",
                                     artifact_id="ws-sec1", revision="ws-sec1-r1")
        self.propose_bp(self.blueprint_payload(ws_ref, section_id="sec1"),
                        artifact_id="bp-sec1", revision="bp-sec1-r1")
        # A different workspace artifact (sec3) may not be silently swapped in.
        planner2 = self.map_session("ses_planner_2", PLANNER_ROLE, model=_PLANNER_MODEL)
        writer2 = self.map_session("ses_writer_2", WRITER_ROLE, model=_WRITER_MODEL)
        ws3 = self.make_workspace(outline_ref, section_id="sec3", artifact_id="ws-sec3",
                                  revision="ws-sec3-r1", planner=planner2, writer=writer2)
        with self.assertRaises(DraftingWorkflowError):
            self.propose_bp(self.blueprint_payload(ws3, section_id="sec3"),
                            artifact_id="bp-sec1", revision="bp-sec1-r2", parent="bp-sec1-r1")


@slow
class FailClosedTests(BlueprintBase):
    def _import_blueprint(self, payload, *, artifact_id="forged-bp", revision="forged-bp-r1",
                          dependencies=()):
        self.artifacts.create_artifact(artifact_id, kind="report", created_by="import")
        return self.artifacts.import_revision(
            artifact_id=artifact_id, revision_id=revision,
            content={"schema_version": 1, "document_type": DOCUMENT_TYPE, "payload": payload},
            origin_locator="import:synthetic", rights=self.rights(artifact_id, revision),
            dependencies=list(dependencies), acquired_by="import",
        )

    def test_foreign_workspace_ref_fails_closed(self):
        foreign = {"project_id": "OTHER", "artifact_id": "ws", "revision_id": "ws-r1"}
        rev = self._import_blueprint(
            {"input": {"workspace_ref": foreign, "content": {}},
             "workspace": {}, "package": {}, "result": {}}
        )
        ref = {"project_id": self.project_id, "artifact_id": rev.artifact_id,
               "revision_id": rev.revision_id}
        with self.assertRaises(DraftingWorkflowError):
            self.wf.read(ref, current=False)

    def test_tampered_blueprint_bytes_fail_closed(self):
        _s, _f, outline_ref = self.approved_chain()
        ws_ref = self.make_workspace(outline_ref)
        bp_ref = self.propose_bp(self.blueprint_payload(ws_ref))
        conn = self.store.connection
        conn.execute("DROP TRIGGER revision_no_update")
        conn.execute(
            "UPDATE revision SET content = ? WHERE artifact_id = ? AND revision_id = ?",
            (b"not-json", bp_ref["artifact_id"], bp_ref["revision_id"]),
        )
        conn.commit()
        with self.assertRaises(DraftingWorkflowError):
            self.wf.read(bp_ref)
        # Status must fail closed on corruption rather than report a benign status.
        with self.assertRaises(DraftingWorkflowError):
            self.wf.status(bp_ref)

    def test_dependency_mismatch_fails_closed(self):
        _s, _f, outline_ref = self.approved_chain()
        ws_ref = self.make_workspace(outline_ref)
        bp_ref = self.propose_bp(self.blueprint_payload(ws_ref))
        stored = json.loads(self.artifacts.read_content(bp_ref["artifact_id"], bp_ref["revision_id"]))
        # Re-import the same envelope under a fresh artifact with a spurious dep.
        self.artifacts.create_artifact("bp-extra", kind="report", created_by="import")
        rev = self.artifacts.import_revision(
            artifact_id="bp-extra", revision_id="bp-extra-r1",
            content=stored, origin_locator="import:synthetic",
            rights=self.rights("bp-extra", "bp-extra-r1"),
            dependencies=[outline_ref], acquired_by="import",
        )
        ref = {"project_id": self.project_id, "artifact_id": rev.artifact_id,
               "revision_id": rev.revision_id}
        with self.assertRaises(DraftingWorkflowError):
            self.wf.read(ref, current=False)

    def test_failed_propose_writes_nothing(self):
        _s, _f, outline_ref = self.approved_chain()
        ws_ref = self.make_workspace(outline_ref)
        payload = self.blueprint_payload(ws_ref)
        payload["content"]["section_id"] = "sec-does-not-exist"
        with self.assertRaises(DraftingWorkflowError):
            self.propose_bp(payload, artifact_id="bp-bad", revision="bp-bad-r1")
        self.assertIsNone(self.artifacts.get_artifact("bp-bad"))


if __name__ == "__main__":
    import unittest

    unittest.main()
