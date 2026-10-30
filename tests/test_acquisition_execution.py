"""Acceptance tests for the durable official acquisition execution library.

Synthetic data only; a loopback low-level fixture replaces the socket, so there is
no network egress. Real store, real registry activation token, real frozen corpus
and human-gate fixtures (``test_discovery_freeze``).

Run with::

    PYTHONPATH=src python3 -m unittest discover -s tests -p test_acquisition_execution.py
"""

from __future__ import annotations

import base64
import json
import sys
import tempfile
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

import test_discovery_freeze as fz  # noqa: E402
from test_acquisition_validation import jats  # noqa: E402

from review_article_agent.acquisition.execution import (  # noqa: E402
    AcquisitionExecutionBlockedError,
    AcquisitionExecutionConflictError,
    AcquisitionExecutionError,
    AcquisitionExecutor,
)
from review_article_agent.acquisition.transport import (  # noqa: E402
    TransportNetworkError,
    TransportResponse,
)
from review_article_agent.domain.records import canonical_json  # noqa: E402
from review_article_agent.storage.artifacts import sha256_digest  # noqa: E402
from review_article_agent.storage import (  # noqa: E402
    ArtifactRepository,
    ProjectStore,
    Registry,
    paths,
)

_TERMS = {
    op: {"terms_ref": "lic:pd-syn", "checked_at": "2026-10-29T00:00:00Z"}
    for op in ("acquisition", "storage")
}
_NO_WAIT = {"sleep": lambda _s: None, "rate_spacing": 0.0, "retry_delay": 0.0, "max_attempts": 1}


def ok(body, media="application/xml"):
    return TransportResponse(status=200, body=body, headers={"Content-Type": media})


def not_found():
    return TransportResponse(status=404, body=b"nope", headers={})


class Net:
    """Loopback low-level fixture: scripted responses, records every URL."""

    def __init__(self, *responses, after_call=None):
        self.responses = list(responses)
        self.urls = []
        self.after_call = after_call

    def __call__(self, url, *, timeout, max_response_bytes):
        self.urls.append(url)
        item = self.responses.pop(0) if self.responses else not_found()
        if self.after_call is not None:
            self.after_call(len(self.urls))
        if isinstance(item, Exception):
            raise item
        return item


