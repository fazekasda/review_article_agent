"""Step 06 slice J1: detached ``acquire`` jobs through the real worker engine.

Drives the actual :class:`WorkerEngine` (registry, store, ``JobRepository``,
serial detached pool) with injected no-egress official GET fixtures. Real
detached-process acceptance is a later task; this exercises the engine lifecycle.
"""

from __future__ import annotations

import copy
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

import test_acquisition_execution as acq  # noqa: E402

from review_article_agent.acquisition.execution import (  # noqa: E402
    AcquisitionExecutionError,
    AcquisitionExecutor,
)
from review_article_agent.runtime.jobs import JobRepository  # noqa: E402
from review_article_agent.storage import ProjectStore, Registry, paths  # noqa: E402
from review_article_agent.worker import acquisition as acquisition_jobs  # noqa: E402
from review_article_agent.worker import discovery as discovery_jobs  # noqa: E402
from review_article_agent.worker.engine import PROTOCOL_VERSION, WorkerEngine  # noqa: E402

_OPTIONS = {"rate_spacing": 0.0, "retry_delay": 0.0, "max_attempts": 1}


def _req(op, **kw):
    return {"version": PROTOCOL_VERSION, "op": op, **kw}


class AcquireWorkerBase(acq.Base):
    def engine(self, net=None, *, registry=None):
        engine = WorkerEngine(
            str(self.tmp / "state"),
            registry=registry or self.registry,
            acquisition_transport=net,
        )
        self.addCleanup(engine.close)
        return engine

    def params(self, request_id, *, candidate=None, **over):
        ex = self.executor(acq.Net())
        args = self.args(ex, request_id, candidate=candidate, **over)
        del args["request_id"]
        args["transport_options"] = dict(_OPTIONS)
        return args

    def submit(self, engine, request_id, parameters, *, project_id=None):
        return engine.handle(
            _req(
                "discovery_submit",
                project_id=project_id or self.project_id,
                request_id=request_id,
                mode="acquire",
                parameters=parameters,
            )
        )

    def job(self, engine, job_id):
        res = engine.handle(_req("job_status", project_id=self.project_id, job_id=job_id))
        self.assertTrue(res["ok"], res)
        return res["job"]

    def drive(self, engine, timeout=15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            hint = engine.tick()
            if hint is None:
                return
            if hint > 0:
                time.sleep(min(hint, 0.02))
        self.fail("engine did not settle")

    def run_job(self, engine, request_id, parameters):
        res = self.submit(engine, request_id, parameters)
        self.assertTrue(res["ok"], res)
        job_id = res["job"]["job_id"]
        self.drive(engine)
        return job_id, self.job(engine, job_id)

    def checkpoint(self, job_id):
        store = ProjectStore.open_at(self.app_dir, expected_project_id=self.project_id)
        try:
            repo = JobRepository(store)
            return [a.checkpoint for a in repo.attempts(job_id)]
        finally:
            store.close()

    def stored_params(self, job_id):
        return JobRepository(self.store).get(job_id).parameters

    def e1_acquire(self, net, request_id, params):
        ex = self.executor(net)
        rest = {
            k: v for k, v in params.items()
            if k not in ("freeze_ref", "candidate_id", "transport_options")
        }
        return ex.acquire(
            params["freeze_ref"], params["candidate_id"], request_id=request_id, **rest
        )


class AdmissionTest(AcquireWorkerBase):
    def test_admission_is_queued_not_completion_and_validates_before_writes(self):
        net = acq.Net(acq.ok(self.full_text()))
        engine = self.engine(net)
        res = self.submit(engine, "adm-1", self.params("adm-1"))
        self.assertTrue(res["ok"])
        self.assertTrue(res["queued"])
        self.assertEqual(res["job"]["task_kind"], "acquisition_fulltext")
        self.assertEqual(res["job"]["state"], "queued")
        self.assertNotIn("accepted_result", res["job"])
        self.assertEqual(net.urls, [])  # admission performs no GET

        bad = []
        base = self.params("adm-bad")
        for key in ("request_id", "url", "actor", "lowlevel", "authorizing", "extra"):
            bad.append(dict(base, **{key: "x"}))
        missing = dict(base)
        del missing["terms_evidence"]
        bad.append(missing)
        bad.append(dict(base, transport_options={"transport": "x"}))
        bad.append(dict(base, transport_options={"max_attempts": 99}))
        bad.append(dict(base, transport_options={"timeout": True}))
        bad.append(dict(base, candidate_id=7))
        bad.append(dict(base, freeze_ref={"project_id": "p", "artifact_id": "a", "revision_id": "r"}))
        denied = copy.deepcopy(base)
        denied["source_rights"]["operations"]["storage"] = "denied"
        bad.append(denied)
        terms = copy.deepcopy(base)
        terms["terms_evidence"]["storage"]["terms_ref"] = "lic:other"
        bad.append(terms)
        for index, parameters in enumerate(bad):
            out = self.submit(engine, f"adm-bad-{index}", parameters)
            self.assertFalse(out["ok"], parameters)
            self.assertIn(out["code"], ("bad_type", "bad_value"))
        with engine.open_repository(self.project_id) as repo:
            self.assertEqual([j.request_id for j in repo.list()], ["adm-1"])
        self.assertEqual(net.urls, [])


class CompletionAndReplayTest(AcquireWorkerBase):
    def test_success_journal_privacy_and_exact_replay(self):
        body = self.full_text()
        net = acq.Net(acq.ok(body))
        engine = self.engine(net)
        snapshots = []
        original = acquisition_jobs.AcquireJournal._persist

        def spy(journal, allow_draining):
            original(journal, allow_draining)
            snapshots.append((len(net.urls), allow_draining, journal.pending is not None))

        params = self.params("ok-1")
        with mock.patch.object(acquisition_jobs.AcquireJournal, "_persist", spy):
            job_id, job = self.run_job(engine, "ok-1", params)
        self.assertEqual(job["state"], "completed")
        self.assertEqual(len(net.urls), 1)
        # may-have-dispatched mark BEFORE the GET (no drain allowed), result after
        self.assertEqual(snapshots, [(0, False, True), (1, True, False)])

        accepted = job["accepted_result"]
        self.assertEqual(accepted["result_kind"], "acquisition_retrieval")
        result = accepted["result"]
        self.assertEqual(result["status"], "retrieved")
        self.assertEqual(result["validation_status"], "ready")
        self.assertEqual(result["attempt_count"], 1)
        self.assertIs(result["authorizing"], False)
        self.assertEqual(result["eligibility"], "eligibility_not_assessed")
        self.assertIsNotNone(result["source_ref"])
        self.assertEqual(
            set(result),
            {
                "kind", "mode", "report_ref", "status", "source_ref", "occurrence_ref",
                "disposition", "validation_status", "validation_check", "attempt_count",
                "failure_count", "interrupted_reason", "authorizing", "eligibility",
            },
        )

        # privacy: no bytes/urls/identifiers/terms/rights in any default status
        listed = engine.handle(_req("job_list", project_id=self.project_id))
        dumped = json.dumps([job, listed], sort_keys=True)
        for forbidden in (
            "PMC111", "fullTextXML", "lic:pd-syn", self.with_pmcid, "source_rights",
            "terms_evidence", "Hello scientific", "base64", "https://",
        ):
            self.assertNotIn(forbidden, dumped)

        # the journal persists bounded safe metadata only
        (checkpoint,) = self.checkpoint(job_id)
        self.assertIs(checkpoint["discovery_predispatch"], True)
        journal = checkpoint["acquisition"]
        self.assertEqual(journal["max_get"], 2)
        self.assertIsNone(journal["pending"])
        (entry,) = journal["attempts"]
        self.assertEqual(entry["outcome"], "response")
        self.assertEqual(entry["status"], 200)
        self.assertEqual(entry["byte_count"], len(body))
        raw = json.dumps(checkpoint)
        for forbidden in ("https://", "Hello scientific", "lic:pd-syn", "PMC111"):
            self.assertNotIn(forbidden, raw)

        # exact request-key replay returns the same job; no second GET
        again = self.submit(engine, "ok-1", copy.deepcopy(params))
        self.assertTrue(again["ok"])
        self.assertEqual(again["job"]["job_id"], job_id)
        self.assertEqual(again["job"]["state"], "completed")
        self.drive(engine)
        self.assertEqual(len(net.urls), 1)
        # divergent parameters under the same key conflict
        diverged = self.submit(engine, "ok-1", dict(params, candidate_id=self.no_pmcid))
        self.assertFalse(diverged["ok"])
        self.assertEqual(diverged["code"], "conflict")

    def test_restart_replays_durable_report_with_zero_get(self):
        engine = self.engine(acq.Net())
        params = self.params("rs-1")
        res = self.submit(engine, "rs-1", params)
        job_id = res["job"]["job_id"]
        engine.close()
        # the first run's report is durable but its accept was lost (crash)
        net1 = acq.Net(acq.ok(self.full_text()))
        first = self.e1_acquire(net1, "rs-1", self.stored_params(job_id))
        self.assertEqual(first["status"], "retrieved")
        store = ProjectStore.open_at(self.app_dir, expected_project_id=self.project_id)
        try:
            repo = JobRepository(store)
            attempt = repo.begin_attempt(job_id, dispatch_kind="local")
            repo.record_checkpoint(
                attempt.attempt_id, discovery_jobs.predispatch_checkpoint("acquire")
            )
        finally:
            store.close()

        net2 = acq.Net(acq.ok(self.full_text()))
        engine2 = self.engine(net2, registry=Registry(self.tmp / "state"))
        engine2.reconcile_startup()
        self.drive(engine2)
        job = self.job(engine2, job_id)
        self.assertEqual(job["state"], "completed")
        self.assertEqual(job["attempts_total"], 1)
        self.assertEqual(net2.urls, [])
        self.assertEqual(
            job["accepted_result"]["result"]["report_ref"], first["report_ref"]
        )

    def test_unproven_possibly_sent_attempt_is_ambiguous_never_resent(self):
        engine = self.engine(acq.Net())
        res = self.submit(engine, "amb-1", self.params("amb-1"))
        job_id = res["job"]["job_id"]
        engine.close()
        store = ProjectStore.open_at(self.app_dir, expected_project_id=self.project_id)
        try:
            repo = JobRepository(store)
            attempt = repo.begin_attempt(job_id, dispatch_kind="local")
            repo.record_checkpoint(
                attempt.attempt_id,
                acquisition_jobs.journal_checkpoint(
                    [], {"attempt_index": 1, "source": "europepmc_oa_fulltext_xml"}, 2
                ),
            )
        finally:
            store.close()
        net2 = acq.Net(acq.ok(self.full_text()))
        engine2 = self.engine(net2, registry=Registry(self.tmp / "state"))
        engine2.reconcile_startup()
        self.drive(engine2)
        job = self.job(engine2, job_id)
        self.assertEqual(job["state"], "waiting_human")
        self.assertNotIn("accepted_result", job)
        self.assertEqual(net2.urls, [])
        self.drive(engine2)
        self.assertEqual(net2.urls, [])
        self.assertEqual(self.job(engine2, job_id)["state"], "waiting_human")


class RefusalTest(AcquireWorkerBase):
    def test_unapproved_freeze_refuses_with_zero_get(self):
        net = acq.Net(acq.ok(self.full_text()))
        engine = self.engine(net)
        params = self.params("stale-1", freeze_ref=self.corpus_ref)
        job_id, job = self.run_job(engine, "stale-1", params)
        self.assertEqual(job["state"], "waiting_human")
        self.assertEqual(job["wait_reason"], "discovery_blocked")
        self.assertNotIn("accepted_result", job)
        self.assertEqual(net.urls, [])
        self.assertEqual(self.count("artifact") > 0, True)
        self.assertIsNone(
            AcquisitionExecutor(
                self.store, registry=self.registry, activation_token=self.token
            ).find_request("stale-1")
        )

    def test_unfrozen_candidate_refuses_with_zero_get(self):
        net = acq.Net(acq.ok(self.full_text()))
        engine = self.engine(net)
        params = self.params("cand-1", candidate="cand-not-in-freeze")
        _, job = self.run_job(engine, "cand-1", params)
        self.assertEqual(job["state"], "waiting_human")
        self.assertEqual(net.urls, [])

    def test_post_get_local_refusal_survives_as_waiting_human_with_journal(self):
        net = acq.Net(acq.ok(self.full_text()))
        engine = self.engine(net)
        boom = AcquisitionExecutionError("storage_failure")
        with mock.patch.object(AcquisitionExecutor, "_write", side_effect=boom):
            job_id, job = self.run_job(engine, "refuse-1", self.params("refuse-1"))
        self.assertEqual(job["state"], "waiting_human")
        self.assertNotIn("accepted_result", job)
        self.assertEqual(len(net.urls), 1)
        (checkpoint,) = self.checkpoint(job_id)
        (entry,) = checkpoint["acquisition"]["attempts"]
        self.assertEqual(entry["outcome"], "response")
        self.assertGreater(entry["byte_count"], 0)

        # restart: no report proves it, so it stays unknown and never re-sends
        net2 = acq.Net(acq.ok(self.full_text()))
        engine2 = self.engine(net2, registry=Registry(self.tmp / "state"))
        engine2.reconcile_startup()
        self.drive(engine2)
        self.assertEqual(self.job(engine2, job_id)["state"], "waiting_human")
        self.assertEqual(net2.urls, [])

    def test_journal_failure_after_send_is_unknown_and_not_retried(self):
        net = acq.Net(acq.not_found(), acq.ok(self.full_text()))
        engine = self.engine(net)
        original = acquisition_jobs.AcquireJournal._persist

        def flaky(journal, allow_draining):
            if allow_draining:
                raise RuntimeError("journal write failed")
            original(journal, allow_draining)

        with mock.patch.object(acquisition_jobs.AcquireJournal, "_persist", flaky):
            job_id, job = self.run_job(engine, "jf-1", self.params("jf-1"))
        self.assertEqual(job["state"], "waiting_human")
        self.assertEqual(len(net.urls), 1)  # no fallback / retry after the lost journal
        self.assertNotIn("accepted_result", job)
        self.drive(engine)
        self.assertEqual(len(net.urls), 1)


class StopAndArbitrationTest(AcquireWorkerBase):
    def start_waterfall(self, request_id):
        first, release = threading.Event(), threading.Event()

        def after_call(n):
            if n == 1:
                first.set()
                release.wait(timeout=10.0)

        net = acq.Net(acq.not_found(), acq.ok(self.full_text()), after_call=after_call)
        engine = self.engine(net)
        res = self.submit(engine, request_id, self.params(request_id))
        job_id = res["job"]["job_id"]
        engine.tick()
        self.assertTrue(first.wait(timeout=10.0), "first GET never started")
        return engine, net, job_id, release

    def assert_first_observation_kept(self, engine, net, job_id, state):
        self.assertEqual(len(net.urls), 1)  # no fallback after the stop
        job = self.job(engine, job_id)
        self.assertEqual(job["state"], state)
        result = job["accepted_result"]["result"]
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["attempt_count"], 1)
        self.assertIsNone(result["source_ref"])
        self.assertEqual(job["wait_reason"], "discovery_interrupted")
        (checkpoint,) = self.checkpoint(job_id)
        self.assertEqual(len(checkpoint["acquisition"]["attempts"]), 1)
        self.assertIsNone(checkpoint["acquisition"]["pending"])

    def test_pause_mid_waterfall_preserves_first_observation(self):
        engine, net, job_id, release = self.start_waterfall("pause-1")
        try:
            self.assertTrue(engine.handle(_req("pause", project_id=self.project_id))["ok"])
        finally:
            release.set()
        self.drive(engine)
        self.assert_first_observation_kept(engine, net, job_id, "paused")

    def test_cancel_mid_waterfall_preserves_first_observation(self):
        engine, net, job_id, release = self.start_waterfall("cancel-1")
        try:
            res = engine.handle(_req("cancel", project_id=self.project_id, job_id=job_id))
            self.assertTrue(res["ok"])
        finally:
            release.set()
        self.drive(engine)
        self.assert_first_observation_kept(engine, net, job_id, "cancelled")

    def test_drain_mid_waterfall_preserves_first_observation(self):
        engine, net, job_id, release = self.start_waterfall("drain-1")
        try:
            self.assertTrue(engine.handle(_req("drain", project_id=self.project_id))["ok"])
        finally:
            release.set()
        self.drive(engine)
        self.assert_first_observation_kept(engine, net, job_id, "paused")

    def test_one_active_project_and_hold_guards(self):
        other = self.registry.create_project(self.tmp / "other", name="Q").project_id
        engine = self.engine(acq.Net(acq.ok(self.full_text())))
        # parameters are bound to their own project: never accepted for another
        wrong = self.submit(engine, "arb-1", self.params("arb-1"), project_id=other)
        self.assertFalse(wrong["ok"])
        self.assertEqual(wrong["code"], "bad_value")
        self.assertTrue(engine.handle(_req("pause", project_id=self.project_id))["ok"])
        paused = self.submit(engine, "arb-2", self.params("arb-2"))
        self.assertFalse(paused["ok"])
        self.assertEqual(paused["code"], "denied")
        with engine.open_repository(other) as repo:
            self.assertEqual(repo.list(), [])

    def test_switch_is_blocked_while_a_get_is_in_flight(self):
        other = self.registry.create_project(self.tmp / "other", name="Q").project_id
        engine, net, job_id, release = self.start_waterfall("sw-1")
        try:
            res = engine.handle(_req("switch", project_id=other))
            self.assertFalse(res["ok"])
            self.assertEqual(
                self.registry.activation().active_project_id, self.project_id
            )
        finally:
            release.set()
        self.drive(engine)

    def test_draining_refuses_new_acquire_and_zero_get(self):
        net = acq.Net(acq.ok(self.full_text()))
        engine = self.engine(net)
        self.assertTrue(engine.handle(_req("drain", project_id=self.project_id))["ok"])
        res = self.submit(engine, "drn-2", self.params("drn-2"))
        self.assertFalse(res["ok"])
        self.assertIn(res["code"], ("denied", "draining"))
        self.assertEqual(net.urls, [])


