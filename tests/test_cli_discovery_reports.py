"""In-process CLI acceptance tests for the discovery **report/freeze** addon (CLI1B).

Covers ``src/review_article_agent/cli/discovery_reports.py`` against the Step 05
reporting/freeze surface (deliverable 3's coverage report + explicit freeze /
search-again decision, deliverable 4's project work/cost reporting, AC5 "reporting
never claims absolute completeness", and AC6 "human approval freezes a specific
corpus revision"):

* ``discovery report create`` records one immutable coverage report from exact
  corpus/plan refs (with real completed expansion jobs and strict token/limitation
  files), returns a **safe summary** (counts/gaps/work/monetary -- never a raw
  query/identifier/source), replays an identical request key historically, and
  never mutates an old report as later work grows;
* a forged/non-existent ``--expansion-job-id`` and any malformed file/ref/rights
  perform **no unsafe write**;
* ``discovery report show`` is a safe summary by default and the private validated
  document only with ``--content``;
* ``discovery freeze propose`` records an **inert** proposal, returns the exact
  named-gate ``approval_request`` (fixed ``artifact_revision``/``discovery.freeze``)
  and out-of-band guidance, and never confirms;
* an unapproved / wrong-gate / rejected / stale / search-again decision never
  authorizes a freeze; the exact real gate yields ``freeze require`` an active
  exact roster;
* no ``--yes``/``--approve``/actor flag exists and no in-band grant is possible.

Every case drives the **real CLI entry point** in-process over a **real temporary
registry and store**; the controlling-terminal owner-auth seams are mocked exactly
as the other human-gate tests do (external I/O only). No case performs a network,
provider, or model call.

Run::

    PYTHONPATH=src python3 -m unittest tests.test_cli_discovery_reports
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
TESTS = Path(__file__).resolve().parent
for _path in (str(SRC), str(TESTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import review_article_agent.cli.main as cli_main  # noqa: E402
from review_article_agent.discovery import context as ctx  # noqa: E402
from review_article_agent.discovery.candidates import (  # noqa: E402
    DEFAULT_CORPUS_ARTIFACT_ID,
    CandidateRepository,
)
from review_article_agent.discovery.freeze import (  # noqa: E402
    DEFAULT_FREEZE_ARTIFACT_ID,
    FREEZE_GATE_ID,
    FREEZE_GATE_KIND,
    FreezeRepository,
)
from review_article_agent.discovery.reporting import (  # noqa: E402
    DEFAULT_COVERAGE_ARTIFACT_ID,
    CoverageRepository,
)
from review_article_agent.runtime import owner_auth  # noqa: E402
from review_article_agent.runtime.gates import prepare_gate_snapshot  # noqa: E402
from review_article_agent.runtime.owner_auth import OwnerApproval  # noqa: E402
from review_article_agent.storage import (  # noqa: E402
    ArtifactRepository,
    ProjectStore,
    Registry,
    paths,
)
from review_article_agent.storage.discovery import DiscoveryRepository  # noqa: E402
from review_article_agent.storage.human_approvals import (  # noqa: E402
    HumanApprovalRepository,
)

import test_discovery_serialization as fx  # noqa: E402

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")
_FIXED_CHALLENGE = "TEST-CHAL-CLI1B"
_OWNER_UID = 1000
_CHECKED_AT = "2026-10-29T00:00:00+00:00"


def _rights(project_id, artifact_id, revision_id, **overrides):
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
        "recorded_at": "2026-10-29T00:00:00Z",
        "derived_status": "valid",
    }
    record.update(overrides)
    return record


def _envelope(document_type, payload):
    return {"schema_version": 1, "document_type": document_type, "payload": payload}


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


class ReportsCliBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-cli-reports-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.state = self.tmp / "state"
        self.project_id = self._create_project()
        info = Registry(str(self.state)).get(self.project_id)
        self.app_dir = paths.app_dir_for(info.root_path)
        self.store = ProjectStore.open_at(
            self.app_dir, expected_project_id=self.project_id
        )
        self.addCleanup(self._close_store)
        self._bind(self.store)
        self._install_owner_auth()

    # -- harness ------------------------------------------------------------- #
    def _close_store(self) -> None:
        try:
            self.store.close()
        except Exception:  # pragma: no cover
            pass

    def _bind(self, store) -> None:
        self.artifacts = ArtifactRepository(store)
        self.discovery = DiscoveryRepository(store)
        self.corpus = CandidateRepository(store)
        self.coverage = CoverageRepository(store)
        self.freeze = FreezeRepository(store)

    def _install_owner_auth(self) -> None:
        patches = [
            mock.patch.object(owner_auth, "_new_challenge", lambda: _FIXED_CHALLENGE),
            mock.patch.object(owner_auth, "_current_owner_uid", lambda: _OWNER_UID),
            mock.patch.object(owner_auth, "_real_uid", lambda: _OWNER_UID),
            mock.patch.object(owner_auth, "_monotonic", lambda: 1000.0),
            mock.patch.object(owner_auth, "_utcnow", lambda: fx_utcnow()),
            mock.patch.object(
                owner_auth, "_open_controlling_tty",
                lambda: _FakeTty(replies=[_FIXED_CHALLENGE]),
            ),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

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

    def _create_project(self) -> str:
        out, err = io.StringIO(), io.StringIO()
        code = cli_main.main(
            ["--data-dir", str(self.state), "create", str(self.tmp / "proj"),
             "--name", "proj"],
            env={}, stdout=out, stderr=err,
        )
        assert code == 0, err.getvalue()
        return json.loads(out.getvalue())["project"]["project_id"]

    def write_json(self, name: str, data: object) -> Path:
        path = self.tmp / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def ref(self, artifact_id, revision_id) -> dict:
        return {
            "project_id": self.project_id,
            "artifact_id": artifact_id,
            "revision_id": revision_id,
        }

    def rights(self, artifact_id, revision_id, **overrides) -> dict:
        return _rights(self.project_id, artifact_id, revision_id, **overrides)

    # -- fixtures ------------------------------------------------------------ #
    @staticmethod
    def rec(**overrides):
        kwargs = {"pmcid": None, "doi": None}
        kwargs.update(overrides)
        return fx.record(**kwargs)

    def pubmed_run(self, records, **overrides):
        return fx.pubmed_result(pages=(fx.page(records=tuple(records)),), **overrides)

    def observe(self, request_id, result):
        return self.discovery.record_search(request_id=request_id, result=result)

    def _put(self, artifact_id, kind, revision_id, document, *, dependencies=(), parent=None):
        self.artifacts.create_artifact(artifact_id, kind=kind)
        return self.artifacts.import_revision(
            artifact_id=artifact_id,
            content=document,
            origin_locator="import:synthetic",
            rights=self.rights(artifact_id, revision_id),
            revision_id=revision_id,
            dependencies=list(dependencies),
            parent_revision_id=parent,
        )

    def approve(self, artifact_id, revision_id, gate_kind, gate_id, *, decision="approved"):
        prepared = prepare_gate_snapshot(
            self.store, self.ref(artifact_id, revision_id),
            gate_kind=gate_kind, gate_id=gate_id,
            decision=decision, rationale="synthetic human gate",
        )
        capability = owner_auth.request_owner_confirmation(prepared.owner_action())
        assert isinstance(capability, OwnerApproval)
        return HumanApprovalRepository(self.store).record_decision(prepared, capability)

    def build_plan(self, *, approve=True):
        records = (self.rec(pmid="999", source_id="seed0"),)
        run = self.observe("seed-run", self.pubmed_run(records))
        brief_ref = self.ref("a-brief", "rev-brief")
        self._put(
            "a-brief", "report", "rev-brief",
            _envelope("discovery_brief", {"objective": "o", "starting_hypothesis": "h", "questions": []}),
        )
        self._put(
            "a-interview", "report", "rev-interview",
            _envelope("discovery_interview", {
                "brief_ref": brief_ref,
                "turns": [{"question": "q", "answer": "a"}],
                "ready_to_advance": True,
            }),
            dependencies=[brief_ref],
        )
        seed_ref = self.ref("a-seed0", "rev-seed0")
        self._put(
            "a-seed0", "document", "rev-seed0",
            _envelope("discovery_seed", {
                "run_id": run.run_id, "record_index": 0, "abstract": None,
                "abstract_context": "provisional", "limitations": ["synthetic"],
            }),
        )
        self._put(
            "a-framing", "framework", "rev-framing",
            _envelope("discovery_framing", {"alternatives": ["alt"], "counterevidence_queries": ["q"], "limitations": []}),
        )
        self._put(
            "a-scope", "framework", "rev-scope",
            _envelope("discovery_scope", {
                "scope": "s", "date_boundary": "none", "language_boundary": "english",
                "source_boundary": ["none"], "intended_synthesis": "syn",
                "terminology": [], "exclusions": [],
            }),
        )
        self._put(
            "a-rubric", "rubric", "rev-rubric",
            _envelope("discovery_rubric", {"eligibility_rules": ["r"], "uncertain_action": "retain", "limitations": []}),
        )
        deps = [
            brief_ref, self.ref("a-interview", "rev-interview"), seed_ref,
            self.ref("a-framing", "rev-framing"), self.ref("a-scope", "rev-scope"),
            self.ref("a-rubric", "rev-rubric"),
        ]
        self._put(
            "a-plan", "report", "rev-plan",
            _envelope("discovery_plan", {
                "protocol": "protocol",
                "brief_ref": brief_ref,
                "interview_ref": self.ref("a-interview", "rev-interview"),
                "seed_refs": [seed_ref],
                "framing_ref": self.ref("a-framing", "rev-framing"),
                "scope_ref": self.ref("a-scope", "rev-scope"),
                "rubric_ref": self.ref("a-rubric", "rev-rubric"),
                "queries": [
                    {"source": "pubmed", "query": "SENSITIVE-QUERY-DO-NOT-ECHO",
                     "page_size": 20, "max_pages": 2}
                ],
                "complementary_sources": [],
                "expansion": {"max_hops": 1, "max_sources": 5, "max_requests": 10, "page_size": 20, "max_pages": 3},
                "impact_review": {"previous_plan": None, "affected": [], "rationale": ""},
            }),
            dependencies=deps,
        )
        if approve:
            self.approve("a-seed0", "rev-seed0", ctx.ARTIFACT_REVISION_GATE_KIND, ctx.SEED_GATE_PREFIX + "a-seed0")
            self.approve("a-plan", "rev-plan", ctx.PROTOCOL_GATE_KIND, ctx.PLAN_GATE_ID)
        return self.ref("a-plan", "rev-plan")

    def build_corpus(self, *, request_id="corpus-1", artifact_id=DEFAULT_CORPUS_ARTIFACT_ID):
        a = self.observe("req-a", self.pubmed_run((self.rec(pmid="111", source_id="a"),)))
        b = self.observe(
            "req-b",
            self.pubmed_run((
                self.rec(pmid="222", source_id="b"),
                self.rec(pmid=None, source_id=None, title="no ids at all"),
            )),
        )
        rid = CandidateRepository.new_revision_id()
        rev = self.corpus.ingest(
            [a.run_id, b.run_id], request_id=request_id,
            rights=self.rights(artifact_id, rid), revision_id=rid,
            artifact_id=artifact_id,
        )
        return self.ref(rev.artifact_id, rev.revision_id), self.corpus.current(artifact_id)

    def record_coverage(self, corpus_ref, plan_ref, *, request_id="cov-1", **kwargs):
        rid = CoverageRepository.new_revision_id()
        rev = self.coverage.record_report(
            corpus_ref, plan_ref=plan_ref, request_id=request_id,
            rights=self.rights(DEFAULT_COVERAGE_ARTIFACT_ID, rid),
            revision_id=rid, **kwargs,
        )
        return self.ref(rev.artifact_id, rev.revision_id)

    def make_review(self, candidate_id, *, disposition="include", statuses=None,
                    rationale="reviewed at entry", checked_at=_CHECKED_AT):
        from review_article_agent.discovery.freeze import INTEGRITY_FIELDS
        st = {name: "none" for name in INTEGRITY_FIELDS}
        if statuses:
            st.update(statuses)
        return {
            "candidate_id": candidate_id,
            "checked_at": checked_at,
            "provenance": {"ref": "integrity:syn", "source": "pubmed"},
            **st,
            "disposition": disposition,
            "rationale": rationale,
        }

    def reviews_for(self, snapshot, *, overrides=None):
        overrides = overrides or {}
        out = []
        for doc in snapshot["candidates"]:
            cid = doc["candidate_id"]
            if cid in overrides:
                out.append(overrides[cid])
                continue
            if doc["resolved"]:
                out.append(self.make_review(cid, disposition="include"))
            else:
                out.append(self.make_review(
                    cid, disposition="exclude",
                    statuses={name: "unknown" for name in ("retraction", "expression_of_concern", "correction", "non_replication")},
                ))
        return out


def fx_utcnow():
    from datetime import datetime, timezone
    return datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# report create / show
# --------------------------------------------------------------------------- #
class ReportCreateTest(ReportsCliBase):
    def _args(self, corpus_ref, plan_ref, *, rev="cov-cli-1", request_id="cov-req-1", **extra):
        rights_path = self.write_json(
            f"rights-{rev}.json", self.rights(DEFAULT_COVERAGE_ARTIFACT_ID, rev)
        )
        args = [
            "discovery", "report", "create", "--project", self.project_id,
            "--corpus-artifact-id", corpus_ref["artifact_id"],
            "--corpus-revision-id", corpus_ref["revision_id"],
            "--plan-artifact-id", plan_ref["artifact_id"],
            "--plan-revision-id", plan_ref["revision_id"],
            "--revision-id", rev, "--rights-file", str(rights_path),
            "--request-id", request_id,
        ]
        return args

    def test_create_returns_safe_summary_and_records_report(self):
        corpus_ref, _snapshot = self.build_corpus()
        plan_ref = self.build_plan()
        out = self.cli(*self._args(corpus_ref, plan_ref))
        report = out["report"]
        self.assertEqual(report["artifact_id"], DEFAULT_COVERAGE_ARTIFACT_ID)
        self.assertEqual(report["revision_id"], "cov-cli-1")
        self.assertEqual(report["document_type"], "discovery_coverage")
        self.assertTrue(report["content_sha256"])
        self.assertEqual(report["corpus"]["counts"]["candidates"], 3)
        self.assertFalse(report["authorizing"])
        self.assertFalse(report["completeness_claimed"])
        # Safe summary: no raw query/source/identifier leaks through.
        blob = json.dumps(out)
        self.assertNotIn("SENSITIVE-QUERY-DO-NOT-ECHO", blob)
        self.assertNotIn("cancer", blob)
        # Gap flags are explicit (no expansion supplied, known set not assessed).
        self.assertTrue(report["gaps"]["expansion_not_performed"])
        self.assertTrue(report["gaps"]["known_relevant_not_assessed"])
        # Work + monetary (known/unknown cost) summary present.
        self.assertIn("work", report)
        self.assertIn("monetary", report)
        self.assertIn("unknown_reserve_usd", report["monetary"]["routes"]["totals"])

    def test_create_replays_identically_and_never_mutates_old_report(self):
        corpus_ref, _snapshot = self.build_corpus()
        plan_ref = self.build_plan()
        first = self.cli(*self._args(corpus_ref, plan_ref))["report"]
        # Later work grows the cumulative project totals.
        self.observe("req-later", self.pubmed_run((self.rec(pmid="444", source_id="d"),)))
        replay = self.cli(*self._args(corpus_ref, plan_ref))
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["report"]["revision_id"], first["revision_id"])
        self.assertEqual(replay["report"]["content_sha256"], first["content_sha256"])
        # The pinned old report is unchanged even after later observations.
        stored = self.coverage.get(
            {"project_id": self.project_id, "artifact_id": DEFAULT_COVERAGE_ARTIFACT_ID,
             "revision_id": first["revision_id"]},
        )
        self.assertEqual(stored["content_sha256"], first["content_sha256"])

    def test_known_relevant_missing_is_an_explicit_gap(self):
        corpus_ref, _snapshot = self.build_corpus()
        plan_ref = self.build_plan()
        tokens = self.write_json(
            "known.json",
            [{"namespace": "pmid", "value": "111"}, {"namespace": "pmid", "value": "NOT-PRESENT"}],
        )
        args = self._args(corpus_ref, plan_ref, rev="cov-kn-1", request_id="cov-kn-req")
        args += ["--known-relevant-file", str(tokens)]
        report = self.cli(*args)["report"]
        self.assertEqual(report["known_relevant"]["status"], "assessed")
        self.assertEqual(report["known_relevant"]["missing"], 1)
        self.assertFalse(report["gaps"]["known_relevant_not_assessed"])

    def test_forged_expansion_job_ref_is_refused_with_no_write(self):
        corpus_ref, _snapshot = self.build_corpus()
        plan_ref = self.build_plan()
        args = self._args(corpus_ref, plan_ref, rev="cov-forge", request_id="cov-forge-req")
        args += ["--expansion-job-id", "job_does_not_exist"]
        self.cli(*args, expect=2)
        self.assertIsNone(self.artifacts.get_artifact(DEFAULT_COVERAGE_ARTIFACT_ID))

    def test_bad_rights_or_ref_writes_nothing(self):
        corpus_ref, _snapshot = self.build_corpus()
        plan_ref = self.build_plan()
        # Bad rights: subject bound to a different revision.
        bad_rights = self.write_json(
            "bad-rights.json", self.rights(DEFAULT_COVERAGE_ARTIFACT_ID, "other-rev")
        )
        args = [
            "discovery", "report", "create", "--project", self.project_id,
            "--corpus-artifact-id", corpus_ref["artifact_id"],
            "--corpus-revision-id", corpus_ref["revision_id"],
            "--plan-artifact-id", plan_ref["artifact_id"],
            "--plan-revision-id", plan_ref["revision_id"],
            "--revision-id", "cov-bad", "--rights-file", str(bad_rights),
            "--request-id", "cov-bad-req",
        ]
        self.cli(*args, expect=2)
        self.assertIsNone(self.artifacts.get_artifact(DEFAULT_COVERAGE_ARTIFACT_ID))
        # Missing corpus revision.
        args2 = self._args(corpus_ref, plan_ref, rev="cov-bad2", request_id="cov-bad2-req")
        args2[args2.index("--corpus-revision-id") + 1] = "rev_missing"
        self.cli(*args2, expect=2)

    def test_malformed_known_file_is_refused(self):
        corpus_ref, _snapshot = self.build_corpus()
        plan_ref = self.build_plan()
        bad = self.write_json("bad-known.json", {"not": "a list"})
        args = self._args(corpus_ref, plan_ref, rev="cov-bk", request_id="cov-bk-req")
        args += ["--known-relevant-file", str(bad)]
        self.cli(*args, expect=2)

    def test_show_summary_then_explicit_content(self):
        corpus_ref, _snapshot = self.build_corpus()
        plan_ref = self.build_plan()
        self.cli(*self._args(corpus_ref, plan_ref))
        summary = self.cli(
            "discovery", "report", "show", "--project", self.project_id
        )["report"]
        self.assertEqual(summary["revision_id"], "cov-cli-1")
        self.assertNotIn("payload", summary)
        content = self.cli(
            "discovery", "report", "show", "--project", self.project_id, "--content"
        )
        self.assertTrue(content["private"])
        self.assertFalse(content["authorizes_execution"])
        self.assertEqual(content["document"]["payload"]["payload_version"], 1)
        # The full private document is the validated report envelope.
        self.assertEqual(content["document"]["document_type"], "discovery_coverage")


# --------------------------------------------------------------------------- #
# freeze propose / status / show / history / require
# --------------------------------------------------------------------------- #
class FreezeCliTest(ReportsCliBase):
    def _setup(self, *, propose_accept="accepted: expansion not performed",
               decision="freeze", rev="frz-cli-1", request_id="frz-req-1",
               reviews=None, accept=True):
        corpus_ref, snapshot = self.build_corpus()
        plan_ref = self.build_plan()
        coverage_ref = self.record_coverage(corpus_ref, plan_ref)
        reviews = reviews if reviews is not None else self.reviews_for(snapshot)
        reviews_path = self.write_json(f"reviews-{rev}.json", reviews)
        rights_path = self.write_json(
            f"frz-rights-{rev}.json", self.rights(DEFAULT_FREEZE_ARTIFACT_ID, rev)
        )
        args = [
            "discovery", "freeze", "propose", "--project", self.project_id,
            "--corpus-artifact-id", corpus_ref["artifact_id"],
            "--corpus-revision-id", corpus_ref["revision_id"],
            "--coverage-artifact-id", coverage_ref["artifact_id"],
            "--coverage-revision-id", coverage_ref["revision_id"],
            "--decision", decision,
            "--entry-reviews-file", str(reviews_path),
            "--rationale", "human decision",
            "--request-id", request_id,
            "--revision-id", rev,
            "--rights-file", str(rights_path),
        ]
        if accept:
            args += ["--accept-coverage-limitations", propose_accept]
        return corpus_ref, coverage_ref, plan_ref, snapshot, args

    def test_propose_is_inert_and_returns_named_gate_request(self):
        _c, _cov, _p, _s, args = self._setup()
        out = self.cli(*args)
        self.assertFalse(out["replayed"])
        request = out["approval_request"]
        self.assertEqual(request["gate_kind"], "artifact_revision")
        self.assertEqual(request["gate_id"], "discovery.freeze")
        self.assertEqual(request["artifact_id"], DEFAULT_FREEZE_ARTIFACT_ID)
        self.assertEqual(request["revision_id"], "frz-cli-1")
        self.assertFalse(request["satisfied"])
        self.assertTrue(request["current"])
        self.assertFalse(request["authorizes_execution"])
        self.assertEqual(request["snapshot_preparation"]["prepare_command"], "gate prepare")
        self.assertEqual(request["snapshot_preparation"]["confirm_command"], "gate confirm")
        self.assertIn("NEW corpus", request["ingest_guidance"])
        self.assertFalse(out["decision"]["authorizing"])
        # Not active before the real gate.
        self.cli(
            "discovery", "freeze", "require", "--project", self.project_id, expect=2
        )

    def test_real_gate_yields_require_active_exact_roster(self):
        corpus_ref, _cov, _p, snapshot, args = self._setup()
        self.cli(*args)
        self.approve(DEFAULT_FREEZE_ARTIFACT_ID, "frz-cli-1", FREEZE_GATE_KIND, FREEZE_GATE_ID)
        out = self.cli("discovery", "freeze", "require", "--project", self.project_id)
        self.assertTrue(out["frozen"])
        expected = sorted(
            c["candidate_id"] for c in snapshot["candidates"] if c["resolved"]
        )
        self.assertEqual(sorted(out["included_candidate_ids"]), expected)
        # status reports an active freeze.
        status = self.cli("discovery", "freeze", "status", "--project", self.project_id)
        self.assertEqual(status["current_decision"]["decision"], "freeze")
        self.assertTrue(status["current_decision"]["approval"]["satisfies_named_gate"])
        self.assertEqual(status["last_frozen_corpus"]["included_count"], len(expected))

    def test_wrong_gate_rejected_and_stale_never_authorize(self):
        # Wrong gate id.
        _c, _cov, _p, _s, args = self._setup(rev="frz-wrong", request_id="frz-wrong-req")
        self.cli(*args)
        self.approve(DEFAULT_FREEZE_ARTIFACT_ID, "frz-wrong", FREEZE_GATE_KIND, "tool.allowed")
        self.cli("discovery", "freeze", "require", "--project", self.project_id, expect=2)

    def test_rejected_decision_never_authorizes(self):
        _c, _cov, _p, _s, args = self._setup(rev="frz-rej", request_id="frz-rej-req")
        self.cli(*args)
        self.approve(DEFAULT_FREEZE_ARTIFACT_ID, "frz-rej", FREEZE_GATE_KIND, FREEZE_GATE_ID,
                     decision="rejected")
        self.cli("discovery", "freeze", "require", "--project", self.project_id, expect=2)
        status = self.cli("discovery", "freeze", "status", "--project", self.project_id)
        self.assertIsNone(status["current_decision"])

    def test_stale_after_new_coverage_revision_is_visible(self):
        corpus_ref, coverage_ref, plan_ref, snapshot, args = self._setup(
            rev="frz-stale", request_id="frz-stale-req"
        )
        self.cli(*args)
        self.approve(DEFAULT_FREEZE_ARTIFACT_ID, "frz-stale", FREEZE_GATE_KIND, FREEZE_GATE_ID)
        # A later coverage revision on the same artifact moves the live head.
        self.record_coverage(corpus_ref, plan_ref, request_id="cov-2")
        self.cli("discovery", "freeze", "require", "--project", self.project_id, expect=2)
        status = self.cli("discovery", "freeze", "status", "--project", self.project_id)
        self.assertIsNone(status["current_decision"])
        self.assertIsNotNone(status["last_frozen_corpus"])
        self.assertFalse(status["last_frozen_corpus"]["frozen"])
        self.assertEqual(status["last_frozen_corpus"]["approval"]["status"], "stale")

    def test_search_again_is_explicit_and_history_preserved(self):
        corpus_ref, coverage_ref, plan_ref, snapshot, args = self._setup(
            rev="frz-sa-1", request_id="frz-sa-req-1"
        )
        self.cli(*args)
        self.approve(DEFAULT_FREEZE_ARTIFACT_ID, "frz-sa-1", FREEZE_GATE_KIND, FREEZE_GATE_ID)
        # A subsequent explicit search-again decision (no acknowledgement needed).
        reviews_path = self.write_json("sa-reviews.json", self.reviews_for(snapshot))
        rights_path = self.write_json(
            "sa-rights.json", self.rights(DEFAULT_FREEZE_ARTIFACT_ID, "frz-sa-2")
        )
        self.cli(
            "discovery", "freeze", "propose", "--project", self.project_id,
            "--corpus-artifact-id", corpus_ref["artifact_id"],
            "--corpus-revision-id", corpus_ref["revision_id"],
            "--coverage-artifact-id", coverage_ref["artifact_id"],
            "--coverage-revision-id", coverage_ref["revision_id"],
            "--decision", "search_again", "--entry-reviews-file", str(reviews_path),
            "--rationale", "search again", "--request-id", "frz-sa-req-2",
            "--revision-id", "frz-sa-2", "--rights-file", str(rights_path),
        )
        self.approve(DEFAULT_FREEZE_ARTIFACT_ID, "frz-sa-2", FREEZE_GATE_KIND, FREEZE_GATE_ID)
        self.cli("discovery", "freeze", "require", "--project", self.project_id, expect=2)
        status = self.cli("discovery", "freeze", "status", "--project", self.project_id)
        self.assertEqual(status["current_decision"]["decision"], "search_again")
        history = self.cli("discovery", "freeze", "history", "--project", self.project_id)
        self.assertEqual(
            [d["decision"] for d in history["decisions"]], ["search_again", "freeze"]
        )

    def test_show_summary_then_explicit_content(self):
        _c, _cov, _p, _s, args = self._setup(rev="frz-show", request_id="frz-show-req")
        self.cli(*args)
        summary = self.cli(
            "discovery", "freeze", "show", "--project", self.project_id,
            "--revision-id", "frz-show",
        )["decision"]
        self.assertEqual(summary["decision"], "freeze")
        self.assertNotIn("entry_reviews", summary)
        # No candidate ids dumped by default.
        self.assertNotIn("candidate_id", json.dumps(summary))
        content = self.cli(
            "discovery", "freeze", "show", "--project", self.project_id,
            "--revision-id", "frz-show", "--content",
        )
        self.assertTrue(content["private"])
        self.assertIn("entry_reviews", content["document"]["payload"])

    def test_freezing_with_gaps_without_ack_is_refused(self):
        _c, _cov, _p, _s, args = self._setup(
            rev="frz-noack", request_id="frz-noack-req", accept=False
        )
        self.cli(*args, expect=2)
        self.assertIsNone(self.artifacts.get_artifact(DEFAULT_FREEZE_ARTIFACT_ID))

    def test_malformed_entry_reviews_writes_nothing(self):
        _c, _cov, _p, _s, args = self._setup(rev="frz-badrev", request_id="frz-badrev-req")
        bad = self.write_json("bad-reviews.json", [{"candidate_id": "x"}])
        args[args.index("--entry-reviews-file") + 1] = str(bad)
        self.cli(*args, expect=2)
        self.assertIsNone(self.artifacts.get_artifact(DEFAULT_FREEZE_ARTIFACT_ID))


# --------------------------------------------------------------------------- #
# flag-surface + help guards
# --------------------------------------------------------------------------- #
class FlagSurfaceTest(ReportsCliBase):
    def test_no_yes_or_approve_flags_anywhere(self):
        for bad in ("--yes", "--approve", "--approved", "--actor"):
            self.cli(
                "discovery", "freeze", "propose", "--project", self.project_id,
                "--corpus-artifact-id", "c", "--corpus-revision-id", "r",
                "--coverage-artifact-id", "c", "--coverage-revision-id", "r",
                "--decision", "freeze", "--entry-reviews-file", "x",
                "--rationale", "r", "--request-id", "q", "--revision-id", "v",
                "--rights-file", "y", bad, "z", expect=2,
            )

    def _help(self, *args: str) -> str:
        parser = cli_main.build_parser()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as raised:
                parser.parse_args(list(args))
        self.assertEqual(raised.exception.code, 0)
        return buf.getvalue()

    def test_help_documents_new_commands(self):
        self.assertIn("--corpus-artifact-id", self._help("discovery", "report", "create", "--help"))
        self.assertIn("--entry-reviews-file", self._help("discovery", "freeze", "propose", "--help"))
        self.assertIn("--accept-coverage-limitations", self._help("discovery", "freeze", "propose", "--help"))
        self.assertIn("--require-active", self._help("discovery", "freeze", "require", "--help"))

    def test_existing_discovery_commands_preserved(self):
        # The CLI1A job groups and the parent lifecycle readbacks still parse.
        self.assertIn("--query-index", self._help("discovery", "search", "run", "--help"))
        self.assertIn("--request-id", self._help("discovery", "lead", "status", "--help"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
