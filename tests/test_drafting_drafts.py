"""Focused S11-DRAFT/ASSEMBLY tests: durable provisional drafts and assembly.

Covers the extended ``src/review_article_agent/drafting/workflow.py`` against
``docs/implementation/11-drafting.md`` AC1/AC2/AC6/AC7 and
``docs/specification.md`` workflow steps 13-14: a section draft may be created
**only** by atomically consuming the exact current *approved* blueprint (its own
named owner gate) and validating the draft against the approved blueprint result
and package; a draft stays provisional (no final/export/scientific-approval
flags); a changed blueprint/global argument stales it while its history stays
inspectable; and a provisional manuscript assembly consumes exactly one current
valid draft per approved outline section (order derived from the outline, never
the caller), labelling secondary accounts/author proposals and citing only stable
verified-corpus keys -- with no final release/export API.

The accepted Step 10 outline seam, the Step 09 synthetic verified-receipt seam, and
the runtime session-mapping fixtures are inherited from
:class:`test_drafting_workflow.BlueprintBase` (which extends
``test_drafting_workspace.WorkspaceBase`` -> ``test_synthesis_workflow``); the
per-section workspace, the approved drafting package, and the real runner /
fake-transport seam are the integrated modules.

**Fixture disclosure:** the planner/writer runtime mappings, the accepted Step 09
receipts, and the corpus candidates are *synthetic* app-owned rows recorded
locally.  The real-runner case uses the fake transport inherited from
``test_extraction_runner`` -- no live OpenCode service/session is created and no
model, network, or provider call is made.

Run::

    PYTHONPATH=src:tests python3 -m unittest test_drafting_drafts -v
"""

from __future__ import annotations

import json

import test_drafting_package as _drafting_package  # noqa: E402
from test_drafting_workflow import (  # noqa: E402
    BlueprintBase,
    _applicability,
    _known,
)
from test_drafting_workspace import _PLANNER_MODEL, _WRITER_MODEL  # noqa: E402
from test_synthesis_workflow import (  # noqa: E402
    GATE_FRAMEWORK,
    GATE_FRAMEWORK_KIND,
    GATE_OUTLINE,
    GATE_OUTLINE_KIND,
    GATE_SYNTHESIS,
    GATE_SYNTHESIS_KIND,
)

from review_article_agent.drafting.package import (  # noqa: E402
    DraftingPackageRepository,
    citation_key,
)
from review_article_agent.drafting.workflow import (  # noqa: E402
    ASSEMBLY_DOCUMENT_TYPE,
    DRAFT_DOCUMENT_TYPE,
    KINDS,
    DraftingWorkflowError,
    DraftingWorkflowRepository,
)
from review_article_agent.drafting.workspace import (  # noqa: E402
    PLANNER_ROLE,
    WRITER_ROLE,
    SectionWorkspaceRepository,
)
from review_article_agent.extraction import workflow as extraction_workflow  # noqa: E402


def _role_config():
    from review_article_agent.runtime.roles import RoleConfiguration

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


class DraftBase(BlueprintBase):
    def setUp(self):
        super().setUp()
        self.wf = DraftingWorkflowRepository(self.store)

    def _reopen(self):
        super()._reopen()
        self.wf = DraftingWorkflowRepository(self.store)

    # -- draft fixtures ------------------------------------------------------ #
    def draft_content(self, blueprint):
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

    def draft_payload(self, blueprint_ref, *, content=None):
        blueprint = self.wf.read(blueprint_ref)["result"]
        return {
            "blueprint_ref": blueprint_ref,
            "content": content if content is not None else self.draft_content(blueprint),
        }

    def propose_draft(self, payload, *, artifact_id="draft", revision="draft-r1", parent=None):
        return self.wf.propose(
            "draft",
            payload,
            artifact_id=artifact_id,
            revision_id=revision,
            rights=self.rights(artifact_id, revision),
            parent_revision_id=parent,
        )