def _status(status, body=b"x", media="application/xml"):
    return acq.TransportResponse(status=status, body=body, headers={"Content-Type": media})


class SendAdmissionRaceTest(AcquireWorkerBase):
    """Send admission is linearizable with pause/cancel and the captured lease.

    The coordinator's repro patches ``AcquireJournal._persist`` so the *first*
    ``allow_draining=False`` call commits a pause/project-lease change **before**
    the original admission write. No GET may then be admitted, and the old run
    must never be rebound to the project's *current* token.
    """

    def _run_with_before_persist_hook(self, request_id, hook):
        net = acq.Net(acq.ok(self.full_text()))
        engine = self.engine(net)
        res = self.submit(engine, request_id, self.params(request_id))
        job_id = res["job"]["job_id"]
        original = acquisition_jobs.AcquireJournal._persist
        state = {"done": False}

        def patched(journal, allow_draining):
            if not allow_draining and not state["done"]:
                state["done"] = True
                hook(engine)
            return original(journal, allow_draining)

        with mock.patch.object(acquisition_jobs.AcquireJournal, "_persist", patched):
            self.drive(engine)
        return engine, net, job_id

    def test_pause_committed_before_admission_issues_no_get(self):
        def pause(engine):
            out = engine.handle(_req("pause", project_id=self.project_id))
            self.assertTrue(out["ok"], out)

        engine, net, job_id = self._run_with_before_persist_hook("pz-send", pause)
        self.assertEqual(net.urls, [])
        job = self.job(engine, job_id)
        self.assertEqual(job["state"], "paused")
        self.assertNotIn("accepted_result", job)

    def test_cancel_committed_before_admission_issues_no_get(self):
        holder = {}

        def cancel(engine):
            out = engine.handle(
                _req("cancel", project_id=self.project_id, job_id=holder["job_id"])
            )
            self.assertTrue(out["ok"], out)

        net = acq.Net(acq.ok(self.full_text()))
        engine = self.engine(net)
        res = self.submit(engine, "cx-send", self.params("cx-send"))
        holder["job_id"] = res["job"]["job_id"]
        original = acquisition_jobs.AcquireJournal._persist
        state = {"done": False}

        def patched(journal, allow_draining):
            if not allow_draining and not state["done"]:
                state["done"] = True
                cancel(engine)
            return original(journal, allow_draining)

        with mock.patch.object(acquisition_jobs.AcquireJournal, "_persist", patched):
            self.drive(engine)
        self.assertEqual(net.urls, [])
        job = self.job(engine, holder["job_id"])
        self.assertEqual(job["state"], "cancelled")
        self.assertNotIn("accepted_result", job)

    def test_captured_stale_lease_never_rebinds_to_current_token(self):
        """The lease changes between E1's lease check and the admission write."""

        def rebind(engine):
            self.registry.release_execution(self.token)
            new = self.registry.activate_execution(self.project_id).activation_token
            self.assertNotEqual(new, self.token)

        engine, net, job_id = self._run_with_before_persist_hook("st-send", rebind)
        self.assertEqual(net.urls, [])  # never admitted under the new token
        job = self.job(engine, job_id)
        self.assertNotIn("accepted_result", job)
        # an unknown/refused outcome requires the owner, never a silent retry
        self.assertEqual(job["state"], "waiting_human")

    def test_admitted_inflight_completes_observation_after_pause(self):
        body = self.full_text()
        started, release = threading.Event(), threading.Event()

        def on_call(n):
            if n == 1:
                started.set()
                release.wait(timeout=10.0)

        net = acq.Net(acq.ok(body), after_call=on_call)
        engine = self.engine(net)
        res = self.submit(engine, "adm-pz", self.params("adm-pz"))
        job_id = res["job"]["job_id"]
        engine.tick()
        self.assertTrue(started.wait(timeout=10.0), "GET never started")
        # admission already committed before the GET; pause may follow safely
        self.assertTrue(engine.handle(_req("pause", project_id=self.project_id))["ok"])
        release.set()
        self.drive(engine)
        self.assertEqual(len(net.urls), 1)
        job = self.job(engine, job_id)
        self.assertEqual(job["state"], "paused")
        result = job["accepted_result"]["result"]
        self.assertEqual(result["status"], "retrieved")
        self.assertEqual(result["attempt_count"], 1)
        (checkpoint,) = self.checkpoint(job_id)
        self.assertEqual(len(checkpoint["acquisition"]["attempts"]), 1)
        self.assertIsNone(checkpoint["acquisition"]["pending"])

    def test_retry_within_one_attempt_does_not_deadlock(self):
        # Two GETs on the same attempt (a retryable 503 then a 200) exercise the
        # admission transaction and the nested checkpoint savepoint twice.
        net = acq.Net(_status(503), acq.ok(self.full_text()))
        engine = self.engine(net)
        params = self.params("retry-1")
        params["transport_options"] = dict(_OPTIONS, max_attempts=2)
        job_id, job = self.run_job(engine, "retry-1", params)
        self.assertEqual(job["state"], "completed")
        self.assertEqual(len(net.urls), 2)
        (checkpoint,) = self.checkpoint(job_id)
        self.assertEqual(len(checkpoint["acquisition"]["attempts"]), 2)


