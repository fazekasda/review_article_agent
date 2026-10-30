"""Detached **generation role** worker jobs (kickoff 16 §5, S17-O2a).

Covers ``src/review_article_agent/worker/generation.py`` plus the narrow
``worker/engine.py`` hooks that register and schedule the explicit
``generation_run`` job kind (submitted through the existing version-1
``discovery_submit`` op):

* strict, pure ``generation_run`` parameter validation (one closed ``operation``
  plus the exact per-operation parameters) **before** any durable write,
  activation change, or POST;
* a queued admission (never a completion) that persists one immutable
  ``JobRepository`` job under the caller request id;
* asynchronous execution of the real role adapter on the single-slot executor
  with an injected deterministic transport, so no network is touched;
* the stored proposal is inert (never approved) and the job result references
  what was written plus the adapter status;
* ``invalid_proposal`` and ``model_not_completed`` are terminal job results
  (never crashes) needing human attention;
* a missing credential and an unsupported operation are refused with **zero**
  POST and no durable job;
* an identical replay under the same request id is one execution;
* the CLI ``generation run submit``/``status`` surface.

Run::

    PYTHONPATH=src:tests python3 -m unittest test_generation_worker
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
TESTS = Path(__file__).resolve().parent
for _path in (str(SRC), str(TESTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import test_generation_runner as gen_runner  # noqa: E402
import test_generation_synthesis_roles as syn_fx  # noqa: E402
import test_generation_drafting_roles as draft_fx  # noqa: E402
import test_generation_delivery_roles as dr_fx  # noqa: E402
import test_screening_runner as scr_fx  # noqa: E402
from test_generation_review_roles import _ReviewTransport  # noqa: E402

from review_article_agent.drafting.workflow import DraftingWorkflowRepository  # noqa: E402
from review_article_agent.generation import hosted_use  # noqa: E402
from review_article_agent.runtime.jobs import JobRepository  # noqa: E402
from review_article_agent.runtime.lead import TransportResponse  # noqa: E402
from review_article_agent.storage import Registry  # noqa: E402
from review_article_agent.storage.artifacts import ArtifactRepository  # noqa: E402
from review_article_agent.worker import discovery as worker_discovery  # noqa: E402
from review_article_agent.worker import generation as worker_generation  # noqa: E402
from review_article_agent.worker.engine import PROTOCOL_VERSION, WorkerEngine  # noqa: E402

import test_cli_discovery_jobs as cli_fx  # noqa: E402

_API_KEY = gen_runner._API_KEY


def _req(op: str, **kw: object) -> dict:
    return {"version": PROTOCOL_VERSION, "op": op, **kw}


class _EngineBase:
    """Shared engine driving helpers (mixin, not a TestCase)."""

    def engine(self, *, transport=None, api_key=_API_KEY, registry=None, data_dir=None):
        engine = WorkerEngine(
            str(data_dir or (self.tmp / "app")),
            registry=registry or self.registry,
            generation_transport=self.transport if transport is None else transport,
            generation_api_key=api_key,
        )
        self.addCleanup(engine.close)
        return engine

    def _drive(self, engine, timeout=20.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            hint = engine.tick()
            if hint is None:
                return
            if hint > 0:
                time.sleep(min(hint, 0.02))
        self.fail("engine did not settle within the timeout")

    def _job(self, engine, job_id):
        res = engine.handle(_req("job_status", project_id=self.project_id, job_id=job_id))
        assert res["ok"], res
        return res["job"]

    def _submit(self, engine, *, request_id, mode=None, parameters):
        return engine.handle(
            _req(
                "discovery_submit",
                project_id=self.project_id,
                request_id=request_id,
                mode=worker_discovery.MODE_GENERATION if mode is None else mode,
                parameters=parameters,
            )
        )

    def _synthesis_parameters(
        self,
        *,
        audit_ref,
        artifact_id="syn-out",
        revision_id="r1",
        hosted_ref=None,
        rights=None,
        operation="synthesis",
        credential_env="OPENROUTER_API_KEY",
    ):
        return {
            "operation": operation,
            "audit_ref": audit_ref,
            "hosted_ref": self.hosted["synthesis"] if hosted_ref is None else hosted_ref,
            "artifact_id": artifact_id,
            "revision_id": revision_id,
            "rights": rights
            if rights is not None
            else self.rights(artifact_id, revision_id),
            "credential_env": credential_env,
        }


# --------------------------------------------------------------------------- #
# Synthesis operation through the worker
# --------------------------------------------------------------------------- #
class GenerationWorkerSynthesisTest(_EngineBase, syn_fx.RoleProposerBase):
    def test_synthesis_end_to_end_stored_not_approved(self) -> None:
        audit_ref, _batch = self.accepted_audit()
        self.transport.responses.append(gen_runner._completion(self.synthesis_content()))
        engine = self.engine()
        res = self._submit(
            engine,
            request_id="gen-syn-1",
            parameters=self._synthesis_parameters(audit_ref=audit_ref),
        )
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["queued"])
        job = res["job"]
        self.assertEqual(job["task_kind"], "generation_run")
        self.assertEqual(job["state"], "queued")
        self.assertIsNone(job.get("accepted_result"))

        job_id = job["job_id"]
        self._drive(engine)
        job = self._job(engine, job_id)
        self.assertEqual(job["state"], "completed")
        accepted = job["accepted_result"]
        self.assertEqual(accepted["result_kind"], "generation_run")
        result = accepted["result"]
        self.assertEqual(result["operation"], "synthesis")
        self.assertEqual(result["status"], "proposed")
        self.assertEqual(self.transport.call_count, 1)

        # The proposal is stored as an inert revision (never approved).
        ref = result["ref"]
        self.assertEqual(ref["artifact_id"], "syn-out")
        stored = self.synth.read(ref, "synthesis")
        self.assertEqual(stored["result"]["kind"], "synthesis")
        from review_article_agent.synthesis.workflow import SynthesisWorkflowError

        with self.assertRaises(SynthesisWorkflowError):
            self.synth.require_synthesis(ref)

        # The credential value and the raw model output are never in the job JSON.
        self.assertNotIn(_API_KEY, json.dumps(job))
        self.assertNotIn("raw_output", json.dumps(result))

    def test_invalid_proposal_is_terminal_result(self) -> None:
        audit_ref, _batch = self.accepted_audit()
        content = self.synthesis_content()
        content["statements"][0]["evidence"] = ["c1:F9"]
        self.transport.responses.append(gen_runner._completion(content))
        engine = self.engine()
        res = self._submit(
            engine,
            request_id="gen-syn-bad",
            parameters=self._synthesis_parameters(audit_ref=audit_ref, artifact_id="syn-bad"),
        )
        job_id = res["job"]["job_id"]
        self._drive(engine)
        job = self._job(engine, job_id)
        self.assertEqual(job["state"], "completed")
        result = job["accepted_result"]["result"]
        self.assertEqual(result["status"], "invalid_proposal")
        self.assertEqual(result["reason"], "unknown_reference")
        self.assertEqual(self.transport.call_count, 1)

    def test_missing_credential_refused_zero_post(self) -> None:
        audit_ref, _batch = self.accepted_audit()
        self.transport.responses.append(gen_runner._completion(self.synthesis_content()))
        engine = self.engine(api_key=None)
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}):
            res = self._submit(
                engine,
                request_id="gen-syn-nokey",
                parameters=self._synthesis_parameters(audit_ref=audit_ref),
            )
            self.assertTrue(res["ok"])
            job_id = res["job"]["job_id"]
            self._drive(engine)
        self.assertEqual(self.transport.call_count, 0)
        job = self._job(engine, job_id)
        self.assertEqual(job["state"], "waiting_human")
        self.assertIsNone(job.get("accepted_result"))

    def test_replay_same_request_id_is_one_execution(self) -> None:
        audit_ref, _batch = self.accepted_audit()
        self.transport.responses.append(gen_runner._completion(self.synthesis_content()))
        engine = self.engine()
        params = self._synthesis_parameters(audit_ref=audit_ref)
        first = self._submit(engine, request_id="gen-syn-rep", parameters=params)
        job_id = first["job"]["job_id"]
        self._drive(engine)
        self.assertEqual(self.transport.call_count, 1)

        second = self._submit(engine, request_id="gen-syn-rep", parameters=params)
        self.assertTrue(second["ok"])
        self.assertEqual(second["job"]["job_id"], job_id)
        self._drive(engine)
        self.assertEqual(self.transport.call_count, 1)

    def test_persisted_read_seam(self) -> None:
        audit_ref, _batch = self.accepted_audit()
        self.transport.responses.append(gen_runner._completion(self.synthesis_content()))
        engine = self.engine()
        self._submit(
            engine,
            request_id="gen-syn-read",
            parameters=self._synthesis_parameters(audit_ref=audit_ref),
        )
        self._drive(engine)
        stored = worker_generation.persisted_generation_run(
            self.store, request_id="gen-syn-read"
        )
        self.assertIsNotNone(stored)
        self.assertEqual(stored["status"], "proposed")
        self.assertIsNone(
            worker_generation.persisted_generation_run(self.store, request_id="absent")
        )


# --------------------------------------------------------------------------- #
# Strict submission validation (before any write)
# --------------------------------------------------------------------------- #
class GenerationSubmitValidationTest(_EngineBase, syn_fx.RoleProposerBase):
    def test_invalid_parameters_rejected_without_writes(self) -> None:
        audit_ref, _batch = self.accepted_audit()
        engine = self.engine()
        good = self._synthesis_parameters(audit_ref=audit_ref)

        # An unknown operation.
        bad_op = dict(good, operation="bogus")
        res = self._submit(engine, request_id="bad-op", parameters=bad_op)
        self.assertFalse(res["ok"])
        self.assertIn(res["code"], {"bad_type", "bad_value"})

        # A recognized-but-unsupported operation.
        unsupported = dict(good, operation="global_review")
        res = self._submit(engine, request_id="bad-unsup", parameters=unsupported)
        self.assertEqual(res["code"], "bad_value")

        # A missing required field.
        missing = dict(good)
        del missing["audit_ref"]
        res = self._submit(engine, request_id="bad-missing", parameters=missing)
        self.assertEqual(res["code"], "bad_value")

        # A wrong credential environment name.
        wrong_env = dict(good, credential_env="SOME_OTHER_KEY")
        res = self._submit(engine, request_id="bad-env", parameters=wrong_env)
        self.assertEqual(res["code"], "bad_value")

        # An unknown extra field.
        extra = dict(good, actor="user")
        res = self._submit(engine, request_id="bad-extra", parameters=extra)
        self.assertEqual(res["code"], "bad_value")

        # No durable job was created for any invalid request.
        with engine.open_repository(self.project_id) as repo:
            self.assertEqual(repo.list(), [])

    def test_wrong_rights_rejected_without_writes(self) -> None:
        audit_ref, _batch = self.accepted_audit()
        engine = self.engine()
        wrong = self._synthesis_parameters(
            audit_ref=audit_ref,
            rights=self.rights("syn-other", "r-other"),
        )
        res = self._submit(engine, request_id="bad-rights", parameters=wrong)
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "bad_value")
        with engine.open_repository(self.project_id) as repo:
            self.assertEqual(repo.list(), [])


# --------------------------------------------------------------------------- #
# Section review through the worker
# --------------------------------------------------------------------------- #
class GenerationWorkerSectionReviewTest(_EngineBase, draft_fx.DirectRolesBase):
    _CRITIC_MODEL = "deepseek/deepseek-v4.1-flash"
    _WRITER_MODEL = "anthropic/claude-sonnet-5-5"

    def setUp(self) -> None:
        super().setUp()
        # A synthetic authorized source for the runner's hosted-transfer check.
        source_ref = self._put_source("a-review-source", "rev-review-source")
        self.source_ref = source_ref

        self.critic_hosted = self._review_hosted(
            "hu-critic", "hu-critic-r1", role="section_critic", model=self._CRITIC_MODEL,
            provider="deepseek",
        )
        self.writer_hosted = self._review_hosted(
            "hu-repair", "hu-repair-r1", role="section_repair", model=self._WRITER_MODEL,
            provider="anthropic",
        )

        # A real approved drafting chain + a stored draft revision.
        _s, _f, outline_ref = self.approved_chain()
        ws_ref = self.make_workspace(outline_ref)
        bp_ref = self.propose_bp(self.blueprint_payload(ws_ref))
        self.approve_bp(bp_ref)
        self.draft_ref = self._propose_draft(bp_ref)

    def _propose_draft(self, blueprint_ref):
        blueprint = self.wf.read(blueprint_ref)["result"]
        payload = {
            "blueprint_ref": blueprint_ref,
            "content": draft_fx._draft_content(blueprint),
        }
        return self.wf.propose(
            "draft",
            payload,
            artifact_id="draft",
            revision_id="draft-r1",
            rights=self.rights("draft", "draft-r1"),
        )

    def _put_source(self, artifact_id, revision_id):
        rights = gen_runner._rights(self.project_id, artifact_id, revision_id)
        self.artifacts.create_artifact(artifact_id, kind="report", created_by="import")
        self.artifacts.import_revision(
            artifact_id=artifact_id,
            revision_id=revision_id,
            content=json.dumps({"kind": "source"}).encode("utf-8"),
            origin_locator="import:synthetic",
            rights=rights,
            acquired_by="import",
        )
        return {
            "project_id": self.project_id,
            "artifact_id": artifact_id,
            "revision_id": revision_id,
        }

    def _review_hosted(self, artifact_id, revision_id, *, role, model, provider):
        ref = hosted_use.propose_hosted_use(
            self.store,
            gen_runner._document(role, model=model),
            artifact_id=artifact_id,
            revision_id=revision_id,
            rights=gen_runner._rights(self.project_id, artifact_id, revision_id),
        )
        self.approve_ref(ref, hosted_use.HOSTED_USE_GATE_KIND, hosted_use.hosted_use_gate_id(role))
        return ref

    def _parameters(self, *, review_id="review-sec1"):
        return {
            "operation": "section_review",
            "review_id": review_id,
            "draft_ref": self.draft_ref,
            "critic_hosted_ref": self.critic_hosted,
            "writer_hosted_ref": self.writer_hosted,
            "rights": gen_runner._rights(self.project_id, review_id, "r0"),
            "source_refs": [self.source_ref],
            "credential_env": "OPENROUTER_API_KEY",
        }

    def _clean_responder(self, payload):
        material = json.loads(payload["messages"][1]["content"])
        if "findings" in material:
            return {"sentences": [], "responses": {}}
        return {"findings": []}

    def test_section_review_through_worker(self) -> None:
        transport = _ReviewTransport(self._clean_responder)
        engine = WorkerEngine(
            str(self.tmp / "app"),
            registry=self.registry,
            generation_transport=transport,
            generation_api_key=_API_KEY,
        )
        self.addCleanup(engine.close)
        res = self._submit(engine, request_id="gen-rev-1", parameters=self._parameters())
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["job"]["task_kind"], "generation_run")
        job_id = res["job"]["job_id"]
        self._drive(engine)
        job = self._job(engine, job_id)
        self.assertEqual(job["state"], "completed")
        self.assertEqual(transport.call_count, 1)
        result = job["accepted_result"]["result"]
        self.assertEqual(result["operation"], "section_review")
        self.assertIn(result["status"], {"complete", "blocked"})
        self.assertEqual(result["review_id"], "review-sec1")
        # The review state is persisted and readable.
        from review_article_agent.review.workflow import SectionReviewRepository

        state = SectionReviewRepository(self.store).read("review-sec1")
        self.assertIsInstance(state, dict)
        self.assertIn(state["status"], {"complete", "blocked"})
        # The credential is never in the job JSON.
        self.assertNotIn(_API_KEY, json.dumps(job))


# --------------------------------------------------------------------------- #
# CLI surface: generation run submit / status
# --------------------------------------------------------------------------- #
class GenerationRunCliTest(cli_fx.DiscoveryJobsCliBase):
    def _params_file(self, pid):
        params = {
            "audit_ref": {"artifact_id": "a-audit", "revision_id": "rev-audit"},
            "hosted_ref": {"artifact_id": "a-hosted", "revision_id": "rev-hosted"},
            "artifact_id": "syn-out",
            "revision_id": "r1",
            "rights": cli_fx._rights_record(pid, "syn-out", "r1"),
        }
        return self.write_json("params.json", params)

    def _submit_args(self, pid):
        return [
            "generation", "run", "submit", "--project", pid,
            "--operation", "synthesis",
            "--params-file", str(self._params_file(pid)),
            "--credential-env", "OPENROUTER_API_KEY",
            "--request-id", "gen-1",
        ]

    def test_run_submit_exact_and_queued(self) -> None:
        pid = self.create_project()
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}):
            with self.fake_worker() as captured:
                out = self.cli(*self._submit_args(pid))
        request = captured["request"]
        self.assertEqual(request["mode"], "generation_run")
        parameters = request["parameters"]
        self.assertEqual(parameters["operation"], "synthesis")
        self.assertEqual(parameters["credential_env"], "OPENROUTER_API_KEY")
        self.assertEqual(parameters["audit_ref"]["project_id"], pid)
        self.assertNotIn("sk-", json.dumps(parameters))
        self.assertTrue(out["queued"])
        self.assertFalse(out["job_completion"])
        self.assertFalse(out["key_value_accepted"])
        self.assertTrue(out["configuration_required"])

    def test_run_submit_bad_credential_env_zero_ipc(self) -> None:
        pid = self.create_project()
        args = self._submit_args(pid)
        args[args.index("--credential-env") + 1] = "SOME_OTHER_KEY"
        with self.fake_worker() as captured:
            self.cli(*args, expect=2)
        self.assertEqual(captured, {})

    def test_run_submit_unsupported_operation_refused(self) -> None:
        pid = self.create_project()
        args = self._submit_args(pid)
        args[args.index("--operation") + 1] = "global_review"
        with self.fake_worker() as captured:
            self.cli(*args, expect=2)
        self.assertEqual(captured, {})

    def test_run_submit_key_value_is_never_a_flag(self) -> None:
        pid = self.create_project()
        args = self._submit_args(pid)
        args.extend(["--key", "sk-or-secret"])
        self.cli(*args, expect=2)

    def test_run_status_reads_recorded_run(self) -> None:
        pid = self.create_project()

        def _make(store):
            repo = JobRepository(store)
            job = repo.submit(
                "gen-1",
                task_kind="generation_run",
                parameters={"operation": "synthesis"},
            )
            attempt = repo.begin_attempt(job.job_id, dispatch_kind="local")
            repo.accept_result(
                attempt.attempt_id,
                result_kind="generation_run",
                result={
                    "kind": "generation_run",
                    "mode": "generation_run",
                    "operation": "synthesis",
                    "request_id": "gen-1",
                    "status": "proposed",
                    "ref": self.ref(pid, "syn-out", "r1"),
                    "authorizing": False,
                },
            )

        self.with_store(pid, _make)
        out = self.cli(
            "generation", "run", "status", "--project", pid, "--request-id", "gen-1"
        )
        self.assertEqual(out["run"]["status"], "proposed")
        self.assertEqual(out["run"]["operation"], "synthesis")
        self.assertEqual(out["run"]["ref"]["artifact_id"], "syn-out")
        self.assertFalse(out["authorizes_publication"])

    def test_run_status_unknown_request_id_refused(self) -> None:
        pid = self.create_project()
        self.cli(
            "generation", "run", "status", "--project", pid, "--request-id", "absent",
            expect=2,
        )

    def test_help_documents_run_commands(self) -> None:
        parser = cli_fx.cli_main.build_parser()
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit):
                parser.parse_args(["generation", "run", "submit", "--help"])
        text = buf.getvalue()
        self.assertIn("--operation", text)
        self.assertIn("--credential-env", text)
        self.assertIn("--params-file", text)


# --------------------------------------------------------------------------- #
# Global review through the worker
# --------------------------------------------------------------------------- #
class GenerationWorkerGlobalReviewTest(_EngineBase, dr_fx.DeliveryRolesBase):
    def _seed_manuscript(self, delivery_id="delivery-1") -> None:
        text = "Intro [@k1].\n\nBody [@k1]. PROBLEM detail."
        state = self.repo.read(delivery_id)
        state["manuscript"] = {"revision_id": "m1", "text": text}
        state["paragraphs"] = {
            "p1": {"text": "Intro [@k1].", "checks": "valid"},
            "p2": {"text": "Body [@k1]. PROBLEM detail.", "checks": "valid"},
        }
        self.repo._persist(state)

    def _parameters(self, *, delivery_id="delivery-1") -> dict:
        return {
            "operation": "global_review",
            "delivery_id": delivery_id,
            "reviewer_hosted_ref": self.reviewer_ref,
            "writer_hosted_ref": self.writer_ref,
            "rights": self.rights(delivery_id, "m1"),
            "source_refs": [self.source_ref],
            "credential_env": "OPENROUTER_API_KEY",
        }

    def test_global_review_completes_through_worker(self) -> None:
        self.start()
        self._seed_manuscript()
        transport = dr_fx._RoleTransport(lambda payload: {"findings": []})
        engine = self.engine(transport=transport)
        res = self._submit(engine, request_id="gen-gr-1", parameters=self._parameters())
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["job"]["task_kind"], "generation_run")
        job_id = res["job"]["job_id"]
        self._drive(engine)
        job = self._job(engine, job_id)
        self.assertEqual(job["state"], "completed")
        result = job["accepted_result"]["result"]
        self.assertEqual(result["operation"], "global_review")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["delivery_id"], "delivery-1")
        self.assertEqual(result["review_status"], "complete")
        self.assertEqual(transport.call_count, 1)
        state = self.repo.read("delivery-1")
        self.assertEqual(state["global_review"]["status"], "complete")
        self.assertNotIn(_API_KEY, json.dumps(job))

    def test_global_review_blocks_with_bounded_rounds(self) -> None:
        self.start()
        self._seed_manuscript()

        def responder(payload):
            material = dr_fx._material_of(payload)
            if "findings" in material:
                return {"paragraphs": []}
            return {"findings": [dr_fx._finding("p2")]}

        transport = dr_fx._RoleTransport(responder)
        engine = self.engine(transport=transport)
        res = self._submit(engine, request_id="gen-gr-block", parameters=self._parameters())
        job_id = res["job"]["job_id"]
        self._drive(engine)
        job = self._job(engine, job_id)
        self.assertEqual(job["state"], "completed")
        result = job["accepted_result"]["result"]
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["rounds_used"], dr_fx.GLOBAL_ROUND_LIMIT)
        state = self.repo.read("delivery-1")
        self.assertEqual(state["global_review"]["status"], "blocked")


# --------------------------------------------------------------------------- #
# Notes plan through the worker (proposal only; never applied)
# --------------------------------------------------------------------------- #
class GenerationWorkerNotesPlanTest(_EngineBase, dr_fx.DeliveryRolesBase):
    def _reviewed_delivery(self) -> None:
        self.start()
        transport = dr_fx._RoleTransport(lambda payload: {"findings": []})
        self.run_review(transport, dr_fx._two_paragraph_manuscript())
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

    def _parameters(self) -> dict:
        return {
            "operation": "notes_plan",
            "delivery_id": "delivery-1",
            "hosted_ref": self.planner_ref,
            "source_refs": [self.source_ref],
            "credential_env": "OPENROUTER_API_KEY",
        }

    def test_notes_plan_proposes_and_never_applies(self) -> None:
        self._reviewed_delivery()

        def responder(payload):
            return {
                "edits": [
                    {
                        "note_id": "n1",
                        "paragraph_id": "p1",
                        "new_text": "Intro [@k1] tightened.",
                        "kind": "wording",
                    },
                    {"note_id": "nX", "paragraph_id": "p1", "new_text": "x", "kind": "wording"},
                ]
            }

        transport = dr_fx._RoleTransport(responder)
        engine = self.engine(transport=transport)
        res = self._submit(engine, request_id="gen-np-1", parameters=self._parameters())
        self.assertTrue(res["ok"], res)
        job_id = res["job"]["job_id"]
        self._drive(engine)
        job = self._job(engine, job_id)
        self.assertEqual(job["state"], "completed")
        result = job["accepted_result"]["result"]
        self.assertEqual(result["operation"], "notes_plan")
        self.assertEqual(result["status"], "proposed")
        self.assertEqual(result["edits_count"], 1)
        self.assertEqual(result["dropped_count"], 1)
        # Nothing was applied: the delivery still has no applied batch and the
        # paragraph text is unchanged.
        state = self.repo.read("delivery-1")
        self.assertEqual(state["applied_batches"], [])
        self.assertEqual(state["paragraphs"]["p1"]["text"], "Intro [@k1].")


# --------------------------------------------------------------------------- #
# Screening case through the worker
# --------------------------------------------------------------------------- #
class GenerationWorkerScreeningCaseTest(_EngineBase, scr_fx.RunnerTestBase):
    def _parameters(self) -> dict:
        return {
            "operation": "screening_case",
            "config_ref": self.config_ref,
            "report_ref": self.report,
            "hosted_ref": self.hosted_ref,
            "credential_env": "OPENROUTER_API_KEY",
        }

    def test_screening_case_commits_decision_result(self) -> None:
        self.transport.responses.append(scr_fx._jev_response(0.1))
        engine = self.engine(transport=self.transport, api_key=scr_fx._API_KEY)
        res = self._submit(engine, request_id="gen-sc-1", parameters=self._parameters())
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["job"]["task_kind"], "generation_run")
        job_id = res["job"]["job_id"]
        self._drive(engine)
        job = self._job(engine, job_id)
        self.assertEqual(job["state"], "completed")
        result = job["accepted_result"]["result"]
        self.assertEqual(result["operation"], "screening_case")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["screening_status"], "completed")
        ref = result["decision_result_ref"]
        self.assertIsNotNone(ref)
        self.assertEqual(self.transport.call_count, 1)

        from review_article_agent.runtime.dispatch import RuntimeDispatchRepository

        dispatch = RuntimeDispatchRepository(self.store).get_dispatch(ref["dispatch_id"])
        self.assertEqual(dispatch.state, "completed")


# --------------------------------------------------------------------------- #
# Strict validation for the newly wired operations (before any write)
# --------------------------------------------------------------------------- #
class GenerationSubmitValidationNewOpsTest(_EngineBase, dr_fx.DeliveryRolesBase):
    def test_new_ops_invalid_params_refused_without_writes(self) -> None:
        engine = self.engine(transport=dr_fx._RoleTransport(lambda payload: {"findings": []}))

        # global_review with an unknown extra field.
        bad_global = {
            "operation": "global_review",
            "delivery_id": "delivery-1",
            "reviewer_hosted_ref": self.reviewer_ref,
            "writer_hosted_ref": self.writer_ref,
            "rights": self.rights("delivery-1", "m1"),
            "actor": "user",
            "credential_env": "OPENROUTER_API_KEY",
        }
        res = self._submit(engine, request_id="bad-gr", parameters=bad_global)
        self.assertEqual(res["code"], "bad_value")

        # screening_case missing its required report_ref.
        bad_screening = {
            "operation": "screening_case",
            "config_ref": self.source_ref,
            "hosted_ref": self.reviewer_ref,
            "credential_env": "OPENROUTER_API_KEY",
        }
        res = self._submit(engine, request_id="bad-sc", parameters=bad_screening)
        self.assertEqual(res["code"], "bad_value")

        # notes_plan with the wrong credential environment name.
        bad_notes = {
            "operation": "notes_plan",
            "delivery_id": "delivery-1",
            "hosted_ref": self.planner_ref,
            "credential_env": "SOME_OTHER_KEY",
        }
        res = self._submit(engine, request_id="bad-np", parameters=bad_notes)
        self.assertEqual(res["code"], "bad_value")

        # No durable job was created for any invalid request.
        with engine.open_repository(self.project_id) as repo:
            self.assertEqual(repo.list(), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
