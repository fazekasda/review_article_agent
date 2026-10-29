"""Owner-controlled detached discovery worker jobs (Step 05, J0).

Covers ``src/review_article_agent/worker/discovery.py`` plus the narrow
``worker/engine.py`` hooks that register and schedule the explicit
``discovery_submit`` operation (IPC version 1):

* strict, owner-only ``discovery_submit`` (keys ``project_id`` / ``request_id`` /
  ``mode`` / ``parameters``; no actor/authority field) with per-mode parameters
  validated before any durable write;
* a queued admission (never a completion) that persists one immutable
  ``JobRepository`` job and the same global registry execution token;
* asynchronous execution (a single-slot executor with its own store connection)
  so a long/blocked provider call never blocks the owner control socket;
* the tool's stop check runs before every real provider attempt (pause / cancel /
  drain between attempts stop further network);
* a matching replay creates no duplicate observation and a divergent replay is a
  conflict;
* an unapproved/stale plan performs zero provider I/O;
* one execution-active project: a running discovery job blocks another project;
* restart reconciliation proves an existing observation (completes the same
  fence) or escalates an unknown outcome, never a silent success or blind repeat.

Synthetic data only; the injected low-level transport is a deterministic fake, so
no test performs a network, provider, or model call. One lightweight real-process
test drives the detached worker with an *unapproved* plan (zero provider I/O).

Run::

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_discovery_worker.py'
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.discovery import execution as ex  # noqa: E402
from review_article_agent.discovery.transport import (  # noqa: E402
    DiscoveryRequestInterrupted,
    DiscoveryTransport,
    TransportResponse,
)
from review_article_agent.runtime import owner_auth  # noqa: E402
from review_article_agent.runtime.gates import prepare_gate_snapshot  # noqa: E402
from review_article_agent.runtime.ipc import WorkerIPCClient  # noqa: E402
from review_article_agent.runtime.jobs import JobRepository  # noqa: E402
from review_article_agent.runtime.owner_auth import OwnerApproval  # noqa: E402
from review_article_agent.storage import (  # noqa: E402
    ArtifactRepository,
    ProjectStore,
    Registry,
    paths,
)
from review_article_agent.storage.discovery import DiscoveryRepository  # noqa: E402
from review_article_agent.worker import discovery as discovery_jobs  # noqa: E402
from review_article_agent.worker.engine import (  # noqa: E402
    PROTOCOL_VERSION,
    WorkerEngine,
)

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")
_BASE_TIME = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
_FIXED_CHALLENGE = "TEST-CHAL-DISC-WORKER-1"
_OWNER_UID = 1000
_OBSERVED_AT = "2026-10-28T12:00:00+00:00"


def _req(op: str, **kw: object) -> dict:
    return {"version": PROTOCOL_VERSION, "op": op, **kw}


def _rights(project_id, artifact_id, revision_id):
    return {
        "rights_id": f"RIGHTS-{artifact_id}-{revision_id}",
        "project_id": project_id,
        "record_revision": 1,
        "subject": {
            "kind": "source_revision",
            "project_id": project_id,
            "artifact_id": artifact_id,
            "revision_id": revision_id,
        },
        "operations": {op: "allowed" for op in _OPS},
        "basis": {
            op: {
                "kind": "public_domain",
                "evidence": {"ref": "lic:pd-syn", "provenance": "publisher:pd-list"},
            }
            for op in _OPS
        },
        "authorization": None,
        "restrictions": [],
        "privacy": {"personal_data": False, "confidentiality": "public", "denial": False},
        "recorded_by": "user",
        "recorded_at": "2026-10-28T00:00:00Z",
        "derived_status": "valid",
    }


class _FakeTty:
    def __init__(self, *, replies=(), is_tty=True):
        self._replies = list(replies)
        self._is_tty = is_tty

    def is_tty(self):
        return self._is_tty

    def write(self, text, *, deadline=None):
        return None

    def read_line(self, *, max_chars, deadline):
        return self._replies.pop(0) if self._replies else None

    def close(self):
        return None


class _FakeTransport:
    """Deterministic low-level transport fake (``(url, *, timeout, ...)``)."""

    def __init__(self, responses, on_call=None):
        self._responses = list(responses)
        self.on_call = on_call
        self.calls = 0

    def __call__(self, url, *, timeout, max_response_bytes):
        self.calls += 1
        if self.on_call is not None:
            self.on_call(self.calls)
        status, body = self._responses[min(self.calls - 1, len(self._responses) - 1)]
        return TransportResponse(status=status, body=body)


def _epmc_page(*ids, hit_count=None, next_cursor=None):
    results = [
        {"source": "MED", "id": pid, "pmid": pid, "title": f"t{pid}", "pubYear": "2020"}
        for pid in ids
    ]
    payload = {
        "hitCount": hit_count if hit_count is not None else len(ids),
        "resultList": {"result": results},
    }
    if next_cursor is not None:
        payload["nextCursorMark"] = next_cursor
    return json.dumps(payload).encode("utf-8")


class DiscoveryWorkerBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-disc-worker-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.data_dir = self.tmp / "state"
        self.clock = [_BASE_TIME]
        self.mono = [1000.0]

        self.registry = Registry(self.data_dir)
        self.root = self.tmp / "p"
        self.info = self.registry.create_project(self.root, name="P")
        self.other = self.registry.create_project(self.tmp / "q", name="Q")
        self.project_id = self.info.project_id
        self.other_id = self.other.project_id
        self.store = ProjectStore.open_at(paths.app_dir_for(str(self.root)))
        self.addCleanup(self.store.close)
        self.artifact_repo = ArtifactRepository(self.store)

        patcher = mock.patch.object(ex, "_SLEEP", lambda _s: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- fixtures ------------------------------------------------------------ #
    def ref(self, artifact_id, revision_id):
        return {
            "project_id": self.project_id,
            "artifact_id": artifact_id,
            "revision_id": revision_id,
        }

    def _put(self, artifact_id, kind, revision_id, document, *, dependencies=(), parent=None):
        self.artifact_repo.create_artifact(artifact_id, kind=kind)
        return self.artifact_repo.import_revision(
            artifact_id=artifact_id,
            content=document,
            origin_locator="import:synthetic",
            rights=_rights(self.project_id, artifact_id, revision_id),
            revision_id=revision_id,
            dependencies=list(dependencies),
            parent_revision_id=parent,
        )

    @staticmethod
    def _env(document_type, payload):
        return {"schema_version": 1, "document_type": document_type, "payload": payload}

    def install(self, *, challenge=_FIXED_CHALLENGE, owner_uid=_OWNER_UID, real_uid=_OWNER_UID):
        for patch in (
            mock.patch.object(owner_auth, "_new_challenge", lambda: challenge),
            mock.patch.object(owner_auth, "_current_owner_uid", lambda: owner_uid),
            mock.patch.object(owner_auth, "_real_uid", lambda: real_uid),
            mock.patch.object(owner_auth, "_utcnow", lambda: self.clock[0]),
            mock.patch.object(owner_auth, "_monotonic", lambda: self.mono[0]),
            mock.patch.object(
                owner_auth, "_open_controlling_tty", lambda: _FakeTty(replies=[challenge])
            ),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def approve(self, artifact_id, revision_id, gate_kind, gate_id, *, decision="approved"):
        self.install()
        prepared = prepare_gate_snapshot(
            self.store,
            self.ref(artifact_id, revision_id),
            gate_kind=gate_kind,
            gate_id=gate_id,
            decision=decision,
            rationale="synthetic human gate",
        )
        capability = owner_auth.request_owner_confirmation(prepared.owner_action())
        assert isinstance(capability, OwnerApproval)
        from review_article_agent.storage.human_approvals import HumanApprovalRepository

        return HumanApprovalRepository(self.store).record_decision(prepared, capability)

    def build_plan(
        self,
        *,
        plan_artifact="a-plan",
        plan_revision="rev-plan",
        queries=None,
        comp=None,
    ):
        brief_ref = self.ref("a-brief", "rev-brief")
        self._put("a-brief", "report", "rev-brief", self._env("discovery_brief", {
            "objective": "o", "starting_hypothesis": "h", "questions": [],
        }))
        self._put("a-interview", "report", "rev-interview", self._env("discovery_interview", {
            "brief_ref": brief_ref,
            "turns": [{"question": "q", "answer": "a"}],
            "ready_to_advance": True,
        }), dependencies=[brief_ref])
        self._put("a-framing", "framework", "rev-framing", self._env("discovery_framing", {
            "alternatives": ["alt"], "counterevidence_queries": ["q"], "limitations": [],
        }))
        self._put("a-scope", "framework", "rev-scope", self._env("discovery_scope", {
            "scope": "s", "date_boundary": "none", "language_boundary": "english",
            "source_boundary": [], "intended_synthesis": "syn", "terminology": [],
            "exclusions": [],
        }))
        self._put("a-rubric", "rubric", "rev-rubric", self._env("discovery_rubric", {
            "eligibility_rules": ["r"], "uncertain_action": "retain", "limitations": [],
        }))
        refs = [
            brief_ref,
            self.ref("a-interview", "rev-interview"),
            self.ref("a-framing", "rev-framing"),
            self.ref("a-scope", "rev-scope"),
            self.ref("a-rubric", "rev-rubric"),
        ]
        payload = {
            "protocol": "protocol",
            "brief_ref": brief_ref,
            "interview_ref": self.ref("a-interview", "rev-interview"),
            "seed_refs": [],
            "framing_ref": self.ref("a-framing", "rev-framing"),
            "scope_ref": self.ref("a-scope", "rev-scope"),
            "rubric_ref": self.ref("a-rubric", "rev-rubric"),
            "queries": queries if queries is not None else [
                {"source": "europepmc", "query": "asthma AND review", "page_size": 5, "max_pages": 1}
            ],
            "complementary_sources": comp if comp is not None else [],
            "expansion": {"max_hops": 1, "max_sources": 5, "max_requests": 10,
                          "page_size": 20, "max_pages": 3},
            "impact_review": {"previous_plan": None, "affected": [], "rationale": ""},
        }
        self._put(plan_artifact, "report", plan_revision,
                  self._env("discovery_plan", payload), dependencies=refs)
        return self.ref(plan_artifact, plan_revision)

    def approve_plan(self, plan_ref, *, decision="approved"):
        self.approve(
            plan_ref["artifact_id"],
            plan_ref["revision_id"],
            ex.PROTOCOL_GATE_KIND,
            ex.PLAN_GATE_ID,
            decision=decision,
        )

    # -- engine helpers ------------------------------------------------------ #
    def engine(self, transport=None, *, registry=None, data_dir=None):
        engine = WorkerEngine(
            str(data_dir or self.data_dir),
            registry=registry or self.registry,
            discovery_transport=transport,
        )
        self.addCleanup(engine.close)
        return engine

    def _drive(self, engine, timeout=15.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            hint = engine.tick()
            if hint is None:
                return
            if hint > 0:
                time.sleep(min(hint, 0.02))
        self.fail("engine did not settle within the timeout")

    def _run_until(self, engine, predicate, timeout=15.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            hint = engine.tick()
            if hint is not None and hint > 0:
                time.sleep(min(hint, 0.02))
            elif hint is None:
                time.sleep(0.01)
        return predicate()

    def _submit(self, engine, *, request_id, mode, parameters, project_id=None):
        return engine.handle(
            _req(
                "discovery_submit",
                project_id=project_id or self.project_id,
                request_id=request_id,
                mode=mode,
                parameters=parameters,
            )
        )

    def _job(self, engine, job_id, project_id=None):
        res = engine.handle(
            _req("job_status", project_id=project_id or self.project_id, job_id=job_id)
        )
        assert res["ok"], res
        return res["job"]

    def _observations(self):
        repo = DiscoveryRepository(self.store)
        return repo.list_searches()


# --------------------------------------------------------------------------- #
# Strict submission validation (before any write)
# --------------------------------------------------------------------------- #
class SubmitValidationTest(DiscoveryWorkerBase):
    def test_unknown_mode_and_parameters_are_rejected_without_writes(self) -> None:
        engine = self.engine()
        res = self._submit(engine, request_id="v-1", mode="bogus", parameters={})
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "bad_value")

        plan_ref = self.build_plan()
        self.approve_plan(plan_ref)

        wrong_keys = self._submit(
            engine, request_id="v-2", mode="query",
            parameters={"plan_ref": plan_ref, "query_index": 0, "extra": 1},
        )
        self.assertEqual(wrong_keys["code"], "bad_value")

        wrong_type = self._submit(
            engine, request_id="v-3", mode="query",
            parameters={"plan_ref": plan_ref, "query_index": "0"},
        )
        self.assertEqual(wrong_type["code"], "bad_type")

        # No durable job was created for any invalid request.
        with engine.open_repository(self.project_id) as repo:
            self.assertEqual(repo.list(), [])

    def test_actor_or_authority_field_is_rejected(self) -> None:
        engine = self.engine()
        plan_ref = self.build_plan()
        self.approve_plan(plan_ref)
        for field in ("actor", "uid", "authority", "auth"):
            res = engine.handle(
                _req(
                    "discovery_submit",
                    project_id=self.project_id,
                    request_id=f"actor-{field}",
                    mode="query",
                    parameters={"plan_ref": plan_ref, "query_index": 0},
                    **{field: "owner"},
                )
            )
            self.assertFalse(res["ok"])
            self.assertEqual(res["code"], "unknown_key")

    def test_seed_and_import_parameter_bounds(self) -> None:
        engine = self.engine()
        plan_ref = self.build_plan(comp=[
            {"source": "regulatory", "mode": "metadata_import", "rationale": "opt-in"}
        ])
        self.approve_plan(plan_ref)

        bad_seed = [
            {"source": "scholar", "query": "q", "page_size": 5, "max_pages": 1},
            {"source": "europepmc", "query": "", "page_size": 5, "max_pages": 1},
            {"source": "europepmc", "query": "q", "page_size": 21, "max_pages": 1},
            {"source": "europepmc", "query": "q", "page_size": 5, "max_pages": 2},
            {"source": "europepmc", "query": "q", "page_size": True, "max_pages": 1},
        ]
        for index, params in enumerate(bad_seed):
            res = self._submit(engine, request_id=f"seed-bad-{index}", mode="seed", parameters=params)
            self.assertFalse(res["ok"], params)
            self.assertIn(res["code"], {"bad_type", "bad_value"})

        bad_import = self._submit(
            engine, request_id="imp-bad", mode="metadata_import",
            parameters={
                "plan_ref": plan_ref, "source": "regulatory",
                "records": [{"source": "pubmed", "pmid": "1"}],
                "origin_locator": "note:batch", "observed_at": _OBSERVED_AT,
            },
        )
        self.assertFalse(bad_import["ok"])
        self.assertEqual(bad_import["code"], "bad_value")

        bad_time = self._submit(
            engine, request_id="imp-bad-time", mode="metadata_import",
            parameters={
                "plan_ref": plan_ref, "source": "regulatory",
                "records": [{"source": "regulatory", "pmid": "1"}],
                "origin_locator": "note:batch", "observed_at": "2026-10-28",
            },
        )
        self.assertEqual(bad_time["code"], "bad_value")

        with engine.open_repository(self.project_id) as repo:
            self.assertEqual(repo.list(), [])

    def test_oversized_metadata_payload_is_rejected(self) -> None:
        engine = self.engine()
        plan_ref = self.build_plan(comp=[
            {"source": "regulatory", "mode": "metadata_import", "rationale": "opt-in"}
        ])
        self.approve_plan(plan_ref)
        big = "a" * 4096
        records = [
            {"source": "regulatory", "source_id": f"R-{i}", "title": big}
            for i in range(20)
        ]
        res = self._submit(
            engine, request_id="imp-huge", mode="metadata_import",
            parameters={
                "plan_ref": plan_ref, "source": "regulatory",
                "records": records, "origin_locator": "note:batch",
                "observed_at": _OBSERVED_AT,
            },
        )
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "bad_value")
        with engine.open_repository(self.project_id) as repo:
            self.assertEqual(repo.list(), [])


# --------------------------------------------------------------------------- #
# Queued admission + asynchronous completion
# --------------------------------------------------------------------------- #
class AdmissionAndCompletionTest(DiscoveryWorkerBase):
    def test_submit_returns_queued_admission_not_completion(self) -> None:
        engine = self.engine(_FakeTransport([(200, _epmc_page("1"))]))
        plan_ref = self.build_plan()
        self.approve_plan(plan_ref)

        res = self._submit(
            engine, request_id="q-1", mode="query",
            parameters={"plan_ref": plan_ref, "query_index": 0},
        )
        self.assertTrue(res["ok"])
        self.assertTrue(res["queued"])
        job = res["job"]
        self.assertEqual(job["task_kind"], "discovery_query")
        self.assertEqual(job["state"], "queued")
        self.assertEqual(job["attempts_total"], 0)
        self.assertIsNone(job.get("accepted_result"))
        # The submission acquired the same global execution token.
        self.assertEqual(self.registry.activation().active_project_id, self.project_id)
        self.assertEqual(self._observations(), [])

    def test_asynchronous_completion_records_exact_observation(self) -> None:
        fake = _FakeTransport([(200, _epmc_page("31452104"))])
        engine = self.engine(fake)
        plan_ref = self.build_plan()
        self.approve_plan(plan_ref)
        res = self._submit(
            engine, request_id="q-1", mode="query",
            parameters={"plan_ref": plan_ref, "query_index": 0},
        )
        job_id = res["job"]["job_id"]
        self._drive(engine)

        job = self._job(engine, job_id)
        self.assertEqual(job["state"], "completed")
        self.assertEqual(job["attempts_total"], 1)
        accepted = job["accepted_result"]
        self.assertIsNotNone(accepted)
        self.assertEqual(accepted["result_kind"], "discovery_observation")
        result = accepted["result"]
        self.assertTrue(result["run_id"].startswith("dsr_"))
        self.assertEqual(len(result["result_sha256"]), 64)
        self.assertEqual(result["binding"]["plan_ref"]["artifact_id"], "a-plan")
        self.assertEqual(result["binding"]["query_index"], 0)
        self.assertEqual(result["source"], "europepmc")
        self.assertEqual(fake.calls, 1)

        # The observation holds the exact plan query; the status projection never
        # carries the raw query text.
        rows = self._observations()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].query, "asthma AND review")
        self.assertEqual(rows[0].run_id, result["run_id"])
        self.assertNotIn("asthma AND review", json.dumps(job))

    def test_seed_mode_end_to_end(self) -> None:
        fake = _FakeTransport([(200, _epmc_page("1"))])
        engine = self.engine(fake)
        res = self._submit(
            engine, request_id="seed-1", mode="seed",
            parameters={"source": "europepmc", "query": "seed query",
                        "page_size": 5, "max_pages": 1},
        )
        job_id = res["job"]["job_id"]
        self._drive(engine)
        job = self._job(engine, job_id)
        self.assertEqual(job["state"], "completed")
        self.assertEqual(fake.calls, 1)
        rows = self._observations()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].query, "seed query")
        self.assertEqual(len(job["accepted_result"]["result"]["binding"]["query_fingerprint"]), 64)
        self.assertNotIn("seed query", json.dumps(job))

    def test_metadata_import_end_to_end_no_network(self) -> None:
        fake = _FakeTransport([(200, _epmc_page("1"))])
        engine = self.engine(fake)
        plan_ref = self.build_plan(comp=[
            {"source": "regulatory", "mode": "metadata_import", "rationale": "opt-in"}
        ])
        self.approve_plan(plan_ref)
        records = [
            {"source": "regulatory", "source_id": "R-1", "pmid": "111", "title": "a"},
            {"source": "regulatory", "source_id": "R-2", "title": "b"},
        ]
        res = self._submit(
            engine, request_id="imp-1", mode="metadata_import",
            parameters={
                "plan_ref": plan_ref, "source": "regulatory", "records": records,
                "origin_locator": "note:batch-1", "observed_at": _OBSERVED_AT,
            },
        )
        job_id = res["job"]["job_id"]
        self._drive(engine)
        job = self._job(engine, job_id)
        self.assertEqual(job["state"], "completed")
        self.assertEqual(fake.calls, 0)  # a metadata import performs no download
        rows = self._observations()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].query, "metadata_import:regulatory")
        binding = job["accepted_result"]["result"]["binding"]
        self.assertEqual(len(binding["origin_locator_sha256"]), 64)
        self.assertNotIn("note:batch-1", json.dumps(job))


# --------------------------------------------------------------------------- #
# Replay / divergence
# --------------------------------------------------------------------------- #
class ReplayTest(DiscoveryWorkerBase):
    def test_matching_replay_no_duplicate_and_divergent_conflicts(self) -> None:
        fake = _FakeTransport([(200, _epmc_page("1"))])
        engine = self.engine(fake)
        plan_ref = self.build_plan(queries=[
            {"source": "europepmc", "query": "alpha", "page_size": 5, "max_pages": 1},
            {"source": "europepmc", "query": "beta", "page_size": 5, "max_pages": 1},
        ])
        self.approve_plan(plan_ref)
        first = self._submit(
            engine, request_id="dup", mode="query",
            parameters={"plan_ref": plan_ref, "query_index": 0},
        )
        job_id = first["job"]["job_id"]
        self._drive(engine)
        self.assertEqual(len(self._observations()), 1)

        # Same request id + identical parameters: same job, no new observation.
        second = self._submit(
            engine, request_id="dup", mode="query",
            parameters={"plan_ref": plan_ref, "query_index": 0},
        )
        self.assertTrue(second["ok"])
        self.assertEqual(second["job"]["job_id"], job_id)
        self._drive(engine)
        self.assertEqual(len(self._observations()), 1)

        # Same request id + divergent parameters (different query index): conflict.
        divergent = self._submit(
            engine, request_id="dup", mode="query",
            parameters={"plan_ref": plan_ref, "query_index": 1},
        )
        self.assertFalse(divergent["ok"])
        self.assertEqual(divergent["code"], "conflict")
        self.assertEqual(len(self._observations()), 1)


# --------------------------------------------------------------------------- #
# Unapproved / stale plan performs no I/O
# --------------------------------------------------------------------------- #
class ApprovalGateTest(DiscoveryWorkerBase):
    def test_unapproved_plan_performs_no_io_and_escalates(self) -> None:
        fake = _FakeTransport([(200, _epmc_page("1"))])
        engine = self.engine(fake)
        plan_ref = self.build_plan()  # never approved
        res = self._submit(
            engine, request_id="unapproved", mode="query",
            parameters={"plan_ref": plan_ref, "query_index": 0},
        )
        self.assertTrue(res["ok"])
        job_id = res["job"]["job_id"]
        self._drive(engine)
        job = self._job(engine, job_id)
        self.assertEqual(job["state"], "waiting_human")
        self.assertEqual(fake.calls, 0)
        self.assertEqual(self._observations(), [])
        self.assertEqual(job["attempts_total"], 1)
        # Nothing was issued, so there is no unresolved remote and the global
        # execution token is released for another project.
        self.assertFalse(job["outcome_unresolved"])
        self.assertEqual(self.registry.activation().state, "none")

    def test_stale_plan_after_approval_still_blocks_network(self) -> None:
        fake = _FakeTransport([(200, _epmc_page("1"))])
        engine = self.engine(fake)
        plan_ref = self.build_plan()
        self.approve_plan(plan_ref)
        # Amend a pinned dependency so the plan is stale before dispatch.
        self._put("a-brief", "report", "rev-brief2", self._env("discovery_brief", {
            "objective": "revised", "starting_hypothesis": "h", "questions": [],
        }), parent="rev-brief")
        res = self._submit(
            engine, request_id="stale", mode="query",
            parameters={"plan_ref": plan_ref, "query_index": 0},
        )
        self.assertTrue(res["ok"])
        job_id = res["job"]["job_id"]
        self._drive(engine)
        self.assertEqual(fake.calls, 0)
        self.assertEqual(self._observations(), [])
        self.assertEqual(self._job(engine, job_id)["state"], "waiting_human")

    def test_post_network_plan_change_cannot_accept_result(self) -> None:
        # Page 1 is received; a pinned dependency is amended while the provider
        # call is in flight, so the completion path revalidates the (now stale)
        # plan and refuses to accept the observation: precise unknown, no hidden
        # successful job.
        first_called, release = threading.Event(), threading.Event()

        def on_call(n: int) -> None:
            if n == 1:
                first_called.set()
                release.wait(timeout=10.0)

        fake = _FakeTransport([(200, _epmc_page("1"))], on_call=on_call)
        engine = self.engine(fake)
        plan_ref = self.build_plan()
        self.approve_plan(plan_ref)
        res = self._submit(
            engine, request_id="postnet", mode="query",
            parameters={"plan_ref": plan_ref, "query_index": 0},
        )
        job_id = res["job"]["job_id"]
        engine.tick()
        try:
            self.assertTrue(first_called.wait(timeout=10.0))
            self._put("a-brief", "report", "rev-briefZ", self._env("discovery_brief", {
                "objective": "amended-during-call", "starting_hypothesis": "h",
                "questions": [],
            }), parent="rev-brief")
        finally:
            release.set()
        self._drive(engine)
        self.assertEqual(fake.calls, 1)
        self.assertEqual(self._observations(), [])  # no observation accepted
        job = self._job(engine, job_id)
        self.assertNotEqual(job["state"], "completed")
        self.assertIn(job["state"], {"waiting_human", "reconciliation_required"})
        self.assertTrue(job["outcome_unresolved"])


# --------------------------------------------------------------------------- #
# Detached control: non-blocking ticking + one serial executor
# --------------------------------------------------------------------------- #
class DetachedControlTest(DiscoveryWorkerBase):
    def test_tick_returns_promptly_while_provider_blocked(self) -> None:
        first_called = threading.Event()
        release = threading.Event()

        def on_call(_n: int) -> None:
            first_called.set()
            release.wait(timeout=10.0)

        fake = _FakeTransport([(200, _epmc_page("1"))], on_call=on_call)
        engine = self.engine(fake)
        plan_ref = self.build_plan()
        self.approve_plan(plan_ref)
        res = self._submit(
            engine, request_id="blocked-1", mode="query",
            parameters={"plan_ref": plan_ref, "query_index": 0},
        )
        job_id = res["job"]["job_id"]

        try:
            engine.tick()  # starts the future (claim + admit + submit)
            self.assertTrue(first_called.wait(timeout=10.0), "provider call never started")

            # The control loop must keep answering promptly while the provider
            # call is blocked: tick returns a bounded hint, not a blocking call.
            start = time.monotonic()
            hint = engine.tick()
            elapsed = time.monotonic() - start
            self.assertIsNotNone(hint)
            self.assertLess(elapsed, 1.0)

            status = engine.handle(_req("status"))
            self.assertTrue(status["ok"])
            self.assertEqual(status["active_project_id"], self.project_id)
            # Pause is available and takes effect while the provider is blocked.
            paused = engine.handle(_req("pause", project_id=self.project_id))
            self.assertTrue(paused["ok"])
            with engine.open_repository(self.project_id) as repo:
                self.assertEqual(repo.get(job_id).state, "paused")
        finally:
            release.set()
        self._drive(engine)

        # The in-flight observation is recorded on the same fence; the paused
        # scheduling state is preserved (never silently advanced).
        job = self._job(engine, job_id)
        self.assertEqual(job["state"], "paused")
        self.assertIsNotNone(job.get("accepted_result"))
        self.assertEqual(fake.calls, 1)
        self.assertEqual(len(self._observations()), 1)

    def test_running_discovery_blocks_another_project(self) -> None:
        first_called = threading.Event()
        release = threading.Event()

        def on_call(_n: int) -> None:
            first_called.set()
            release.wait(timeout=10.0)

        fake = _FakeTransport([(200, _epmc_page("1"))], on_call=on_call)
        engine = self.engine(fake)
        plan_ref = self.build_plan()
        self.approve_plan(plan_ref)
        res = self._submit(
            engine, request_id="conflict-1", mode="query",
            parameters={"plan_ref": plan_ref, "query_index": 0},
        )
        try:
            engine.tick()
            self.assertTrue(first_called.wait(timeout=10.0))
            # A second project cannot dispatch while the discovery job holds the
            # single serial execution slot.
            blocked = engine.handle(
                _req(
                    "submit", project_id=self.other_id, request_id="b-1",
                    task_kind="synthetic_numeric", steps=1, delay_ms=0,
                    outcome="complete", stage="s", reserve_usd="0.10",
                )
            )
            self.assertFalse(blocked["ok"])
            self.assertEqual(blocked["code"], "switch_blocked")
        finally:
            release.set()
        self._drive(engine)
        self.assertEqual(self._job(engine, res["job"]["job_id"])["state"], "completed")


# --------------------------------------------------------------------------- #
# Pause / cancel / drain between attempts
# --------------------------------------------------------------------------- #
class StopBetweenAttemptsTest(DiscoveryWorkerBase):
    """A stop mid-query preserves the observed page and issues no further request.

    The job stop uses the shared transport's opt-in interrupt: the shared
    ``DiscoveryTransport`` records a failed page with **no fabricated attempt** and
    the provider adapter keeps the pages already observed, so the partial
    observation is committed and the held (paused/cancelled) job state is preserved
    per ``JobRepository`` semantics.
    """

    def _two_page_plan(self):
        return self.build_plan(queries=[
            {"source": "europepmc", "query": "asthma AND review",
             "page_size": 1, "max_pages": 2},
        ])

    def _page_one_fake(self, first_called, release):
        """One real page-1 call (2 hits, advancing cursor) then the test's stop."""

        def on_call(n: int) -> None:
            if n == 1:
                first_called.set()
                release.wait(timeout=10.0)

        return _FakeTransport(
            [(200, _epmc_page("31452104", hit_count=2, next_cursor="C1"))],
            on_call=on_call,
        )

    def _start_two_page(self, request_id):
        first_called, release = threading.Event(), threading.Event()
        fake = self._page_one_fake(first_called, release)
        engine = self.engine(fake)
        plan_ref = self._two_page_plan()
        self.approve_plan(plan_ref)
        res = self._submit(
            engine, request_id=request_id, mode="query",
            parameters={"plan_ref": plan_ref, "query_index": 0},
        )
        job_id = res["job"]["job_id"]
        engine.tick()
        self.assertTrue(first_called.wait(timeout=10.0), "page 1 never started")
        return engine, fake, job_id, release

    def _assert_partial_preserved(self, engine, fake, job_id, expected_state):
        self.assertEqual(fake.calls, 1)  # no second-page request
        rows = self._observations()
        self.assertEqual(len(rows), 1)
        # The successully observed page 1 and its one real attempt are retained.
        self.assertEqual([r.source_id for r in rows[0].result.records], ["MED:31452104"])
        self.assertEqual(rows[0].attempt_count, 1)
        self.assertFalse(rows[0].complete)
        self.assertEqual(rows[0].stop_reason, "failure")
        self.assertEqual(rows[0].failure, "attempts_exhausted")
        job = self._job(engine, job_id)
        self.assertEqual(job["state"], expected_state)
        self.assertEqual(job["attempts_total"], 1)
        accepted = job["accepted_result"]["result"]
        self.assertEqual(accepted["record_count"], 1)
        self.assertEqual(accepted["attempt_count"], 1)
        self.assertFalse(accepted["complete"])
        self.assertEqual(accepted["stop_reason"], "failure")
        self.assertEqual(job["wait_reason"], "discovery_interrupted")

    def test_pause_before_second_page_preserves_first_page(self) -> None:
        engine, fake, job_id, release = self._start_two_page("pause-2page")
        try:
            self.assertTrue(
                engine.handle(_req("pause", project_id=self.project_id))["ok"]
            )
        finally:
            release.set()
        self._drive(engine)
        self._assert_partial_preserved(engine, fake, job_id, "paused")
        # The paused partial is stable: another tick adds no new observation.
        engine.tick()
        self.assertEqual(len(self._observations()), 1)

    def test_cancel_before_second_page_preserves_first_page(self) -> None:
        engine, fake, job_id, release = self._start_two_page("cancel-2page")
        try:
            cancelled = engine.handle(
                _req("cancel", project_id=self.project_id, job_id=job_id)
            )
            self.assertTrue(cancelled["ok"])
        finally:
            release.set()
        self._drive(engine)
        self._assert_partial_preserved(engine, fake, job_id, "cancelled")

    def test_drain_before_second_page_preserves_first_page_and_allows_switch(self) -> None:
        engine, fake, job_id, release = self._start_two_page("drain-2page")
        try:
            self.assertTrue(
                engine.handle(_req("drain", project_id=self.project_id))["ok"]
            )
        finally:
            release.set()
        self._drive(engine)
        # Drain pauses the project's jobs; the partial observation is still
        # recorded exactly once and the held state is respected.
        self._assert_partial_preserved(engine, fake, job_id, "paused")
        self.assertEqual(len(self._observations()), 1)
        # The drain response quiesced the project, so another project can switch.
        switched = engine.handle(_req("switch", project_id=self.other_id))
        self.assertTrue(switched["ok"])
        self.assertEqual(
            self.registry.activation().active_project_id, self.other_id
        )

    def test_guard_transport_uses_optin_interrupt_after_a_real_attempt(self) -> None:
        # A stop with no attempt yet aborts hard (nothing to preserve); after a
        # real attempt it raises the shared opt-in interrupt so the pages already
        # observed survive — with no fabricated attempt for the refused request.
        counter = discovery_jobs.AttemptCounter()
        stopped = {"v": False}
        base_calls = {"n": 0}

        def base(url, *, timeout, max_response_bytes):
            base_calls["n"] += 1
            return TransportResponse(status=200, body=b"{}")

        transport = discovery_jobs.guard_transport(
            base, stop_requested=lambda: stopped["v"], counter=counter
        )
        stopped["v"] = True
        with self.assertRaises(discovery_jobs.DiscoveryJobStopped):
            transport("https://example.invalid", timeout=1.0, max_response_bytes=10)
        self.assertEqual(base_calls["n"], 0)

        stopped["v"] = False
        self.assertEqual(
            transport(
                "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                timeout=1.0, max_response_bytes=10,
            ).status,
            200,
        )
        self.assertEqual(counter.value, 1)

        stopped["v"] = True
        wrapped = DiscoveryTransport(transport=transport, sleep=lambda _s: None)
        result = wrapped.get("https://www.ebi.ac.uk/europepmc/webservices/rest/search")
        self.assertFalse(result.ok)
        self.assertEqual(result.failure, "attempts_exhausted")
        self.assertEqual(result.attempts, ())  # no fabricated attempt
        self.assertEqual(base_calls["n"], 1)
        self.assertTrue(counter.interrupted)