class DraftGateAndProposeTests(DraftBase):
    def test_kinds_extended_with_draft_and_assembly(self):
        self.assertEqual(KINDS, ("blueprint", "draft", "assembly"))

    def test_unapproved_blueprint_refuses_draft_and_writes_nothing(self):
        _s, _f, outline_ref = self.approved_chain()
        ws_ref = self.make_workspace(outline_ref)
        bp_ref = self.propose_bp(self.blueprint_payload(ws_ref))  # not approved
        with self.assertRaises(DraftingWorkflowError):
            self.propose_draft(
                self.draft_payload(bp_ref),
                artifact_id="draft-bad",
                revision="draft-bad-r1",
            )
        self.assertIsNone(self.artifacts.get_artifact("draft-bad"))

    def test_approved_own_section_draft_despite_other_unapproved_section(self):
        _s, _f, outline_ref = self.approved_chain()
        ws1 = self.make_workspace(
            outline_ref, section_id="sec1", artifact_id="ws-sec1", revision="ws-sec1-r1"
        )
        planner2 = self.map_session("ses_planner_2", PLANNER_ROLE, model=_PLANNER_MODEL)
        writer2 = self.map_session("ses_writer_2", WRITER_ROLE, model=_WRITER_MODEL)
        ws3 = self.make_workspace(
            outline_ref, section_id="sec3", artifact_id="ws-sec3", revision="ws-sec3-r1",
            planner=planner2, writer=writer2,
        )
        bp1 = self.propose_bp(
            self.blueprint_payload(ws1, section_id="sec1"),
            artifact_id="bp-sec1", revision="bp-sec1-r1",
        )
        bp3 = self.propose_bp(
            self.blueprint_payload(ws3, section_id="sec3"),
            artifact_id="bp-sec3", revision="bp-sec3-r1",
        )
        # Only sec1's blueprint is approved; sec3's stays unapproved.
        self.approve_bp(bp1)
        d_ref = self.propose_draft(
            self.draft_payload(bp1), artifact_id="draft-sec1", revision="draft-sec1-r1"
        )
        envelope = self.wf.read(d_ref, "draft")
        self.assertEqual(set(envelope), {"input", "blueprint", "package", "result"})
        self.assertEqual(envelope["input"]["blueprint_ref"], bp1)
        self.assertEqual(envelope["result"]["kind"], "section_draft")
        self.assertEqual(envelope["result"]["section_id"], "sec1")
        self.assertEqual(envelope["result"]["status"], "provisional")
        self.assertFalse(envelope["result"]["authorizes_export"])
        # sec3's draft still cannot be created (its blueprint is unapproved).
        with self.assertRaises(DraftingWorkflowError):
            self.propose_draft(
                self.draft_payload(bp3), artifact_id="draft-sec3", revision="draft-sec3-r1"
            )

    def test_draft_is_stored_with_its_blueprint_and_package_snapshot(self):
        _s, _f, outline_ref = self.approved_chain()
        ws_ref = self.make_workspace(outline_ref)
        bp_ref = self.propose_bp(self.blueprint_payload(ws_ref))
        self.approve_bp(bp_ref)
        d_ref = self.propose_draft(self.draft_payload(bp_ref))
        envelope = self.wf.read(d_ref, "draft")
        self.assertEqual(envelope["blueprint"]["kind"], "drafting_blueprint")
        self.assertEqual(envelope["package"]["section"]["section_id"], "sec1")
        # The stored generic document is the extended draft document type.
        raw = json.loads(
            self.artifacts.read_content(d_ref["artifact_id"], d_ref["revision_id"])
        )
        self.assertEqual(raw["document_type"], DRAFT_DOCUMENT_TYPE)
        # Reopening the store reconstructs the exact same snapshot.
        self._reopen()
        self.assertEqual(self.wf.read(d_ref, "draft"), envelope)

    def test_draft_payload_contract_is_exact(self):
        _s, _f, outline_ref = self.approved_chain()
        ws_ref = self.make_workspace(outline_ref)
        bp_ref = self.propose_bp(self.blueprint_payload(ws_ref))
        self.approve_bp(bp_ref)
        payload = self.draft_payload(bp_ref)
        payload["extra"] = True
        with self.assertRaises(DraftingWorkflowError):
            self.propose_draft(payload, artifact_id="draft-bad", revision="draft-bad-r1")
        self.assertIsNone(self.artifacts.get_artifact("draft-bad"))


