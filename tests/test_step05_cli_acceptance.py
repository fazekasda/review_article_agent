"""Step 05 FINAL integrated acceptance tests (Step E2, CLI half).

Authority
=========

* ``docs/implementation/05-discovery.md`` acceptance criteria AC1-AC7.
* ``docs/decisions/003-step05-discovery-and-direct-lead.md`` (D-1..D-5).
* ``POL-OPS-COVERAGE`` (no universal numeric stop/completeness rule; the exact
  candidate revision is frozen by explicit human approval) and ``POL-P3`` (an
  unknown integrity status is not clean).

Scope of this module
====================

These are **integrated CLI end-to-end** acceptance tests that drive the *real*
``review_article_agent.cli.main`` entrypoint against a *real* disposable
``Registry`` + ``ProjectStore`` with the *real* owner-capability approval seam
and an *actual* ``WorkerEngine`` (driven over the **real** owner-only Unix
control socket). The single external boundary stubbed is the low-level discovery
HTTP transport; nothing else is mocked, no live/paid/upload/provider call is
made, and no credential appears in any output.

They chain the whole personal-CLI chain:

    approved plan + seed setup (through the ``discovery context`` CLI)
      -> an actual queued discovery job with a durable accepted result
      -> ``discovery corpus ingest``
      -> an actual owned-worker citation-expansion job (both directions)
      -> ``discovery report create --expansion-job-id <REAL job id>``
      -> ``discovery report show`` (safe summary; ``--content`` opt-in)
      -> ``discovery freeze propose`` (inert; exact named-gate request)
      -> ``discovery freeze require`` fails before the authentic gate
      -> the authentic owner-capability gate
      -> ``discovery freeze require`` (safe count/ref; ``--content`` roster)
      -> later work keeps the frozen roster and refuses the old artifact
      -> an approved ``search_again`` shows status/history with no new freeze.

They deliberately **reuse** the existing test fixtures/helpers by composition
(``test_cli_discovery_artifacts`` document builders + rights record, and
``test_discovery_job_routes`` owner-auth and citation-provider helpers); they do
**not** re-run the individual single-command cases already covered by
``tests/test_cli_discovery_reports.py``, ``tests/test_cli_discovery_jobs.py`` or
``tests/test_cli_discovery_artifacts.py``, nor the scenario cases of the E0/E1
suites. Every fixture is synthetic; no test performs a network, provider, paid,
or upload call. No product, existing test, doc, or schema file is changed.

Run::

    PYTHONPATH=src python3 -m unittest tests.test_step05_cli_acceptance -v
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
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

import review_article_agent.cli.main as cli_main  # noqa: E402
from review_article_agent.discovery.candidates import (  # noqa: E402
    DEFAULT_CORPUS_ARTIFACT_ID,
    CandidateRepository,
)
from review_article_agent.discovery.freeze import (  # noqa: E402
    DEFAULT_FREEZE_ARTIFACT_ID,
    FREEZE_GATE_ID,
    FREEZE_GATE_KIND,
    INTEGRITY_FIELDS,
)
from review_article_agent.discovery.reporting import (  # noqa: E402
    DEFAULT_COVERAGE_ARTIFACT_ID,
)
from review_article_agent.discovery.transport import (  # noqa: E402
    TransportResponse,
)
from review_article_agent.runtime import ipc, owner_auth  # noqa: E402
from review_article_agent.runtime.gates import prepare_gate_snapshot  # noqa: E402
from review_article_agent.runtime.jobs import JobRepository  # noqa: E402
from review_article_agent.runtime.owner_auth import OwnerApproval  # noqa: E402
from review_article_agent.storage import (  # noqa: E402
    ArtifactRepository,
    ProjectStore,
    Registry,
    paths,
)
from review_article_agent.storage.human_approvals import (  # noqa: E402
    HumanApprovalRepository,
)
from review_article_agent.worker.engine import WorkerEngine  # noqa: E402

import test_cli_discovery_artifacts as arts  # noqa: E402
import test_discovery_job_routes as routes  # noqa: E402

# Composition, never a re-declaration: reuse the CLI1 document/rights builders.
_envelope = arts._envelope
_brief_payload = arts._brief_payload
_interview_payload = arts._interview_payload
_framing_payload = arts._framing_payload
_scope_payload = arts._scope_payload
_rubric_payload = arts._rubric_payload
_rights_record = arts._rights_record

_CHECKED_AT = "2026-10-29T00:00:00+00:00"
_SECOND_CORPUS = "discovery.corpus.2"
_ROOT_PMID = "1"


def _epmc_search_body(pmids):
    results = [
        {"source": "MED", "id": pmid, "pmid": pmid, "title": f"t{pmid}", "pubYear": "2020"}
        for pmid in pmids
    ]
    payload = {"hitCount": len(pmids), "resultList": {"result": results}}
    return json.dumps(payload).encode("utf-8")


def _plan_payload(refs, *, expansion=None):
    return {
        "protocol": "protocol-1",
        "brief_ref": refs["brief_ref"],
        "interview_ref": refs["interview_ref"],
        "seed_refs": list(refs.get("seed_refs", [])),
        "framing_ref": refs["framing_ref"],
        "scope_ref": refs["scope_ref"],
        "rubric_ref": refs["rubric_ref"],
        "queries": [
            {
                "source": "europepmc",
                "query": "asthma AND review",
                "page_size": 20,
                "max_pages": 1,
            }
        ],
        "complementary_sources": [
            {"source": "regulatory", "mode": "metadata_import", "rationale": "opt-in"}
        ],
        "expansion": expansion
        or {
            "max_hops": 1,
            "max_sources": 5,
            "max_requests": 10,
            "page_size": 20,
            "max_pages": 2,
        },
        "impact_review": {"previous_plan": None, "affected": [], "rationale": ""},
    }


class _CombinedTransport:
    """The one external GET stub: europepmc search pages + both-direction citations.

    A single callable passed as the worker engine's low-level discovery
    transport. Search URLs (no trailing citation relation) return a fixed
    europepmc page; ``.../<source>/<id>/references|citations`` URLs are served by
    the reused deterministic citation provider.
    """

    def __init__(self):
        self.citation = routes._CitationProvider(
            {
                ("MED", _ROOT_PMID, "references"): [
                    routes._CitationProvider.response(
                        routes._ref_payload(1, [routes._cite_entry("2")])
                    )
                ],
                ("MED", _ROOT_PMID, "citations"): [
                    routes._CitationProvider.response(
                        routes._cit_payload(1, [routes._cite_entry("3")])
                    )
                ],
                ("MED", "2", "references"): [
                    routes._CitationProvider.response(routes._ref_payload(0, []))
                ],
                ("MED", "2", "citations"): [
                    routes._CitationProvider.response(routes._cit_payload(0, []))
                ],
                ("MED", "3", "references"): [
                    routes._CitationProvider.response(routes._ref_payload(0, []))
                ],
                ("MED", "3", "citations"): [
                    routes._CitationProvider.response(routes._cit_payload(0, []))
                ],
            }
        )
        self.calls = []

    def __call__(self, url, *, timeout, max_response_bytes):
        self.calls.append(url)
        base = url.split("?")[0].rstrip("/")
        if base.endswith("/references") or base.endswith("/citations"):
            return self.citation(url, timeout=timeout, max_response_bytes=max_response_bytes)
        return TransportResponse(200, _epmc_search_body([_ROOT_PMID]))


class CliAcceptanceBase(unittest.TestCase):
    """Real CLI + registry/store + owner seam + an in-thread real worker."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-step05-cli-e2-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.state = self.tmp / "state"

        self.transport = _CombinedTransport()
        self._install_owner_auth()
        self._start_worker()
        self.addCleanup(self._stop_worker)

    # -- owner-auth seam (real owner capability, no in-band flag) ------------- #
    def _install_owner_auth(self) -> None:
        patches = [
            mock.patch.object(owner_auth, "_new_challenge", lambda: routes._FIXED_CHALLENGE),
            mock.patch.object(owner_auth, "_current_owner_uid", lambda: routes._OWNER_UID),
            mock.patch.object(owner_auth, "_real_uid", lambda: routes._OWNER_UID),
            mock.patch.object(owner_auth, "_utcnow", lambda: routes._BASE_TIME),
            mock.patch.object(owner_auth, "_monotonic", lambda: 1000.0),
            mock.patch.object(
                owner_auth,
                "_open_controlling_tty",
                lambda: routes._FakeTty(replies=[routes._FIXED_CHALLENGE]),
            ),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    # -- real worker (actual engine + real owner-only control socket) --------- #
    def _start_worker(self) -> None:
        self.engine = WorkerEngine(str(self.state), discovery_transport=self.transport)
        self.server = ipc.WorkerIPCServer(str(self.state), self.engine.handle)
        self.server.start()
        self._stop = threading.Event()
        self._worker_error: list = []

        def _loop():
            try:
                self.engine.reconcile_startup()
                while not self._stop.is_set():
                    try:
                        hint = self.engine.tick()
                    except Exception as exc:  # pragma: no cover - surfaced below
                        self._worker_error.append(exc)
                        return
                    if hint is None:
                        wait = 0.05
                    elif hint <= 0.0:
                        wait = 0.0
                    else:
                        wait = min(hint, 0.05)
                    try:
                        self.server.serve_once(wait)
                    except ipc.WorkerNotRunning:  # pragma: no cover - closing
                        return
            except Exception as exc:  # pragma: no cover - surfaced below
                self._worker_error.append(exc)

        self._thread = threading.Thread(target=_loop, name="raas-e2-worker", daemon=True)
        self._thread.start()

    def _stop_worker(self) -> None:
        try:
            self._stop.set()
            try:
                self.engine.close()
            finally:
                self.server.close()
            self._thread.join(10)
        except Exception:  # pragma: no cover - defensive cleanup
            pass

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
        return {
            "project_id": pid,
            "artifact_id": artifact_id,
            "revision_id": revision_id,
        }

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

    # -- real approvals ------------------------------------------------------ #
    def approve(self, pid, artifact_id, revision_id, gate_kind, gate_id, *, decision="approved"):
        def _approve(store):
            prepared = prepare_gate_snapshot(
                store,
                self.ref(pid, artifact_id, revision_id),
                gate_kind=gate_kind,
                gate_id=gate_id,
                decision=decision,
                rationale="synthetic integrated CLI acceptance gate",
            )
            capability = owner_auth.request_owner_confirmation(prepared.owner_action())
            assert isinstance(capability, OwnerApproval)
            return HumanApprovalRepository(store).record_decision(prepared, capability)

        return self.with_store(pid, _approve)

    # -- discovery CLI wrappers --------------------------------------------- #
    def report_create(
        self,
        *,
        revision_id,
        corpus_artifact,
        corpus_revision,
        plan_ref,
        coverage_artifact=DEFAULT_COVERAGE_ARTIFACT_ID,
        expansion_job_id=None,
        known_file=None,
        request_id=None,
        expect=0,
    ):
        args = [
            "discovery", "report", "create", "--project", self.pid,
            "--corpus-artifact-id", corpus_artifact,
            "--corpus-revision-id", corpus_revision,
            "--plan-artifact-id", plan_ref["artifact_id"],
            "--plan-revision-id", plan_ref["revision_id"],
            "--artifact-id", coverage_artifact,
            "--revision-id", revision_id,
            "--rights-file", str(self.write_json(
                f"cov-rights-{revision_id}.json",
                _rights_record(self.pid, coverage_artifact, revision_id))),
            "--request-id", request_id or f"cov-req-{revision_id}",
        ]
        if expansion_job_id is not None:
            args += ["--expansion-job-id", expansion_job_id]
        if known_file is not None:
            args += ["--known-relevant-file", str(known_file)]
        return self.cli(*args, expect=expect)

    def freeze_propose(
        self,
        *,
        corpus_artifact,
        corpus_revision,
        coverage_artifact,
        coverage_revision,
        revision_id,
        reviews,
        decision="freeze",
        request_id=None,
        accept_explanation="accepted: known-relevant gap reviewed",
        extra=(),
        expect=0,
    ):
        reviews_path = self.write_json(f"reviews-{revision_id}.json", reviews)
        args = [
            "discovery", "freeze", "propose", "--project", self.pid,
            "--corpus-artifact-id", corpus_artifact,
            "--corpus-revision-id", corpus_revision,
            "--coverage-artifact-id", coverage_artifact,
            "--coverage-revision-id", coverage_revision,
            "--decision", decision,
            "--entry-reviews-file", str(reviews_path),
            "--rationale", "human decision",
            "--request-id", request_id or f"frz-req-{revision_id}",
            "--revision-id", revision_id,
            "--rights-file", str(self.write_json(
                f"frz-rights-{revision_id}.json",
                _rights_record(self.pid, DEFAULT_FREEZE_ARTIFACT_ID, revision_id))),
        ]
        if accept_explanation is not None:
            args += ["--accept-coverage-limitations", accept_explanation]
        args += list(extra)
        return self.cli(*args, expect=expect)

    def context_import(self, *, pid, artifact_id, revision_id, document, expect=0):
        doc_path = self.write_json(f"ctx-{artifact_id}-{revision_id}.json", document)
        rights = _rights_record(pid, artifact_id, revision_id)
        rights_path = self.write_json(f"rights-{artifact_id}-{revision_id}.json", rights)
        return self.cli(
            "discovery", "context", "import", "--project", pid,
            "--artifact-id", artifact_id, "--revision-id", revision_id,
            "--file", str(doc_path), "--rights-file", str(rights_path),
            expect=expect,
        )

    def build_plan(self, pid, *, plan_artifact="a-plan", plan_revision="rev-plan"):
        self.context_import(
            pid=pid, artifact_id="a-brief", revision_id="rev-brief",
            document=_envelope("discovery_brief", _brief_payload()),
        )
        self.context_import(
            pid=pid, artifact_id="a-interview", revision_id="rev-interview",
            document=_envelope(
                "discovery_interview",
                _interview_payload(self.ref(pid, "a-brief", "rev-brief")),
            ),
        )
        for artifact_id, document_type, payload in (
            ("a-framing", "discovery_framing", _framing_payload()),
            ("a-scope", "discovery_scope", _scope_payload()),
            ("a-rubric", "discovery_rubric", _rubric_payload()),
        ):
            self.context_import(
                pid=pid, artifact_id=artifact_id, revision_id="rev-" + artifact_id[2:],
                document=_envelope(document_type, payload),
            )
        refs = {
            "brief_ref": self.ref(pid, "a-brief", "rev-brief"),
            "interview_ref": self.ref(pid, "a-interview", "rev-interview"),
            "seed_refs": [],
            "framing_ref": self.ref(pid, "a-framing", "rev-framing"),
            "scope_ref": self.ref(pid, "a-scope", "rev-scope"),
            "rubric_ref": self.ref(pid, "a-rubric", "rev-rubric"),
        }
        out = self.context_import(
            pid=pid, artifact_id=plan_artifact, revision_id=plan_revision,
            document=_envelope("discovery_plan", _plan_payload(refs)),
        )
        assert out["revision"]["document_type"] == "discovery_plan", out
        return self.ref(pid, plan_artifact, plan_revision)

    def corpus_ingest(self, *, revision_id, request_id, run_ids, pid=None,
                      artifact_id=None, expect=0):
        pid = pid or self.pid
        rights = _rights_record(pid, artifact_id or DEFAULT_CORPUS_ARTIFACT_ID, revision_id)
        rights_path = self.write_json(f"corpus-rights-{revision_id}.json", rights)
        args = [
            "discovery", "corpus", "ingest", "--project", pid,
            "--revision-id", revision_id, "--request-id", request_id,
            "--rights-file", str(rights_path),
        ]
        if artifact_id is not None:
            args += ["--artifact-id", artifact_id]
        for run_id in run_ids:
            args += ["--run-id", run_id]
        return self.cli(*args, expect=expect)

    # -- jobs ----------------------------------------------------------------- #
    def run_search_job(self, plan_ref, request_id):
        out = self.cli(
            "discovery", "search", "run", "--project", self.pid,
            "--artifact-id", plan_ref["artifact_id"],
            "--revision-id", plan_ref["revision_id"],
            "--query-index", "0", "--request-id", request_id,
        )
        job = self.wait_job(self.pid, out["job_id"])
        self.assertEqual(job["state"], "completed", job)
        _kind, observation = self.accepted_result(self.pid, out["job_id"])
        return out, job, observation["run_id"]

    def wait_job(self, pid, job_id, *, timeout=20.0):
        deadline = time.monotonic() + timeout
        state = None
        while time.monotonic() < deadline:
            job = self.cli("job", "status", job_id, "--project", pid)["job"]
            state = job["state"]
            if state in {"completed", "failed", "cancelled", "waiting_human"}:
                return job
            time.sleep(0.05)
        self.fail(f"job {job_id} did not settle (last state={state})")

    def accepted_result(self, pid, job_id):
        def _read(store):
            accepted = JobRepository(store).accepted_result(job_id)
            return None if accepted is None else (accepted.result_kind, accepted.result)

        return self.with_store(pid, _read)

    # -- entry reviews -------------------------------------------------------- #
    def snapshot(self, pid, artifact_id=DEFAULT_CORPUS_ARTIFACT_ID):
        return self.with_store(pid, lambda s: CandidateRepository(s).current(artifact_id))

    def reviews_for(self, snapshot, *, overrides=None, dispositions=None):
        overrides = overrides or {}
        dispositions = dispositions or {}
        out = []
        for doc in snapshot["candidates"]:
            cid = doc["candidate_id"]
            if cid in overrides:
                out.append(overrides[cid])
                continue
            resolved = doc["resolved"] and not doc.get("conflict_refs")
            disposition = dispositions.get(cid, "include" if resolved else "exclude")
            statuses = {} if resolved else {name: "unknown" for name in INTEGRITY_FIELDS}
            out.append(
                {
                    "candidate_id": cid,
                    "checked_at": _CHECKED_AT,
                    "provenance": {"ref": "integrity:syn", "source": "europepmc"},
                    **{name: statuses.get(name, "none") for name in INTEGRITY_FIELDS},
                    "disposition": disposition,
                    "rationale": "reviewed at entry",
                }
            )
        return out

    # -- the driven chain ---------------------------------------------------- #
    def drive_to_frozen(
        self,
        *,
        plan_ref=None,
        corpus_revision="corpus-1",
        coverage_revision="cov-1",
        freeze_revision="frz-1",
        freeze_request_id="frz-req-1",
        freeze_decision="freeze",
        accept_explanation="accepted: known-relevant gap reviewed",
        coverage_artifact=DEFAULT_COVERAGE_ARTIFACT_ID,
        approve=True,
        gate_id=FREEZE_GATE_ID,
        gate_kind=FREEZE_GATE_KIND,
        gate_decision="approved",
        known_tokens=None,
        extra_propose=(),
    ):
        """Drive the approved plan -> real jobs -> corpus -> report -> propose gate.

        Everything goes through the real CLI + the real in-thread worker; only the
        single external GET is stubbed. Returns every exact intermediate value so a
        caller can assert on it.
        """
        if plan_ref is None:
            plan_ref = self.build_plan(self.pid)
            self.approve(self.pid, plan_ref["artifact_id"], plan_ref["revision_id"],
                         "protocol", "discovery.plan")

        search = self.cli(
            "discovery", "search", "run", "--project", self.pid,
            "--artifact-id", plan_ref["artifact_id"],
            "--revision-id", plan_ref["revision_id"],
            "--query-index", "0", "--request-id", f"job-q-{coverage_revision}",
        )
        search_job = self.wait_job(self.pid, search["job_id"])
        self.assertEqual(search_job["state"], "completed", search_job)
        _kind, observation = self.accepted_result(self.pid, search["job_id"])
        run_id = observation["run_id"]

        corpus_rights = self.write_json(
            f"corpus-rights-{corpus_revision}.json",
            _rights_record(self.pid, DEFAULT_CORPUS_ARTIFACT_ID, corpus_revision),
        )
        self.cli(
            "discovery", "corpus", "ingest", "--project", self.pid,
            "--revision-id", corpus_revision, "--request-id", f"corpus-req-{corpus_revision}",
            "--rights-file", str(corpus_rights), "--run-id", run_id,
        )
        snapshot = self.snapshot(self.pid)
        self.assertEqual(snapshot["revision_id"], corpus_revision)

        roots = self.write_json(f"roots-{corpus_revision}.json",
                                [{"source": "MED", "id": _ROOT_PMID}])
        expand = self.cli(
            "discovery", "expand", "run", "--project", self.pid,
            "--artifact-id", plan_ref["artifact_id"],
            "--revision-id", plan_ref["revision_id"],
            "--corpus-artifact-id", snapshot["artifact_id"],
            "--corpus-revision-id", corpus_revision,
            "--roots-file", str(roots), "--request-id", f"job-exp-{corpus_revision}",
        )
        expand_job = self.wait_job(self.pid, expand["job_id"])
        self.assertEqual(expand_job["state"], "completed", expand_job)
        _exp_kind, expand_report = self.accepted_result(self.pid, expand_job["job_id"])

        if known_tokens is None:
            known_tokens = [
                {"namespace": "pmid", "value": _ROOT_PMID},
                {"namespace": "pmid", "value": "999999"},
            ]
        known = self.write_json(f"known-{coverage_revision}.json", known_tokens)
        report = self.report_create(
            revision_id=coverage_revision,
            corpus_artifact=snapshot["artifact_id"],
            corpus_revision=corpus_revision,
            plan_ref=plan_ref,
            coverage_artifact=coverage_artifact,
            expansion_job_id=expand_job["job_id"],
            known_file=known,
        )["report"]

        propose = self.freeze_propose(
            corpus_artifact=snapshot["artifact_id"],
            corpus_revision=corpus_revision,
            coverage_artifact=coverage_artifact,
            coverage_revision=coverage_revision,
            revision_id=freeze_revision,
            reviews=self.reviews_for(snapshot),
            decision=freeze_decision,
            request_id=freeze_request_id,
            accept_explanation=accept_explanation,
            extra=extra_propose,
        )

        # Before the authentic gate, require fails cleanly on BOTH output paths.
        self.cli("discovery", "freeze", "require", "--project", self.pid, expect=2)
        self.cli("discovery", "freeze", "require", "--project", self.pid,
                 "--content", expect=2)

        if approve:
            self.approve(self.pid, DEFAULT_FREEZE_ARTIFACT_ID, freeze_revision,
                         gate_kind, gate_id, decision=gate_decision)

        expected = sorted(
            c["candidate_id"] for c in snapshot["candidates"]
            if c["resolved"] and not c.get("conflict_refs")
        )
        return {
            "plan_ref": plan_ref,
            "run_id": run_id,
            "search_job_id": search["job_id"],
            "corpus_revision": corpus_revision,
            "corpus_artifact": snapshot["artifact_id"],
            "snapshot": snapshot,
            "expand_job_id": expand_job["job_id"],
            "expand_report": expand_report,
            "report": report,
            "propose": propose,
            "freeze_revision": freeze_revision,
            "expected_roster": expected,
        }


class FullChainTest(CliAcceptanceBase):
    """The whole chain, driven only through the real CLI."""

    @slow
    def test_end_to_end_cli_chain_freezes_exact_roster(self):
        self.pid = self.create_project()
        c = self.drive_to_frozen()

        # The expansion report came from a REAL completed job and never claims
        # completeness/verified support.
        self.assertIn("format", c["expand_report"])
        self.assertEqual(
            c["expand_report"]["guarantees"],
            {"completeness_claimed": False, "verified_support": False},
        )

        report = c["report"]
        self.assertEqual(report["artifact_id"], DEFAULT_COVERAGE_ARTIFACT_ID)
        self.assertEqual(report["revision_id"], "cov-1")
        self.assertEqual(report["document_type"], "discovery_coverage")
        self.assertFalse(report["authorizing"])
        self.assertFalse(report["completeness_claimed"])
        # Expansion resolved from the real job: both directions, pinned.
        self.assertTrue(report["citations"]["expansion_performed"])
        self.assertEqual(report["citations"]["directions"], ["citations", "references"])
        self.assertFalse(report["gaps"]["expansion_not_performed"])
        # Known-relevant: one present, one missing -> explicit gap.
        self.assertEqual(report["known_relevant"]["status"], "assessed")
        self.assertEqual(report["known_relevant"]["matched"], 1)
        self.assertEqual(report["known_relevant"]["missing"], 1)
        self.assertEqual(report["gaps"]["known_relevant_missing"], 1)

        # The propose is inert and returns the exact named-gate request.
        request = c["propose"]["approval_request"]
        self.assertFalse(c["propose"]["replayed"])
        self.assertEqual(request["gate_kind"], "artifact_revision")
        self.assertEqual(request["gate_id"], "discovery.freeze")
        self.assertFalse(request["satisfied"])
        self.assertTrue(request["current"])
        self.assertFalse(c["propose"]["decision"]["authorizing"])

        # report show: safe summary; --content the private pinned document.
        summary = self.cli(
            "discovery", "report", "show", "--project", self.pid,
        )["report"]
        self.assertNotIn("payload", summary)
        content = self.cli(
            "discovery", "report", "show", "--project", self.pid, "--content",
        )
        self.assertTrue(content["private"])
        self.assertTrue(content["non_authorizing"])
        self.assertFalse(content["authorizes_execution"])
        doc = content["document"]
        self.assertEqual(doc["document_type"], "discovery_coverage")
        self.assertTrue(doc["payload"]["citations"]["expansion_performed"])
        self.assertFalse(doc["payload"]["completeness_claimed"])
        self.assertFalse(doc["payload"]["guarantees"]["literature_complete"])
        # The exact pinned corpus/plan refs are shown on both output paths.
        self.assertEqual(summary["corpus"]["ref"],
                         self.ref(self.pid, c["corpus_artifact"], "corpus-1"))
        self.assertEqual(summary["plan"]["ref"], c["plan_ref"])
        self.assertEqual(doc["payload"]["corpus"]["ref"],
                         self.ref(self.pid, c["corpus_artifact"], "corpus-1"))
        self.assertEqual(doc["payload"]["plan"]["ref"], c["plan_ref"])
        self.assertEqual(len(doc["payload"]["citations"]["reports"]), 1)

        # A real job of the WRONG type (a query) and a fabricated job id are both
        # refused: only a real typed completed expansion report may be pinned.
        self.report_create(
            revision_id="cov-forged-type",
            corpus_artifact=c["corpus_artifact"],
            corpus_revision="corpus-1",
            plan_ref=c["plan_ref"],
            expansion_job_id=c["search_job_id"],
            expect=2,
        )
        self.report_create(
            revision_id="cov-forged-none",
            corpus_artifact=c["corpus_artifact"],
            corpus_revision="corpus-1",
            plan_ref=c["plan_ref"],
            expansion_job_id="job_does_not_exist",
            expect=2,
        )
        self.assertIsNone(
            self.with_store(
                self.pid,
                lambda s: ArtifactRepository(s).get_revision(
                    DEFAULT_COVERAGE_ARTIFACT_ID, "cov-forged-none"),
            )
        )

        # require: safe count/ref; no candidate ids by default.
        require = self.cli("discovery", "freeze", "require", "--project", self.pid)
        expected = c["expected_roster"]
        self.assertTrue(require["frozen"])
        self.assertEqual(require["included_count"], len(expected))
        self.assertEqual(require["corpus_ref"],
                         self.ref(self.pid, c["corpus_artifact"], "corpus-1"))
        self.assertNotIn("included_candidate_ids", require)
        self.assertNotIn("document", require)
        blob = json.dumps(require)
        for cid in expected:
            self.assertNotIn(cid, blob)

        # require --content: the exact roster + private flags.
        entry_content = self.cli(
            "discovery", "freeze", "require", "--project", self.pid, "--content",
        )
        self.assertTrue(entry_content["private"])
        self.assertTrue(entry_content["non_authorizing"])
        self.assertFalse(entry_content["authorizes_execution"])
        self.assertEqual(sorted(entry_content["included_candidate_ids"]), expected)
        self.assertIn("entry_reviews", entry_content["document"]["payload"])


@slow
class LaterWorkTest(CliAcceptanceBase):
    """Later work with different artifact ids never mutates the frozen roster."""

    def test_later_search_new_corpus_and_reports_keep_old_freeze(self):
        self.pid = self.create_project()
        c = self.drive_to_frozen()
        old_ids = c["expected_roster"]

        # The freeze is active.
        before = self.cli("discovery", "freeze", "require", "--project", self.pid, "--content")
        self.assertEqual(sorted(before["included_candidate_ids"]), old_ids)

        # A later observation still records (never blocked by the freeze).
        _out, _job, later_run = self.run_search_job(c["plan_ref"], "job-q-later")

        # A NEW corpus artifact reconciles freely.
        self.corpus_ingest(
            revision_id="corpus-2", artifact_id=_SECOND_CORPUS,
            request_id="corpus-req-2", run_ids=[later_run],
        )
        # But appending to the ORIGINAL frozen corpus artifact is refused.
        self.corpus_ingest(
            revision_id="corpus-1b", request_id="append-original",
            run_ids=[later_run], expect=2,
        )

        # A new coverage report on a DIFFERENT coverage artifact keeps the freeze.
        self.report_create(
            revision_id="cov-2",
            corpus_artifact=_SECOND_CORPUS,
            corpus_revision="corpus-2",
            plan_ref=c["plan_ref"],
            coverage_artifact="discovery.coverage.2",
        )

        # The freeze still authorizes exactly the original roster.
        after = self.cli("discovery", "freeze", "require", "--project", self.pid, "--content")
        self.assertEqual(sorted(after["included_candidate_ids"]), old_ids)
        # The original frozen snapshot is unchanged.
        old = self.snapshot(self.pid)
        self.assertEqual(
            sorted(x["candidate_id"] for x in old["candidates"]),
            sorted(x["candidate_id"] for x in c["snapshot"]["candidates"]),
        )
        self.assertEqual(old["revision_id"], "corpus-1")


@slow
class SearchAgainTest(CliAcceptanceBase):
    """An approved search_again keeps history; a proposal alone never reopens."""

    def test_approved_search_again_keeps_history_and_no_new_freeze(self):
        self.pid = self.create_project()
        c = self.drive_to_frozen()
        old_ids = c["expected_roster"]

        # A raw (unapproved) search-again proposal is inert.
        self.freeze_propose(
            corpus_artifact=c["corpus_artifact"],
            corpus_revision=c["corpus_revision"],
            coverage_artifact=DEFAULT_COVERAGE_ARTIFACT_ID,
            coverage_revision=c["report"]["revision_id"],
            revision_id="frz-sa-2",
            reviews=self.reviews_for(c["snapshot"]),
            decision="search_again",
            accept_explanation=None,
        )
        self.cli("discovery", "freeze", "require", "--project", self.pid, expect=2)
        status = self.cli("discovery", "freeze", "status", "--project", self.pid)
        self.assertIsNone(status["current_decision"])
        self.assertEqual(status["last_frozen_corpus"]["included_count"], len(old_ids))

        # The authentic named gate reopens it; the old membership is retained.
        self.approve(self.pid, DEFAULT_FREEZE_ARTIFACT_ID, "frz-sa-2",
                     FREEZE_GATE_KIND, FREEZE_GATE_ID)
        self.cli("discovery", "freeze", "require", "--project", self.pid, expect=2)
        self.cli("discovery", "freeze", "require", "--project", self.pid,
                 "--content", expect=2)
        status = self.cli("discovery", "freeze", "status", "--project", self.pid)
        self.assertEqual(status["current_decision"]["decision"], "search_again")
        self.assertEqual(status["last_frozen_corpus"]["included_count"], len(old_ids))
        self.assertFalse(status["last_frozen_corpus"]["frozen"])
        history = self.cli("discovery", "freeze", "history", "--project", self.pid)
        self.assertEqual(
            [d["decision"] for d in history["decisions"]], ["search_again", "freeze"]
        )


@slow
class NegativeGateTest(CliAcceptanceBase):
    """Stale / rejected / wrong-gate approvals never authorize a freeze."""

    def test_stale_after_new_coverage_head_never_authorizes(self):
        self.pid = self.create_project()
        c = self.drive_to_frozen()
        # A later coverage revision on the SAME coverage artifact moves the head.
        self.report_create(
            revision_id="cov-stale",
            corpus_artifact=c["corpus_artifact"],
            corpus_revision=c["corpus_revision"],
            plan_ref=c["plan_ref"],
        )
        self.cli("discovery", "freeze", "require", "--project", self.pid, expect=2)
        status = self.cli("discovery", "freeze", "status", "--project", self.pid)
        self.assertIsNone(status["current_decision"])
        self.assertIsNotNone(status["last_frozen_corpus"])
        self.assertFalse(status["last_frozen_corpus"]["frozen"])
        self.assertEqual(status["last_frozen_corpus"]["approval"]["status"], "stale")

    def test_rejected_decision_never_authorizes(self):
        self.pid = self.create_project()
        c = self.drive_to_frozen(gate_decision="rejected")
        self.cli("discovery", "freeze", "require", "--project", self.pid, expect=2)
        status = self.cli("discovery", "freeze", "status", "--project", self.pid)
        self.assertIsNone(status["current_decision"])

    def test_generic_wrong_gate_never_authorizes(self):
        self.pid = self.create_project()
        self.drive_to_frozen(gate_id="tool.allowed")
        self.cli("discovery", "freeze", "require", "--project", self.pid, expect=2)
        status = self.cli("discovery", "freeze", "status", "--project", self.pid)
        self.assertIsNone(status["current_decision"])


@slow
class FakeInstructionAndPrivacyTest(CliAcceptanceBase):
    """A fake instruction payload cannot approve; replay makes no new external call."""

    def test_fake_payload_is_refused_and_replay_makes_no_external_call(self):
        self.pid = self.create_project()
        c = self.drive_to_frozen()

        # A basic fake "approved" instruction on a review is refused outright.
        reviews = self.reviews_for(c["snapshot"])
        injected = [dict(reviews[0], approved=True)] + reviews[1:]
        self.freeze_propose(
            corpus_artifact=c["corpus_artifact"],
            corpus_revision=c["corpus_revision"],
            coverage_artifact=DEFAULT_COVERAGE_ARTIFACT_ID,
            coverage_revision=c["report"]["revision_id"],
            revision_id="frz-inject",
            reviews=injected,
            expect=2,
        )
        self.assertIsNone(
            self.with_store(
                self.pid,
                lambda s: ArtifactRepository(s).get_revision(
                    DEFAULT_FREEZE_ARTIFACT_ID, "frz-inject"),
            )
        )
        # An in-band approval flag is not even a valid command surface.
        self.freeze_propose(
            corpus_artifact=c["corpus_artifact"],
            corpus_revision=c["corpus_revision"],
            coverage_artifact=DEFAULT_COVERAGE_ARTIFACT_ID,
            coverage_revision=c["report"]["revision_id"],
            revision_id="frz-flag",
            reviews=reviews,
            extra=("--authorize", "true"),
            expect=2,
        )

        # -- replay makes no new external provider call --------------------- #
        # First advance the observable "current work" with a fresh search job.
        self.run_search_job(c["plan_ref"], "job-q-later-replay")
        before_calls = len(self.transport.calls)

        # 1) Job replay (same request id) returns the SAME durable job.
        out, _job, _run = self.run_search_job(
            c["plan_ref"], f"job-q-{c['report']['revision_id']}")
        self.assertEqual(out["job_id"], c["search_job_id"])
        self.assertEqual(len(self.transport.calls), before_calls)

        # 2) CLI report replay is historical (same revision/digest), no new call.
        known = self.write_json(
            f"known-{c['report']['revision_id']}.json",
            [{"namespace": "pmid", "value": _ROOT_PMID},
             {"namespace": "pmid", "value": "999999"}],
        )
        replay = self.report_create(
            revision_id=c["report"]["revision_id"],
            corpus_artifact=c["corpus_artifact"],
            corpus_revision=c["corpus_revision"],
            plan_ref=c["plan_ref"],
            expansion_job_id=c["expand_job_id"],
            known_file=known,
            request_id=f"cov-req-{c['report']['revision_id']}",
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(
            replay["report"]["content_sha256"], c["report"]["content_sha256"])
        self.assertEqual(len(self.transport.calls), before_calls)

        # -- output / error privacy ----------------------------------------- #
        # The raw approved query text and the data-dir path never leak.
        self.assertNotIn("asthma", json.dumps(out))
        self.assertNotIn(str(self.tmp), json.dumps(out))
        # A clean typed error, never a traceback or a leaked path.
        code, _out, err = self._run(
            "discovery", "expand", "show", "--project", self.pid,
            "--request-id", "no-such-request",
        )
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", err)
        self.assertNotIn(str(self.tmp), err)
        self.assertEqual(json.loads(err)["error"]["category"], "validation")


class NoForcedCompletenessTest(CliAcceptanceBase):
    """All known-relevant tokens present still never forces a complete claim."""

    @slow
    def test_all_known_present_still_never_claims_completeness(self):
        self.pid = self.create_project()
        c = self.drive_to_frozen(
            known_tokens=[{"namespace": "pmid", "value": _ROOT_PMID}]
        )
        report = c["report"]
        self.assertEqual(report["known_relevant"]["status"], "assessed")
        self.assertEqual(report["known_relevant"]["matched"], 1)
        self.assertEqual(report["known_relevant"]["missing"], 0)
        self.assertEqual(report["gaps"]["known_relevant_missing"], 0)
        # No scientific completeness threshold is ever applied or claimed.
        self.assertFalse(report["completeness_claimed"])
        self.assertFalse(report["authorizing"])
        content = self.cli(
            "discovery", "freeze", "require", "--project", self.pid, "--content")
        self.assertFalse(content["document"]["payload"]["completeness_claimed"])
        self.assertFalse(content["document"]["payload"]["authorizing"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