# --------------------------------------------------------------------------- #
# Restart / fence reconciliation
# --------------------------------------------------------------------------- #
class RestartRecoveryTest(DiscoveryWorkerBase):
    def _commit_attempt(self, job_id, *, with_observation: bool) -> None:
        token = self.registry.activation().activation_token
        store = ProjectStore.open_at(
            paths.app_dir_for(str(self.root)), expected_project_id=self.project_id
        )
        try:
            repo = JobRepository(store)
            job = repo.get(job_id)
            attempt = repo.begin_attempt(job_id, dispatch_kind="local")
            repo.record_checkpoint(
                attempt.attempt_id, discovery_jobs.predispatch_checkpoint("query")
            )
            if with_observation:
                discovery_jobs.dispatch(
                    store, self.registry, token, job,
                    _FakeTransport([(200, _epmc_page("31452104"))]),
                )
        finally:
            store.close()

    def _prepare(self, request_id, *, queries=None):
        engine = self.engine(_FakeTransport([(200, _epmc_page("1"))]))
        plan_ref = self.build_plan(queries=queries)
        self.approve_plan(plan_ref)
        res = self._submit(
            engine, request_id=request_id, mode="query",
            parameters={"plan_ref": plan_ref, "query_index": 0},
        )
        self.assertTrue(res["ok"])
        job_id = res["job"]["job_id"]
        # Stop the first engine (no future was ever started).
        engine.close()
        return job_id

    def _commit_partial(self, job_id) -> int:
        """Commit a *partial* observation (page 1 ok, page 2 interrupted) with no accept."""
        token = self.registry.activation().activation_token
        store = ProjectStore.open_at(
            paths.app_dir_for(str(self.root)), expected_project_id=self.project_id
        )
        calls = {"n": 0}
        try:
            repo = JobRepository(store)
            job = repo.get(job_id)
            attempt = repo.begin_attempt(job_id, dispatch_kind="local")
            repo.record_checkpoint(
                attempt.attempt_id, discovery_jobs.predispatch_checkpoint("query")
            )

            def transport(url, *, timeout, max_response_bytes):
                calls["n"] += 1
                if calls["n"] == 1:
                    return TransportResponse(
                        status=200,
                        body=_epmc_page("31452104", hit_count=2, next_cursor="C1"),
                    )
                raise DiscoveryRequestInterrupted("stop before page 2")

            discovery_jobs.dispatch(store, self.registry, token, job, transport)
        finally:
            store.close()
        return calls["n"]

    def test_restart_with_standing_observation_completes_same_fence(self) -> None:
        job_id = self._prepare("restart-known")
        self._commit_attempt(job_id, with_observation=True)

        engine2 = self.engine(
            _FakeTransport([(200, _epmc_page("999"))]), registry=Registry(self.data_dir)
        )
        engine2.reconcile_startup()
        self._drive(engine2)
        job = self._job(engine2, job_id)
        self.assertEqual(job["state"], "completed")
        self.assertEqual(job["attempts_total"], 1)  # no new identity
        self.assertIsNotNone(job["accepted_result"])
        self.assertEqual(len(self._observations()), 1)

    def test_restart_without_observation_escalates_never_silent_success(self) -> None:
        job_id = self._prepare("restart-unknown")
        self._commit_attempt(job_id, with_observation=False)

        fake2 = _FakeTransport([(200, _epmc_page("999"))])
        engine2 = self.engine(fake2, registry=Registry(self.data_dir))
        engine2.reconcile_startup()
        self._drive(engine2)
        job = self._job(engine2, job_id)
        self.assertEqual(job["state"], "waiting_human")
        self.assertNotEqual(job["state"], "completed")
        self.assertEqual(job["attempts_total"], 1)
        self.assertIsNone(job.get("accepted_result"))
        self.assertEqual(fake2.calls, 0)  # never a blind repeat
        self.assertEqual(self._observations(), [])

    def test_restart_proven_undispatched_is_reopened_and_runs(self) -> None:
        job_id = self._prepare("restart-undispatched")
        # A queued attempt with NO dispatch marker proves nothing was issued.
        store = ProjectStore.open_at(
            paths.app_dir_for(str(self.root)), expected_project_id=self.project_id
        )
        try:
            repo = JobRepository(store)
            repo.begin_attempt(job_id, dispatch_kind="local")
        finally:
            store.close()

        fake2 = _FakeTransport([(200, _epmc_page("1"))])
        engine2 = self.engine(fake2, registry=Registry(self.data_dir))
        engine2.reconcile_startup()
        self._drive(engine2)
        job = self._job(engine2, job_id)
        self.assertEqual(job["state"], "completed")
        self.assertEqual(job["attempts_total"], 1)  # reused the same identity
        self.assertEqual(fake2.calls, 1)

    def test_restart_replays_saved_partial_row_without_io(self) -> None:
        # A partial (page 1 ok, page 2 interrupted) observation was durably
        # committed but the job was never accepted (a crash). A fresh engine must
        # prove it by replay: no provider I/O, no duplicate row, same fence.
        queries = [
            {"source": "europepmc", "query": "asthma AND review",
             "page_size": 1, "max_pages": 2},
        ]
        job_id = self._prepare("restart-partial", queries=queries)
        self.assertEqual(self._commit_partial(job_id), 2)  # 1 real page + 1 refused
        rows = self._observations()
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0].complete)
        self.assertEqual(rows[0].record_count, 1)
        self.assertEqual(rows[0].attempt_count, 1)

        fake2 = _FakeTransport([(200, _epmc_page("999"))])
        engine2 = self.engine(fake2, registry=Registry(self.data_dir))
        engine2.reconcile_startup()
        self._drive(engine2)
        self.assertEqual(fake2.calls, 0)  # replayed, no I/O
        self.assertEqual(len(self._observations()), 1)  # no duplicate
        job = self._job(engine2, job_id)
        self.assertEqual(job["state"], "completed")
        self.assertEqual(job["attempts_total"], 1)
        accepted = job["accepted_result"]["result"]
        self.assertEqual(accepted["run_id"], rows[0].run_id)
        self.assertFalse(accepted["complete"])
        self.assertEqual(accepted["record_count"], 1)