class DraftGroundingTests(DraftBase):
    def _approved_blueprint(self, outline_ref):
        ws_ref = self.make_workspace(outline_ref)
        bp_ref = self.propose_bp(self.blueprint_payload(ws_ref))
        self.approve_bp(bp_ref)
        return bp_ref

    def test_invented_text_is_rejected(self):
        _s, _f, outline_ref = self.approved_chain()
        bp_ref = self._approved_blueprint(outline_ref)
        blueprint = self.wf.read(bp_ref)["result"]
        content = self.draft_content(blueprint)
        content["paragraphs"][0]["sentences"][0]["text"] = (
            "Compound Z reduced tumor volume in mice."
        )
        with self.assertRaises(DraftingWorkflowError):
            self.propose_draft(
                {"blueprint_ref": bp_ref, "content": content},
                artifact_id="draft-bad",
                revision="draft-bad-r1",
            )
        self.assertIsNone(self.artifacts.get_artifact("draft-bad"))

    def test_missing_intent_is_rejected(self):
        _s, _f, outline_ref = self.approved_chain()
        bp_ref = self._approved_blueprint(outline_ref)
        blueprint = self.wf.read(bp_ref)["result"]
        content = self.draft_content(blueprint)
        content["paragraphs"][0]["sentences"] = []
        with self.assertRaises(DraftingWorkflowError):
            self.propose_draft(
                {"blueprint_ref": bp_ref, "content": content},
                artifact_id="draft-bad",
                revision="draft-bad-r1",
            )
        self.assertIsNone(self.artifacts.get_artifact("draft-bad"))

    def test_inband_status_is_not_authority(self):
        _s, _f, outline_ref = self.approved_chain()
        bp_ref = self._approved_blueprint(outline_ref)
        content = self.draft_content(self.wf.read(bp_ref)["result"])
        content["status"] = "approved"
        with self.assertRaises(DraftingWorkflowError):
            self.propose_draft(
                {"blueprint_ref": bp_ref, "content": content},
                artifact_id="draft-bad",
                revision="draft-bad-r1",
            )
        self.assertIsNone(self.artifacts.get_artifact("draft-bad"))


class DraftStalenessAndHistoryTests(DraftBase):
    def _approved_blueprint_and_draft(self, outline_ref):
        ws_ref = self.make_workspace(outline_ref)
        bp_ref = self.propose_bp(self.blueprint_payload(ws_ref))
        self.approve_bp(bp_ref)
        d_ref = self.propose_draft(self.draft_payload(bp_ref))
        return bp_ref, d_ref

    def test_framework_change_stales_draft_and_history_inspectable(self):
        synthesis_ref, _f, outline_ref = self.approved_chain()
        _bp, d_ref = self._approved_blueprint_and_draft(outline_ref)
        before = self.wf.read(d_ref, "draft")

        self.put_framework(
            synthesis_ref, revision="fw-r2", parent="fw-r1",
            c1_definition="A revised Compound Z definition.",
        )
        with self.assertRaises(DraftingWorkflowError):
            self.wf.read(d_ref, "draft", current=True)
        report = self.wf.status(d_ref, "draft")
        self.assertTrue(report["stale"])
        self.assertTrue(report["reasons"])
        self.assertTrue(report["provisional"])
        self.assertFalse(report["authorizes_export"])
        self.assertFalse(report["final"])
        self.assertFalse(report["scientific_validation"])
        self.assertEqual(report["pending"], ["source_checks", "origin_tracing"])
        # The historical revision stays inspectable and unchanged.
        self.assertEqual(self.wf.read(d_ref, "draft", current=False), before)

    def test_new_blueprint_revision_stales_draft_and_history_inspectable(self):
        _s, _f, outline_ref = self.approved_chain()
        ws_ref = self.make_workspace(outline_ref)
        bp1 = self.propose_bp(self.blueprint_payload(ws_ref), revision="bp-r1")
        self.approve_bp(bp1)
        d_ref = self.propose_draft(self.draft_payload(bp1))
        before = self.wf.read(d_ref, "draft")

        # A new (unapproved) revision of the same blueprint artifact.
        self.propose_bp(self.blueprint_payload(ws_ref), revision="bp-r2", parent="bp-r1")
        with self.assertRaises(DraftingWorkflowError):
            self.wf.read(d_ref, "draft", current=True)
        self.assertTrue(self.wf.status(d_ref, "draft")["stale"])
        self.assertEqual(self.wf.read(d_ref, "draft", current=False), before)

    def test_edit_may_not_switch_blueprint_identity(self):
        _s, _f, outline_ref = self.approved_chain()
        ws1 = self.make_workspace(
            outline_ref, section_id="sec1", artifact_id="ws-sec1", revision="ws-sec1-r1"
        )
        bp1 = self.propose_bp(
            self.blueprint_payload(ws1, section_id="sec1"),
            artifact_id="bp-sec1", revision="bp-sec1-r1",
        )
        self.approve_bp(bp1)
        self.propose_draft(
            self.draft_payload(bp1), artifact_id="draft-sec1", revision="draft-sec1-r1"
        )
        # A distinct blueprint artifact may not be swapped in on an edit.
        planner2 = self.map_session("ses_planner_2", PLANNER_ROLE, model=_PLANNER_MODEL)
        writer2 = self.map_session("ses_writer_2", WRITER_ROLE, model=_WRITER_MODEL)
        ws3 = self.make_workspace(
            outline_ref, section_id="sec3", artifact_id="ws-sec3", revision="ws-sec3-r1",
            planner=planner2, writer=writer2,
        )
        bp3 = self.propose_bp(
            self.blueprint_payload(ws3, section_id="sec3"),
            artifact_id="bp-sec3", revision="bp-sec3-r1",
        )
        self.approve_bp(bp3)
        with self.assertRaises(DraftingWorkflowError):
            self.propose_draft(
                self.draft_payload(bp3),
                artifact_id="draft-sec1", revision="draft-sec1-r2", parent="draft-sec1-r1",
            )


