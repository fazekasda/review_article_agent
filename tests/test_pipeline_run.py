"""``pipeline run`` orchestrator tests (kickoff 16 §6, write half).

Covers ``src/review_article_agent/pipeline/run.py`` (the pure ``plan_next`` and the
submitting ``run``), the ``hosted_use``/``budget`` blocker extension of
``pipeline/status.py``, and the ``pipeline run`` CLI surface:

* from an approved outline plus approved hosted-use documents, ``pipeline run``
  submits the drafting workspace/blueprint job(s) and stops at the blueprint gate
  with the exact ``gate confirm`` command;
* re-running submits nothing new (same deterministic request id; the job count is
  unchanged);
* a missing hosted-use approval stops with a ``hosted_use`` blocker (and the gate
  command) and zero jobs;
* a missing credential is refused with zero jobs;
* an exhausted per-role budget reports a ``budget`` blocker.

The heavy scenarios are engine-level/in-process: the real worker engine runs the
real role adapter with the deterministic fake transport from
``test_generation_runner``; the real approved-outline/hosted-use fixtures come from
``test_generation_drafting_roles``. No model, network, or provider call is made.

Run::

    python3 scripts/run_tests.py --slow -j 4 test_pipeline_run
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

import test_generation_runner as gen_runner  # noqa: E402
from test_drafting_workflow import (  # noqa: E402
    GATE_ID as BP_GATE_ID,
    GATE_KIND as BP_GATE_KIND,
    blueprint_content,
)
from test_generation_drafting_roles import DirectRolesBase  # noqa: E402
from test_synthesis_workflow import (  # noqa: E402
    GATE_FRAMEWORK,
    GATE_FRAMEWORK_KIND,
    GATE_OUTLINE,
    GATE_OUTLINE_KIND,
    GATE_SYNTHESIS,
    GATE_SYNTHESIS_KIND,
)
from test_tracing_workflow import POLICY  # noqa: E402

import review_article_agent.cli.main as cli_main  # noqa: E402
from review_article_agent.delivery.workflow import DeliveryRepository  # noqa: E402
from review_article_agent.drafting.package import DraftingPackageRepository  # noqa: E402
from review_article_agent.drafting.workspace import SectionWorkspaceRepository  # noqa: E402
from review_article_agent.generation import hosted_use  # noqa: E402
from review_article_agent.pipeline import run as pipeline_run  # noqa: E402
from review_article_agent.pipeline import status as pipeline_status  # noqa: E402
from review_article_agent.pipeline.run import (  # noqa: E402
    PipelineConfig,
    PipelineConfigError,
    PipelineRunError,
    delivery_id_for,
    plan_next,
    run,
    trace_id_for,
)
from review_article_agent.runtime.jobs import JobRepository  # noqa: E402
from review_article_agent.worker.engine import WorkerEngine  # noqa: E402

from _tiers import slow  # noqa: E402

_API_KEY = gen_runner._API_KEY
_CREDENTIAL_ENV = "OPENROUTER_API_KEY"
_CRITIC_MODEL = "deepseek/deepseek-v4.1-flash"
_REPAIR_MODEL = "anthropic/claude-sonnet-5-5"


# --------------------------------------------------------------------------- #
# Fast tier: configuration validation + CLI registration
# --------------------------------------------------------------------------- #
class PipelineConfigTest(unittest.TestCase):
    def _valid(self) -> dict:
        return {
            "credential_env": _CREDENTIAL_ENV,
            "sections": ["sec1"],
            "roles": {
                "blueprint": {
                    "hosted_artifact_id": "hu-blueprint",
                    "target_artifact_id": "bp-{section_id}",
                    "target_revision_id": "bp-{section_id}-r1",
                },
                "section_writer": {"hosted_artifact_id": "hu-writer"},
            },
            "workspace_artifact_id": "ws-{section_id}",
            "workspace_revision_id": "ws-{section_id}-r1",
            "rights": {"rights_id": "RIGHTS-template"},
        }

    def test_valid_config_parses(self):
        config = PipelineConfig.from_mapping(self._valid())
        self.assertEqual(config.credential_env, _CREDENTIAL_ENV)
        self.assertEqual(config.sections, ("sec1",))
        self.assertEqual(config.roles["blueprint"].hosted_artifact_id, "hu-blueprint")
        self.assertIsNone(config.roles["section_writer"].target_artifact_id)

    def test_wrong_credential_env_is_refused(self):
        value = self._valid()
        value["credential_env"] = "SOME_OTHER_KEY"
        with self.assertRaises(PipelineConfigError):
            PipelineConfig.from_mapping(value)

    def test_unknown_role_is_refused(self):
        value = self._valid()
        value["roles"]["not-a-role"] = {"hosted_artifact_id": "hu-x"}
        with self.assertRaises(PipelineConfigError):
            PipelineConfig.from_mapping(value)

    def test_extra_top_level_key_is_refused(self):
        value = self._valid()
        value["surprise"] = 1
        with self.assertRaises(PipelineConfigError):
            PipelineConfig.from_mapping(value)

    def test_missing_hosted_artifact_id_is_refused(self):
        value = self._valid()
        del value["roles"]["blueprint"]["hosted_artifact_id"]
        with self.assertRaises(PipelineConfigError):
            PipelineConfig.from_mapping(value)

    def test_load_config_missing_file_is_refused(self):
        with self.assertRaises(PipelineConfigError):
            pipeline_run.load_config("/nonexistent/pipeline-config.json")

    def test_tracing_policy_is_optional_and_parsed(self):
        value = self._valid()
        value["tracing_policy"] = dict(POLICY)
        config = PipelineConfig.from_mapping(value)
        self.assertEqual(config.tracing_policy, POLICY)
        self.assertIsNone(PipelineConfig.from_mapping(self._valid()).tracing_policy)

    def test_non_object_tracing_policy_is_refused(self):
        value = self._valid()
        value["tracing_policy"] = [1, 2, 3]
        with self.assertRaises(PipelineConfigError):
            PipelineConfig.from_mapping(value)


class PipelineRunCliRegistrationTest(unittest.TestCase):
    def test_run_is_registered_with_its_flags(self):
        parser = cli_main.build_parser()
        args = parser.parse_args(
            ["pipeline", "run", "--config", "cfg.json", "--project", "prj_x",
             "--wait", "--timeout", "5", "--max-steps", "3"]
        )
        self.assertEqual(args.pipeline_command, "run")
        self.assertEqual(args.pipeline_config, "cfg.json")
        self.assertTrue(args.wait)
        self.assertEqual(args.max_steps, "3")
        self.assertIn("pipeline", parser.format_help())


class PipelineRunCliTest(unittest.TestCase):
    def setUp(self) -> None:
        from review_article_agent.storage import Registry

        self._tmp = tempfile.TemporaryDirectory(prefix="raa-pipe-run-cli-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.state = self.tmp / "state"
        self.registry = Registry(self.state)
        self.info = self.registry.create_project(str(self.tmp / "project"))
        self.config_path = self.tmp / "cfg.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "credential_env": _CREDENTIAL_ENV,
                    "sections": ["sec1"],
                    "roles": {
                        "blueprint": {
                            "hosted_artifact_id": "hu-blueprint",
                            "target_artifact_id": "bp-{section_id}",
                        }
                    },
                    "rights": {"rights_id": "RIGHTS-template"},
                }
            )
        )

    def _run(self, *args: str):
        out, err = io.StringIO(), io.StringIO()
        code = cli_main.main(
            ["--data-dir", str(self.state), *args], env={}, stdout=out, stderr=err
        )
        return code, out.getvalue(), err.getvalue()

    def test_empty_project_submits_nothing(self):
        code, out, err = self._run(
            "pipeline", "run", "--project", self.info.project_id,
            "--config", str(self.config_path),
        )
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["command"], "pipeline run")
        self.assertEqual(data["submitted"], [])
        self.assertEqual(data["stage"], "discovery.plan")

    def test_malformed_config_is_validation_error(self):
        bad = self.tmp / "bad.json"
        bad.write_text(json.dumps({"surprise": 1}))
        code, _out, err = self._run(
            "pipeline", "run", "--project", self.info.project_id, "--config", str(bad)
        )
        self.assertEqual(code, cli_main.EXIT_VALIDATION)
        self.assertEqual(json.loads(err)["error"]["category"], "validation")


# --------------------------------------------------------------------------- #
# Slow tier: engine-level end-to-end orchestration
# --------------------------------------------------------------------------- #
@slow
class PipelineRunBase(DirectRolesBase):
    """Shared config + engine helpers over the approved drafting-role fixtures."""

    def config_mapping(
        self,
        *,
        sections=("sec1",),
        blueprint_hosted="hu-blueprint",
        writer_hosted="hu-writer",
    ) -> dict:
        return {
            "credential_env": _CREDENTIAL_ENV,
            "sections": list(sections),
            "roles": {
                "blueprint": {
                    "hosted_artifact_id": blueprint_hosted,
                    "target_artifact_id": "bp-{section_id}",
                    "target_revision_id": "bp-{section_id}-r1",
                },
                "section_writer": {"hosted_artifact_id": writer_hosted},
            },
            "workspace_artifact_id": "ws-{section_id}",
            "workspace_revision_id": "ws-{section_id}-r1",
            "rights": gen_runner._rights(self.project_id, "template", "t0"),
        }

    def pipeline_config(self, **kwargs) -> PipelineConfig:
        return PipelineConfig.from_mapping(self.config_mapping(**kwargs))

    def engine(self, *, api_key=_API_KEY) -> WorkerEngine:
        engine = WorkerEngine(
            str(self.tmp / "app"),
            registry=self.registry,
            generation_transport=self.transport,
            generation_api_key=api_key,
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

    def job_count(self) -> int:
        return len(JobRepository(self.store).list())

    def workspace_ref(self, section_id="sec1") -> dict:
        return {
            "project_id": self.project_id,
            "artifact_id": f"ws-{section_id}",
            "revision_id": f"ws-{section_id}-r1",
        }


@slow
class PipelineRunDraftingTest(PipelineRunBase):
    def test_submits_blueprint_and_stops_at_gate(self):
        _s, _f, _outline = self.approved_chain()
        config = self.pipeline_config()
        engine = self.engine()

        first = self.run_once(engine, config)
        self.assertEqual(len(first["submitted"]), 1)
        self.assertEqual(first["already_done"], [])
        self.assertEqual(self.job_count(), 1)
        # The deterministic drafting workspace was written.
        SectionWorkspaceRepository(self.store).read(self.workspace_ref())

        # Re-running before the job settles submits nothing new (same request id).
        second = self.run_once(engine, config)
        self.assertEqual(second["submitted"], [])
        self.assertEqual(second["already_done"], [first["submitted"][0]])
        self.assertEqual(self.job_count(), 1)

        # Settle the queued blueprint job with a deterministic fake completion.
        workspace = SectionWorkspaceRepository(self.store).read(self.workspace_ref())[
            "result"
        ]
        package = DraftingPackageRepository(self.store).build(
            workspace["outline_ref"], workspace["section_id"]
        )
        self.transport.responses.append(
            gen_runner._completion(
                blueprint_content(
                    section_id="sec1",
                    finding_key=package["evidence"][0]["finding_key"],
                )
            )
        )
        self.drive(engine)

        # The blueprint now exists (unapproved): stop at its exact gate.
        third = self.run_once(engine, config)
        self.assertEqual(third["submitted"], [])
        self.assertEqual(third["stopped_at"]["kind"], "human_gate")
        self.assertIn("gate confirm", third["next_command"])
        self.assertIn("--gate-id drafting.blueprint", third["next_command"])
        self.assertIn("bp-sec1", third["next_command"])
        self.assertEqual(self.job_count(), 1)

    def test_plan_next_is_pure_and_deterministic(self):
        _s, _f, _outline = self.approved_chain()
        config = self.pipeline_config()
        before = self.job_count()
        first = plan_next(self.store, self.registry, config=config)
        second = plan_next(self.store, self.registry, config=config)
        self.assertEqual(first, second)
        # Nothing was written and no job was created by planning.
        self.assertEqual(self.job_count(), before)
        self.assertEqual([spec["kind"] for spec in first], ["prepare_workspace"])


@slow
class PipelineRunHostedUseTest(PipelineRunBase):
    def test_missing_hosted_use_approval_zero_jobs(self):
        _s, _f, _outline = self.approved_chain()
        # Supersede the approved blueprint hosted-use document with an unapproved
        # revision, so its named gate is no longer active for the current head.
        self.artifacts.import_revision(
            artifact_id="hu-blueprint",
            revision_id="hu-blueprint-r2",
            content=gen_runner._document("blueprint"),
            origin_locator="generation:local",
            rights=gen_runner._rights(self.project_id, "hu-blueprint", "hu-blueprint-r2"),
            parent_revision_id="hu-blueprint-r1",
            acquired_by="user",
        )
        state = pipeline_status.resolve(self.store)
        self.assertEqual(state["stage"], "drafting.blueprint")
        self.assertEqual(state["blocked_on"]["kind"], "hosted_use")
        self.assertIn("gate confirm", state["blocked_on"]["next_command"])
        self.assertIn(
            "generation.hosted_use.blueprint", state["blocked_on"]["next_command"]
        )

        config = self.pipeline_config()
        engine = self.engine()
        result = self.run_once(engine, config)
        self.assertEqual(result["submitted"], [])
        self.assertEqual(result["stopped_at"]["kind"], "hosted_use")
        self.assertEqual(self.job_count(), 0)


@slow
class PipelineRunCredentialTest(PipelineRunBase):
    def test_missing_credential_refused_zero_jobs(self):
        _s, _f, _outline = self.approved_chain()
        config = self.pipeline_config()
        engine = self.engine()
        with self.assertRaises(PipelineRunError) as ctx:
            run(
                self.store,
                self.registry,
                config=config,
                submit=lambda request: engine.handle(request),
                environ={},
            )
        self.assertEqual(ctx.exception.code, "missing_credential")
        self.assertEqual(self.job_count(), 0)


@slow
class PipelineRunBudgetTest(PipelineRunBase):
    def test_exhausted_budget_reports_budget_blocker(self):
        _s, _f, _outline = self.approved_chain()
        config = self.pipeline_config()
        engine = self.engine()
        with mock.patch.object(
            pipeline_status._generation_runner,
            "_role_exposure",
            return_value=Decimal("999.00"),
        ):
            state = pipeline_status.resolve(self.store)
            self.assertEqual(state["blocked_on"]["kind"], "budget")
            result = self.run_once(engine, config)
        self.assertEqual(result["submitted"], [])
        self.assertEqual(result["stopped_at"]["kind"], "budget")
        self.assertEqual(self.job_count(), 0)


# --------------------------------------------------------------------------- #
# Slow tier: the full automatic chain (synthesis -> ... -> delivery)
# --------------------------------------------------------------------------- #
def _model_framework_content():
    """The framework content as a compliant model emits it (aliased keys)."""
    from test_synthesis_workflow import SynthesisBase

    def alias(value):
        if isinstance(value, dict):
            renamed = {
                "concept_role" if k == "role" else
                "relationship_scope" if k == "scope" else k: alias(v)
                for k, v in value.items()
            }
            return renamed
        if isinstance(value, list):
            return [alias(item) for item in value]
        return value

    return alias(SynthesisBase.framework_content())


@slow
class PipelineRunFullBase(PipelineRunBase):
    """Adds the hosted-use documents + config for every driven role."""

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
        for role, (artifact_id, model) in self._EXTRA_HOSTED.items():
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
            self.approve_ref(
                ref,
                hosted_use.HOSTED_USE_GATE_KIND,
                hosted_use.hosted_use_gate_id(role),
            )

    def full_config_mapping(self) -> dict:
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

    def full_config(self) -> PipelineConfig:
        return PipelineConfig.from_mapping(self.full_config_mapping())

    def ref(self, artifact_id: str, revision_id: str) -> dict:
        return {
            "project_id": self.project_id,
            "artifact_id": artifact_id,
            "revision_id": revision_id,
        }

    def settle(self, engine: WorkerEngine, response) -> None:
        """Queue one fake completion and drive the engine to settle the job."""
        self.transport.responses.append(response)
        self.drive(engine)


@slow
class PipelineRunSynthesisTest(PipelineRunFullBase):
    def test_synthesis_to_outline_stops_at_each_gate(self):
        self.accepted_audit()
        config = self.full_config()
        engine = self.engine()

        # synthesis.memo: submit, settle, stop at the synthesis gate.
        first = self.run_once(engine, config)
        self.assertEqual(len(first["submitted"]), 1)
        self.assertEqual(first["stage"], "synthesis.memo")
        self.settle(engine, gen_runner._completion(self.synthesis_content()))
        gate = self.run_once(engine, config)
        self.assertEqual(gate["submitted"], [])
        self.assertEqual(gate["stopped_at"]["kind"], "human_gate")
        self.assertIn("--gate-id synthesis.memo", gate["next_command"])
        self.approve_ref(
            self.ref("syn", "syn-r1"), GATE_SYNTHESIS_KIND, GATE_SYNTHESIS
        )

        # framework.
        second = self.run_once(engine, config)
        self.assertEqual(len(second["submitted"]), 1)
        self.settle(engine, gen_runner._completion(_model_framework_content()))
        gate = self.run_once(engine, config)
        self.assertEqual(gate["stopped_at"]["kind"], "human_gate")
        self.assertIn("--gate-id synthesis.framework", gate["next_command"])
        self.approve_ref(
            self.ref("fw", "fw-r1"), GATE_FRAMEWORK_KIND, GATE_FRAMEWORK
        )

        # outline.
        third = self.run_once(engine, config)
        self.assertEqual(len(third["submitted"]), 1)
        self.settle(engine, gen_runner._completion(self.outline_content()))
        gate = self.run_once(engine, config)
        self.assertEqual(gate["stopped_at"]["kind"], "human_gate")
        self.assertIn("--gate-id synthesis.outline", gate["next_command"])

    def test_re_run_before_settle_submits_nothing(self):
        self.accepted_audit()
        config = self.full_config()
        engine = self.engine()
        first = self.run_once(engine, config)
        self.assertEqual(len(first["submitted"]), 1)
        second = self.run_once(engine, config)
        self.assertEqual(second["submitted"], [])
        self.assertEqual(second["already_done"], [first["submitted"][0]])
        self.assertEqual(self.job_count(), 1)


@slow
class PipelineRunDraftReviewTest(PipelineRunFullBase):
    def _blueprint_result(self):
        from review_article_agent.drafting.workflow import DraftingWorkflowRepository

        return DraftingWorkflowRepository(self.store).read(
            self.ref("bp-sec1", "bp-sec1-r1")
        )["result"]

    def test_draft_then_section_review(self):
        from review_article_agent.review.workflow import SectionReviewRepository

        self.approved_chain()
        config = self.full_config()
        engine = self.engine()

        # drafting.blueprint: workspace + blueprint.
        first = self.run_once(engine, config)
        self.assertEqual(first["stage"], "drafting.blueprint")
        self.assertEqual(len(first["submitted"]), 1)
        workspace = SectionWorkspaceRepository(self.store).read(
            self.workspace_ref()
        )["result"]
        package = DraftingPackageRepository(self.store).build(
            workspace["outline_ref"], workspace["section_id"]
        )
        self.settle(
            engine,
            gen_runner._completion(
                blueprint_content(
                    section_id="sec1",
                    finding_key=package["evidence"][0]["finding_key"],
                )
            ),
        )
        gate = self.run_once(engine, config)
        self.assertEqual(gate["stopped_at"]["kind"], "human_gate")
        self.approve_ref(self.ref("bp-sec1", "bp-sec1-r1"), BP_GATE_KIND, BP_GATE_ID)

        # review.sections: the draft is planned first.
        draft = self.run_once(engine, config)
        self.assertEqual(draft["stage"], "review.sections")
        self.assertEqual(len(draft["submitted"]), 1)
        self.settle(
            engine,
            gen_runner._completion(_draft_content(self._blueprint_result())),
        )

        # the section review is planned next; it runs through the real critic port.
        review = self.run_once(engine, config)
        self.assertEqual(len(review["submitted"]), 1)
        self.settle(engine, gen_runner._completion({"findings": []}))
        review_id = pipeline_run.review_id_for("sec1", "draft-sec1-r1")
        state = SectionReviewRepository(self.store).read(review_id)
        self.assertIsNotNone(state)


def _draft_content(blueprint):
    from test_generation_drafting_roles import _draft_content as build

    return build(blueprint)


@slow
class PipelineRunDeliveryTest(PipelineRunFullBase):
    def _reach_reviewed_section(self, engine, config):
        self.approved_chain()
        first = self.run_once(engine, config)
        self.assertEqual(first["stage"], "drafting.blueprint")
        workspace = SectionWorkspaceRepository(self.store).read(
            self.workspace_ref()
        )["result"]
        package = DraftingPackageRepository(self.store).build(
            workspace["outline_ref"], workspace["section_id"]
        )
        self.settle(
            engine,
            gen_runner._completion(
                blueprint_content(
                    section_id="sec1",
                    finding_key=package["evidence"][0]["finding_key"],
                )
            ),
        )
        self.run_once(engine, config)
        self.approve_ref(self.ref("bp-sec1", "bp-sec1-r1"), BP_GATE_KIND, BP_GATE_ID)

        from review_article_agent.drafting.workflow import DraftingWorkflowRepository

        blueprint = DraftingWorkflowRepository(self.store).read(
            self.ref("bp-sec1", "bp-sec1-r1")
        )["result"]
        self.run_once(engine, config)
        self.settle(engine, gen_runner._completion(_draft_content(blueprint)))
        self.run_once(engine, config)
        self.settle(engine, gen_runner._completion({"findings": []}))

    def _seed_complete_trace(self) -> str:
        """Seed the deterministic complete origin trace for the section.

        The pipeline's own tracing over this synthetic extraction fixture stops at
        the secondary-origin human warning (the cited source is a review that
        secondarily reports); a complete trace is the human-approved precondition
        the delivery planning consumes, so it is seeded directly here.
        """
        from review_article_agent.tracing.workflow import (
            DOCUMENT_TYPE as TRACE_DOCUMENT_TYPE,
            SCHEMA_VERSION as TRACE_SCHEMA_VERSION,
        )

        trace_id = trace_id_for("sec1", "draft-sec1-r1")
        self.artifacts.create_artifact(trace_id, kind="report", created_by="user")
        self.artifacts.import_revision(
            artifact_id=trace_id,
            revision_id="trace-r1",
            content=json.dumps(
                {
                    "schema_version": TRACE_SCHEMA_VERSION,
                    "document_type": TRACE_DOCUMENT_TYPE,
                    "payload": {
                        "schema_version": TRACE_SCHEMA_VERSION,
                        "trace_id": trace_id,
                        "document_type": TRACE_DOCUMENT_TYPE,
                        "status": "complete",
                        "claims": [],
                    },
                }
            ).encode("utf-8"),
            origin_locator="tracing:seed",
            acquired_by="user",
            rights=self.rights(trace_id, "trace-r1"),
        )
        return trace_id

    def test_reviewed_sections_to_final_approval_gate(self):
        config = self.full_config()
        engine = self.engine()
        self._reach_reviewed_section(engine, config)
        self._seed_complete_trace()

        # delivery start (in-process) + global review job submitted.
        result = self.run_once(engine, config)
        self.assertTrue(
            any(
                spec.startswith("pipe.delivery.global_review")
                for spec in result["submitted"]
            ),
            result["submitted"],
        )
        delivery_id = delivery_id_for(self.project_id, ("sec1",))
        self.assertIsNotNone(DeliveryRepository(self.store).read(delivery_id))
        self.settle(engine, gen_runner._completion({"findings": []}))

        # record the human freshness cutoff, then prepare the final approval.
        DeliveryRepository(self.store).freshness(
            delivery_id,
            "2026-01-01",
            {"action": "accept_cutoff"},
            rights=self.rights,
        )
        final = self.run_once(engine, config)
        self.assertEqual(final["stopped_at"]["kind"], "human_gate")
        self.assertIn("gate confirm", final["next_command"])
        self.assertIn("delivery.final_approval", final["next_command"])

    def test_tracing_over_secondary_fixture_stops_at_human_warning(self):
        config = self.full_config()
        engine = self.engine()
        self._reach_reviewed_section(engine, config)
        # The pipeline runs tracing in-process; the synthetic secondary source
        # requires final human approval, so the run stops at the tracing gate.
        result = self.run_once(engine, config)
        self.assertEqual(result["submitted"], [])
        self.assertEqual(result["stopped_at"]["kind"], "failure")


@slow
class PipelineRunFailureTest(PipelineRunFullBase):
    def test_invalid_proposal_stops_with_failure_blocker(self):
        audit_ref, _batch = self.accepted_audit()
        config = self.full_config()
        engine = self.engine()

        first = self.run_once(engine, config)
        self.assertEqual(len(first["submitted"]), 1)
        # An invalid synthesis proposal (unknown finding key) writes nothing.
        bad = self.synthesis_content()
        bad["statements"][0]["evidence"] = ["c1:F999"]
        self.settle(engine, gen_runner._completion(bad))

        result = self.run_once(engine, config)
        self.assertEqual(result["submitted"], [])
        self.assertEqual(result["stopped_at"]["kind"], "failure")
        self.assertIn("generation run status", result["next_command"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