# --------------------------------------------------------------------------- #
# Synthetic regressions stay intact
# --------------------------------------------------------------------------- #
class SyntheticRegressionTest(DiscoveryWorkerBase):
    def test_synthetic_numeric_still_completes(self) -> None:
        engine = self.engine()
        res = engine.handle(
            _req(
                "submit", project_id=self.project_id, request_id="s-1",
                task_kind="synthetic_numeric", steps=3, delay_ms=0,
                outcome="complete", stage="s", reserve_usd="0.10",
            )
        )
        self.assertTrue(res["ok"])
        job_id = res["job"]["job_id"]
        self._drive(engine)
        job = self._job(engine, job_id)
        self.assertEqual(job["state"], "completed")
        self.assertEqual(job["accepted_result"]["result_kind"], "synthetic_numeric")
        # No discovery observation was created.
        self.assertEqual(self._observations(), [])


# --------------------------------------------------------------------------- #
# Lightweight real-process acceptance (unapproved plan => zero provider I/O)
# --------------------------------------------------------------------------- #
class DetachedProcessTest(DiscoveryWorkerBase):
    def test_detached_worker_admits_then_escalates_unapproved_plan(self) -> None:
        plan_ref = self.build_plan()  # never approved: no provider I/O is possible
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([str(SRC), env.get("PYTHONPATH", "")])
        proc = subprocess.Popen(
            [sys.executable, "-m", "review_article_agent.worker",
             "--data-dir", str(self.data_dir)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.addCleanup(proc.kill)
        self.addCleanup(self._close_proc, proc)
        client = WorkerIPCClient(str(self.data_dir), io_timeout=5.0)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                client.request(_req("status"))
                break
            except Exception:
                time.sleep(0.05)
        else:
            self.fail("worker did not become ready")

        res = client.request(_req(
            "discovery_submit", project_id=self.project_id, request_id="proc-1",
            mode="query", parameters={"plan_ref": plan_ref, "query_index": 0},
        ))
        self.assertTrue(res["ok"])
        self.assertTrue(res["queued"])
        job_id = res["job"]["job_id"]

        deadline = time.monotonic() + 15.0
        state = None
        while time.monotonic() < deadline:
            status = client.request(_req(
                "job_status", project_id=self.project_id, job_id=job_id
            ))
            state = status["job"]["state"]
            if state in ("waiting_human", "failed", "completed"):
                break
            time.sleep(0.05)
        self.assertEqual(state, "waiting_human")

        stop = client.request(_req("stop"))
        self.assertTrue(stop["stopping"])
        proc.wait(timeout=10)
        self.assertEqual(proc.returncode, 0)

    @staticmethod
    def _close_proc(proc: subprocess.Popen) -> None:
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