class AssemblyBase(DraftBase):
    # -- two-section approved chain (a small assembly fixture) --------------- #
    def two_section_chain(self):
        audit_ref, _batch = self.accepted_audit()
        synthesis_ref = self.put_synthesis(audit_ref)
        self.approve_ref(synthesis_ref, GATE_SYNTHESIS_KIND, GATE_SYNTHESIS)
        framework_ref = self.put_framework(synthesis_ref)
        self.approve_ref(framework_ref, GATE_FRAMEWORK_KIND, GATE_FRAMEWORK)
        outline_content = {
            "schema_version": 1,
            "title": "Two-section outline",
            "sections": [
                {"section_id": "sec1", "order": 1, "title": "Background",
                 "concept_dependencies": ["C1"]},
                {"section_id": "sec2", "order": 2, "title": "Evidence",
                 "relationship_dependencies": ["R1"]},
            ],
        }
        outline_ref = self.synth.propose(
            "outline",
            {"synthesis_ref": synthesis_ref, "framework_ref": framework_ref,
             "content": outline_content},
            artifact_id="out", revision_id="out-r1", rights=self.rights("out", "out-r1"),
        )
        self.approve_ref(outline_ref, GATE_OUTLINE_KIND, GATE_OUTLINE)
        return synthesis_ref, framework_ref, outline_ref

    def approved_section_draft(self, outline_ref, section_id, *, n, content=None):
        planner = self.map_session(f"ses_planner_{n}", PLANNER_ROLE, model=_PLANNER_MODEL)
        writer = self.map_session(f"ses_writer_{n}", WRITER_ROLE, model=_WRITER_MODEL)
        ws_ref = self.make_workspace(
            outline_ref, section_id=section_id, artifact_id=f"ws-{section_id}",
            revision=f"ws-{section_id}-r1", planner=planner, writer=writer,
        )
        bp_ref = self.propose_bp(
            self.blueprint_payload(ws_ref, section_id=section_id),
            artifact_id=f"bp-{section_id}", revision=f"bp-{section_id}-r1",
        )
        self.approve_bp(bp_ref)
        d_ref = self.propose_draft(
            self.draft_payload(bp_ref, content=content),
            artifact_id=f"draft-{section_id}", revision=f"draft-{section_id}-r1",
        )
        return bp_ref, d_ref

    def propose_assembly(self, outline_ref, draft_refs, *, artifact_id="asm", revision="asm-r1"):
        return self.wf.propose(
            "assembly",
            {"outline_ref": outline_ref, "draft_refs": draft_refs},
            artifact_id=artifact_id, revision_id=revision,
            rights=self.rights(artifact_id, revision),
        )


