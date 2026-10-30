"""MVP end-to-end integration test: ``pipeline run`` from accepted extraction
to the delivery final-approval gate (kickoff 16 §6 and §8).

One disposable synthetic project store is driven end-to-end by **repeated
``pipeline run``** (in-process, over the real worker engine) plus **scripted
owner-authenticated gate approvals**, using the deterministic fake model
transport.  It replaces the hand-glued Step 15 e2e chains for the MVP path and
closes the Step 08 "extraction -> synthesis" seam: synthesis consumes the
**real** Step 09 receipts from a real ``extraction.runner.execute_run`` (fake
transport returning schema-valid extraction/quality JSON), not the synthetic
receipt fixture.

The corpus is a **primary-source** JATS fixture (its passage carries an explicit
own-result cue, ``in this study we found``) so the deterministic origin tracing
reaches ``origin_traced`` at depth 0 and the delivery stage is ``export_ready``
without the secondary-attribution warning the old synthetic fixture produced.

Manual (non-``pipeline run``) steps, performed here with the documented
library calls (kickoff 16 §6: extraction and screening are not automatic because
they need human pilot design):

* **acquisition + parsing** -- the frozen corpus + approved acquisition manifest
  are built through the existing ``ManifestBase`` builders (no network).
* **extraction config + hosted-use + pilot** -- ``extraction.runner.execute_run``
  in ``pilot`` mode (extraction + quality) with the real config/pilot gates
  (``ExtractionWorkflowRepository``), via the inherited ``_pilot`` helper.
* **bulk extraction + quality** -- ``execute_run`` in ``bulk`` mode, then the
  batch gate (``_bulk_case``).
* **extraction output audit** -- ``ExtractionWorkflowRepository.propose("audit")``
  + the ``extraction.outputs`` gate.
* **hosted-use documents** -- one ``generation_hosted_use`` document + named gate
  per generation role (``generation.hosted_use.<role>``).
* **delivery freshness** -- ``DeliveryRepository.freshness`` (an explicit human
  cutoff decision, never automatic).
* **delivery final approval** -- the exact-revision ``artifact_revision`` /
  ``delivery.final_approval`` owner gate, recorded with the same scripted
  owner-authenticated helper the other tests use.

Remaining fixtures: the frozen corpus / discovery snapshot and the synthetic
extraction JSON shapes are the existing shared fixtures
(``test_acquisition_manifest`` / ``test_extraction_runner``); the JATS bytes are
synthetic and uncopyrighted.  No live model, network, provider, or OpenCode
session call is made.

The delivery manuscript is now recorded at ``delivery start`` from the
**post-review** section drafts (``DeliveryRepository.start_global_review`` reads
each section's ``review_id`` and renders the final reviewed draft as Markdown
with Pandoc ``[@<cit_key>]`` citations).  The full tail is therefore reachable:
final approval -> ``delivery export`` writes the seven files -> ``pipeline
status`` reports ``complete`` only after the export.

Run::

    python3 scripts/run_tests.py --slow -j 4 test_mvp_pipeline_e2e
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _path in (str(_HERE.parent / "src"), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import test_acquisition_validation as av  # noqa: E402
import test_extraction_runner as extraction_runner  # noqa: E402
import test_generation_runner as gen_runner  # noqa: E402
from test_drafting_workflow import (  # noqa: E402
    GATE_ID as BP_GATE_ID,
    GATE_KIND as BP_GATE_KIND,
    blueprint_content,
)
from test_generation_drafting_roles import _draft_content  # noqa: E402
from test_pipeline_run import _model_framework_content  # noqa: E402
from test_synthesis_workflow import (  # noqa: E402
    GATE_FRAMEWORK,
    GATE_FRAMEWORK_KIND,
    GATE_OUTLINE,
    GATE_OUTLINE_KIND,
    GATE_SYNTHESIS,
    GATE_SYNTHESIS_KIND,
    SynthesisBase,
)
from test_tracing_workflow import POLICY  # noqa: E402

from review_article_agent.delivery.workflow import (  # noqa: E402
    FINAL_APPROVAL_GATE_ID,
    FINAL_APPROVAL_GATE_KIND,
    DeliveryRepository,
)
from review_article_agent.drafting.package import DraftingPackageRepository  # noqa: E402
from review_article_agent.drafting.workflow import DraftingWorkflowRepository  # noqa: E402
from review_article_agent.drafting.workspace import SectionWorkspaceRepository  # noqa: E402
from review_article_agent.extraction import workflow as extraction_workflow  # noqa: E402
from review_article_agent.generation import hosted_use  # noqa: E402
from review_article_agent.pipeline import status as pipeline_status  # noqa: E402
from review_article_agent.pipeline.run import (  # noqa: E402
    PipelineConfig,
    delivery_id_for,
    review_id_for,
    run,
    trace_id_for,
)
from review_article_agent.review.workflow import SectionReviewRepository  # noqa: E402
from review_article_agent.runtime.jobs import JobRepository  # noqa: E402
from review_article_agent.storage.artifacts import ArtifactRepository  # noqa: E402
from review_article_agent.tracing.workflow import (  # noqa: E402
    OriginTraceRepository,
    origin_coverage,
)
from review_article_agent.worker.engine import WorkerEngine  # noqa: E402

from _tiers import slow  # noqa: E402

_CREDENTIAL_ENV = "OPENROUTER_API_KEY"
_CRITIC_MODEL = "deepseek/deepseek-v4.1-flash"
_REPAIR_MODEL = "anthropic/claude-sonnet-5-5"

#: The real extraction case id and its single finding (one primary passage).
_CASE = "case-1"
_FINDING = f"{_CASE}:F1"
#: The synthetic fixture's finding key that the shared content builders emit.
_FIXTURE_FINDING = "c1:F1"

#: A primary-source JATS body whose claim-relevant passage carries an explicit
#: own-result cue, so origin tracing classifies depth 0 as ``own_result`` ->
#: ``origin_traced`` (no secondary warning, ``export_ready``).
_PRIMARY_BODY = (
    b"<sec><p>In this study we found that compound Z did not reduce tumor "
    b"volume in mice.</p></sec>"
)
_PRIMARY_JATS = av.jats(pmid=b"111", pmcid=None, doi=None, body=_PRIMARY_BODY)


def _rebind(value):
    """Recursively replace the synthetic fixture finding key with the real one."""
    if isinstance(value, dict):
        return {key: _rebind(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_rebind(item) for item in value]
    if isinstance(value, str):
        return value.replace(_FIXTURE_FINDING, _FINDING)
    return value


def _synthesis_content():
    return _rebind(SynthesisBase.synthesis_content())


def _framework_content():
    return _rebind(_model_framework_content())


@slow
class MvpPipelineE2ETest(extraction_runner.FullFlowIntegrationTest):
    """One disposable store: real extraction -> ``pipeline run`` -> gate."""

    # The inherited full-flow cases are deselected; this module contributes the
    # one MVP pipeline case below (the giant fixture runs once, in its own module).
    test_positive_full_flow_to_synthesis = None
    test_important_audit_pauses_exact_batch_until_new_batch = None
    test_presend_revalidates_revoked_pilot = None
    test_bulk_source_not_in_manifest_membership_blocked = None

    _EXTRA_HOSTED = {
        "synthesis": ("hu-synthesis", None),
        "framework": ("hu-framework", None),
        "outline": ("hu-outline", None),
        "section_critic": ("hu-critic", _CRITIC_MODEL),
        "section_repair": ("hu-repair", _REPAIR_MODEL),
        "global_reviewer": ("hu-greviewer", _CRITIC_MODEL),
        "global_writer": ("hu-gwriter", _REPAIR_MODEL),
        "notes_planner": ("hu-notes", None),
    }

    def setUp(self) -> None:
        super().setUp()
        hosted_roles = [
            (artifact_id, role, model)
            for role, (artifact_id, model) in self._EXTRA_HOSTED.items()
        ]
        hosted_roles += [
            ("hu-blueprint", "blueprint", None),
            ("hu-writer", "section_writer", None),
        ]
        for artifact_id, role, model in hosted_roles:
            document = (
                gen_runner._document(role)
                if model is None
                else gen_runner._document(role, model=model)
            )
            ref = hosted_use.propose_hosted_use(
                self.store,
                document,
                artifact_id=artifact_id,
                revision_id=f"{artifact_id}-r1",
                rights=self.rights(artifact_id, f"{artifact_id}-r1"),
            )
            self.approve(
                ref["artifact_id"],
                ref["revision_id"],
                hosted_use.HOSTED_USE_GATE_KIND,
                hosted_use.hosted_use_gate_id(role),
            )

    # -- real extraction (manual steps) -------------------------------------- #
    def _acquire_primary(self):
        """Freeze the corpus and approve a manifest over the primary JATS source."""
        self.setup_frozen()
        primary = self.cands["111"]
        excluded = self.cands["222"]
        src = self.put_source(_PRIMARY_JATS)
        entries = [
            self.entry(primary, source=src),
            self.entry(excluded, "exclude", rationale="out of scope after reading abstract"),
        ]
        revision = self.make(entries)
        self.gate(revision)
        self.acq.require_acquired()
        return src, self.ref_of(revision)

    def _accepted_audit(self):
        """Run real extraction (pilot + bulk) and approve the output audit."""
        src, acquisition_ref = self._acquire_primary()
        self._config_and_report(src)
        pilot_ref = self._pilot()
        batch_ref, _cases = self._bulk_case(pilot_ref, acquisition_ref)
        audit_ref = self.workflow.propose(
            "audit",
            {"batch_ref": batch_ref, "checks": {_CASE: self._check(important_error=False)}},
            artifact_id="art-audit",
            revision_id="rev-audit",
            rights=self.rights("art-audit", "rev-audit"),
        )
        self.approve(
            "art-audit",
            "rev-audit",
            extraction_workflow.GATE_KIND,
            extraction_workflow.GATE_OUTPUTS,
        )
        self._extraction_posts = self.transport.call_count
        return audit_ref

    # -- pipeline driving ---------------------------------------------------- #
    def config_mapping(self) -> dict:
        return {
            "credential_env": _CREDENTIAL_ENV,
            "sections": ["sec1"],
            "roles": {
                "synthesis": {
                    "hosted_artifact_id": "hu-synthesis",
                    "target_artifact_id": "syn",
                    "target_revision_id": "syn-r1",
                },
                "framework": {
                    "hosted_artifact_id": "hu-framework",
                    "target_artifact_id": "fw",
                    "target_revision_id": "fw-r1",
                },
                "outline": {
                    "hosted_artifact_id": "hu-outline",
                    "target_artifact_id": "out",
                    "target_revision_id": "out-r1",
                },
                "blueprint": {
                    "hosted_artifact_id": "hu-blueprint",
                    "target_artifact_id": "bp-{section_id}",
                    "target_revision_id": "bp-{section_id}-r1",
                },
                "section_writer": {
                    "hosted_artifact_id": "hu-writer",
                    "target_artifact_id": "draft-{section_id}",
                    "target_revision_id": "draft-{section_id}-r1",
                },
                "section_critic": {"hosted_artifact_id": "hu-critic"},
                "section_repair": {"hosted_artifact_id": "hu-repair"},
                "global_reviewer": {"hosted_artifact_id": "hu-greviewer"},
                "global_writer": {"hosted_artifact_id": "hu-gwriter"},
                "notes_planner": {"hosted_artifact_id": "hu-notes"},
            },
            "workspace_artifact_id": "ws-{section_id}",
            "workspace_revision_id": "ws-{section_id}-r1",
            "tracing_policy": dict(POLICY),
            "rights": gen_runner._rights(self.project_id, "template", "t0"),
        }

    def pipeline_config(self) -> PipelineConfig:
        return PipelineConfig.from_mapping(self.config_mapping())

    def engine(self) -> WorkerEngine:
        engine = WorkerEngine(
            str(self.tmp / "app"),
            registry=self.registry,
            generation_transport=self.transport,
            generation_api_key=gen_runner._API_KEY,
        )
        self.addCleanup(engine.close)
        return engine

    def drive(self, engine: WorkerEngine, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            hint = engine.tick()
            if hint is None:
                return
            if hint > 0:
                time.sleep(min(hint, 0.02))
        self.fail("engine did not settle within the timeout")

    def run_once(self, engine: WorkerEngine, config: PipelineConfig) -> dict:
        return run(
            self.store,
            self.registry,
            config=config,
            submit=lambda request: engine.handle(request),
            environ={_CREDENTIAL_ENV: "x"},
        )

    def settle(self, engine: WorkerEngine, response) -> None:
        self.transport.responses.append(response)
        self.drive(engine)

    def job_count(self) -> int:
        return len(JobRepository(self.store).list())

    def ref(self, artifact_id: str, revision_id: str) -> dict:
        return {
            "project_id": self.project_id,
            "artifact_id": artifact_id,
            "revision_id": revision_id,
        }

    def workspace_ref(self, section_id="sec1") -> dict:
        return self.ref(f"ws-{section_id}", f"ws-{section_id}-r1")

    def _write_rights_file(self, artifact_id="delivery", revision_id="r1") -> str:
        path = self.tmp / "delivery-rights.json"
        path.write_text(json.dumps(self.rights(artifact_id, revision_id)), encoding="utf-8")
        return str(path)

    def _run_cli(self, *args: str):
        import review_article_agent.cli.main as cli_main

        out, err = io.StringIO(), io.StringIO()
        code = cli_main.main(
            ["--data-dir", str(self.tmp / "registry"), *args],
            env={},
            stdout=out,
            stderr=err,
        )
        return code, out.getvalue(), err.getvalue()

    # -- the one end-to-end case --------------------------------------------- #
    def test_pipeline_run_from_extraction_to_final_approval_gate(self):
        audit_ref = self._accepted_audit()
        # The real Step 09 receipt is consumable by synthesis.
        accepted = extraction_workflow.ExtractionWorkflowRepository(
            self.store
        ).require_for_synthesis(audit_ref)
        self.assertTrue(accepted["accepted"])
        self.assertEqual(accepted["case_ids"], [_CASE])

        config = self.pipeline_config()
        engine = self.engine()

        # --- synthesis.memo ------------------------------------------------- #
        first = self.run_once(engine, config)
        self.assertEqual(first["stage"], "synthesis.memo")
        self.assertEqual(len(first["submitted"]), 1)
        request_id = first["submitted"][0]

        # Idempotence: re-running at the gate submits nothing new.
        again = self.run_once(engine, config)
        self.assertEqual(again["submitted"], [])
        self.assertEqual(again["already_done"], [request_id])
        self.assertEqual(self.job_count(), 1)

        self.settle(engine, gen_runner._completion(_synthesis_content()))
        gate = self.run_once(engine, config)
        self.assertEqual(gate["submitted"], [])
        self.assertEqual(gate["stopped_at"]["kind"], "human_gate")
        self.assertIn("gate confirm", gate["next_command"])
        self.assertIn("--gate-id synthesis.memo", gate["next_command"])
        self.approve("syn", "syn-r1", GATE_SYNTHESIS_KIND, GATE_SYNTHESIS)

        # --- synthesis.framework -------------------------------------------- #
        framework = self.run_once(engine, config)
        self.assertEqual(framework["stage"], "synthesis.framework")
        self.assertEqual(len(framework["submitted"]), 1)
        self.settle(engine, gen_runner._completion(_framework_content()))
        gate = self.run_once(engine, config)
        self.assertEqual(gate["stopped_at"]["kind"], "human_gate")
        self.assertIn("--gate-id synthesis.framework", gate["next_command"])
        self.approve("fw", "fw-r1", GATE_FRAMEWORK_KIND, GATE_FRAMEWORK)

        # --- synthesis.outline ---------------------------------------------- #
        outline = self.run_once(engine, config)
        self.assertEqual(outline["stage"], "synthesis.outline")
        self.assertEqual(len(outline["submitted"]), 1)
        self.settle(engine, gen_runner._completion(SynthesisBase.outline_content()))
        gate = self.run_once(engine, config)
        self.assertEqual(gate["stopped_at"]["kind"], "human_gate")
        self.assertIn("--gate-id synthesis.outline", gate["next_command"])
        self.approve("out", "out-r1", GATE_OUTLINE_KIND, GATE_OUTLINE)

        # --- drafting.blueprint (workspace + blueprint) --------------------- #
        blueprint = self.run_once(engine, config)
        self.assertEqual(blueprint["stage"], "drafting.blueprint")
        self.assertEqual(len(blueprint["submitted"]), 1)
        workspace = SectionWorkspaceRepository(self.store).read(self.workspace_ref())["result"]
        package = DraftingPackageRepository(self.store).build(
            workspace["outline_ref"], workspace["section_id"]
        )
        self.assertEqual(package["evidence"][0]["finding_key"], _FINDING)
        self.settle(
            engine,
            gen_runner._completion(blueprint_content(section_id="sec1", finding_key=_FINDING)),
        )
        gate = self.run_once(engine, config)
        self.assertEqual(gate["stopped_at"]["kind"], "human_gate")
        self.assertIn("--gate-id drafting.blueprint", gate["next_command"])
        self.approve("bp-sec1", "bp-sec1-r1", BP_GATE_KIND, BP_GATE_ID)

        # --- review.sections (draft, then section review) ------------------- #
        draft = self.run_once(engine, config)
        self.assertEqual(draft["stage"], "review.sections")
        self.assertEqual(len(draft["submitted"]), 1)
        blueprint_result = DraftingWorkflowRepository(self.store).read(
            self.ref("bp-sec1", "bp-sec1-r1")
        )["result"]
        self.settle(engine, gen_runner._completion(_draft_content(blueprint_result)))
        review = self.run_once(engine, config)
        self.assertEqual(len(review["submitted"]), 1)
        self.settle(engine, gen_runner._completion({"findings": []}))
        review_state = SectionReviewRepository(self.store).read(
            review_id_for("sec1", "draft-sec1-r1")
        )
        self.assertIsNotNone(review_state)
        self.assertEqual(review_state["status"], "complete")
        self.assertTrue(review_state["checks"])
        self.assertTrue(
            all(check["verdict"] == "supported" for check in review_state["checks"])
        )

        # --- tracing.origins (deterministic, local) ------------------------- #
        self.run_once(engine, config)
        trace_state = OriginTraceRepository(self.store).read(
            trace_id_for("sec1", "draft-sec1-r1")
        )
        self.assertIsNotNone(trace_state)
        coverage = origin_coverage(trace_state)
        self.assertTrue(coverage["complete"])
        # The primary source is own-result at depth 0: no secondary warning.
        self.assertEqual(trace_state["claims"][0]["outcome"], "origin_traced")
        self.assertFalse(trace_state["claims"][0]["requires_final_human_approval"])
        self.assertTrue(coverage["export_ready"])

        # --- delivery: start + global review -------------------------------- #
        self.run_once(engine, config)
        delivery_id = delivery_id_for(self.project_id, ("sec1",))
        self.assertIsNotNone(DeliveryRepository(self.store).read(delivery_id))
        self.settle(engine, gen_runner._completion({"findings": []}))
        state = DeliveryRepository(self.store).read(delivery_id)
        self.assertEqual(state["global_review"]["status"], "complete")

        # --- freshness (manual human cutoff) -------------------------------- #
        DeliveryRepository(self.store).freshness(
            delivery_id, "2026-01-01", {"action": "accept_cutoff"}, rights=self.rights
        )

        # --- delivery approve --prepare -> final-approval gate -------------- #
        final = self.run_once(engine, config)
        self.assertEqual(final["stopped_at"]["kind"], "human_gate")
        self.assertIn("gate confirm", final["next_command"])
        self.assertIn("delivery.final_approval", final["next_command"])
        manifest_revision = ArtifactRepository(self.store).current_revision(
            delivery_id
        ).revision_id
        self.approve(
            delivery_id, manifest_revision, FINAL_APPROVAL_GATE_KIND, FINAL_APPROVAL_GATE_ID
        )
        self.assertTrue(
            DeliveryRepository(self.store).final_approval_gate_satisfied(delivery_id)
        )

        # --- delivery manuscript recorded from the reviewed sections -------- #
        # The manuscript is built from the post-review section drafts and recorded
        # as a durable, content-bound revision at delivery start, so the export
        # below is reachable.
        state = DeliveryRepository(self.store).read(delivery_id)
        manuscript = state.get("manuscript") or {}
        self.assertIsNotNone(manuscript.get("revision_id"))
        self.assertTrue(state.get("paragraphs"))
        self.assertIn("[@cit_", manuscript.get("text", ""))

        # --- status: final approval alone is not completion ----------------- #
        pending = pipeline_status.resolve(self.store)
        self.assertEqual(pending["stage"], "delivery")
        self.assertEqual(pending["blocked_on"]["kind"], "human")
        self.assertIn("export pending", pending["blocked_on"]["detail"])
        self.assertIn("delivery export", pending["blocked_on"]["next_command"])

        # --- delivery export (in-process CLI) writes the package ------------ #
        out_dir = self.tmp / "export"
        code, out, err = self._run_cli(
            "delivery", "export", "--project", self.project_id,
            "--delivery-id", delivery_id, "--out-dir", str(out_dir),
            "--rights-file", self._write_rights_file(),
        )
        self.assertEqual(code, 0, err)
        exported = json.loads(out)
        self.assertEqual(exported["status"], "exported")
        self.assertTrue(exported["consistency_ok"])

        expected = {
            "manuscript.md",
            "references.bib",
            "references.ris",
            "companion.md",
            "summary.md",
            "ai_declaration.md",
            "export_manifest.json",
        }
        self.assertEqual(set(os.listdir(out_dir)), expected)

        # The manifest hashes match the bytes actually written (the manifest
        # covers the six content files, not itself).
        manifest = json.loads(
            (out_dir / "export_manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(set(manifest["file_hashes"]), expected - {"export_manifest.json"})
        self.assertEqual(manifest["manuscript_revision"], manuscript["revision_id"])
        for name, digest in manifest["file_hashes"].items():
            content = (out_dir / name).read_text(encoding="utf-8")
            self.assertEqual(
                hashlib.sha256(content.encode("utf-8")).hexdigest(),
                digest,
                f"recorded hash mismatch for {name}",
            )

        # The bibliography/RIS carry the fixture's real frozen-corpus metadata.
        bib = (out_dir / "references.bib").read_text(encoding="utf-8")
        ris = (out_dir / "references.ris").read_text(encoding="utf-8")
        self.assertIn("title = {A study of things}", bib)
        self.assertIn("author = {Ada Lovelace and Alan Turing}", bib)
        self.assertIn("journal = {Journal of Synthetic Studies}", bib)
        self.assertIn("year = {2020}", bib)
        self.assertIn("TI  - A study of things", ris)
        self.assertIn("AU  - Ada Lovelace", ris)
        self.assertIn("AU  - Alan Turing", ris)
        self.assertIn("JO  - Journal of Synthetic Studies", ris)
        self.assertIn("PY  - 2020", ris)

        # The manuscript text cites the reviewed draft's verified-corpus keys as
        # Pandoc citations, which the export reader recognises.
        manuscript_md = (out_dir / "manuscript.md").read_text(encoding="utf-8")
        self.assertRegex(manuscript_md, r"\[@cit_[0-9a-f]{32}\]")

        # --- status is complete only after the export ----------------------- #
        self.assertEqual(pipeline_status.resolve(self.store)["stage"], "complete")

        # Every generation job made exactly one POST (no duplicate submissions).
        generation_posts = self.transport.call_count - self._extraction_posts
        self.assertEqual(generation_posts, self.job_count())


if __name__ == "__main__":  # pragma: no cover
    import unittest

    unittest.main()
