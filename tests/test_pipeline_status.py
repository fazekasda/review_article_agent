"""Tests for the read-only ``pipeline status`` resolver (kickoff 16 §6).

Covers ``src/review_article_agent/pipeline/status.py`` and the ``pipeline`` CLI
group over the *real* integrated stage repositories:

* an empty project reports the first stage (``discovery.plan``) with a next
  action and nothing blocking;
* a frozen corpus with no acquisition reports the ``acquisition.manifest`` stage;
* an approved synthesis/framework chain with no outline approval is blocked on
  the outline gate with the exact ``gate confirm`` command;
* reviewed drafts advance to origin tracing;
* resolving twice is idempotent (equal output, unchanged artifact list);
* a new outline revision after approval reports the outline gate again (stale);
* the in-process ``pipeline status`` CLI prints the same JSON.

The heavy scenarios reuse the existing integrated fixtures
(``test_discovery_freeze.FreezeTestBase``,
``test_synthesis_workflow.SynthesisBase``, ``test_drafting_drafts.DraftBase`` and
the review fakes from ``test_review_workflow``).  No model, network, provider, or
runner call is made.

Run::

    python3 scripts/run_tests.py --slow -j 4 test_pipeline_status
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from _tiers import slow

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

import review_article_agent.cli.main as cli_main  # noqa: E402
from review_article_agent.pipeline.status import STAGES, resolve  # noqa: E402
from review_article_agent.review.workflow import SectionReviewRepository  # noqa: E402
from review_article_agent.storage import (  # noqa: E402
    ArtifactRepository,
    ProjectStore,
    Registry,
    paths,
)
from review_article_agent.synthesis.workflow import (  # noqa: E402
    GATE_FRAMEWORK,
    GATE_FRAMEWORK_KIND,
    GATE_OUTLINE,
    GATE_OUTLINE_KIND,
    GATE_SYNTHESIS,
    GATE_SYNTHESIS_KIND,
)
from test_drafting_drafts import DraftBase  # noqa: E402
from test_discovery_freeze import FreezeTestBase  # noqa: E402
from test_review_workflow import _CRITIC, _WRITER, FakeCritic, FakeWriter  # noqa: E402
from test_synthesis_workflow import SynthesisBase  # noqa: E402


def _artifact_signature(store) -> list:
    """A stable signature of every artifact's current revision (id + digest)."""
    repo = ArtifactRepository(store)
    signature = []
    for artifact in repo.list_artifacts():
        head = repo.current_revision(artifact.artifact_id)
        signature.append(
            (
                artifact.artifact_id,
                None if head is None else head.revision_id,
                None if head is None else head.content_sha256,
            )
        )
    return sorted(signature)


def _public(result: dict) -> dict:
    """The resolver's public fields (excluding the CLI-only ``command`` key)."""
    return {key: result[key] for key in ("stage", "completed", "blocked_on", "next_action", "warnings")}


class EmptyProjectStageTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-pipe-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.project_id = paths.new_project_id()
        self.store = ProjectStore.create_at(
            paths.app_dir_for(str(self.tmp / "project")), self.project_id
        )
        self.addCleanup(self.store.close)

    def test_empty_project_reports_first_stage_with_next_action(self):
        result = resolve(self.store)
        self.assertEqual(result["stage"], "discovery.plan")
        self.assertEqual(result["completed"], [])
        self.assertIsNone(result["blocked_on"])
        self.assertEqual(result["next_action"]["kind"], "human")
        self.assertEqual(
            result["next_action"]["inputs"]["command"], "discovery context import"
        )
        self.assertFalse(result["authorizes_execution"])
        self.assertFalse(result["scientific_validation"])

    def test_stage_table_is_ordered_and_unique(self):
        ids = [stage.id for stage in STAGES]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids[0], "discovery.plan")
        self.assertEqual(ids[-1], "delivery")


@slow
class FrozenCorpusStageTest(FreezeTestBase):
    def test_frozen_corpus_without_acquisition(self):
        corpus_ref, coverage_ref, _plan, snapshot = self.freeze_setup()
        revision = self.propose(corpus_ref, coverage_ref, snapshot, accept="accepted")
        self.approve_freeze(revision)

        result = resolve(self.store)
        self.assertEqual(result["stage"], "acquisition.manifest")
        self.assertEqual(
            result["completed"],
            ["discovery.plan", "discovery.coverage", "discovery.freeze"],
        )
        self.assertIsNone(result["blocked_on"])
        self.assertIn(
            "acquisition manifest propose", result["next_action"]["inputs"]["command"]
        )