class AssemblyOrderingTests(AssemblyBase):
    def test_assembly_covers_all_sections_in_outline_order(self):
        _s, _f, outline_ref = self.two_section_chain()
        _bp1, d1 = self.approved_section_draft(outline_ref, "sec1", n=1)
        _bp2, d2 = self.approved_section_draft(outline_ref, "sec2", n=2)
        # Caller order is reversed: the outline order must win.
        a_ref = self.propose_assembly(outline_ref, [d2, d1])
        envelope = self.wf.read(a_ref, "assembly")
        raw = json.loads(
            self.artifacts.read_content(a_ref["artifact_id"], a_ref["revision_id"])
        )
        self.assertEqual(raw["document_type"], ASSEMBLY_DOCUMENT_TYPE)
        result = envelope["result"]
        self.assertEqual(result["kind"], "manuscript_assembly")
        self.assertEqual([s["section_id"] for s in result["sections"]], ["sec1", "sec2"])
        self.assertEqual(result["sections"][0]["draft_ref"], d1)
        self.assertEqual(result["sections"][1]["draft_ref"], d2)
        self.assertEqual(result["status"], "provisional")
        self.assertFalse(result["authorizes_export"])
        self.assertFalse(result["final"])
        self.assertFalse(result["scientific_validation"])
        self.assertEqual(result["pending"], ["source_checks", "origin_tracing"])
        # Reopening reconstructs the exact same assembly.
        self._reopen()
        self.assertEqual(self.wf.read(a_ref, "assembly"), envelope)

    def test_missing_section_is_rejected(self):
        _s, _f, outline_ref = self.two_section_chain()
        _bp1, d1 = self.approved_section_draft(outline_ref, "sec1", n=1)
        with self.assertRaises(DraftingWorkflowError):
            self.propose_assembly(outline_ref, [d1], artifact_id="asm-bad", revision="asm-bad-r1")
        self.assertIsNone(self.artifacts.get_artifact("asm-bad"))

    def test_duplicate_section_is_rejected(self):
        _s, _f, outline_ref = self.two_section_chain()
        bp1, d1 = self.approved_section_draft(outline_ref, "sec1", n=1)
        _bp2, d2 = self.approved_section_draft(outline_ref, "sec2", n=2)
        # A second, distinct draft revision for the same section (sec1).
        d1b = self.propose_draft(
            self.draft_payload(bp1),
            artifact_id="draft-sec1-b", revision="draft-sec1-b-r1",
        )
        with self.assertRaises(DraftingWorkflowError):
            self.propose_assembly(
                outline_ref, [d1, d1b, d2],
                artifact_id="asm-bad", revision="asm-bad-r1",
            )
        self.assertIsNone(self.artifacts.get_artifact("asm-bad"))

    def test_repeated_draft_ref_is_rejected(self):
        _s, _f, outline_ref = self.two_section_chain()
        _bp1, d1 = self.approved_section_draft(outline_ref, "sec1", n=1)
        with self.assertRaises(DraftingWorkflowError):
            self.propose_assembly(
                outline_ref, [d1, d1], artifact_id="asm-bad", revision="asm-bad-r1"
            )
        self.assertIsNone(self.artifacts.get_artifact("asm-bad"))

    def test_wrong_outline_revision_is_rejected(self):
        synthesis_ref, framework_ref, outline_ref = self.two_section_chain()
        _bp1, d1 = self.approved_section_draft(outline_ref, "sec1", n=1)
        _bp2, d2 = self.approved_section_draft(outline_ref, "sec2", n=2)
        # Advance the outline to a new approved revision of the same artifact.
        outline_r2 = self.synth.propose(
            "outline",
            {"synthesis_ref": synthesis_ref, "framework_ref": framework_ref,
             "content": {
                 "schema_version": 1, "title": "Two-section outline (revised)",
                 "sections": [
                     {"section_id": "sec1", "order": 1, "title": "Background",
                      "concept_dependencies": ["C1"]},
                     {"section_id": "sec2", "order": 2, "title": "Evidence",
                      "relationship_dependencies": ["R1"]},
                 ],
             }},
            artifact_id="out", revision_id="out-r2", parent_revision_id="out-r1",
            rights=self.rights("out", "out-r2"),
        )
        self.approve_ref(outline_r2, GATE_OUTLINE_KIND, GATE_OUTLINE)
        # A draft over the new outline revision may not be mixed into out-r1.
        planner3 = self.map_session("ses_planner_3", PLANNER_ROLE, model=_PLANNER_MODEL)
        writer3 = self.map_session("ses_writer_3", WRITER_ROLE, model=_WRITER_MODEL)
        ws1b = self.make_workspace(
            outline_r2, section_id="sec1", artifact_id="ws-sec1b", revision="ws-sec1b-r1",
            planner=planner3, writer=writer3,
        )
        bp1b = self.propose_bp(
            self.blueprint_payload(ws1b, section_id="sec1"),
            artifact_id="bp-sec1b", revision="bp-sec1b-r1",
        )
        self.approve_bp(bp1b)
        d1b = self.propose_draft(
            self.draft_payload(bp1b), artifact_id="draft-sec1b", revision="draft-sec1b-r1"
        )
        with self.assertRaises(DraftingWorkflowError):
            self.propose_assembly(
                outline_ref, [d1b, d2],
                artifact_id="asm-bad", revision="asm-bad-r1",
            )
        self.assertIsNone(self.artifacts.get_artifact("asm-bad"))


