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
from test_drafting_workflow import blueprint_content  # noqa: E402
from test_generation_drafting_roles import DirectRolesBase  # noqa: E402

import review_article_agent.cli.main as cli_main  # noqa: E402
from review_article_agent.drafting.package import DraftingPackageRepository  # noqa: E402
from review_article_agent.drafting.workspace import SectionWorkspaceRepository  # noqa: E402
from review_article_agent.pipeline import run as pipeline_run  # noqa: E402
from review_article_agent.pipeline import status as pipeline_status  # noqa: E402
from review_article_agent.pipeline.run import (  # noqa: E402
    PipelineConfig,
    PipelineConfigError,
    PipelineRunError,
    plan_next,
    run,
)
from review_article_agent.runtime.jobs import JobRepository  # noqa: E402
from review_article_agent.worker.engine import WorkerEngine  # noqa: E402

from _tiers import slow  # noqa: E402

_API_KEY = gen_runner._API_KEY
_CREDENTIAL_ENV = "OPENROUTER_API_KEY"


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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
