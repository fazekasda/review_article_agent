"""In-process CLI acceptance tests for the ``discovery`` **job** addon (CLI1A).

Covers ``src/review_article_agent/cli/discovery_jobs.py`` against Step 05
deliverables D1-D4 (ADR 003: "two APIs plus imports", the live-capable direct
lead route, approved expansion/reporting, the narrow direct-lead exception) and
the surface requirements of the CLI1A task:

* ``discovery search run`` / ``search seed`` / ``search import`` and
  ``discovery expand run`` submit one exact version-1 ``discovery_submit``
  message and return a **queued admission only** (never a completion);
* strict parameters / envelopes / files are validated locally and an empty or
  malformed input performs **zero** IPC;
* a matching retry replays and a divergent reuse conflicts, reporting the
  reused owner error categories;
* ``discovery expand show`` returns a safe summary by default and the private
  full mapping only with ``--content``;
* ``discovery lead configure`` stores one ``discovery_hosted_use`` batch,
  returns a named-``batch`` non-authorizing approval request, pins exact source
  dependencies (plus a ``version_of`` lineage edge on an amendment), and never
  lets a historical replay inherit a newer approval;
* ``discovery lead run`` requires ``--credential-env OPENROUTER_API_KEY`` and
  never accepts, sends, persists, or logs a credential value;
* no ``--key``/``--yes``/``--actor`` flag exists anywhere.

Most cases drive the real CLI entry point in-process over a **real temporary
registry and store**, with the worker launch and the IPC client substituted by
controlled seams (fake external I/O only). One case starts a **real owned
worker process** (a disposable fixture with an injected deterministic provider)
and drives a seed job to completion through ``job status``.

Run::

    PYTHONPATH=src python3 -m unittest tests.test_cli_discovery_jobs
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
TESTS = Path(__file__).resolve().parent
for _path in (str(SRC), str(TESTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import review_article_agent.cli.main as cli_main  # noqa: E402
from review_article_agent.discovery.lead_runner import (  # noqa: E402
    HOSTED_USE_GATE_KIND,
)
from review_article_agent.runtime import ipc, owner_auth  # noqa: E402
from review_article_agent.runtime.gates import prepare_gate_snapshot  # noqa: E402
from review_article_agent.runtime.owner_auth import OwnerApproval  # noqa: E402
from review_article_agent.storage import (  # noqa: E402
    ArtifactRepository,
    ProjectStore,
    Registry,
    paths,
)
from review_article_agent.storage.human_approvals import HumanApprovalRepository  # noqa: E402
from review_article_agent.worker import process  # noqa: E402

import test_discovery_lead_runner as leadfx  # noqa: E402
import test_discovery_job_routes as routes  # noqa: E402

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")
_FIXED_CHALLENGE = "TEST-CHAL-CLI1A"
_OWNER_UID = 1000
_SECRET = "SYNTHETIC-OPENROUTER-KEY-DO-NOT-ECHO"
_QUERY_TEXT = "SYNTHETIC-QUERY-DO-NOT-ECHO"

_MODE_TASK_KIND = {
    "query": "discovery_query",
    "seed": "discovery_seed",
    "metadata_import": "discovery_import",
    "expand": "discovery_expand",
    "lead": "discovery_lead",
}
_RESULT_KIND = {
    "query": "discovery_observation",
    "seed": "discovery_observation",
    "metadata_import": "discovery_observation",
    "expand": "discovery_expansion",
    "lead": "discovery_lead",
}

# A disposable worker that runs the *real* engine over an injected deterministic
# provider (never the network). Its status response is the real engine's, so the
# launcher's marker/PID verification passes unchanged.
FIXTURE_WORKER = r'''
import json
import sys

from review_article_agent.discovery.transport import TransportResponse
from review_article_agent.runtime import ipc
from review_article_agent.worker.engine import WorkerEngine

data_dir = sys.argv[1]


def fake_transport(url, *, timeout, max_response_bytes):
    payload = {
        "hitCount": 1,
        "resultList": {
            "result": [
                {"source": "MED", "id": "1", "pmid": "1",
                 "title": "t1", "pubYear": "2020"}
            ]
        },
    }
    return TransportResponse(status=200, body=json.dumps(payload).encode("utf-8"))


engine = WorkerEngine(data_dir, discovery_transport=fake_transport)
server = ipc.WorkerIPCServer(data_dir, engine.handle)
server.start()
try:
    engine.reconcile_startup()
    while not engine.stopping:
        timeout = engine.tick()
        if timeout is None:
            wait = None
        elif timeout <= 0.0:
            wait = 0.0
        else:
            wait = min(timeout, 0.5)
        server.serve_once(wait)
finally:
    try:
        server.close()
    finally:
        engine.close()
'''


def _envelope(document_type: str, payload: dict) -> dict:
    return {"schema_version": 1, "document_type": document_type, "payload": payload}


def _rights_record(project_id: str, artifact_id: str, revision_id: str, **overrides) -> dict:
    record = {
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
    record.update(overrides)
    return record


def _brief_payload() -> dict:
    return {"objective": "o", "starting_hypothesis": "h", "questions": []}


class DiscoveryJobsCliBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-cli-djobs-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.state = self.tmp / "state"
        self.addCleanup(lambda: process.stop_worker(str(self.state), timeout=10))

    # -- CLI driving --------------------------------------------------------- #
    def _run(self, *args: str):
        out, err = io.StringIO(), io.StringIO()
        code = cli_main.main(
            ["--data-dir", str(self.state), *args], env={}, stdout=out, stderr=err
        )
        return code, out.getvalue(), err.getvalue()

    def cli(self, *args: str, expect: int = 0) -> dict:
        code, out, err = self._run(*args)
        self.assertEqual(
            code, expect, f"args={args} rc={code}\nstdout={out}\nstderr={err}"
        )
        if expect == 0:
            return json.loads(out)
        self.assertNotIn("Traceback", err)
        return json.loads(err)

    def write_json(self, name: str, data: object) -> Path:
        path = self.tmp / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def create_project(self, name: str = "proj") -> str:
        out = self.cli("create", str(self.tmp / name), "--name", name)
        return out["project"]["project_id"]

    def ref(self, pid: str, artifact_id: str, revision_id: str) -> dict:
        return {"project_id": pid, "artifact_id": artifact_id, "revision_id": revision_id}

    def store(self, pid: str) -> ProjectStore:
        info = Registry(str(self.state)).get(pid)
        return ProjectStore.open_at(
            paths.app_dir_for(info.root_path), expected_project_id=pid
        )

    def with_store(self, pid: str, fn):
        store = self.store(pid)
        try:
            return fn(store)
        finally:
            store.close()

    # -- controlled IPC / worker-launch seam --------------------------------- #
    @contextlib.contextmanager
    def fake_worker(self, responder=None):
        captured: dict = {}

        def _respond(request):
            return responder(request) if responder is not None else self._queued(request)

        def _request(self_client, request, *, timeout=None, expected_peer_pid=None):
            captured["request"] = request
            return _respond(request)

        patches = [
            mock.patch.object(
                process, "start_worker",
                lambda data_dir, *, timeout=None: {"running": True, "pid": 4242},
            ),
            mock.patch.object(
                process, "worker_status",
                lambda data_dir, *, timeout=None: {
                    "running": True, "verified": True, "pid": 4242
                },
            ),
            mock.patch.object(ipc.WorkerIPCClient, "request", _request),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        yield captured

    @staticmethod
    def _job(request) -> dict:
        mode = request.get("mode")
        return {
            "project_id": request.get("project_id"),
            "job_id": "job-fake-1",
            "request_id": request.get("request_id"),
            "task_kind": _MODE_TASK_KIND.get(mode),
            "state": "queued",
            "outcome_unresolved": False,
            "current_attempt_id": None,
            "current_fence": 0,
            "attempts_total": 0,
            "retries_used": 0,
            "accepted_fence": None,
            "wait_reason": None,
        }

    def _queued(self, request) -> dict:
        return {"ok": True, "queued": True, "job": self._job(request)}

    # -- real out-of-band approvals (owner-auth seams) ----------------------- #
    def install_owner_auth(self) -> None:
        patches = [
            mock.patch.object(owner_auth, "_new_challenge", lambda: _FIXED_CHALLENGE),
            mock.patch.object(owner_auth, "_current_owner_uid", lambda: _OWNER_UID),
            mock.patch.object(owner_auth, "_real_uid", lambda: _OWNER_UID),
            mock.patch.object(owner_auth, "_utcnow", lambda: leadfx._BASE_TIME),
            mock.patch.object(owner_auth, "_monotonic", lambda: 1000.0),
            mock.patch.object(
                owner_auth, "_open_controlling_tty",
                lambda: leadfx._FakeTty(replies=[_FIXED_CHALLENGE]),
            ),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def approve(self, pid, artifact_id, revision_id, gate_kind, gate_id):
        def _approve(store):
            prepared = prepare_gate_snapshot(
                store, self.ref(pid, artifact_id, revision_id),
                gate_kind=gate_kind, gate_id=gate_id,
                decision="approved", rationale="synthetic human gate",
            )
            capability = owner_auth.request_owner_confirmation(prepared.owner_action())
            assert isinstance(capability, OwnerApproval)
            return HumanApprovalRepository(store).record_decision(prepared, capability)

        return self.with_store(pid, _approve)

    # -- store fixtures ------------------------------------------------------ #
    def put(self, pid, artifact_id, kind, revision_id, document, *, rights=None,
            dependencies=(), parent=None):
        def _put(store):
            repo = ArtifactRepository(store)
            repo.create_artifact(artifact_id, kind=kind)
            return repo.import_revision(
                artifact_id=artifact_id,
                content=document,
                origin_locator="import:synthetic",
                rights=rights or _rights_record(pid, artifact_id, revision_id),
                revision_id=revision_id,
                dependencies=list(dependencies),
                parent_revision_id=parent,
            )

        return self.with_store(pid, _put)

    def make_brief(self, pid, artifact_id="a-brief", revision_id="rev-brief") -> dict:
        self.put(pid, artifact_id, "report", revision_id,
                 _envelope("discovery_brief", _brief_payload()))
        return self.ref(pid, artifact_id, revision_id)


# --------------------------------------------------------------------------- #
# search submission
# --------------------------------------------------------------------------- #
class SearchSubmitTest(DiscoveryJobsCliBase):
    def test_search_run_submits_exact_discovery_submit(self) -> None:
        pid = self.create_project()
        with self.fake_worker() as captured:
            out = self.cli(
                "discovery", "search", "run", "--project", pid,
                "--artifact-id", "a-plan", "--revision-id", "rev-plan",
                "--query-index", "2", "--request-id", "req-1",
            )
        request = captured["request"]
        self.assertEqual(request["version"], 1)
        self.assertEqual(request["op"], "discovery_submit")
        self.assertEqual(request["mode"], "query")
        self.assertEqual(request["project_id"], pid)
        self.assertEqual(request["request_id"], "req-1")
        self.assertEqual(
            request["parameters"],
            {
                "plan_ref": self.ref(pid, "a-plan", "rev-plan"),
                "query_index": 2,
            },
        )
        # Admission only: never a completion, and always a queued job.
        self.assertTrue(out["queued"])
        self.assertTrue(out["admission_only"])
        self.assertFalse(out["job_completion"])
        self.assertEqual(out["mode"], "query")
        self.assertEqual(out["result_kind"], "discovery_observation")
        self.assertEqual(out["job_id"], "job-fake-1")

    def test_search_invalid_input_zero_ipc(self) -> None:
        # ``test_search_run_bad_query_index_performs_zero_ipc`` and
        # ``test_search_seed_empty_query_zero_ipc``: a rejected search argument is
        # a validation error (exit 2) that performs zero IPC.
        pid = self.create_project()
        with self.subTest(case="test_search_run_bad_query_index_performs_zero_ipc"):
            with self.fake_worker() as captured:
                self.cli(
                    "discovery", "search", "run", "--project", pid,
                    "--artifact-id", "a-plan", "--revision-id", "rev-plan",
                    "--query-index", "not-an-int", "--request-id", "req-1",
                    expect=2,
                )
            self.assertEqual(captured, {})
        with self.subTest(case="test_search_seed_empty_query_zero_ipc"):
            with self.fake_worker() as captured:
                self.cli(
                    "discovery", "search", "seed", "--project", pid,
                    "--source", "europepmc", "--query", "   ",
                    "--page-size", "5", "--max-pages", "1", "--request-id", "seed-3",
                    expect=2,
                )
            self.assertEqual(captured, {})

    def test_search_seed_is_provisional_and_exact(self) -> None:
        pid = self.create_project()
        with self.fake_worker() as captured:
            out = self.cli(
                "discovery", "search", "seed", "--project", pid,
                "--source", "europepmc", "--query", _QUERY_TEXT,
                "--page-size", "5", "--max-pages", "1", "--request-id", "seed-1",
            )
        request = captured["request"]
        self.assertEqual(request["mode"], "seed")
        self.assertEqual(
            request["parameters"],
            {"source": "europepmc", "query": _QUERY_TEXT, "page_size": 5, "max_pages": 1},
        )
        self.assertTrue(out["provisional"])
        self.assertFalse(out["job_completion"])
        # The queued admission never echoes the raw query text.
        self.assertNotIn(_QUERY_TEXT, json.dumps(out))

    def test_search_seed_ceiling_is_worker_side(self) -> None:
        # The CLI does not duplicate the worker's hard ceiling: an over-budget
        # shape is submitted and the *worker* refuses it (bad_value -> exit 2).
        pid = self.create_project()

        def refuse(request):
            return {"ok": False, "code": "bad_value"}

        with self.fake_worker(refuse) as captured:
            self.cli(
                "discovery", "search", "seed", "--project", pid,
                "--source", "europepmc", "--query", "q",
                "--page-size", "21", "--max-pages", "1", "--request-id", "seed-2",
                expect=2,
            )
        self.assertIn("request", captured)


# --------------------------------------------------------------------------- #
# metadata import
# --------------------------------------------------------------------------- #
class ImportSubmitTest(DiscoveryJobsCliBase):
    def _import_args(self, pid, path, **extra):
        args = [
            "discovery", "search", "import", "--project", pid,
            "--artifact-id", "a-plan", "--revision-id", "rev-plan",
            "--file", str(path), "--source", "regulatory",
            "--origin-locator", "reg:set-1", "--observed-at", "2026-10-28T00:00:00Z",
            "--request-id", "imp-1",
        ]
        return args

    def test_import_submits_exact_records(self) -> None:
        pid = self.create_project()
        records = [{"source": "regulatory", "source_id": "R-1", "title": "t"}]
        path = self.write_json("records.json", records)
        with self.fake_worker() as captured:
            out = self.cli(*self._import_args(pid, path))
        request = captured["request"]
        self.assertEqual(request["mode"], "metadata_import")
        self.assertEqual(request["parameters"]["plan_ref"], self.ref(pid, "a-plan", "rev-plan"))
        self.assertEqual(request["parameters"]["records"], records)
        self.assertEqual(request["parameters"]["source"], "regulatory")
        self.assertEqual(request["parameters"]["origin_locator"], "reg:set-1")
        self.assertEqual(request["parameters"]["observed_at"], "2026-10-28T00:00:00Z")
        self.assertEqual(out["result_kind"], "discovery_observation")
        # The queued admission never echoes the raw locator or record fields.
        self.assertNotIn("reg:set-1", json.dumps(out))
        self.assertNotIn("title", json.dumps(out))

    def test_import_invalid_inputs_zero_ipc(self) -> None:
        # ``test_import_empty_or_bad_file_zero_ipc``,
        # ``test_import_missing_file_zero_ipc`` and
        # ``test_import_payload_cap_zero_ipc``: every rejected import input is a
        # validation error (exit 2) that performs zero IPC.
        pid = self.create_project()
        with self.subTest(case="test_import_empty_or_bad_file_zero_ipc"):
            for bad in ([], "not-a-list", {"a": 1}, [1, 2]):
                path = self.write_json(f"bad-{abs(hash(json.dumps(bad)))}.json", bad)
                with self.fake_worker() as captured:
                    self.cli(*self._import_args(pid, path), expect=2)
                self.assertEqual(captured, {})
        with self.subTest(case="test_import_missing_file_zero_ipc"):
            with self.fake_worker() as captured:
                self.cli(*self._import_args(pid, self.tmp / "nope.json"), expect=2)
            self.assertEqual(captured, {})
        with self.subTest(case="test_import_payload_cap_zero_ipc"):
            # Far larger than the 64 KiB control-transport request bound.
            records = [
                {"source": "regulatory", "source_id": f"R-{i}", "title": "x" * 200}
                for i in range(1000)
            ]
            path = self.write_json("huge.json", records)
            with self.fake_worker() as captured:
                self.cli(*self._import_args(pid, path), expect=2)
            self.assertEqual(captured, {})


# --------------------------------------------------------------------------- #
# expand submission + inspection
# --------------------------------------------------------------------------- #
class ExpandSubmitTest(DiscoveryJobsCliBase):
    def test_expand_run_submits_exact(self) -> None:
        pid = self.create_project()
        roots = [{"source": "MED", "id": "1"}, {"source": "MED", "id": "1"}]
        path = self.write_json("roots.json", roots)
        with self.fake_worker() as captured:
            out = self.cli(
                "discovery", "expand", "run", "--project", pid,
                "--artifact-id", "a-plan", "--revision-id", "rev-plan",
                "--corpus-artifact-id", "a-corpus", "--corpus-revision-id", "rev-corpus",
                "--roots-file", str(path), "--request-id", "exp-1",
            )
        request = captured["request"]
        self.assertEqual(request["mode"], "expand")
        self.assertEqual(
            request["parameters"],
            {
                "plan_ref": self.ref(pid, "a-plan", "rev-plan"),
                "corpus_ref": self.ref(pid, "a-corpus", "rev-corpus"),
                "roots": roots,
            },
        )
        self.assertEqual(out["result_kind"], "discovery_expansion")

    def test_expand_run_invalid_arguments_zero_ipc(self) -> None:
        # ``test_expand_run_bad_roots_zero_ipc`` and
        # ``test_expand_run_has_no_cli_limits_override``: a bad roots file and an
        # unsupported ``--limits`` override are both validation errors (exit 2)
        # with zero IPC.
        pid = self.create_project()
        with self.subTest(case="test_expand_run_bad_roots_zero_ipc"):
            for bad in ([], [{"source": "MED"}], [{"source": "", "id": "1"}],
                        [{"source": "MED", "id": "1", "extra": 2}], "nope"):
                path = self.write_json(f"roots-{abs(hash(json.dumps(bad)))}.json", bad)
                with self.fake_worker() as captured:
                    self.cli(
                        "discovery", "expand", "run", "--project", pid,
                        "--artifact-id", "a-plan", "--revision-id", "rev-plan",
                        "--corpus-artifact-id", "a-corpus", "--corpus-revision-id", "rev-corpus",
                        "--roots-file", str(path), "--request-id", "exp-2",
                        expect=2,
                    )
                self.assertEqual(captured, {})
        with self.subTest(case="test_expand_run_has_no_cli_limits_override"):
            path = self.write_json("roots.json", [{"source": "MED", "id": "1"}])
            with self.fake_worker() as captured:
                self.cli(
                    "discovery", "expand", "run", "--project", pid,
                    "--artifact-id", "a-plan", "--revision-id", "rev-plan",
                    "--corpus-artifact-id", "a-corpus", "--corpus-revision-id", "rev-corpus",
                    "--roots-file", str(path), "--request-id", "exp-3",
                    "--limits", "max_requests=999",
                    expect=2,
                )
            self.assertEqual(captured, {})


class ExpandShowTest(DiscoveryJobsCliBase):
    def test_expand_show_needs_exactly_one_target(self) -> None:
        pid = self.create_project()
        self.cli("discovery", "expand", "show", "--project", pid, expect=2)
        self.cli(
            "discovery", "expand", "show", "--project", pid,
            "--job-id", "j", "--request-id", "r", expect=2,
        )
        self.cli("discovery", "expand", "show", "--project", pid,
                 "--request-id", "missing", expect=2)


class ExpandShowContentTest(routes.DiscoveryJobRoutesBase):
    """A real completed expand job, read back through the CLI (summary/content).

    Reuses the job-routes fixtures: a real registry/store, an approved plan, a
    real observed corpus, and a deterministic citation provider (no network).
    """

    def _cli(self, *args: str, expect: int = 0) -> dict:
        out, err = io.StringIO(), io.StringIO()
        code = cli_main.main(
            ["--data-dir", str(self.data_dir), *args], env={}, stdout=out, stderr=err
        )
        self.assertEqual(code, expect, f"args={args}\nstdout={out.getvalue()}\nstderr={err.getvalue()}")
        return json.loads(out.getvalue() if expect == 0 else err.getvalue())

    def test_expand_show_summary_then_explicit_content(self) -> None:
        provider = routes._CitationProvider({
            ("MED", "1", "references"): [
                routes._CitationProvider.response(
                    routes._ref_payload(1, [routes._cite_entry("2")])
                )
            ],
            ("MED", "1", "citations"): [
                routes._CitationProvider.response(routes._cit_payload(0, []))
            ],
            ("MED", "2", "references"): [
                routes._CitationProvider.response(routes._ref_payload(0, []))
            ],
            ("MED", "2", "citations"): [
                routes._CitationProvider.response(routes._cit_payload(0, []))
            ],
        })
        engine = self.engine(provider)
        plan_ref = self.build_plan()
        self.approve_plan(plan_ref)
        run = self.observe_epmc([
            routes.fx.record(source=routes.SOURCE_EUROPEPMC, source_id="MED:1",
                             pmid="1", pmcid=None, doi=None)
        ])
        corpus_ref = self.ingest_corpus([run.run_id])
        res = self._submit(
            engine, request_id="exp-show", mode="expand",
            parameters={"plan_ref": plan_ref, "corpus_ref": corpus_ref,
                        "roots": [{"source": "MED", "id": "1"}]},
        )
        self.assertTrue(res["ok"], res)
        self._drive(engine)
        engine.close()

        summary_out = self._cli(
            "discovery", "expand", "show", "--project", self.project_id,
            "--request-id", "exp-show",
        )
        summary = summary_out["expansion"]
        self.assertEqual(summary["mode"], "expand")
        self.assertNotIn("edges", summary)
        self.assertNotIn("relations", summary)
        self.assertEqual(summary["guarantees"],
                         {"completeness_claimed": False, "verified_support": False})

        content_out = self._cli(
            "discovery", "expand", "show", "--project", self.project_id,
            "--request-id", "exp-show", "--content",
        )
        self.assertTrue(content_out["private"])
        self.assertTrue(content_out["non_authorizing"])
        self.assertFalse(content_out["authorizes_execution"])
        full = content_out["document"]
        self.assertTrue(full["relations"])
        self.assertTrue(full["edges"])
        self.assertEqual(full["guarantees"],
                         {"completeness_claimed": False, "verified_support": False})


# --------------------------------------------------------------------------- #
# lead configure
# --------------------------------------------------------------------------- #
class LeadConfigureTest(DiscoveryJobsCliBase):
    def _hosted_use(self, pid, purpose="interview", **overrides):
        source_ref = self.ref(pid, "a-brief", "rev-brief")
        document = leadfx._hosted_use_document(
            purpose, leadfx._config_map(), [source_ref]
        )
        document["payload"].update(overrides)
        return document

    def _configure(self, pid, document, *, artifact_id="a-batch", revision_id="rev-batch",
                   rights=None, parent=None, request_id="conf-1", expect=0):
        doc_path = self.write_json(f"hu-{artifact_id}-{revision_id}.json", document)
        rights = rights or _rights_record(pid, artifact_id, revision_id)
        rights_path = self.write_json(f"hu-rights-{artifact_id}-{revision_id}.json", rights)
        args = [
            "discovery", "lead", "configure", "--project", pid,
            "--artifact-id", artifact_id, "--revision-id", revision_id,
            "--file", str(doc_path), "--rights-file", str(rights_path),
            "--request-id", request_id,
        ]
        if parent is not None:
            args += ["--parent-revision-id", parent]
        return self.cli(*args, expect=expect)

    def test_configure_stores_and_returns_non_authorizing_request(self) -> None:
        pid = self.create_project()
        self.make_brief(pid)
        out = self._configure(pid, self._hosted_use(pid))
        self.assertEqual(out["replayed"], False)
        rev = out["revision"]
        self.assertEqual(rev["document_type"], "discovery_hosted_use")
        self.assertEqual(rev["purpose"], "interview")
        self.assertEqual(rev["scientific_state"], "proposal")
        self.assertFalse(rev["authorizes_execution"])
        request = out["approval_request"]
        self.assertEqual(request["gate_kind"], "batch")
        self.assertEqual(request["gate_id"], "discovery.hosted_use.interview")
        self.assertFalse(request["satisfied"])
        self.assertTrue(request["current"])
        self.assertFalse(request["authorizes_execution"])
        self.assertEqual(request["source_refs"], [self.ref(pid, "a-brief", "rev-brief")])
        self.assertEqual(
            request["snapshot_preparation"]["gate_id"], "discovery.hosted_use.interview"
        )

    def test_configure_gate_id_is_per_purpose(self) -> None:
        pid = self.create_project()
        self.make_brief(pid)
        out = self._configure(
            pid, self._hosted_use(pid, purpose="framing"),
            artifact_id="a-batch-framing", revision_id="rev-batch-framing",
        )
        self.assertEqual(out["approval_request"]["gate_id"], "discovery.hosted_use.framing")

    def test_configure_invalid_input_writes_nothing(self) -> None:
        # ``test_configure_invalid_envelope_writes_nothing`` and
        # ``test_configure_bad_rights_writes_nothing``: an invalid envelope or a
        # bad rights record is refused (exit 2) and writes no artifact.
        with self.subTest(case="test_configure_invalid_envelope_writes_nothing"):
            pid = self.create_project()
            self.make_brief(pid)
            bad = self._hosted_use(pid)
            bad["document_type"] = "discovery_not_hosted_use"
            self._configure(pid, bad, expect=2)
            with self.store(pid) as store:
                self.assertIsNone(ArtifactRepository(store).get_artifact("a-batch"))
        with self.subTest(case="test_configure_bad_rights_writes_nothing"):
            pid = self.create_project()
            self.make_brief(pid)
            self._configure(
                pid, self._hosted_use(pid), rights={"operations": {}},
                request_id="conf-rights", expect=2,
            )
            with self.store(pid) as store:
                self.assertIsNone(ArtifactRepository(store).get_artifact("a-batch"))

    def test_configure_replay_and_conflict(self) -> None:
        pid = self.create_project()
        self.make_brief(pid)
        document = self._hosted_use(pid)
        first = self._configure(pid, document, request_id="conf-replay")
        second = self._configure(pid, document, request_id="conf-replay")
        self.assertTrue(second["replayed"])
        self.assertEqual(
            second["revision"]["revision_id"], first["revision"]["revision_id"]
        )
        # A divergent document under the same revision id conflicts.
        other = self._hosted_use(pid)
        other["payload"]["provider_terms"] = {
            "ref": "evidence:other", "verified_at": "2026-10-28T00:00:00Z"
        }
        self._configure(pid, other, request_id="conf-replay", expect=3)

    def test_configure_amendment_pins_version_of_and_fresh_gate(self) -> None:
        pid = self.create_project()
        self.make_brief(pid)
        first = self._configure(pid, self._hosted_use(pid), request_id="conf-amend")
        # Approve the first revision at its exact named gate.
        self.install_owner_auth()
        self.approve(pid, "a-batch", "rev-batch", HOSTED_USE_GATE_KIND,
                     "discovery.hosted_use.interview")
        head = self._configure(
            pid, self._hosted_use(pid), request_id="conf-amend-2",
        )
        self.assertTrue(head["approval_request"]["satisfied"])
        # An amendment with an exact parent + version_of lineage edge + fresh gate.
        amended = self._configure(
            pid, self._hosted_use(pid), revision_id="rev-batch-2",
            parent="rev-batch", request_id="conf-amend-3",
        )
        deps = amended["revision"]["dependencies"]
        self.assertTrue(
            any(d.get("role") == "version_of" and d["revision_id"] == "rev-batch"
                for d in deps)
        )
        self.assertEqual(amended["approval_request"]["gate_id"],
                         "discovery.hosted_use.interview")
        self.assertFalse(amended["approval_request"]["satisfied"])
        self.assertTrue(amended["approval_request"]["current"])
        # A historical replay of rev-batch now reports a stale, unsatisfied gate
        # (rev-batch-2 is the head and rev-batch-2 was never approved here).
        replay = self._configure(
            pid, self._hosted_use(pid), request_id="conf-amend",
        )
        self.assertTrue(replay["replayed"])
        self.assertFalse(replay["approval_request"]["current"])
        self.assertTrue(replay["approval_request"]["stale"])
        self.assertFalse(replay["approval_request"]["satisfied"])

    def test_configure_json_dependencies_are_native_dicts(self) -> None:
        # Sanity: the returned revision mapping is JSON-serializable.
        pid = self.create_project()
        self.make_brief(pid)
        out = self._configure(pid, self._hosted_use(pid))
        json.dumps(out)


# --------------------------------------------------------------------------- #
# lead run
# --------------------------------------------------------------------------- #
class LeadRunTest(DiscoveryJobsCliBase):
    def _run_lead(self, pid, **extra):
        args = [
            "discovery", "lead", "run", "--project", pid,
            "--batch-artifact-id", "a-batch", "--batch-revision-id", "rev-batch",
            "--credential-env", "OPENROUTER_API_KEY", "--request-id", "lead-1",
        ]
        return self.cli(*args, **extra)

    def test_lead_run_requires_named_env_and_queues(self) -> None:
        pid = self.create_project()
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.fake_worker() as captured:
                out = self._run_lead(pid)
        request = captured["request"]
        self.assertEqual(request["mode"], "lead")
        self.assertEqual(
            request["parameters"],
            {"batch_ref": self.ref(pid, "a-batch", "rev-batch"),
             "credential_env": "OPENROUTER_API_KEY"},
        )
        self.assertTrue(out["queued"])
        self.assertFalse(out["job_completion"])
        self.assertFalse(out["key_value_accepted"])
        # The named key is absent from this process's environment.
        self.assertTrue(out["configuration_required"])
        self.assertIn("notice", out)

    def test_lead_run_never_logs_key_value(self) -> None:
        pid = self.create_project()
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": _SECRET}):
            with self.fake_worker() as captured:
                out = self._run_lead(pid)
        # The value is nowhere in the CLI output or the IPC request; only the
        # fixed environment-variable *name* is ever carried.
        self.assertNotIn(_SECRET, json.dumps(out))
        self.assertNotIn(_SECRET, json.dumps(captured["request"]))
        self.assertFalse(out["key_value_accepted"])
        self.assertFalse(out["configuration_required"])
        self.assertEqual(captured["request"]["parameters"]["credential_env"],
                         "OPENROUTER_API_KEY")

    def test_lead_run_bad_credential_env_zero_ipc(self) -> None:
        pid = self.create_project()
        with self.fake_worker() as captured:
            self.cli(
                "discovery", "lead", "run", "--project", pid,
                "--batch-artifact-id", "a-batch", "--batch-revision-id", "rev-batch",
                "--credential-env", "SOME_OTHER_KEY", "--request-id", "lead-2",
                expect=2,
            )
        self.assertEqual(captured, {})

    def test_lead_run_key_value_is_never_a_flag(self) -> None:
        pid = self.create_project()
        # A direct credential value is not a supported option anywhere.
        self.cli(
            "discovery", "lead", "run", "--project", pid,
            "--batch-artifact-id", "a-batch", "--batch-revision-id", "rev-batch",
            "--credential-env", "OPENROUTER_API_KEY", "--request-id", "lead-3",
            "--key", _SECRET, expect=2,
        )


# --------------------------------------------------------------------------- #
# flag-surface guards
# --------------------------------------------------------------------------- #
class FlagSurfaceTest(DiscoveryJobsCliBase):
    def test_no_actor_or_yes_flags(self) -> None:
        pid = self.create_project()
        for bad in ("--actor", "--yes", "--approve", "--approved"):
            self.cli(
                "discovery", "search", "run", "--project", pid,
                "--artifact-id", "a", "--revision-id", "r",
                "--query-index", "0", "--request-id", "req",
                bad, "x", expect=2,
            )

    def _help(self, *args: str) -> str:
        parser = cli_main.build_parser()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as raised:
                parser.parse_args(list(args))
        self.assertEqual(raised.exception.code, 0)
        return buf.getvalue()

    def test_help_documents_new_commands(self) -> None:
        self.assertIn("--query-index", self._help("discovery", "search", "run", "--help"))

        self.assertIn("--roots-file", self._help("discovery", "expand", "run", "--help"))
        self.assertIn("--credential-env", self._help("discovery", "lead", "run", "--help"))
        # The existing lead readbacks are preserved by the extended group.
        self.assertIn("--request-id", self._help("discovery", "lead", "status", "--help"))
        self.assertIn("--request-id", self._help("discovery", "lead", "content", "--help"))


# --------------------------------------------------------------------------- #
# real owned worker process (E2E seed)
# --------------------------------------------------------------------------- #
class RealWorkerProcessTest(DiscoveryJobsCliBase):
    def test_seed_job_completes_in_an_owned_worker_process(self) -> None:
        pid = self.create_project()
        script = self.tmp / "fixture_worker.py"
        script.write_text(FIXTURE_WORKER, encoding="utf-8")
        with mock.patch.object(
            process, "_build_command", lambda data_dir: [sys.executable, str(script), data_dir]
        ):
            self.cli(
                "discovery", "search", "seed", "--project", pid,
                "--source", "europepmc", "--query", "synthetic seed",
                "--page-size", "5", "--max-pages", "1", "--request-id", "e2e-seed",
            )
            # Poll the owned worker to a terminal scheduling state.
            deadline = time.monotonic() + 30.0
            state = None
            job_id = None
            while time.monotonic() < deadline:
                jobs = self.cli("job", "list", "--project", pid)["jobs"]
                for job in jobs:
                    if job["request_id"] == "e2e-seed":
                        job_id = job["job_id"]
                        state = job["state"]
                        break
                if state in {"completed", "failed", "cancelled", "waiting_human"}:
                    break
                time.sleep(0.1)
            self.assertEqual(state, "completed")
            status = self.cli("job", "status", job_id, "--project", pid)["job"]
            self.assertEqual(status["task_kind"], "discovery_seed")
            # The generic job status projects the accepted result to its safe
            # result kind (the raw observation metadata is never echoed by the
            # lifecycle projection).
            self.assertEqual(
                status["accepted_result"]["result_kind"], "discovery_observation"
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