class AssemblyLabelTests(AssemblyBase):
    def rich_blueprint_payload(self, ws_ref, section_id="sec1"):
        ws = self.workspace.read(ws_ref)["result"]
        package = DraftingPackageRepository(self.store).build(ws["outline_ref"], section_id)
        finding_key = package["evidence"][0]["finding_key"]

        def claim(claim_id, text, attribution, classification):
            return {
                "claim_id": claim_id,
                "text": text,
                "finding_keys": [finding_key],
                "attribution": attribution,
                "classification": classification,
                "applicability": _applicability(),
                "causal_limitations": _known("synthetic fixture"),
            }

        content = {
            "schema_version": 1,
            "section_id": section_id,
            "paragraphs": [
                {
                    "paragraph_id": "P1",
                    "intents": [
                        {"intent_id": "I1", "intent": "Report the effect.",
                         "claims": [claim("K1", "Compound Z did not reduce tumor volume in mice.",
                                          "direct_observation", "reported_evidence")]},
                        {"intent_id": "I2", "intent": "Attribute the secondary account.",
                         "claims": [claim("K2", "A secondary account repeats the reduction.",
                                          "secondary_account", "reported_evidence")]},
                        {"intent_id": "I3", "intent": "Offer a labelled author proposal.",
                         "claims": [claim("K3", "A shared mechanism may explain the effect.",
                                          "proposed_synthesis", "proposed_synthesis")]},
                    ],
                }
            ],
        }
        return {"workspace_ref": ws_ref, "content": content}

    def _rich_draft(self, outline_ref, *, n=1):
        planner = self.map_session(f"ses_planner_{n}", PLANNER_ROLE, model=_PLANNER_MODEL)
        writer = self.map_session(f"ses_writer_{n}", WRITER_ROLE, model=_WRITER_MODEL)
        ws_ref = self.make_workspace(
            outline_ref, section_id="sec1", artifact_id="ws-sec1",
            revision="ws-sec1-r1", planner=planner, writer=writer,
        )
        bp_ref = self.propose_bp(
            self.rich_blueprint_payload(ws_ref, "sec1"),
            artifact_id="bp-sec1", revision="bp-sec1-r1",
        )
        self.approve_bp(bp_ref)
        return self.propose_draft(
            self.draft_payload(bp_ref), artifact_id="draft-sec1", revision="draft-sec1-r1"
        )

    def test_provisional_labels_and_corpus_citations(self):
        _s, _f, outline_ref = self.two_section_chain()
        d1 = self._rich_draft(outline_ref, n=1)
        _bp2, d2 = self.approved_section_draft(outline_ref, "sec2", n=2)
        a_ref = self.propose_assembly(outline_ref, [d1, d2])
        result = self.wf.read(a_ref, "assembly")["result"]

        markdown = result["markdown"]
        # The provisional header and the pending checks are visibly labelled.
        self.assertIn("PROVISIONAL", markdown)
        self.assertIn("source checks", markdown.lower())
        self.assertIn("origin tracing", markdown.lower())
        # Per-sentence attribution/proposal labels are visible.
        self.assertIn("secondary account", markdown)
        self.assertIn("author proposal", markdown)

        sec1 = result["sections"][0]
        sentences = {s["intent_id"]: s for p in sec1["paragraphs"] for s in p["sentences"]}
        self.assertIn("secondary account", sentences["I2"]["labels"])
        self.assertIn("author proposal", sentences["I3"]["labels"])
        # The labels are derived from the schema metadata (attributions).
        self.assertIn("secondary_account", sentences["I2"]["attributions"])
        self.assertTrue(sentences["I3"]["proposed_synthesis"])

        # Citations use only the stable verified-corpus keys of the package.
        package = DraftingPackageRepository(self.store).build(outline_ref, "sec1")
        expected_key = citation_key(
            self.project_id, self.cands["111"]
        )
        self.assertEqual(list(result["citations"]), [expected_key])
        self.assertEqual(list(package["citations"]), [expected_key])
        self.assertIn(expected_key, markdown)
        # No model bibliography / title is present -- only the stable key record.
        self.assertEqual(
            result["citations"][expected_key]["citation_key"], expected_key
        )