class Base(fz.FreezeTestBase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-acqexec-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.registry = Registry(self.tmp / "state")
        root = self.tmp / "project"
        info = self.registry.create_project(root, name="P")
        self.app_dir = paths.app_dir_for(str(root))
        self.store = ProjectStore.open_at(self.app_dir)
        self.addCleanup(self._close_store)
        self.project_id = self.store.project_id
        self.assertEqual(self.project_id, info.project_id)
        self._bind(self.store)
        self.freeze = fz.FreezeRepository(self.store)
        self._install_owner_auth()
        self.token = self.registry.activate_execution(self.project_id).activation_token
        self.n = 0
        self.build_frozen()

    # -- fixtures ------------------------------------------------------------ #
    def build_frozen(self):
        a = self.observe(
            "req-a", self.pubmed_run((self.rec(pmid="111", pmcid="PMC111", source_id="a"),))
        )
        b = self.observe(
            "req-b", self.pubmed_run((self.rec(pmid="222", source_id="b"),))
        )
        rid = fz.CandidateRepository.new_revision_id()
        rev = self.corpus.ingest(
            [a.run_id, b.run_id], request_id="corpus-1",
            rights=self.rights(fz.DEFAULT_CORPUS_ARTIFACT_ID, rid), revision_id=rid,
        )
        self.corpus_ref = self.ref(rev.artifact_id, rev.revision_id)
        self.snapshot = self.corpus.current()
        self.plan_ref = self.build_plan()
        self.coverage_ref = self.record_coverage(self.corpus_ref, self.plan_ref)
        proposal = self.propose(
            self.corpus_ref, self.coverage_ref, self.snapshot, accept="accepted"
        )
        self.approve_freeze(proposal)
        self.freeze_ref = self.ref(proposal.artifact_id, proposal.revision_id)
        self.with_pmcid = self.cand("pmid", "111")
        self.no_pmcid = self.cand("pmid", "222")

    def cand(self, namespace, value):
        for doc in self.snapshot["candidates"]:
            if [namespace, value] in doc["tokens"]:
                return doc["candidate_id"]
        raise AssertionError("candidate not found")

    def executor(self, net, **kw):
        return AcquisitionExecutor(
            self.store, registry=self.registry, activation_token=kw.pop("token", self.token),
            lowlevel=net, transport_options=_NO_WAIT, **kw,
        )

    def args(self, ex, request_id="req-1", candidate=None, **over):
        self.n += 1
        n = self.n
        src_art = over.pop("source_artifact_id", "doc-a")
        src_rev = over.pop("source_revision_id", f"src-{n}")
        occ_rev, rep_rev = f"occ-rev-{n}", f"rep-rev-{n}"
        occ_art = ex.occurrence_artifact_id(request_id)
        rep_art = ex.report_artifact_id(request_id)
        args = {
            "freeze_ref": self.freeze_ref,
            "candidate_id": candidate or self.with_pmcid,
            "request_id": request_id,
            "source_artifact_id": src_art,
            "source_revision_id": src_rev,
            "source_rights": fz._rights(self.project_id, src_art, src_rev),
            "occurrence_revision_id": occ_rev,
            "occurrence_rights": fz._rights(self.project_id, occ_art, occ_rev),
            "report_revision_id": rep_rev,
            "report_rights": fz._rights(self.project_id, rep_art, rep_rev),
            "terms_evidence": json.loads(json.dumps(_TERMS)),
        }
        args.update(over)
        return args

    def count(self, table):
        return self.store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def full_text(self, **kw):
        kw.setdefault("pmid", b"111")
        kw.setdefault("pmcid", b"PMC111")
        kw.setdefault("doi", None)
        return jats(**kw)

    def reopen_store(self):
        self.store.close()
        self.store = ProjectStore.open_at(self.app_dir, expected_project_id=self.project_id)
        self.addCleanup(self._close_store)
        self._bind(self.store)


class SuccessAndReplayTests(Base):
    def test_success_imports_source_and_report_pins_everything_then_replays(self):
        body = self.full_text()
        net = Net(ok(body))
        ex = self.executor(net)
        args = self.args(ex)
        result = ex.acquire(**args)
        self.assertEqual(result["status"], "retrieved")
        self.assertFalse(result["replayed"])
        self.assertEqual(len(net.urls), 1)
        self.assertIn("/PMC111/fullTextXML", net.urls[0])
        self.assertEqual(result["validation_status"], "ready")
        self.assertEqual(result["disposition"], "created_new_revision")

        report = result["report"]
        self.assertIs(report["authorizing"], False)
        self.assertEqual(report["eligibility"], "eligibility_not_assessed")
        self.assertEqual(report["identifiers"], {"pmcid": "PMC111", "pmid": "111"})
        self.assertEqual(report["attempts"][0]["outcome"], "ok")

        # bytes live ONLY in the source revision; never in the report envelope
        src = result["source_ref"]
        repo = ArtifactRepository(self.store)
        self.assertEqual(repo.read_content(src["artifact_id"], src["revision_id"]), body)
        raw = repo.read_content(
            result["report_ref"]["artifact_id"], result["report_ref"]["revision_id"]
        )
        self.assertNotIn(base64.b64encode(body), raw)
        self.assertNotIn(b"Hello scientific", raw)
        for forbidden in (b"base64", b"text_preview", b'"content"'):
            self.assertNotIn(forbidden, raw)

        # report pins freeze + source + occurrence exactly
        deps = {
            (d.artifact_id, d.revision_id)
            for d in repo.list_dependencies(
                result["report_ref"]["artifact_id"], result["report_ref"]["revision_id"]
            )
        }
        self.assertEqual(
            deps,
            {
                (self.freeze_ref["artifact_id"], self.freeze_ref["revision_id"]),
                (src["artifact_id"], src["revision_id"]),
                (
                    result["occurrence_ref"]["artifact_id"],
                    result["occurrence_ref"]["revision_id"],
                ),
            },
        )

        # restart + changed scope: exact replay, zero GET, historical result
        self.record_coverage(self.corpus_ref, self.plan_ref, request_id="cov-2")
        self.reopen_store()
        net2 = Net()
        ex2 = self.executor(net2)
        again = ex2.acquire(**args)
        self.assertTrue(again["replayed"])
        self.assertEqual(again["report_ref"], result["report_ref"])
        self.assertEqual(again["source_ref"], src)
        self.assertEqual(net2.urls, [])
        self.assertEqual(ex2.find_request("req-1")["report_ref"], result["report_ref"])
        self.assertEqual(ex2.get_report(result["report_ref"])["status"], "retrieved")

    def test_divergent_replay_conflicts_without_io(self):
        net = Net(ok(self.full_text()))
        ex = self.executor(net)
        args = self.args(ex)
        ex.acquire(**args)
        net2 = Net()
        ex2 = self.executor(net2)
        diverged = dict(args, candidate_id=self.no_pmcid)
        with self.assertRaises(AcquisitionExecutionConflictError):
            ex2.acquire(**diverged)
        other_rev = self.args(ex2, request_id="req-1")
        with self.assertRaises(AcquisitionExecutionConflictError):
            ex2.acquire(**other_rev)
        self.assertEqual(net2.urls, [])

    def test_exact_duplicate_bytes_reuse_source_in_new_request(self):
        body = self.full_text()
        ex = self.executor(Net(ok(body), ok(body)))
        first = ex.acquire(**self.args(ex, "req-1"))
        second = ex.acquire(
            **self.args(ex, "req-2", parent_revision_id=first["source_ref"]["revision_id"])
        )
        self.assertEqual(second["disposition"], "duplicate_bytes_of_existing_revision")
        self.assertEqual(second["source_ref"], first["source_ref"])
        self.assertNotEqual(second["occurrence_ref"], first["occurrence_ref"])


class ZeroIoTests(Base):
    def assert_blocked_no_io(self, exc, mutate=None, **over):
        net = Net(ok(self.full_text()))
        ex = self.executor(net, **over.pop("ex_kw", {}))
        args = self.args(ex, **over)
        before = self.count("artifact")
        if mutate:
            mutate(args)
        with self.assertRaises(exc):
            ex.acquire(**args)
        self.assertEqual(net.urls, [])
        self.assertEqual(self.count("artifact"), before)
        self.assertIsNone(
            ArtifactRepository(self.store).get_artifact("doc-a")
        )

    def test_unapproved_freeze(self):
        # a second, never-approved proposal is not a freeze
        prop = self.propose(
            self.corpus_ref, self.coverage_ref, self.snapshot, request_id="freeze-2",
            accept="accepted",
        )
        self.assert_blocked_no_io(
            AcquisitionExecutionBlockedError,
            mutate=lambda a: a.update(freeze_ref=self.ref(prop.artifact_id, prop.revision_id)),
        )

    def test_stale_freeze(self):
        self.record_coverage(self.corpus_ref, self.plan_ref, request_id="cov-2")
        self.assert_blocked_no_io(AcquisitionExecutionBlockedError)

    def test_wrong_or_unknown_candidate(self):
        self.assert_blocked_no_io(AcquisitionExecutionBlockedError, candidate="cand_unknown")

    def test_wrong_rights_subject_unknown_storage_and_denied_acquisition(self):
        def wrong_subject(a):
            a["source_rights"] = fz._rights(self.project_id, "doc-a", "someone-else")

        def unknown_storage(a):
            r = a["source_rights"]
            r["operations"]["storage"] = "unknown"
            r["basis"]["storage"] = {"kind": "unknown", "evidence": {"ref": "", "provenance": ""}}

        def denied_acq(a):
            r = a["source_rights"]
            r["operations"]["acquisition"] = "denied"
            r["basis"]["acquisition"] = {
                "kind": "contract_or_license",
                "evidence": {"ref": "lic:pd-syn", "provenance": "p"},
            }

        def privacy(a):
            a["source_rights"]["privacy"]["denial"] = True

        for mutate in (wrong_subject, unknown_storage, denied_acq, privacy):
            net = Net(ok(self.full_text()))
            ex = self.executor(net)
            args = self.args(ex)
            mutate(args)
            rights_before = self.count("rights_record")
            with self.assertRaises(AcquisitionExecutionError):
                ex.acquire(**args)
            self.assertEqual(net.urls, [])
            self.assertEqual(self.count("rights_record"), rights_before)
            self.assertIsNone(ArtifactRepository(self.store).get_artifact("doc-a"))
            self.assertIsNone(ex.find_request("req-1"))

    def test_report_rights_without_storage_is_refused_before_io(self):
        net = Net(ok(self.full_text()))
        ex = self.executor(net)
        args = self.args(ex)
        args["report_rights"]["operations"]["storage"] = "unknown"
        args["report_rights"]["basis"]["storage"] = {
            "kind": "unknown", "evidence": {"ref": "", "provenance": ""},
        }
        with self.assertRaises(AcquisitionExecutionError):
            ex.acquire(**args)
        self.assertEqual(net.urls, [])

    def test_terms_must_match_rights_evidence_and_not_be_future(self):
        for bad in (
            {"acquisition": {"terms_ref": "other", "checked_at": "2026-10-29T00:00:00Z"},
             "storage": _TERMS["storage"]},
            {"acquisition": {"terms_ref": "lic:pd-syn", "checked_at": "2999-01-01T00:00:00Z"},
             "storage": _TERMS["storage"]},
            {"acquisition": _TERMS["acquisition"]},
        ):
            net = Net(ok(self.full_text()))
            ex = self.executor(net)
            with self.assertRaises(AcquisitionExecutionError):
                ex.acquire(**self.args(ex, terms_evidence=bad))
            self.assertEqual(net.urls, [])

    def test_inactive_stale_token_and_draining(self):
        net = Net(ok(self.full_text()))
        ex = self.executor(net, token="stale-token")
        with self.assertRaises(AcquisitionExecutionBlockedError):
            ex.acquire(**self.args(ex))
        ex = self.executor(net)
        self.registry.begin_drain(self.token)
        with self.assertRaises(AcquisitionExecutionBlockedError):
            ex.acquire(**self.args(ex))
        self.assertEqual(net.urls, [])

    def test_superseded_rights_identity_conflicts_before_io(self):
        net = Net(ok(self.full_text()))
        ex = self.executor(net)
        first = self.args(ex, "req-1")
        ex.acquire(**first)
        net2 = Net(ok(self.full_text()))
        ex2 = self.executor(net2)
        again = self.args(ex2, "req-2")
        # same rights_id as the stored source rights, but revision 0-style conflict
        again["source_rights"]["rights_id"] = first["source_rights"]["rights_id"]
        with self.assertRaises(AcquisitionExecutionConflictError):
            ex2.acquire(**again)
        self.assertEqual(net2.urls, [])


class ManualAndFailureTests(Base):
    def assert_no_source(self):
        repo = ArtifactRepository(self.store)
        self.assertIsNone(repo.get_artifact("doc-a"))
        rows = self.store.connection.execute(
            "SELECT rights_id FROM rights_record WHERE rights_id LIKE 'RIGHTS-doc-a%'"
        ).fetchall()
        self.assertEqual(rows, [])

    def test_missing_pmcid_is_durable_manual_report_without_source(self):
        net = Net(ok(self.full_text()))
        ex = self.executor(net)
        args = self.args(ex, candidate=self.no_pmcid)
        result = ex.acquire(**args)
        self.assertEqual(net.urls, [])
        self.assertEqual(result["status"], "manual_required")
        self.assertIsNone(result["source_ref"])
        self.assertIsNone(result["occurrence_ref"])
        self.assertEqual(result["report"]["failures"], [{"code": "missing_pmcid"}])
        self.assert_no_source()
        # replay: zero I/O
        again = self.executor(net).acquire(**args)
        self.assertTrue(again["replayed"])
        self.assertEqual(net.urls, [])

    def test_both_official_sources_fail_retains_attempts_and_no_source_rights(self):
        net = Net(not_found(), TransportNetworkError("x"))
        ex = self.executor(net)
        result = ex.acquire(**self.args(ex))
        self.assertEqual(result["status"], "manual_required")
        self.assertEqual(len(net.urls), 2)
        self.assertIn("fullTextXML", net.urls[0])
        self.assertIn("efetch", net.urls[1])
        attempts = result["report"]["attempts"]
        self.assertEqual([a["outcome"] for a in attempts], ["permanent", "network_error"])
        self.assertEqual(len(result["report"]["failures"]), 2)
        self.assertIsNone(result["source_ref"])
        self.assert_no_source()

    def test_html_login_body_is_a_failure_not_a_source(self):
        net = Net(ok(b"<html><body>login</body></html>", "text/html"), not_found())
        ex = self.executor(net)
        result = ex.acquire(**self.args(ex))
        self.assertEqual(result["status"], "manual_required")
        self.assertEqual(result["report"]["failures"][0]["code"], "html_login")
        self.assert_no_source()


class ValidationTests(Base):
    def test_wrong_paper_is_imported_only_as_explicit_proposal(self):
        wrong = self.full_text(pmid=b"999", pmcid=b"PMC999")
        ex = self.executor(Net(ok(wrong)))
        result = ex.acquire(**self.args(ex))
        self.assertEqual(result["status"], "retrieved")
        self.assertNotEqual(result["validation_status"], "ready")
        self.assertIsNotNone(result["source_ref"])
        report = result["report"]
        self.assertIs(report["authorizing"], False)
        self.assertEqual(report["validation"]["status"], result["validation_status"])
        self.assertIs(report["validation"]["authorizes_scientific_processing"], False)
        self.assertEqual(report["identifiers"]["pmid"], "111")
        # reading re-verifies against the stored bytes
        self.assertEqual(
            ex.get_report(result["report_ref"])["validation_status"],
            result["validation_status"],
        )


class InterruptionTests(Base):
    def test_stop_between_attempts_preserves_first_and_skips_fallback(self):
        calls = {"n": 0}

        def stop():
            calls["n"] += 1
            return calls["n"] >= 2

        seen = []
        net = Net(not_found(), ok(self.full_text()))
        ex = self.executor(net, stop_requested=stop, before_attempt=seen.append)
        result = ex.acquire(**self.args(ex))
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(len(net.urls), 1)
        self.assertEqual(result["attempt_count"], 1)
        self.assertEqual(result["report"]["interrupted_reason"], "stop_requested")
        self.assertIsNone(result["source_ref"])
        self.assertEqual([s["attempt_index"] for s in seen], [1])
        self.assertEqual(seen[0]["source"], "europepmc_oa_fulltext_xml")
        self.assertIsNone(ArtifactRepository(self.store).get_artifact("doc-a"))
        # interrupted reports are durable: the same request replays without I/O
        self.assertEqual(
            ex.find_request("req-1")["report_ref"], result["report_ref"]
        )
        self.assertEqual(len(net.urls), 1)

    def test_invalidated_freeze_mid_waterfall_preserves_first_attempt(self):
        def after(count):
            if count == 1:
                self.record_coverage(self.corpus_ref, self.plan_ref, request_id="cov-x")

        net = Net(not_found(), ok(self.full_text()), after_call=after)
        ex = self.executor(net)
        result = ex.acquire(**self.args(ex))
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["report"]["interrupted_reason"], "freeze_not_current")
        self.assertEqual(len(net.urls), 1)
        self.assertEqual(result["attempt_count"], 1)
        self.assertIsNone(result["source_ref"])

    def test_stop_before_any_attempt_raises_and_persists_nothing(self):
        net = Net(ok(self.full_text()))
        ex = self.executor(net, stop_requested=lambda: True)
        before = self.count("artifact")
        with self.assertRaises(AcquisitionExecutionBlockedError):
            ex.acquire(**self.args(ex))
        self.assertEqual(net.urls, [])
        self.assertEqual(self.count("artifact"), before)

    def test_callback_exception_fences_the_request(self):
        def boom(_info):
            raise RuntimeError("engine fence")

        net = Net(ok(self.full_text()))
        ex = self.executor(net, before_attempt=boom)
        with self.assertRaises(AcquisitionExecutionBlockedError):
            ex.acquire(**self.args(ex))
        self.assertEqual(net.urls, [])

    def test_drain_after_retrieval_still_commits_received_result(self):
        net = Net(ok(self.full_text()), after_call=lambda _n: self.registry.begin_drain(self.token))
        ex = self.executor(net)
        result = ex.acquire(**self.args(ex))
        self.assertEqual(result["status"], "retrieved")
        self.assertEqual(len(net.urls), 1)

    def test_freeze_stale_after_retrieval_discards_bytes(self):
        def after(count):
            self.record_coverage(self.corpus_ref, self.plan_ref, request_id="cov-late")

        net = Net(ok(self.full_text()), after_call=after)
        ex = self.executor(net)
        result = ex.acquire(**self.args(ex))
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["report"]["interrupted_reason"], "freeze_changed_before_commit")
        self.assertIsNone(result["source_ref"])
        self.assertIsNone(ArtifactRepository(self.store).get_artifact("doc-a"))