class ModuleSeamTest(AcquireWorkerBase):
    def test_default_lowlevel_is_the_acquisition_transport(self):
        from review_article_agent.acquisition.transport import http_get

        engine = WorkerEngine(str(self.tmp / "state"), registry=self.registry)
        self.addCleanup(engine.close)
        self.assertIs(engine._acquisition_lowlevel, http_get)
        with self.assertRaises(ValueError):
            WorkerEngine(
                str(self.tmp / "state"), registry=self.registry, acquisition_transport="x"
            )

    def test_mode_and_task_kind_registry(self):
        self.assertIn("acquire", discovery_jobs.DISCOVERY_MODES)
        self.assertEqual(discovery_jobs.task_kind_for_mode("acquire"), "acquisition_fulltext")
        self.assertEqual(discovery_jobs.mode_for_task_kind("acquisition_fulltext"), "acquire")
        self.assertTrue(discovery_jobs.is_discovery_task_kind("acquisition_fulltext"))
        self.assertEqual(discovery_jobs.result_kind_for_mode("acquire"), "acquisition_retrieval")
        for mode in ("query", "seed", "metadata_import", "expand", "lead"):
            self.assertIn(mode, discovery_jobs.DISCOVERY_MODES)

    def test_status_projection_drops_unknown_fields(self):
        projected = discovery_jobs.status_projection(
            "acquisition_retrieval",
            {"kind": "acquisition_retrieval", "status": "retrieved", "terms": "secret"},
        )
        self.assertEqual(projected, {"kind": "acquisition_retrieval", "status": "retrieved"})


if __name__ == "__main__":
    unittest.main()
