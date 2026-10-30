"""Step 05 J1 admission / interruption **adversarial** acceptance (E1B follow-up).

Closes three exact critical seams not already covered by the J0/J1 suites:

1. **Stale corpus / drained (released-activation) token mid-expansion** — the
   low-level provider returns the first relation page, then the test supersedes
   the corpus head (a legitimate ``ingest``) or begins a registry drain of the
   exact activation token; the *next* relation attempt must perform **zero**
   provider I/O, the already-fetched page/edge stays durably persisted with an
   explicit ``before_request_refused`` gap, and the accepted report is never
   authorizing. The reported request/attempt counts must match the durable
   observation. This is distinct from the existing plan-change case
   (``test_discovery_job_routes.ExpandRoutesTest.test_post_start_plan_change_*``).
2. **Reserved-but-unsent hosted lead at the drain boundary** — a legitimate
   registry drain is triggered through a test patch seam positioned *after*
   ``mark_submitted`` and *before* the single POST; the POST never happens, the
   conservative reserve/sent-claim is retained, and a restart reconciliation with
   an exploding transport **never** re-POSTs, leaving the job ``waiting_human``
   and the dispatch ``submitted`` with an **unknown** (never zero) cost and the
   global remote occupancy still held.
3. **Owner child killed DURING the first fake POST (before any response/accepted
   result)** — a real detached worker process makes its one POST (a durable
   counter file records it) and then blocks before returning; the test kills
   **only its own child**, restarts the same data dir, and proves there is **no**
   additional POST and no fabricated success: the standing attempt/fence is
   reconciled to ``waiting_human``/unknown, the cost stays unknown, and the
   remote occupancy is retained. This is the kill-before-accepted seam, not the
   pre-dispatch timing.

Other J1 outcomes named by the coordinator (paid-unknown caps, pause/cancel/drain
partial pages, cross-project switch gating) are already exercised by
``tests/test_discovery_worker.py`` and ``tests/test_discovery_job_routes.py``;
this module cites those source anchors rather than duplicating the matrix (see the
``Anchors`` note at the bottom).

Everything is synthetic: real ``Registry``/``ProjectStore``, injected
deterministic transports, injected owner-auth seams. No network, provider, model,
upload, or paid call is made.

Run::

    PYTHONPATH=src python3 -m unittest tests.test_step05_job_adversarial -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from _tiers import slow

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

import test_discovery_job_routes as jr  # noqa: E402
import test_discovery_serialization as fx  # noqa: E402
from test_discovery_job_routes import DiscoveryJobRoutesBase  # noqa: E402

from review_article_agent.discovery import lead_runner as runner  # noqa: E402
from review_article_agent.discovery.records import SOURCE_EUROPEPMC  # noqa: E402
from review_article_agent.runtime.dispatch import (  # noqa: E402
    RuntimeDispatchRepository,
    runtime_dispatch_remote_state,
)
from review_article_agent.runtime.ipc import WorkerIPCClient  # noqa: E402
from review_article_agent.storage import ProjectStore, paths  # noqa: E402
from review_article_agent.worker import discovery as discovery_jobs  # noqa: E402


# --------------------------------------------------------------------------- #
# 1. Corpus / token interruption mid-expansion: zero further I/O
# --------------------------------------------------------------------------- #
class ExpansionInterruptionAdversarialTest(DiscoveryJobRoutesBase):
    def _observed_corpus(self, *, tag: str):
        run = self.observe_epmc(
            [
                fx.record(
                    source=SOURCE_EUROPEPMC,
                    source_id="MED:1",
                    pmid="1",
                    pmcid=None,
                    doi=None,
                )
            ],
            request_id=f"run-{tag}",
        )
        return self.ingest_corpus([run.run_id], request_id=f"corpus-{tag}")

    def test_stale_corpus_and_drained_token_stop_further_io(self) -> None:
        for case in ("stale_corpus", "drained_token"):
            with self.subTest(case=case):
                first_called, release = threading.Event(), threading.Event()

                def on_call(n: int, _fc=first_called, _rel=release) -> None:
                    if n == 1:
                        _fc.set()
                        _rel.wait(timeout=10.0)

                # Both directions are scripted; whichever runs first blocks, and
                # the second attempt is refused before it is ever sent.
                provider = jr._CitationProvider(
                    {
                        ("MED", "1", "references"): [
                            jr._CitationProvider.response(
                                jr._ref_payload(1, [jr._cite_entry("2")])
                            )
                        ],
                        ("MED", "1", "citations"): [
                            jr._CitationProvider.response(
                                jr._cit_payload(1, [jr._cite_entry("3")])
                            )
                        ],
                    },
                    on_call=on_call,
                )
                engine = self.engine(provider)
                plan_ref = self.build_plan()
                self.approve_plan(plan_ref)
                corpus_ref = self._observed_corpus(tag=case)
                request_id = f"exp-{case}"
                res = self._submit(
                    engine,
                    request_id=request_id,
                    mode="expand",
                    parameters={
                        "plan_ref": plan_ref,
                        "corpus_ref": corpus_ref,
                        "roots": [{"source": "MED", "id": "1"}],
                    },
                )
                self.assertTrue(res["ok"], res)
                job_id = res["job"]["job_id"]
                engine.tick()
                self.assertTrue(first_called.wait(timeout=10.0))

                if case == "stale_corpus":
                    # Supersede the corpus head through the legitimate ingest seam.
                    superseding = self.observe_epmc(
                        [
                            fx.record(
                                source=SOURCE_EUROPEPMC,
                                source_id="MED:9",
                                pmid="9",
                                pmcid=None,
                                doi=None,
                            )
                        ],
                        request_id=f"run-supersede-{case}",
                    )
                    self.ingest_corpus(
                        [superseding.run_id], request_id=f"corpus-supersede-{case}"
                    )
                else:
                    # Begin a legitimate registry drain of the exact token.
                    token = self.registry.activation().activation_token
                    self.assertIsNotNone(token)
                    self.registry.begin_drain(token)
                release.set()
                self._drive(engine)

                # The refused next attempt did zero I/O: exactly one real request.
                self.assertEqual(len(provider.calls), 1)
                job = self._job(engine, job_id)
                self.assertEqual(job["state"], "completed")
                summary = job["accepted_result"]["result"]
                self.assertIn("before_request_refused", summary["stop_reasons"])
                # Never a fabricated accepted authority/completeness.
                self.assertEqual(
                    summary["guarantees"],
                    {"completeness_claimed": False, "verified_support": False},
                )

                with engine.open_store(self.project_id) as store:
                    full = discovery_jobs.persisted_expansion_report(
                        store, request_id=request_id
                    )
                self.assertIsNotNone(full)
                self.assertFalse(full["guarantees"]["completeness_claimed"])
                self.assertFalse(full["guarantees"]["verified_support"])
                # Reported request/attempt counts match the durable observation.
                self.assertEqual(len(provider.calls), 1)
                self.assertEqual(
                    sum(relation["attempt_count"] for relation in full["relations"]), 1
                )
                # The one observed child edge of the first page is persisted.
                children = [edge["child_source_id"] for edge in full["edges"]]
                self.assertEqual(len(children), 1)
                self.assertIn(children[0], {"MED:2", "MED:3"})


# --------------------------------------------------------------------------- #
# 2. Reserved-but-unsent hosted lead at the drain boundary
# --------------------------------------------------------------------------- #
class ReservedUnsentLeadBoundaryTest(DiscoveryJobRoutesBase):
    def test_drained_before_post_never_sends_and_retains_reserve(self) -> None:
        lead_transport = jr._LeadTransport([jr._completion_body()])
        engine = self.engine(lead_transport=lead_transport, lead_api_key="sk-or-test")
        batch_ref = self.build_lead_batch()
        self.approve_batch(batch_ref)

        real_gate = runner._assert_gate_satisfied
        calls = {"n": 0}

        def drain_after_reserve(store, artifact_id, purpose):
            # First call is the preflight gate (before the reserve); the second is
            # the post-reserve gate, immediately after ``mark_submitted`` and just
            # before the single POST. Drain the exact token there.
            calls["n"] += 1
            if calls["n"] >= 2:
                self.registry.begin_drain(self.registry.activation().activation_token)
            return real_gate(store, artifact_id, purpose)

        with mock.patch.object(runner, "_assert_gate_satisfied", drain_after_reserve):
            res = self._submit(
                engine,
                request_id="lead-unsent",
                mode="lead",
                parameters={
                    "batch_ref": batch_ref,
                    "credential_env": "OPENROUTER_API_KEY",
                },
            )
            self.assertTrue(res["ok"], res)
            job_id = res["job"]["job_id"]
            self._drive(engine)

        # Zero POST; the job never fabricated an accepted result.
        self.assertEqual(lead_transport.call_count, 0)
        job = self._job(engine, job_id)
        self.assertIsNone(job.get("accepted_result"))

        with engine.open_store(self.project_id) as store:
            dispatch = RuntimeDispatchRepository(store).get_dispatch_by_request(
                "lead-unsent"
            )
            remote = runtime_dispatch_remote_state(store.connection, self.project_id)
        # Reserved + submitted (the send claim) is retained; never zero cost; the
        # global remote occupancy is still held.
        self.assertEqual(dispatch.state, "submitted")
        self.assertIsNotNone(dispatch.reserve_usd)
        self.assertEqual(dispatch.cost_state, "unknown")
        self.assertIsNone(dispatch.cost_amount_usd)
        self.assertEqual(remote, "remote")

        # Close/reopen/reconcile with an exploding transport: NEVER re-POST.
        exploding = jr._LeadTransport([jr._completion_body()])
        engine.close()
        from review_article_agent.storage import Registry

        engine2 = self.engine(
            lead_transport=exploding, registry=Registry(self.data_dir)
        )
        engine2.reconcile_startup()
        self._drive(engine2)
        self.assertEqual(exploding.call_count, 0)
        restarted = self._job(engine2, job_id)
        self.assertIn(
            restarted["state"], {"waiting_human", "reconciliation_required"}
        )
        self.assertIsNone(restarted.get("accepted_result"))
        with engine2.open_store(self.project_id) as store:
            dispatch2 = RuntimeDispatchRepository(store).get_dispatch_by_request(
                "lead-unsent"
            )
            remote2 = runtime_dispatch_remote_state(store.connection, self.project_id)
        self.assertIn(dispatch2.state, {"submitted", "unknown"})
        self.assertEqual(dispatch2.cost_state, "unknown")
        self.assertIsNone(dispatch2.cost_amount_usd)
        self.assertEqual(remote2, "remote")
        self.assertNotIn("sk-or-test", json.dumps(restarted))


# --------------------------------------------------------------------------- #
# 3. Real owned child killed DURING the first fake POST, before any response
# --------------------------------------------------------------------------- #
class RealProcessKillDuringPostTest(DiscoveryJobRoutesBase):
    def _open_store(self):
        return ProjectStore.open_at(
            paths.app_dir_for(str(self.root)), expected_project_id=self.project_id
        )

    def _start_child(self, *, counter=None, block=None, release=None):
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(jr.SRC), str(jr.TESTS), env.get("PYTHONPATH", "")]
        )
        env["RAA_FIXTURE_LEAD_MODEL"] = jr._MODEL_ID
        if counter is not None:
            env["RAA_FIXTURE_LEAD_COUNTER"] = str(counter)
        if block is not None:
            env["RAA_FIXTURE_LEAD_BLOCK_FILE"] = str(block)
        if release is not None:
            env["RAA_FIXTURE_LEAD_RELEASE_FILE"] = str(release)
        proc = subprocess.Popen(
            [
                sys.executable,
                str(Path(jr.TESTS) / "step05_process_fixture.py"),
                "--data-dir",
                str(self.data_dir),
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(self._reap_child, proc)
        client = WorkerIPCClient(str(self.data_dir), io_timeout=5.0)
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            try:
                client.request(jr._req("status"))
                return proc, client
            except Exception:
                time.sleep(0.05)
        _out, err = proc.communicate(timeout=5)
        self.fail(
            "fixture worker did not become ready; stderr="
            + err.decode("utf-8", "replace")
        )

    @staticmethod
    def _reap_child(proc: subprocess.Popen) -> None:
        try:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def _job_status(self, client, job_id):
        res = client.request(
            jr._req("job_status", project_id=self.project_id, job_id=job_id)
        )
        return res.get("job") if res.get("ok") else None

    def _poll_job(self, client, job_id, predicate, *, timeout=25.0):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                last = self._job_status(client, job_id)
            except Exception:
                time.sleep(0.05)
                continue
            if last is not None and predicate(last):
                return last
            time.sleep(0.05)
        return last

    @slow
    def test_kill_during_first_fake_post_before_response_never_reposts(self):
        batch_ref = self.build_lead_batch()
        self.approve_batch(batch_ref)
        counter = self.tmp / "post_counter.txt"
        block = self.tmp / "post_started.marker"
        release = self.tmp / "release.marker"

        proc, client = self._start_child(counter=counter, block=block, release=release)
        res = client.request(
            jr._req(
                "discovery_submit",
                project_id=self.project_id,
                request_id="lead-kill",
                mode="lead",
                parameters={
                    "batch_ref": batch_ref,
                    "credential_env": "OPENROUTER_API_KEY",
                },
            )
        )
        self.assertTrue(res["ok"], res)
        job_id = res["job"]["job_id"]

        # Wait until the one fake POST has started and is blocked before any
        # response: the durable counter shows one POST and the marker exists.
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if (
                block.exists()
                and counter.exists()
                and counter.read_text(encoding="utf-8").count("post") >= 1
            ):
                break
            time.sleep(0.05)
        self.assertTrue(block.exists(), "the fake POST never started")
        self.assertEqual(counter.read_text(encoding="utf-8").count("post"), 1)

        before = self._job_status(client, job_id)
        self.assertIsNotNone(before)
        before_attempt = before["current_attempt_id"]
        before_fence = before["current_fence"]
        self.assertIsNone(before.get("accepted_result"))

        # Kill ONLY our own child: the in-flight POST never produces a response.
        proc.kill()
        proc.wait(timeout=10)

        # Restart the same data dir. The counter is shared, so any re-POST would
        # be visible; a restart must never blindly repeat a paid call.
        proc2, client2 = self._start_child(counter=counter)
        job = self._poll_job(
            client2,
            job_id,
            lambda j: j["state"]
            in {"waiting_human", "reconciliation_required", "unknown"},
        )
        self.assertIsNotNone(job, "restart never resolved the in-flight POST")
        self.assertIn(
            job["state"], {"waiting_human", "reconciliation_required", "unknown"}
        )
        self.assertIsNone(job.get("accepted_result"))
        self.assertTrue(job["outcome_unresolved"])
        # The same standing attempt/fence is reconciled; no new attempt was made.
        self.assertEqual(job["current_attempt_id"], before_attempt)
        self.assertEqual(job["current_fence"], before_fence)
        # Exactly one POST across the restart: no blind re-POST.
        self.assertEqual(counter.read_text(encoding="utf-8").count("post"), 1)
        # No credential ever appears in the status projection.
        self.assertNotIn("sk-or", json.dumps(job))

        with self._open_store() as store:
            dispatch = RuntimeDispatchRepository(store).get_dispatch_by_request(
                "lead-kill"
            )
            remote = runtime_dispatch_remote_state(store.connection, self.project_id)
        self.assertIsNotNone(dispatch)
        self.assertIn(dispatch.state, {"submitted", "unknown"})
        self.assertEqual(dispatch.cost_state, "unknown")
        self.assertNotEqual(dispatch.cost_amount_usd, "0")
        self.assertEqual(remote, "remote")

        try:
            client2.request(jr._req("stop"))
        except Exception:
            pass
        proc2.wait(timeout=10)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


# Anchors for the outcomes already covered elsewhere (not duplicated here):
# * paid-unknown caps + never-re-POST: test_discovery_job_routes.LeadRoutesTest
#   .test_unknown_retains_occupancy_and_never_reposts and
#   test_discovery_worker.RestartRecoveryTest.
# * pause/cancel/drain keep partial pages: test_discovery_worker
#   .StopBetweenAttemptsTest.*; test_step05_core_acceptance.ExpansionInterruptedTest.
# * cross-project switch gated until quiescent: test_discovery_worker
#   .DetachedControlTest.test_running_discovery_blocks_another_project;
#   test_discovery_job_routes.ExpandRoutesTest.test_running_expand_blocks_*
