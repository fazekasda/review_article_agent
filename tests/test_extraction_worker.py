"""Detached **extraction/quality run** worker jobs (Step 09, S9-WORKER).

Covers ``src/review_article_agent/worker/extraction.py`` plus the narrow
``worker/engine.py`` hooks that register and schedule the explicit
``extraction_run`` job kind (submitted through the existing version-1
``discovery_submit`` op):

* strict, pure ``extraction_run`` parameter validation (mirroring
  ``extraction.runner.execute_run``'s keyword arguments) **before** any durable
  write, activation change, or POST;
* a queued admission (never a completion) that persists one immutable
  ``JobRepository`` job under the caller request id;
* asynchronous execution of the real
  :func:`~review_article_agent.extraction.runner.execute_run` on the single-slot
  executor with an injected deterministic transport, so no network is touched;
* the recorded run receipt is readable via
  :func:`review_article_agent.extraction.runner.require_run`;
* a missing credential and a missing gate are refused with **zero** POST;
* an identical replay under the same request id is one run; an ambiguous outcome
  is retried under NEW deterministic identities (never re-sent) and, when the
  overhead cap refuses a further attempt, resolves to a definitive failure that
  recovery replays with zero POST (the runner resolves the retry chain);
* the CLI ``extraction run submit``/``status`` surface.

Run::

    PYTHONPATH=src python3 -m unittest tests.test_extraction_worker
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

import test_extraction_runner as fx  # noqa: E402
from review_article_agent.extraction.runner import require_run  # noqa: E402
from review_article_agent.extraction import workflow  # noqa: E402
from review_article_agent.runtime.jobs import JobRepository  # noqa: E402
from review_article_agent.storage import Registry  # noqa: E402
from review_article_agent.worker import discovery as worker_discovery  # noqa: E402
from review_article_agent.worker import extraction as worker_extraction  # noqa: E402
from review_article_agent.worker.engine import (  # noqa: E402
    PROTOCOL_VERSION,
    WorkerEngine,
)

import test_cli_discovery_jobs as cli_fx  # noqa: E402


def _req(op: str, **kw: object) -> dict:
    return {"version": PROTOCOL_VERSION, "op": op, **kw}


class ExtractionWorkerBase(fx.RunnerTestBase):
    """Reuses the real runner fixtures (config/report/hosted-use + gates)."""

    # -- engine helpers ------------------------------------------------------ #
    def engine(self, *, transport=None, api_key=None, registry=None, data_dir=None):
        engine = WorkerEngine(
            str(data_dir or (self.tmp / "app")),
            registry=registry or self.registry,
            extraction_transport=self.transport if transport is None else transport,
            extraction_api_key=api_key,
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
        res = engine.handle(
            _req("job_status", project_id=self.project_id, job_id=job_id)
        )
        assert res["ok"], res
        return res["job"]

    def submit(
        self,
        engine,
        *,
        request_id,
        purpose="extraction",
        mode="pilot",
        extraction_ref=None,
        pilot_ref=None,
        artifact_id="a-run",
        revision_id="rev-run",
        config_ref=None,
        report_refs=None,
        hosted_ref=None,
        rights=None,
        credential_env="OPENROUTER_API_KEY",
    ):
        parameters = {
            "config_ref": self.config_ref if config_ref is None else config_ref,
            "report_refs": [self.report_ref] if report_refs is None else report_refs,
            "purpose": purpose,
            "mode": mode,
            "hosted_ref": self.hosted_ref if hosted_ref is None else hosted_ref,
            "extraction_ref": extraction_ref,
            "pilot_ref": pilot_ref,
            "artifact_id": artifact_id,
            "revision_id": revision_id,
            "rights": rights
            if rights is not None
            else fx._rights(self.project_id, artifact_id, revision_id),
            "credential_env": credential_env,
        }
        return engine.handle(
            _req(
                "discovery_submit",
                project_id=self.project_id,
                request_id=request_id,
                mode=worker_extraction.MODE_EXTRACT,
                parameters=parameters,
            )
        )

    def _queue_completion(self):
        self.transport.responses.append(
            fx._completion(fx._extraction_output(self.passages, self.source_keys))
        )


# --------------------------------------------------------------------------- #
# Strict submission validation (before any write)
# --------------------------------------------------------------------------- #
class ExtractSubmitValidationTest(ExtractionWorkerBase):
    def test_invalid_parameters_rejected_without_writes(self) -> None:
        engine = self.engine()
        bad_purpose = self.submit(engine, request_id="bad-1", purpose="bogus")
        self.assertFalse(bad_purpose["ok"])
        self.assertIn(bad_purpose["code"], {"bad_type", "bad_value"})

        bad_mode = self.submit(engine, request_id="bad-2", mode="bogus")
        self.assertEqual(bad_mode["code"], "bad_value")

        # quality requires an extraction_ref; pilot forbids a pilot_ref.
        missing_ref = self.submit(engine, request_id="bad-3", purpose="quality")
        self.assertEqual(missing_ref["code"], "bad_value")
        extra_pilot = self.submit(
            engine, request_id="bad-4", pilot_ref=self.config_ref
        )
        self.assertEqual(extra_pilot["code"], "bad_value")

        bad_credential = self.submit(
            engine, request_id="bad-5", credential_env="SOME_OTHER_KEY"
        )
        self.assertEqual(bad_credential["code"], "bad_value")

        # No durable job was created for any invalid request.
        with engine.open_repository(self.project_id) as repo:
            self.assertEqual(repo.list(), [])

    def test_wrong_rights_rejected_without_writes(self) -> None:
        engine = self.engine()
        wrong = self.submit(
            engine,
            request_id="bad-rights",
            rights=fx._rights(self.project_id, "a-other", "rev-other"),
        )
        self.assertFalse(wrong["ok"])
        self.assertEqual(wrong["code"], "bad_value")
        with engine.open_repository(self.project_id) as repo:
            self.assertEqual(repo.list(), [])


# --------------------------------------------------------------------------- #
# Queued admission + asynchronous completion
# --------------------------------------------------------------------------- #
class ExtractAdmissionAndCompletionTest(ExtractionWorkerBase):
    def test_submit_returns_queued_admission_then_completes(self) -> None:
        self._queue_completion()
        engine = self.engine(api_key="sk-or-test")
        res = self.submit(engine, request_id="run-1")
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["queued"])
        job = res["job"]
        self.assertEqual(job["task_kind"], "extraction_run")
        self.assertEqual(job["state"], "queued")
        self.assertIsNone(job.get("accepted_result"))

        job_id = job["job_id"]
        self._drive(engine)
        job = self._job(engine, job_id)
        self.assertEqual(job["state"], "completed")
        accepted = job["accepted_result"]
        self.assertEqual(accepted["result_kind"], "extraction_run")
        result = accepted["result"]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["outcome"], "completed")
        self.assertIsNotNone(result["run_ref"])
        self.assertEqual(self.transport.call_count, 1)

        # The run receipt is readable through the public runner seam.
        receipt = require_run(self.store, result["run_ref"], purpose="extraction")
        self.assertEqual(set(receipt), set(workflow.RECEIPT_KEYS))
        self.assertEqual(receipt["observed_model"], fx._MODEL)

        # The credential and the raw model output are never in the job JSON.
        self.assertNotIn("sk-or-test", json.dumps(job))
        self.assertNotIn("raw_output", json.dumps(result))


# --------------------------------------------------------------------------- #
# Missing credential / missing gate => zero POST
# --------------------------------------------------------------------------- #
class ExtractRefusalTest(ExtractionWorkerBase):
    def test_missing_credential_refused_zero_post(self) -> None:
        self._queue_completion()
        engine = self.engine(api_key=None)
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}):
            res = self.submit(engine, request_id="run-nokey")
            self.assertTrue(res["ok"])
            job_id = res["job"]["job_id"]
            self._drive(engine)
        self.assertEqual(self.transport.call_count, 0)
        job = self._job(engine, job_id)
        self.assertEqual(job["state"], "waiting_human")
        self.assertIsNone(job.get("accepted_result"))

    def test_missing_gate_fails_closed_zero_post(self) -> None:
        # A second hosted-use revision whose named gate is never approved.
        hosted = self._put_document(
            "a-hosted2",
            "rev-hosted2",
            fx._hosted_document(self.config_ref, [self.report_ref]),
            dependencies=[self.config_ref, self.report_ref, self.source_ref],
        )
        self._queue_completion()
        engine = self.engine(api_key="sk-or-test")
        res = self.submit(engine, request_id="run-nogate", hosted_ref=hosted)
        self.assertTrue(res["ok"])
        job_id = res["job"]["job_id"]
        self._drive(engine)
        self.assertEqual(self.transport.call_count, 0)
        job = self._job(engine, job_id)
        self.assertEqual(job["state"], "waiting_human")
        self.assertIsNone(job.get("accepted_result"))


# --------------------------------------------------------------------------- #
# Replay / recovery
# --------------------------------------------------------------------------- #
class ExtractReplayTest(ExtractionWorkerBase):
    def test_replay_same_request_id_is_one_run(self) -> None:
        self._queue_completion()
        engine = self.engine(api_key="sk-or-test")
        first = self.submit(engine, request_id="run-1")
        job_id = first["job"]["job_id"]
        self._drive(engine)
        self.assertEqual(self.transport.call_count, 1)

        second = self.submit(engine, request_id="run-1")
        self.assertTrue(second["ok"])
        self.assertEqual(second["job"]["job_id"], job_id)
        self._drive(engine)
        self.assertEqual(self.transport.call_count, 1)

    def test_unknown_outcome_never_reposts_on_recovery(self) -> None:
        # A transport failure is an ambiguous outcome. The bounded retry sends
        # NEW identities (never the same request) and, when the per-stage
        # overhead cap refuses a further attempt, resolves the run to a
        # definitive non-completion (the single serial remote slot freed).
        # Recovery then replays the terminal outcome with zero POST.
        from review_article_agent.extraction import runner as runner_mod
        from review_article_agent.runtime.dispatch import (
            runtime_dispatch_remote_state,
        )

        self.transport.responses.append(RuntimeError("ambiguous transport failure"))
        engine = self.engine(api_key="sk-or-test")
        res = self.submit(engine, request_id="run-unknown")
        job_id = res["job"]["job_id"]
        with mock.patch.object(runner_mod, "_SLEEP", lambda _delay: None):
            self._drive(engine)
        # base + one retry (-r1); each attempt is a distinct request identity.
        self.assertEqual(self.transport.call_count, 2)
        job = self._job(engine, job_id)
        # A terminal run (here a definitive non-completion) is the job's accepted
        # result; the run status/outcome distinguishes it.
        self.assertEqual(job["state"], "completed")
        self.assertFalse(job["outcome_unresolved"])
        accepted = job.get("accepted_result")
        self.assertIsNotNone(accepted)
        self.assertEqual(accepted["result"]["status"], "failed")
        self.assertEqual(accepted["result"]["outcome"], "failed")
        # No ambiguous row is left occupying the single serial remote slot.
        self.assertEqual(
            runtime_dispatch_remote_state(self.store.connection, self.project_id),
            "none",
        )

        # A restart proves the outcome by replay and issues no POST.
        engine.close()
        replay_transport = fx._FakeTransport()
        engine2 = self.engine(
            transport=replay_transport, registry=Registry(str(self.tmp / "app"))
        )
        engine2.reconcile_startup()
        self._drive(engine2)
        self.assertEqual(replay_transport.call_count, 0)
        self.assertEqual(self._job(engine2, job_id)["state"], "completed")

    def test_persisted_run_read_seam(self) -> None:
        self._queue_completion()
        engine = self.engine(api_key="sk-or-test")
        res = self.submit(engine, request_id="run-1")
        self._drive(engine)
        self.assertTrue(res["ok"])
        stored = worker_extraction.persisted_extraction_run(
            self.store, request_id="run-1"
        )
        self.assertIsNotNone(stored)
        self.assertEqual(stored["status"], "completed")
        self.assertIsNone(
            worker_extraction.persisted_extraction_run(self.store, request_id="absent")
        )


# --------------------------------------------------------------------------- #
# CLI surface: extraction run submit / status
# --------------------------------------------------------------------------- #
class ExtractionRunCliTest(cli_fx.DiscoveryJobsCliBase):
    def _refs_file(self):
        return self.write_json(
            "refs.json", [{"artifact_id": "a-rep", "revision_id": "rev-rep"}]
        )

    def _submit_args(self, pid):
        rights = self.write_json(
            "rights.json", cli_fx._rights_record(pid, "a-run", "rev-run")
        )
        return [
            "extraction", "run", "submit", "--project", pid,
            "--config-artifact-id", "a-config", "--config-revision-id", "rev-config",
            "--report-refs-file", str(self._refs_file()),
            "--hosted-artifact-id", "a-hosted", "--hosted-revision-id", "rev-hosted",
            "--purpose", "extraction", "--mode", "pilot",
            "--receipt-artifact-id", "a-run", "--receipt-revision-id", "rev-run",
            "--rights-file", str(rights),
            "--credential-env", "OPENROUTER_API_KEY",
            "--request-id", "run-1",
        ]

    def test_run_submit_exact_and_queued(self) -> None:
        pid = self.create_project()
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}):
            with self.fake_worker() as captured:
                out = self.cli(*self._submit_args(pid))
        request = captured["request"]
        self.assertEqual(request["mode"], "extraction_run")
        parameters = request["parameters"]
        self.assertEqual(parameters["purpose"], "extraction")
        self.assertEqual(parameters["mode"], "pilot")
        self.assertEqual(parameters["credential_env"], "OPENROUTER_API_KEY")
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

    def test_run_submit_key_value_is_never_a_flag(self) -> None:
        pid = self.create_project()
        args = self._submit_args(pid)
        args.extend(["--key", "sk-or-secret"])
        self.cli(*args, expect=2)

    def test_run_status_reads_recorded_run(self) -> None:
        pid = self.create_project()

        def _make(store):
            repo = JobRepository(store)
            job = repo.submit("run-1", task_kind="extraction_run", parameters={})
            attempt = repo.begin_attempt(job.job_id, dispatch_kind="local")
            repo.accept_result(
                attempt.attempt_id,
                result_kind="extraction_run",
                result={
                    "kind": "extraction_run",
                    "mode": "extraction_run",
                    "status": "completed",
                    "outcome": "completed",
                    "run_ref": self.ref(pid, "a-run", "rev-run"),
                    "authorizing": False,
                },
            )

        self.with_store(pid, _make)
        out = self.cli(
            "extraction", "run", "status", "--project", pid, "--request-id", "run-1"
        )
        self.assertEqual(out["run"]["status"], "completed")
        self.assertEqual(out["run"]["run_ref"]["artifact_id"], "a-run")
        self.assertFalse(out["authorizes_extraction"])
        self.assertFalse(out["authorizes_synthesis"])

    def test_run_status_unknown_request_id_refused(self) -> None:
        pid = self.create_project()
        self.cli(
            "extraction", "run", "status", "--project", pid, "--request-id", "absent",
            expect=2,
        )

    def test_help_documents_run_commands(self) -> None:
        parser = cli_fx.cli_main.build_parser()
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit):
                parser.parse_args(["extraction", "run", "submit", "--help"])
        text = buf.getvalue()
        self.assertIn("--credential-env", text)
        self.assertIn("--report-refs-file", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
