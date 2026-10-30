"""In-process CLI acceptance tests for the ``acquisition retrieve`` addon (Step 06, C4).

Covers ``src/review_article_agent/cli/acquisition_retrieval.py`` against Step 06
(C4: a minimal public CLI over the already-detached official retrieval route):

* ``acquisition retrieve id`` prints deterministic report/occurrence artifact ids
  and is strictly read-only (no worker, no write);
* ``acquisition retrieve run`` strictly validates the exact acquire driver
  parameters with the real
  :func:`~review_article_agent.worker.discovery.validate_submission`
  (``mode="acquire"``) **before** any worker start, sends one exact version-1
  ``discovery_submit`` (``mode="acquire"``) message over the owner-only control
  transport, and returns a **queued admission only**;
* a malformed / unknown-key / ineligible-rights request performs **zero IPC**;
* ``acquisition retrieve show`` reads the exact verified stored E1 report with a
  safe summary by default and the private body only with ``--content``, issues no
  GET and acquires no execution, and never redacts away the non-authorizing /
  historical nature of the record.

Synthetic data only: a real temporary registry and project store, the real
acquisition/freeze test fixtures, and controlled worker-launch / IPC seams. No
network, provider, model, or approval call is performed.

Run::

    PYTHONPATH=src python3 -m unittest tests.test_cli_acquisition_retrieval
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock
from _tiers import slow

SRC = Path(__file__).resolve().parents[1] / "src"
TESTS = Path(__file__).resolve().parent
for _path in (str(SRC), str(TESTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import review_article_agent.cli.acquisition_retrieval as retr  # noqa: E402
import review_article_agent.cli.main as cli_main  # noqa: E402
from review_article_agent.acquisition.execution import AcquisitionExecutor  # noqa: E402
from review_article_agent.runtime import ipc  # noqa: E402
from review_article_agent.storage import ArtifactRepository  # noqa: E402
from review_article_agent.worker import process  # noqa: E402
from review_article_agent.worker import discovery as worker_discovery  # noqa: E402

import test_acquisition_execution as acqexec  # noqa: E402


def _no_lowlevel(url, *, timeout, max_response_bytes):  # pragma: no cover
    raise AssertionError("the CLI never issues a request in these tests")


class RetrievalCliBase(acqexec.Base):
    """A real registry/store/freeze fixture, driven through the real CLI."""

    def setUp(self) -> None:
        super().setUp()
        self.state = self.tmp / "state"

    # -- CLI driving --------------------------------------------------------- #
    def _run(self, *args: str):
        out, err = io.StringIO(), io.StringIO()
        code = cli_main.main(
            ["--data-dir", str(self.state), *args], env={}, stdout=out, stderr=err
        )
        return code, out.getvalue(), err.getvalue()

    def cli(self, *args: str, expect: int = 0) -> dict:
        code, out, err = self._run(*args)
        self.assertEqual(code, expect, f"args={args} rc={code}\n{out}\n{err}")
        if expect == 0:
            return json.loads(out)
        self.assertNotIn("Traceback", err)
        return json.loads(err)

    def write_json(self, name: str, data: object) -> str:
        path = self.tmp / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return str(path)

    def write_bytes(self, name: str, data: bytes) -> str:
        path = self.tmp / name
        path.write_bytes(data)
        return str(path)

    # -- controlled worker-launch / IPC seam --------------------------------- #
    @contextlib.contextmanager
    def fake_worker(self, responder=None):
        captured: dict = {}

        def _request(self_client, request, *, timeout=None, expected_peer_pid=None):
            captured["request"] = request
            return responder(request) if responder is not None else self._queued(request)

        patches = [
            mock.patch.object(
                process,
                "start_worker",
                lambda data_dir, *, timeout=None: {"running": True, "pid": 4242},
            ),
            mock.patch.object(
                process,
                "worker_status",
                lambda data_dir, *, timeout=None: {
                    "running": True,
                    "verified": True,
                    "pid": 4242,
                },
            ),
            mock.patch.object(ipc.WorkerIPCClient, "request", _request),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        yield captured

    def _queued(self, request) -> dict:
        return {
            "ok": True,
            "queued": True,
            "job": {
                "project_id": request.get("project_id"),
                "job_id": "job-fake-1",
                "request_id": request.get("request_id"),
                "task_kind": "acquisition_fulltext",
                "state": "queued",
                "outcome_unresolved": False,
                "current_attempt_id": None,
                "current_fence": 0,
                "attempts_total": 0,
                "retries_used": 0,
                "accepted_fence": None,
                "wait_reason": None,
            },
        }

    # -- fixtures ------------------------------------------------------------ #
    def acquire_parameters(self, request_id: str, candidate: str) -> dict:
        """The exact acquire driver parameters (without the request id)."""
        ex = AcquisitionExecutor(
            self.store,
            registry=self.registry,
            activation_token=None,
            lowlevel=_no_lowlevel,
            stop_requested=lambda: True,
        )
        full = self.args(ex, request_id=request_id, candidate=candidate)
        full.pop("request_id")
        return full

    def store_report(self, request_id: str, candidate: str, responses=()):
        net = acqexec.Net(*responses)
        ex = self.executor(net)
        result = ex.acquire(**self.args(ex, request_id=request_id, candidate=candidate))
        return net, result

    def store_counts(self) -> tuple[int, int]:
        repo = ArtifactRepository(self.store)
        arts = repo.list_artifacts()
        return len(arts), sum(len(repo.list_revisions(a.artifact_id)) for a in arts)

    def job_count(self) -> int:
        return self.store.connection.execute(
            "SELECT COUNT(*) FROM job"
        ).fetchone()[0]


# --------------------------------------------------------------------------- #
# id (deterministic, read-only)
# --------------------------------------------------------------------------- #
class RetrieveIdTest(RetrievalCliBase):
    def test_id_is_deterministic_read_only_and_no_worker(self):
        before = self.store_counts()
        jobs_before = self.job_count()
        with mock.patch.object(
            process, "start_worker", side_effect=AssertionError("no worker")
        ):
            first = self.cli(
                "acquisition", "retrieve", "id",
                "--project", self.project_id, "--request-id", "req-id",
            )
            again = self.cli(
                "acquisition", "retrieve", "id",
                "--project", self.project_id, "--request-id", "req-id",
            )
            other = self.cli(
                "acquisition", "retrieve", "id",
                "--project", self.project_id, "--request-id", "req-other",
            )
        self.assertEqual(first, again)
        self.assertNotEqual(first["report_artifact_id"], other["report_artifact_id"])
        self.assertNotEqual(
            first["occurrence_artifact_id"], other["occurrence_artifact_id"]
        )
        self.assertTrue(first["report_artifact_id"].startswith("acr_"))
        self.assertTrue(first["occurrence_artifact_id"].startswith("occ_"))
        # The report and occurrence ids are distinct for one request.
        self.assertNotEqual(
            first["report_artifact_id"], first["occurrence_artifact_id"]
        )
        self.assertEqual(self.store_counts(), before)
        self.assertEqual(self.job_count(), jobs_before)

    def test_id_unknown_project_and_bad_request_id(self):
        unknown = self.cli(
            "acquisition", "retrieve", "id",
            "--project", "no-such-project", "--request-id", "req-id",
            expect=cli_main.EXIT_VALIDATION,
        )
        self.assertEqual(unknown["error"]["category"], "validation")
        bad = self.cli(
            "acquisition", "retrieve", "id",
            "--project", self.project_id, "--request-id", "bad id",
            expect=cli_main.EXIT_VALIDATION,
        )
        self.assertEqual(bad["error"]["category"], "validation")


# --------------------------------------------------------------------------- #
# run (strict validation before any worker; queued admission only)
# --------------------------------------------------------------------------- #
class RetrieveRunTest(RetrievalCliBase):
    def _run_args(self, path, request_id="run-1"):
        return [
            "acquisition", "retrieve", "run",
            "--project", self.project_id,
            "--request-id", request_id,
            "--request-file", str(path),
        ]

    def test_run_submits_exact_acquire_queue_admission(self):
        parameters = self.acquire_parameters("run-1", self.no_pmcid)
        path = self.write_json("req.json", parameters)
        with self.fake_worker() as captured:
            out = self.cli(*self._run_args(path))
        request = captured["request"]
        self.assertEqual(request["version"], 1)
        self.assertEqual(request["op"], "discovery_submit")
        self.assertEqual(request["mode"], "acquire")
        self.assertEqual(request["project_id"], self.project_id)
        self.assertEqual(request["request_id"], "run-1")
        # The payload is exactly the canonical, validated acquire parameters.
        expected = worker_discovery.validate_submission(
            "acquire", parameters, self.project_id, "run-1"
        )
        self.assertEqual(request["parameters"], expected)
        # Admission only: never a completion, never a GET, no source bytes.
        self.assertTrue(out["queued"])
        self.assertTrue(out["admission_only"])
        self.assertFalse(out["job_completion"])
        self.assertFalse(out["scientific_validation"])
        self.assertEqual(out["mode"], "acquire")
        self.assertEqual(out["result_kind"], "acquisition_retrieval")
        self.assertEqual(out["job_id"], "job-fake-1")
        self.assertNotIn("freeze_ref", json.dumps(out))
        self.assertNotIn("source_rights", json.dumps(out))

    def test_run_rejects_malformed_json_with_zero_ipc(self):
        for bad in (b"{", b"[1, 2]", b'{"a": 1, "a": 2}', b'{"a": NaN}', b"\xff\xfe"):
            path = self.write_bytes("bad.json", bad)
            with self.fake_worker() as captured:
                err = self.cli(*self._run_args(path), expect=cli_main.EXIT_VALIDATION)
            self.assertEqual(err["error"]["category"], "validation")
            self.assertEqual(captured, {})

    def test_run_rejects_unknown_keys_and_ineligible_rights_zero_ipc(self):
        valid = self.acquire_parameters("run-1", self.no_pmcid)

        unknown = dict(valid, an_unknown_field=1)
        path = self.write_json("unknown.json", unknown)
        with self.fake_worker() as captured:
            err = self.cli(*self._run_args(path), expect=cli_main.EXIT_VALIDATION)
        self.assertEqual(err["error"]["category"], "validation")
        self.assertEqual(captured, {})

        # The source rights must allow acquisition AND storage, with no denial.
        denied = json.loads(json.dumps(valid))
        denied["source_rights"]["operations"]["acquisition"] = "unknown"
        path = self.write_json("rights.json", denied)
        with self.fake_worker() as captured:
            err = self.cli(*self._run_args(path), expect=cli_main.EXIT_VALIDATION)
        self.assertEqual(err["error"]["category"], "validation")
        self.assertEqual(captured, {})

    def test_run_non_dict_request_file_zero_ipc(self):
        path = self.write_json("list.json", [1, 2, 3])
        with self.fake_worker() as captured:
            err = self.cli(*self._run_args(path), expect=cli_main.EXIT_VALIDATION)
        self.assertEqual(err["error"]["category"], "validation")
        self.assertEqual(captured, {})


# --------------------------------------------------------------------------- #
# show (verified readback; safe default; --content private; no GET)
# --------------------------------------------------------------------------- #
class RetrieveShowTest(RetrievalCliBase):
    def _show(self, request_id, *extra, expect=0):
        return self.cli(
            "acquisition", "retrieve", "show",
            "--project", self.project_id, "--request-id", request_id, *extra,
            expect=expect,
        )

    def _no_get_no_worker(self):
        stack = contextlib.ExitStack()
        m_no_io = stack.enter_context(
            mock.patch.object(
                retr, "_no_io", side_effect=AssertionError("GET attempted")
            )
        )
        stack.enter_context(
            mock.patch.object(
                process, "start_worker", side_effect=AssertionError("no worker")
            )
        )
        return stack, m_no_io

    def test_show_safe_then_content_verified_report_no_get(self):
        net, _ = self.store_report(
            "show-ok", self.with_pmcid, responses=(acqexec.ok(self.full_text()),)
        )
        self.assertEqual(len(net.urls), 1)
        stack, m_no_io = self._no_get_no_worker()
        with stack:
            safe = self._show("show-ok")
            content = self._show("show-ok", "--content")
        self.assertEqual(m_no_io.call_count, 0)
        # Safe default: refs/status/counts/validation state only, non-authorizing.
        self.assertEqual(safe["status"], "retrieved")
        self.assertEqual(safe["report_ref"]["artifact_id"][:4], "acr_")
        self.assertEqual(safe["attempt_count"], 1)
        self.assertTrue(safe["replayed"])
        self.assertTrue(safe["historical"])
        self.assertFalse(safe["authorizing"])
        self.assertEqual(safe["eligibility"], "eligibility_not_assessed")
        self.assertFalse(safe["private_content_included"])
        for key in ("report", "identifiers", "attempts", "terms_evidence", "origin_locator"):
            self.assertNotIn(key, safe)
        # The safe summary never echoes the private identifiers.
        self.assertNotIn("pmid", json.dumps(safe))
        # --content is the explicit private opt-in (the report holds no source bytes).
        self.assertTrue(content["private"])
        self.assertTrue(content["non_authorizing"])
        self.assertFalse(content["authorizing"])
        report = content["report"]
        self.assertEqual(report["authorizing"], False)
        self.assertEqual(report["scientific_acceptance"], "not_assessed")
        self.assertEqual(report["identifiers"]["pmcid"], "PMC111")
        # The report carries only counts/hashes of the retrieved bytes, never the
        # raw source content itself.
        self.assertIsInstance(report["content_bytes"], int)
        for key in ("body", "content", "preview", "content_b64"):
            self.assertNotIn(key, report)

    def test_show_manual_report_is_a_replayed_historical_proposal(self):
        net, result = self.store_report("show-manual", self.no_pmcid)
        self.assertEqual(net.urls, [])
        self.assertEqual(result["status"], "manual_required")
        stack, m_no_io = self._no_get_no_worker()
        with stack:
            first = self._show("show-manual")
            second = self._show("show-manual")
            content = self._show("show-manual", "--content")
        self.assertEqual(m_no_io.call_count, 0)
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "manual_required")
        self.assertIsNone(first["source_ref"])
        self.assertIsNone(first["occurrence_ref"])
        self.assertTrue(first["replayed"])
        self.assertTrue(first["historical"])
        self.assertFalse(first["authorizing"])
        # The historical stored record is a non-authorizing proposal, read back as-is.
        self.assertEqual(content["report"]["failures"], [{"code": "missing_pmcid"}])
        self.assertEqual(content["report"]["scientific_acceptance"], "not_assessed")
        self.assertIn("report_ref", content)

    def test_show_unknown_request_and_project(self):
        missing = self._show("never-stored", expect=cli_main.EXIT_VALIDATION)
        self.assertEqual(missing["error"]["category"], "validation")
        unknown = self.cli(
            "acquisition", "retrieve", "show",
            "--project", "no-such-project", "--request-id", "req-id",
            expect=cli_main.EXIT_VALIDATION,
        )
        self.assertEqual(unknown["error"]["category"], "validation")


# --------------------------------------------------------------------------- #
# real detached worker (no-network manual-required smoke)
# --------------------------------------------------------------------------- #
class RetrieveRealWorkerSmokeTest(RetrievalCliBase):
    """One real, owned, detached worker over a disposable data dir (no network).

    A genuinely frozen candidate *without* a PMCID cannot use the official
    full-text waterfall, so the worker writes a durable ``manual_required`` report
    **without issuing any GET**. This drives the whole route through the real
    worker lifecycle (``worker start`` -> queued admission -> ``job status`` ->
    ``acquisition retrieve show``) and stops only this fixture's worker.
    """

    @slow
    def test_real_worker_writes_manual_required_report_without_get(self):
        parameters = self.acquire_parameters("smoke-1", self.no_pmcid)
        path = self.write_json("smoke-req.json", parameters)
        self.cli("worker", "start")
        self.addCleanup(
            lambda: self.cli("worker", "stop", expect=cli_main.EXIT_SUCCESS)
        )
        submitted = self.cli(
            "acquisition", "retrieve", "run",
            "--project", self.project_id,
            "--request-id", "smoke-1",
            "--request-file", path,
        )
        self.assertTrue(submitted["queued"])
        self.assertFalse(submitted["job_completion"])
        job_id = submitted["job_id"]
        state = None
        for _ in range(600):
            status = self.cli(
                "job", "status", job_id, "--project", self.project_id
            )
            state = status["job"]["state"]
            if state in ("completed", "failed", "cancelled"):
                break
            time.sleep(0.05)
        self.assertEqual(state, "completed")
        shown = self.cli(
            "acquisition", "retrieve", "show",
            "--project", self.project_id, "--request-id", "smoke-1",
        )
        self.assertEqual(shown["status"], "manual_required")
        self.assertIsNone(shown["source_ref"])
        self.assertIsNone(shown["occurrence_ref"])
        self.assertTrue(shown["replayed"])
        self.assertFalse(shown["authorizing"])


if __name__ == "__main__":
    unittest.main()