class ParentPreflightTests(Base):
    def seed_source(self):
        ex = self.executor(Net(ok(self.full_text())))
        return ex.acquire(**self.args(ex, "req-seed"))

    def test_invalid_parents_fail_before_any_get(self):
        first = self.seed_source()
        head = first["source_ref"]["revision_id"]
        cases = {
            "wrong parent": {"parent_revision_id": "not-the-head"},
            "missing parent on existing artifact": {},
            "parent on absent artifact": {
                "source_artifact_id": "doc-new", "parent_revision_id": head,
            },
        }
        for name, over in cases.items():
            net = Net(ok(self.full_text(pmid=b"111")))
            ex = self.executor(net)
            before = self.count("revision")
            with self.assertRaises(AcquisitionExecutionConflictError, msg=name):
                ex.acquire(**self.args(ex, "req-" + name.replace(" ", "-"), **over))
            self.assertEqual(net.urls, [], name)
            self.assertEqual(self.count("revision"), before, name)

    def test_exact_replay_still_wins_over_parent_preflight(self):
        ex = self.executor(Net(ok(self.full_text())))
        args = self.args(ex, "req-1")
        ex.acquire(**args)
        net = Net()
        again = self.executor(net).acquire(**args)  # artifact exists now; no parent given
        self.assertTrue(again["replayed"])
        self.assertEqual(net.urls, [])