class DraftOverRealRunnerTest(_drafting_package.DraftingOverRealRunnerTest):
    """Real runner / fake transport: real receipt -> package -> gated draft.

    Extends the integrated ``DraftingOverRealRunnerTest`` (no fixture is
    replicated); the inherited full-flow cases are deselected in the parent so the
    giant fixture runs once.  No live model/network call is made.
    """

    # Deselect the inherited package case so the giant fixture runs only for the
    # draft integration case here.
    test_package_from_real_verified_receipts = None

    def setUp(self):
        super().setUp()
        self.workspace = SectionWorkspaceRepository(self.store)
        self.wf = DraftingWorkflowRepository(self.store)
        self._role_config = _role_config()

    def test_provisional_draft_from_real_receipts(self):
        src, acquisition_ref = self._acquire()
        self._config_and_report(src)
        pilot_ref = self._pilot()
        batch_ref, _cases = self._bulk_case(pilot_ref, acquisition_ref)
        audit_ref = self.workflow.propose(
            "audit", {"batch_ref": batch_ref, "checks": {"case-1": self._check(important_error=False)}},
            artifact_id="art-audit", revision_id="rev-audit",
            rights=self.rights("art-audit", "rev-audit"),
        )
        self.approve("art-audit", "rev-audit", extraction_workflow.GATE_KIND, extraction_workflow.GATE_OUTPUTS)

        accepted = self.workflow.require_batch(batch_ref)["cases"]
        finding_keys = [
            f"{case['id']}:{finding['finding_id']}"
            for case in accepted
            for finding in case["extraction"]["findings"]
        ]
        self.assertTrue(finding_keys)
        dispositions = [
            {"finding": key, "disposition": "null_evidence", "rationale": "integration context"}
            for key in finding_keys
        ]
        synthesis_ref = self.synth.propose(
            "synthesis",
            {"audit_ref": audit_ref, "content": {
                "schema_version": 1, "memo": "integration memo", "statements": [],
                "dispositions": dispositions,
            }},
            artifact_id="art-syn", revision_id="rev-syn",
            rights=self.rights("art-syn", "rev-syn"),
        )
        self.approve("art-syn", "rev-syn", GATE_SYNTHESIS_KIND, GATE_SYNTHESIS)

        framework = {
            "schema_version": 1,
            "title": "Integration framework",
            "contribution": "organizes the accepted evidence",
            "criteria": {
                "contribution": "organizing relationship",
                "admission": "definition, role, support, counterevidence, limits",
                "support": "accepted finding keys",
                "revision": "changed definitions reopen dependent sections",
            },
            "concepts": [{
                "concept_id": "C1", "definition": "Integration concept.",
                "role": "central outcome", "original_support": finding_keys,
                "counterevidence_note": "no counterevidence identified",
                "applicability_limits": "synthetic corpus only",
                "testable_implication": "a repeat run would reproduce the effect",
                "status": "proposed", "novelty": "existing",
            }],
        }
        framework_ref = self.synth.propose(
            "framework", {"synthesis_ref": synthesis_ref, "content": framework},
            artifact_id="art-fw", revision_id="rev-fw", rights=self.rights("art-fw", "rev-fw"),
        )
        self.approve("art-fw", "rev-fw", GATE_FRAMEWORK_KIND, GATE_FRAMEWORK)
        outline = {
            "schema_version": 1, "title": "Integration outline",
            "sections": [{"section_id": "sec1", "order": 1, "title": "Evidence",
                          "concept_dependencies": ["C1"]}],
        }
        outline_ref = self.synth.propose(
            "outline", {"synthesis_ref": synthesis_ref, "framework_ref": framework_ref,
                        "content": outline},
            artifact_id="art-out", revision_id="rev-out", rights=self.rights("art-out", "rev-out"),
        )
        self.approve("art-out", "rev-out", GATE_OUTLINE_KIND, GATE_OUTLINE)

        package = DraftingPackageRepository(self.store).build(outline_ref, "sec1")
        self.assertTrue(package["evidence"])

        from review_article_agent.storage.agent_runtime import RuntimeRepository

        planner = RuntimeRepository(self.store).record_session_mapping(
            service_instance_id="svc_1", opencode_project_id="oc_proj_1",
            clean_location=str(self.tmp / "clean"), session_id="ses_planner_1",
            role=PLANNER_ROLE, model=_PLANNER_MODEL, configuration=self._role_config,
        )
        writer = RuntimeRepository(self.store).record_session_mapping(
            service_instance_id="svc_1", opencode_project_id="oc_proj_1",
            clean_location=str(self.tmp / "clean"), session_id="ses_writer_1",
            role=WRITER_ROLE, model=_WRITER_MODEL, configuration=self._role_config,
        )
        ws_ref = self.workspace.propose(
            {"outline_ref": outline_ref, "section_id": "sec1",
             "planner_mapping_id": planner.mapping_id, "writer_mapping_id": writer.mapping_id},
            artifact_id="ws-sec1", revision_id="ws-sec1-r1",
            rights=self.rights("ws-sec1", "ws-sec1-r1"),
        )
        finding_key = package["evidence"][0]["finding_key"]
        blueprint_content = {
            "schema_version": 1,
            "section_id": "sec1",
            "paragraphs": [{
                "paragraph_id": "P1",
                "intents": [{
                    "intent_id": "I1",
                    "intent": "Report the grounded finding.",
                    "claims": [{
                        "claim_id": "K1",
                        "text": "Reported: integration finding.",
                        "finding_keys": [finding_key],
                        "attribution": "direct_observation",
                        "classification": "reported_evidence",
                        "applicability": {
                            key: {"state": "not_applicable", "value": None}
                            for key in ("species", "population", "assay", "outcome", "conditions")
                        },
                        "causal_limitations": {"state": "not_applicable", "value": None},
                    }],
                }],
            }],
        }
        bp_ref = self.wf.propose(
            "blueprint", {"workspace_ref": ws_ref, "content": blueprint_content},
            artifact_id="bp-sec1", revision_id="bp-sec1-r1",
            rights=self.rights("bp-sec1", "bp-sec1-r1"),
        )
        self.approve("bp-sec1", "bp-sec1-r1", "artifact_revision", "drafting.blueprint")

        blueprint = self.wf.read(bp_ref)["result"]
        draft_content = {
            "schema_version": 1,
            "section_id": "sec1",
            "paragraphs": [{
                "paragraph_id": "P1",
                "sentences": [{
                    "intent_id": "I1",
                    "claim_ids": ["K1"],
                    "text": blueprint["paragraphs"][0]["intents"][0]["claims"][0]["text"],
                }],
            }],
        }
        d_ref = self.wf.propose(
            "draft", {"blueprint_ref": bp_ref, "content": draft_content},
            artifact_id="draft-sec1", revision_id="draft-sec1-r1",
            rights=self.rights("draft-sec1", "draft-sec1-r1"),
        )
        envelope = self.wf.read(d_ref, "draft")
        self.assertEqual(envelope["result"]["kind"], "section_draft")
        self.assertEqual(envelope["result"]["status"], "provisional")
        self.assertFalse(envelope["result"]["authorizes_export"])
        # The real verified receipt's per-passage source ref feeds the package.
        self.assertFalse(package["evidence"][0]["original_passage"]["source_ref_fallback"])
        self.assertEqual(package["evidence"][0]["citation_keys"], [citation_key(self.project_id, self.cands["111"])])


if __name__ == "__main__":
    import unittest

    unittest.main()