@slow
class OutlineGateStageTest(SynthesisBase):
    def _chain_up_to_unapproved_outline(self):
        audit_ref, _batch = self.accepted_audit()
        synthesis_ref = self.put_synthesis(audit_ref)
        self.approve_ref(synthesis_ref, GATE_SYNTHESIS_KIND, GATE_SYNTHESIS)
        framework_ref = self.put_framework(synthesis_ref)
        self.approve_ref(framework_ref, GATE_FRAMEWORK_KIND, GATE_FRAMEWORK)
        outline_ref = self.put_outline(synthesis_ref, framework_ref)  # NOT approved
        return synthesis_ref, framework_ref, outline_ref

    def test_approved_chain_without_outline_gate(self):
        _s, _f, outline_ref = self._chain_up_to_unapproved_outline()
        result = resolve(self.store)
        self.assertEqual(result["stage"], "synthesis.outline")
        self.assertEqual(result["blocked_on"]["kind"], "human_gate")
        command = result["blocked_on"]["next_command"]
        self.assertIn("gate confirm", command)
        self.assertIn("--gate-kind outline", command)
        self.assertIn("--gate-id synthesis.outline", command)
        self.assertIn(f"--artifact-id {outline_ref['artifact_id']}", command)
        self.assertIn(f"--revision-id {outline_ref['revision_id']}", command)
        # The completed stages include the approved synthesis and framework.
        self.assertIn("synthesis.memo", result["completed"])
        self.assertIn("synthesis.framework", result["completed"])
        self.assertNotIn("synthesis.outline", result["completed"])

    def test_resolve_is_idempotent(self):
        self._chain_up_to_unapproved_outline()
        before = _artifact_signature(self.store)
        first = resolve(self.store)
        second = resolve(self.store)
        after = _artifact_signature(self.store)
        self.assertEqual(_public(first), _public(second))
        self.assertEqual(before, after)

    def test_stale_approval_reports_gate_again(self):
        audit_ref, _batch = self.accepted_audit()
        synthesis_ref = self.put_synthesis(audit_ref)
        self.approve_ref(synthesis_ref, GATE_SYNTHESIS_KIND, GATE_SYNTHESIS)
        framework_ref = self.put_framework(synthesis_ref)
        self.approve_ref(framework_ref, GATE_FRAMEWORK_KIND, GATE_FRAMEWORK)
        outline_ref = self.put_outline(synthesis_ref, framework_ref)
        self.approve_ref(outline_ref, GATE_OUTLINE_KIND, GATE_OUTLINE)
        # Sanity: with the approval current, the outline stage is complete.
        self.assertEqual(resolve(self.store)["stage"], "drafting.blueprint")
        # A new revision after the approval makes the approval stale.
        new_ref = self.put_outline(
            synthesis_ref,
            framework_ref,
            revision="out-r2",
            parent=outline_ref["revision_id"],
        )
        result = resolve(self.store)
        self.assertEqual(result["stage"], "synthesis.outline")
        self.assertEqual(result["blocked_on"]["kind"], "human_gate")
        self.assertIn(f"--revision-id {new_ref['revision_id']}", result["blocked_on"]["next_command"])


@slow
class DraftingReviewStageTest(DraftBase):
    def test_reviewed_drafts_report_tracing(self):
        _s, _f, outline_ref = self.approved_chain()
        ws_ref = self.make_workspace(outline_ref)
        bp_ref = self.propose_bp(self.blueprint_payload(ws_ref))
        self.approve_bp(bp_ref)
        draft_ref = self.propose_draft(self.draft_payload(bp_ref))

        draft_env = self.wf.read(draft_ref, "draft")
        review = SectionReviewRepository(self.store)
        state = review.run(
            "review-sec1",
            draft=draft_env["result"],
            package=draft_env["package"],
            writer_model=_WRITER,
            critic_model=_CRITIC,
            critic=FakeCritic([]),
            writer=FakeWriter(),
            rights=self.rights,
        )
        self.assertEqual(state["status"], "complete")

        result = resolve(self.store)
        self.assertEqual(result["stage"], "tracing.origins")
        self.assertIsNone(result["blocked_on"])
        self.assertIn("drafting.blueprint", result["completed"])
        self.assertIn("review.sections", result["completed"])
        # No CLI exists for tracing yet: the next action names the library.
        self.assertIsNone(result["next_action"]["inputs"]["command"])
        self.assertIn(
            "OriginTraceRepository.run", result["next_action"]["inputs"]["library"]
        )


class PipelineCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-pipe-cli-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.state = self.tmp / "state"
        self.registry = Registry(self.state)
        self.info = self.registry.create_project(str(self.tmp / "project"))
        self.project_id = self.info.project_id

    def _run(self, *args: str):
        out, err = io.StringIO(), io.StringIO()
        code = cli_main.main(
            ["--data-dir", str(self.state), *args], env={}, stdout=out, stderr=err
        )
        return code, out.getvalue(), err.getvalue()

    def test_group_registered(self):
        parser = cli_main.build_parser()
        self.assertIn("pipeline", parser.format_help())
        with self.assertRaises(cli_main.CliUsageError):
            parser.parse_args(["pipeline"])  # a subcommand is required

    def test_pipeline_status_in_process(self):
        code, out, err = self._run(
            "pipeline", "status", "--project", self.project_id
        )
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["stage"], "discovery.plan")
        self.assertEqual(data["command"], "pipeline status")
        self.assertEqual(data["project_id"], self.project_id)
        self.assertIsNone(data["blocked_on"])

    def test_pipeline_status_unknown_project_is_validation_error(self):
        code, _out, err = self._run(
            "pipeline", "status", "--project", "prj_" + "0" * 32
        )
        self.assertEqual(code, cli_main.EXIT_VALIDATION)
        self.assertEqual(json.loads(err)["error"]["category"], "not_found")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