class AfterAttemptTests(Base):
    def test_called_exactly_once_per_get_with_safe_metadata(self):
        seen = []
        net = Net(TransportNetworkError("x"), ok(self.full_text()))
        ex = self.executor(net, after_attempt=seen.append)
        result = ex.acquire(**self.args(ex))
        self.assertEqual(result["status"], "retrieved")
        self.assertEqual(len(net.urls), 2)
        self.assertEqual([m["attempt_index"] for m in seen], [1, 2])
        self.assertEqual(seen[0]["outcome"], "network_error")
        self.assertEqual(seen[0]["failure"], "network_error")
        self.assertIsNone(seen[0]["status"])
        self.assertEqual(seen[1]["outcome"], "response")
        self.assertEqual(seen[1]["status"], 200)
        self.assertEqual(seen[1]["source"], "pmc_efetch_pmc")
        self.assertGreater(seen[1]["byte_count"], 0)
        self.assertEqual(len(seen[1]["body_sha256"]), 64)
        for meta in seen:
            self.assertEqual(
                set(meta),
                {"attempt_index", "source", "url", "outcome", "status", "byte_count",
                 "body_sha256", "truncated", "started_at", "completed_at", "failure"},
            )
            for value in meta.values():
                self.assertNotIsInstance(value, (bytes, bytearray))

    def test_post_get_local_conflict_keeps_observation_without_retry(self):
        seen = []
        holder = {}

        def conflict(count):
            repo = ArtifactRepository(self.store)
            rev = holder["args"]["source_revision_id"]
            repo.create_artifact("doc-a", kind="document")
            repo.import_revision(
                artifact_id="doc-a", content=b"already here", origin_locator="x:y",
                rights=fz._rights(self.project_id, "doc-a", rev), revision_id=rev,
            )

        net = Net(ok(self.full_text()), after_call=conflict)
        ex = self.executor(net, after_attempt=seen.append)
        holder["args"] = self.args(ex)
        result = ex.acquire(**holder["args"])
        self.assertEqual(len(net.urls), 1)  # never re-fetched
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["status"], 200)
        # metadata-only fallback report: attempt preserved, nothing imported
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(
            result["report"]["interrupted_reason"], "local_commit_rejected:rights_conflict"
        )
        self.assertEqual(result["attempt_count"], 1)
        self.assertIsNone(result["source_ref"])
        self.assertEqual(ex.get_report(result["report_ref"])["status"], "interrupted")
        self.assertEqual(len(ArtifactRepository(self.store).list_revisions("doc-a")), 1)

    def test_local_conflict_with_unusable_report_rights_raises_original(self):
        seen = []
        holder = {}

        def conflict(count):
            repo = ArtifactRepository(self.store)
            rev = holder["args"]["source_revision_id"]
            repo.create_artifact("doc-a", kind="document")
            repo.import_revision(
                artifact_id="doc-a", content=b"already here", origin_locator="x:y",
                rights=fz._rights(self.project_id, "doc-a", rev), revision_id=rev,
            )
            # report rights chain gets superseded after dispatch: must not be bypassed
            newer = dict(holder["args"]["report_rights"], record_revision=2)
            ArtifactRepository(self.store).store_rights_record(newer)

        net = Net(ok(self.full_text()), after_call=conflict)
        ex = self.executor(net, after_attempt=seen.append)
        holder["args"] = self.args(ex)
        with self.assertRaises(AcquisitionExecutionError):
            ex.acquire(**holder["args"])
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(net.urls), 1)
        self.assertIsNone(ex.find_request("req-1"))

    def test_callback_failure_after_request_is_ambiguous_and_stops_fallback(self):
        def boom(_meta):
            raise RuntimeError("journal down")

        net = Net(not_found(), ok(self.full_text()))
        ex = self.executor(net, after_attempt=boom)
        result = ex.acquire(**self.args(ex))
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["report"]["interrupted_reason"], "after_attempt_callback_failed")
        self.assertEqual(len(net.urls), 1)
        self.assertEqual(result["attempt_count"], 1)
        self.assertIsNone(result["source_ref"])

    def test_callback_failure_on_final_response_discards_bytes(self):
        def boom(_meta):
            raise RuntimeError("journal down")

        net = Net(ok(self.full_text()))
        ex = self.executor(net, after_attempt=boom)
        result = ex.acquire(**self.args(ex))
        self.assertEqual(result["status"], "interrupted")
        self.assertIsNone(result["source_ref"])
        self.assertIsNone(ArtifactRepository(self.store).get_artifact("doc-a"))

    def test_non_callable_hooks_are_refused(self):
        with self.assertRaises(AcquisitionExecutionError):
            self.executor(Net(), after_attempt="not callable")


class ProvenanceTests(Base):
    def test_adapter_vs_document_version_and_get_limits(self):
        ex = self.executor(Net(ok(self.full_text())))
        report = ex.acquire(**self.args(ex))["report"]
        self.assertEqual(report["adapter_version"], report["source_version"])
        self.assertEqual(report["adapter_version"], "europepmc/rest/fullTextXML")
        self.assertEqual(report["document_version"]["status"], "unknown")
        limits = report["limits"]
        self.assertNotIn("max_requests", limits)
        self.assertEqual(limits["max_sources"], 2)
        self.assertEqual(limits["max_attempts_per_source"], 1)
        self.assertEqual(limits["max_get_attempts"], 2)


class TamperTests(Base):
    """Trigger-drop + hash-recalc forgeries must not be trusted on read."""

    def tamper(self, result, mutate, *, sql=None):
        conn = self.store.connection
        for trig in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
            conn.execute(f"DROP TRIGGER {trig['name']}")
        ref = result["report_ref"]
        repo = ArtifactRepository(self.store)
        env = json.loads(repo.read_content(ref["artifact_id"], ref["revision_id"]))
        mutate(env["payload"])
        raw = canonical_json(env)
        conn.execute(
            "UPDATE revision SET content = ?, content_sha256 = ?, byte_size = ? "
            "WHERE artifact_id = ? AND revision_id = ?",
            (raw, sha256_digest(raw), len(raw), ref["artifact_id"], ref["revision_id"]),
        )
        if sql:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute(sql, (ref["artifact_id"], ref["revision_id"]))

    def acquire_one(self, n):
        # Distinct bytes per case (identifiers pmid/pmcid stay constant) so every
        # tamper case exercises a genuinely retrieved report rather than a
        # cross-artifact duplicate fallback.
        body = b"<sec><p>Tamper case " + str(n).encode() + b".</p></sec>"
        ex = self.executor(Net(ok(self.full_text(body=body))))
        result = ex.acquire(**self.args(ex, f"req-t{n}", source_artifact_id=f"doc-t{n}"))
        return ex, result

    def assert_rejected(self, mutate, n, *, sql=None):
        ex, result = self.acquire_one(n)
        self.tamper(result, mutate, sql=sql)
        with self.assertRaises(AcquisitionExecutionError) as caught:
            ex.get_report(result["report_ref"])
        self.assertEqual(caught.exception.code, "report_invalid")

    @slow
    def test_forged_fields_are_rejected(self):
        cases = [
            lambda p: p.update(candidate_id=self.no_pmcid),
            lambda p: p.update(identifiers={"pmid": "111", "pmcid": "PMC999"}),
            lambda p: p["identifiers"].update(doi="10.1/forged"),
            lambda p: p.update(identifier_conflicts=["doi"]),
            lambda p: p["attempts"][0].update(body_bytes=True),
            lambda p: p["attempts"][0].update(status=99),
            lambda p: p["attempts"][0].update(requested_url="https://evil.example/x"),
            lambda p: p["attempts"][0].update(outcome="made-up"),
            lambda p: p["attempts"][0].update(requested_at="yesterday"),
            lambda p: p["attempts"][0].update(body_sha256="0" * 64),
            lambda p: p["limits"].update(max_get_attempts=99),
            lambda p: p.update(adapter_version="forged/1"),
            lambda p: p.update(document_version={"status": "final", "reason": "x"}),
            lambda p: p.update(origin_locator="https://evil.example/x"),
            lambda p: p.update(request_digest="nothex"),
            lambda p: p["failures"].append({"code": "not-a-code"}),
            lambda p: p["validation"].update(status="bogus"),
            lambda p: p["validation"].update(byte_size=1),
        ]
        for n, mutate in enumerate(cases):
            with self.subTest(case=n):
                self.assert_rejected(mutate, n)

    def test_unknown_validator_version_is_stale_never_trusted_ready(self):
        ex, result = self.acquire_one(100)
        self.assertEqual(result["validation_status"], "ready")
        self.assertEqual(result["validation_check"], "verified")
        self.tamper(result, lambda p: p.update(validator_version="future/9"))
        again = ex.get_report(result["report_ref"])
        self.assertEqual(again["validation_check"], "stale_validator_version")
        self.assertEqual(again["validation_status"], "stale_validator_version")

    def test_forged_validation_with_unknown_version_still_checked_structurally(self):
        ex, result = self.acquire_one(101)

        def forge(p):
            p["validator_version"] = "future/9"
            p["validation"]["sha256"] = "1" * 64

        self.tamper(result, forge)
        with self.assertRaises(AcquisitionExecutionError):
            ex.get_report(result["report_ref"])

    def test_foreign_project_dependency_is_rejected(self):
        ex, result = self.acquire_one(102)
        conn = self.store.connection
        for trig in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
            conn.execute(f"DROP TRIGGER {trig['name']}")
        conn.execute("PRAGMA foreign_keys = OFF")
        ref = result["report_ref"]
        conn.execute(
            "UPDATE dependency SET to_project_id = 'foreign' WHERE from_artifact_id = ? "
            "AND from_revision_id = ?",
            (ref["artifact_id"], ref["revision_id"]),
        )
        with self.assertRaises(AcquisitionExecutionError):
            ex.get_report(result["report_ref"])


if __name__ == "__main__":
    unittest.main()
