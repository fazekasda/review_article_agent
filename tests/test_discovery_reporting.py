"""Coverage / work / monetary report artifact tests (Step 05, R1).

Covers ``src/review_article_agent/discovery/reporting.py`` against the R1
acceptance criteria: deterministic coverage/gaps, known-relevant assessment,
forward/backward citation limits and gaps, cumulative project work, runtime vs
synthetic monetary separation, exact report revision dependencies, project
isolation, reopen, later-observation non-mutation, forged-input rejection, and
non-authorizing guarantees.

Every fixture is synthetic; no test performs a network, provider, or model call.
Tests use real project stores and scripted observations only.

Run::

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_discovery_*.py'
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from review_article_agent.discovery import transport  # noqa: E402
from review_article_agent.discovery.candidates import (  # noqa: E402
    DEFAULT_CORPUS_ARTIFACT_ID,
    CandidateRepository,
)
from review_article_agent.discovery.context import (  # noqa: E402
    read_context_document,
)
from review_article_agent.discovery.expansion import (  # noqa: E402
    CitationNode,
    ExpansionLimits,
    expand_citations,
)
from review_article_agent.discovery.reporting import (  # noqa: E402
    COVERAGE_NOTICE,
    DEFAULT_COVERAGE_ARTIFACT_ID,
    DOCUMENT_TYPE,
    CoverageConflictError,
    CoverageCorruptionError,
    CoverageRepository,
    CoverageValidationError,
    build_coverage_report,
)
from review_article_agent.domain.records import canonical_json  # noqa: E402
from review_article_agent.runtime.dispatch import RuntimeDispatchRepository  # noqa: E402
from review_article_agent.runtime.jobs import JobRepository  # noqa: E402
from review_article_agent.runtime.roles import BoundedTask, RoleConfiguration  # noqa: E402
from review_article_agent.storage import (  # noqa: E402
    ArtifactRepository,
    ProjectStore,
    paths,
)
from review_article_agent.storage.agent_runtime import RuntimeRepository  # noqa: E402
from review_article_agent.storage.discovery import DiscoveryRepository  # noqa: E402
import test_discovery_serialization as fx  # noqa: E402

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")
_DIG_A = "a" * 64
_DIG_B = "b" * 64
_DIG_C = "c" * 64


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
        "recorded_at": "2026-10-27T00:00:00Z",
        "derived_status": "valid",
    }
    record.update(overrides)
    return record


def _envelope(document_type: str, payload: dict) -> dict:
    return {"schema_version": 1, "document_type": document_type, "payload": payload}


# --------------------------------------------------------------------------- #
# Scripted Europe PMC expansion provider (mirrors test_discovery_expansion)
# --------------------------------------------------------------------------- #
def _json_response(payload) -> transport.TransportResponse:
    return transport.TransportResponse(
        status=200, body=json.dumps(payload).encode("utf-8"), headers={}
    )


def _ref_payload(hit_count, items) -> dict:
    return {"version": "6.9", "hitCount": hit_count, "referenceList": {"reference": list(items)}}


def _cit_payload(hit_count, items) -> dict:
    return {"version": "6.9", "hitCount": hit_count, "citationList": {"citation": list(items)}}


def _entry(rec_id=None, source="MED", **extra) -> dict:
    item: dict = {}
    if source is not None:
        item["source"] = source
    if rec_id is not None:
        item["id"] = rec_id
    item.update(extra)
    return item


class FakeProvider:
    def __init__(self, script=None) -> None:
        self._script = {key: list(value) for key, value in (script or {}).items()}
        self.calls: list[str] = []

    def __call__(self, url, *, timeout, max_response_bytes):
        self.calls.append(url)
        parts = urlsplit(url)
        segments = parts.path.rstrip("/").split("/")
        source, rec_id, relation = segments[-3], segments[-2], segments[-1]
        items = self._script.get((source, rec_id, relation))
        if not items:
            payload = _ref_payload(0, []) if relation == "references" else _cit_payload(0, [])
            return _json_response(payload)
        item = items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class ReportingTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-coverage-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.project_id = paths.new_project_id()
        self.app_dir = paths.app_dir_for(str(self.tmp / "project"))
        self.store = ProjectStore.create_at(self.app_dir, self.project_id)
        self.addCleanup(self.store.close)
        self.artifacts = ArtifactRepository(self.store)
        self.discovery = DiscoveryRepository(self.store)
        self.corpus = CandidateRepository(self.store)
        self.coverage = CoverageRepository(self.store)

    # -- fixtures ------------------------------------------------------------ #
    @staticmethod
    def rec(**overrides):
        kwargs = {"pmcid": None, "doi": None}
        kwargs.update(overrides)
        return fx.record(**kwargs)

    def pubmed_run(self, records, **overrides):
        return fx.pubmed_result(pages=(fx.page(records=tuple(records)),), **overrides)

    def observe(self, request_id: str, result):
        return self.discovery.record_search(request_id=request_id, result=result)

    def rights(self, artifact_id: str, revision_id: str, **overrides) -> dict:
        return _rights_record(self.project_id, artifact_id, revision_id, **overrides)

    def ingest_corpus(self, run_ids, *, request_id, revision_id=None, artifact_id=DEFAULT_CORPUS_ARTIFACT_ID):
        rid = revision_id or CandidateRepository.new_revision_id()
        return self.corpus.ingest(
            run_ids,
            request_id=request_id,
            rights=self.rights(artifact_id, rid),
            revision_id=rid,
            artifact_id=artifact_id,
        )

    # -- plan context -------------------------------------------------------- #
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

    def ref(self, artifact_id, revision_id):
        return {
            "project_id": self.project_id,
            "artifact_id": artifact_id,
            "revision_id": revision_id,
        }

    def build_plan(self, *, request_id="plan-run", records=None, queries=None, complementary=None):
        records = records or (self.rec(pmid="999", source_id="seed"),)
        run = self.observe(request_id, self.pubmed_run(records))
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
        self._put(
            "a-seed0", "document", "rev-seed0",
            _envelope("discovery_seed", {
                "run_id": run.run_id,
                "record_index": 0,
                "abstract": None,
                "abstract_context": "provisional",
                "limitations": ["synthetic"],
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
        seed_ref = self.ref("a-seed0", "rev-seed0")
        deps = [
            brief_ref,
            self.ref("a-interview", "rev-interview"),
            seed_ref,
            self.ref("a-framing", "rev-framing"),
            self.ref("a-scope", "rev-scope"),
            self.ref("a-rubric", "rev-rubric"),
        ]
        plan = self._put(
            "a-plan", "report", "rev-plan",
            _envelope("discovery_plan", {
                "protocol": "protocol",
                "brief_ref": brief_ref,
                "interview_ref": self.ref("a-interview", "rev-interview"),
                "seed_refs": [seed_ref],
                "framing_ref": self.ref("a-framing", "rev-framing"),
                "scope_ref": self.ref("a-scope", "rev-scope"),
                "rubric_ref": self.ref("a-rubric", "rev-rubric"),
                "queries": queries or [
                    {"source": "pubmed", "query": "cancer therapy", "page_size": 20, "max_pages": 2}
                ],
                "complementary_sources": complementary or [],
                "expansion": {"max_hops": 1, "max_sources": 5, "max_requests": 10, "page_size": 20, "max_pages": 3},
                "impact_review": {"previous_plan": None, "affected": [], "rationale": ""},
            }),
            dependencies=deps,
        )
        return self.ref("a-plan", plan.revision_id)

    # -- report helpers ------------------------------------------------------ #
    def build(self, corpus_ref, plan_ref, **kwargs):
        return build_coverage_report(self.store, corpus_ref, plan_ref=plan_ref, **kwargs)

    def record(self, corpus_ref, plan_ref, *, request_id, **kwargs):
        rid = kwargs.pop("revision_id", None) or CoverageRepository.new_revision_id()
        artifact_id = kwargs.pop("artifact_id", DEFAULT_COVERAGE_ARTIFACT_ID)
        return self.coverage.record_report(
            corpus_ref,
            plan_ref=plan_ref,
            request_id=request_id,
            rights=self.rights(artifact_id, rid),
            revision_id=rid,
            artifact_id=artifact_id,
            **kwargs,
        )

    def _reopen(self):
        self.store.close()
        reopened = ProjectStore.open_at(self.app_dir, expected_project_id=self.project_id)
        self.addCleanup(reopened.close)
        self.store = reopened
        self.artifacts = ArtifactRepository(reopened)
        self.discovery = DiscoveryRepository(reopened)
        self.corpus = CandidateRepository(reopened)
        self.coverage = CoverageRepository(reopened)
        return reopened


# --------------------------------------------------------------------------- #
# Known-relevant
# --------------------------------------------------------------------------- #
class KnownRelevantTests(ReportingTestBase):
    def _corpus_ref(self):
        a = self.observe("req_a", self.pubmed_run((self.rec(pmid="111", source_id="a"),)))
        b = self.observe("req_b", self.pubmed_run((self.rec(pmid="222", source_id="b"),)))
        revision = self.ingest_corpus([a.run_id, b.run_id], request_id="corpus-1")
        return self.ref(revision.artifact_id, revision.revision_id)

    def test_present_and_missing_tokens(self) -> None:
        corpus_ref = self._corpus_ref()
        plan_ref = self.build_plan()
        report = self.build(
            corpus_ref, plan_ref,
            known_relevant=[
                {"namespace": "pmid", "value": "111"},
                {"namespace": "pmid", "value": "333"},
            ],
        )
        known = report["payload"]["known_relevant"]
        self.assertEqual(known["status"], "assessed")
        self.assertEqual(known["matched"], 1)
        self.assertEqual(known["missing"], 1)
        by_value = {token["value"]: token for token in known["tokens"]}
        self.assertTrue(by_value["111"]["present"])
        self.assertFalse(by_value["333"]["present"])

    def test_unprovided_set_is_not_assessed(self) -> None:
        corpus_ref = self._corpus_ref()
        plan_ref = self.build_plan()
        report = self.build(corpus_ref, plan_ref)
        known = report["payload"]["known_relevant"]
        self.assertEqual(known["status"], "not_assessed")
        self.assertEqual(known["tokens"], [])
        # Never a pass/complete claim.
        self.assertIs(report["payload"]["completeness_claimed"], False)

    def test_invalid_namespace_is_refused(self) -> None:
        corpus_ref = self._corpus_ref()
        plan_ref = self.build_plan()
        with self.assertRaises(CoverageValidationError):
            self.build(corpus_ref, plan_ref, known_relevant=[{"namespace": "junk", "value": "1"}])
        with self.assertRaises(CoverageValidationError):
            self.build(corpus_ref, plan_ref, known_relevant="pmid:111")


# --------------------------------------------------------------------------- #
# Citations
# --------------------------------------------------------------------------- #
class CitationTests(ReportingTestBase):
    def _corpus_ref(self):
        a = self.observe("req_a", self.pubmed_run((self.rec(pmid="1", source_id="a"),)))
        revision = self.ingest_corpus([a.run_id], request_id="corpus-1")
        return self.ref(revision.artifact_id, revision.revision_id)

    def _expansion(self, *, limits=None):
        provider = FakeProvider(
            {
                ("MED", "1", "references"): [_json_response(_ref_payload(1, [_entry("2")]))],
                ("MED", "1", "citations"): [_json_response(_cit_payload(1, [_entry("3")]))],
                ("MED", "2", "references"): [_json_response(_ref_payload(0, []))],
                ("MED", "2", "citations"): [_json_response(_cit_payload(0, []))],
                ("MED", "3", "references"): [_json_response(_ref_payload(0, []))],
                ("MED", "3", "citations"): [_json_response(_cit_payload(0, []))],
            }
        )
        args = dict(
            request_id="exp-1",
            limits=limits or ExpansionLimits(
                max_hops=3, max_sources=32, max_requests=64, page_size=25, max_pages=5
            ),
            transport=provider,
        )
        return expand_citations(self.discovery, [CitationNode("MED", "1")], **args)

    def test_both_directions_and_no_gap(self) -> None:
        corpus_ref = self._corpus_ref()
        plan_ref = self.build_plan()
        report = self._expansion()
        out = self.build(corpus_ref, plan_ref, expansion_reports=[report])
        citations = out["payload"]["citations"]
        self.assertTrue(citations["expansion_performed"])
        self.assertEqual(citations["directions"], ["citations", "references"])
        limit_gaps = {
            gap["reason"]
            for gap in citations["gaps"]
            if gap["reason"] in ("hop_limit", "source_limit", "request_limit")
        }
        self.assertEqual(limit_gaps, set())
        self.assertEqual(citations["counts"]["relations"], 6)

    def test_limit_gap_is_explicit(self) -> None:
        corpus_ref = self._corpus_ref()
        plan_ref = self.build_plan()
        report = self._expansion(
            limits=ExpansionLimits(max_hops=0, max_sources=32, max_requests=10, page_size=25, max_pages=5)
        )
        out = self.build(corpus_ref, plan_ref, expansion_reports=[report])
        citations = out["payload"]["citations"]
        reasons = {gap["reason"] for gap in citations["gaps"]}
        self.assertIn("hop_limit", reasons)
        summary = " ".join(out["payload"]["limitations_summary"])
        self.assertIn("citation expansion gap", summary)

    def test_forged_counts_are_rejected(self) -> None:
        corpus_ref = self._corpus_ref()
        plan_ref = self.build_plan()
        forged = copy.deepcopy(self._expansion().to_mapping())
        forged["counts"]["edges"] += 1
        with self.assertRaises(CoverageValidationError):
            self.build(corpus_ref, plan_ref, expansion_reports=[forged])

    def test_forged_relation_digest_is_rejected(self) -> None:
        corpus_ref = self._corpus_ref()
        plan_ref = self.build_plan()
        forged = copy.deepcopy(self._expansion().to_mapping())
        forged["relations"][0]["result_sha256"] = "0" * 64
        with self.assertRaises(CoverageValidationError):
            self.build(corpus_ref, plan_ref, expansion_reports=[forged])

    def test_forged_support_flag_is_rejected(self) -> None:
        corpus_ref = self._corpus_ref()
        plan_ref = self.build_plan()
        forged = copy.deepcopy(self._expansion().to_mapping())
        forged["guarantees"]["completeness_claimed"] = True
        with self.assertRaises(CoverageValidationError):
            self.build(corpus_ref, plan_ref, expansion_reports=[forged])

    def test_expansion_from_artifact_ref_is_pinned(self) -> None:
        corpus_ref = self._corpus_ref()
        plan_ref = self.build_plan()
        mapping = self._expansion().to_mapping()
        expansion_artifact = self.artifacts.create_artifact("a-exp", kind="report").artifact_id
        rev = self.artifacts.import_revision(
            artifact_id=expansion_artifact,
            content=_envelope("discovery_citation_expansion", mapping),
            origin_locator="import:synthetic",
            rights=self.rights(expansion_artifact, "rev-exp"),
            revision_id="rev-exp",
        )
        revision = self.record(
            corpus_ref, plan_ref, request_id="coverage-1",
            expansion_reports=[self.ref(expansion_artifact, rev.revision_id)],
        )
        deps = {
            (dep.artifact_id, dep.revision_id)
            for dep in self.artifacts.list_dependencies(revision.artifact_id, revision.revision_id)
        }
        self.assertIn((expansion_artifact, "rev-exp"), deps)


# --------------------------------------------------------------------------- #
# Source reconstruction / work
# --------------------------------------------------------------------------- #
class SourceReconstructionTests(ReportingTestBase):
    def test_partial_failure_and_overlap_reconstructed(self) -> None:
        ok_page = fx.page(records=(self.rec(pmid="5", source_id="5"),))
        failed_page = fx.page(
            page_index=1,
            records=(),
            total_count=1,
            complete=False,
            stop_reason="failure",
            failure="http_status",
            attempts=(fx.attempt(attempt_no=2, status=503, outcome="retryable"),),
        )
        partial = fx.result((ok_page, failed_page), stop_reason="failure", failure="http_status")
        a = self.observe("req_a", partial)
        missing = self.rec(
            source="europepmc",
            pmid=None,
            source_id=None,
            title="none",
            publication_year=None,
        )
        b = self.observe(
            "req_b",
            fx.result(
                (
                    fx.page(
                        source="europepmc",
                        records=(missing,),
                        page_size=100,
                        total_count=1,
                    ),
                ),
                source="europepmc",
            ),
        )
        revision = self.ingest_corpus([a.run_id, b.run_id], request_id="corpus-1")
        corpus_ref = self.ref(revision.artifact_id, revision.revision_id)
        plan_ref = self.build_plan()
        report = self.build(corpus_ref, plan_ref)
        sources = report["payload"]["sources"]
        rows = {row["run_id"]: row for row in sources["runs"]}
        self.assertEqual(set(rows), {a.run_id, b.run_id})
        self.assertEqual(rows[a.run_id]["stop_reason"], "failure")
        self.assertEqual(rows[a.run_id]["failure"], "http_status")
        self.assertEqual(rows[a.run_id]["page_count"], 2)
        self.assertEqual(rows[a.run_id]["record_count"], 1)
        self.assertFalse(rows[a.run_id]["complete"])
        self.assertFalse(rows[a.run_id]["is_metadata_import"])
        # Corpus totals derived from the actual snapshot, not supplied.
        self.assertEqual(report["payload"]["totals"]["missing_identifier_candidates"], 1)
        # Partial failure is visible in the limitations summary.
        self.assertIn("page limit", " ".join(report["payload"]["limitations_summary"]))

    def test_work_is_cumulative_project_scope(self) -> None:
        a = self.observe("req_a", self.pubmed_run((self.rec(pmid="1", source_id="a"),)))
        revision = self.ingest_corpus([a.run_id], request_id="corpus-1")
        # A later observation, not in the corpus, still counts toward project work.
        self.observe("req_b", self.pubmed_run((self.rec(pmid="2", source_id="b"),)))
        corpus_ref = self.ref(revision.artifact_id, revision.revision_id)
        plan_ref = self.build_plan()
        report = self.build(corpus_ref, plan_ref)
        work = report["payload"]["work"]
        self.assertEqual(report["payload"]["sources"]["totals"]["runs"], 1)
        self.assertEqual(work["totals"]["observations"], 3)  # corpus run + later + plan seed run

    def test_metadata_import_is_not_provider_http(self) -> None:
        from review_article_agent.discovery.execution import METADATA_IMPORT_QUERY_PREFIX

        record = self.rec(source="pubmed", source_id="imp1", pmid="700")
        page = fx.page(
            source="pubmed",
            query=METADATA_IMPORT_QUERY_PREFIX + "pubmed",
            records=(record,),
            attempts=(),
        )
        result = fx.result((page,), query=METADATA_IMPORT_QUERY_PREFIX + "pubmed")
        a = self.observe("req_imp", result)
        revision = self.ingest_corpus([a.run_id], request_id="corpus-1")
        corpus_ref = self.ref(revision.artifact_id, revision.revision_id)
        plan_ref = self.build_plan()
        report = self.build(corpus_ref, plan_ref)
        work = report["payload"]["work"]
        row = next(r for r in work["observations"] if r["run_id"] == a.run_id)
        self.assertTrue(row["is_metadata_import"])
        self.assertEqual(row["attempt_count"], 0)
        self.assertEqual(work["totals"]["metadata_imports"], 1)
        self.assertEqual(work["totals"]["imported_occurrences"], 1)
        self.assertIn("Metadata-import", " ".join(report["payload"]["limitations_summary"]))


# --------------------------------------------------------------------------- #
# Monetary
# --------------------------------------------------------------------------- #
class MonetaryTests(ReportingTestBase):
    def _corpus_ref(self):
        a = self.observe("req_a", self.pubmed_run((self.rec(pmid="1", source_id="a"),)))
        revision = self.ingest_corpus([a.run_id], request_id="corpus-1")
        return self.ref(revision.artifact_id, revision.revision_id)

    def _config(self):
        return RoleConfiguration.from_mapping(
            {
                "schema_version": 1,
                "roles": {
                    "writer": {
                        "provider_id": "openrouter",
                        "model_id": "anthropic/claude-sonnet-5-5",
                        "variant": "high",
                        "steps": 8,
                    }
                },
            }
        )

    def _task(self):
        runtime = RuntimeRepository(self.store)
        artifact = self.artifacts.create_artifact("a-dispatch-src", kind="document")
        self.artifacts.import_revision(
            artifact_id=artifact.artifact_id,
            content=b"dispatch-source-bytes",
            origin_locator="import:synthetic",
            rights=self.rights(artifact.artifact_id, "rev_d1"),
            revision_id="rev_d1",
        )
        mapping = {
            "project_id": self.project_id,
            "role": "writer",
            "target": {
                "project_id": self.project_id,
                "artifact_id": artifact.artifact_id,
                "revision_id": "rev_d1",
            },
            "input_refs": [],
            "scope": ["section:introduction"],
            "prompt_version": "writer.v1",
            "size_limit": 4096,
            "step_limit": 4,
        }
        bounded = BoundedTask.from_mapping(mapping, configuration=self._config())
        return runtime.record_task(bounded, request_id="task-req", configuration=self._config())

    def test_runtime_routes_and_unknown_reserve(self) -> None:
        corpus_ref = self._corpus_ref()
        plan_ref = self.build_plan()
        dispatch = RuntimeDispatchRepository(self.store)
        task = self._task()
        direct = dispatch.reserve_dispatch(
            task, request_id="call-1", route="direct_call", stage="s1",
            input_digest=_DIG_A, version_digest=_DIG_B, reserve_usd="0.20",
            bound_evidence_digest=_DIG_C,
        )
        dispatch.settle_cost(direct.dispatch_id, cost_state="unknown")
        report = self.build(corpus_ref, plan_ref)
        monetary = report["payload"]["monetary"]
        self.assertEqual(monetary["runtime_dispatch"]["direct_call"]["dispatches"], 1)
        self.assertEqual(monetary["runtime_dispatch"]["direct_call"]["unknown_dispatches"], 1)
        self.assertEqual(monetary["runtime_dispatch"]["direct_call"]["unknown_reserve_usd"], "0.20")
        self.assertNotEqual(monetary["runtime_dispatch"]["direct_call"]["unknown_reserve_usd"], "0")
        self.assertEqual(monetary["pins"][0]["dispatch_id"], direct.dispatch_id)

    def test_synthetic_virtual_reserve_labelled_and_not_doubled(self) -> None:
        corpus_ref = self._corpus_ref()
        plan_ref = self.build_plan()
        jobs = JobRepository(self.store)
        job = jobs.submit("job-1", task_kind="synthetic_probe", parameters={"n": 1})
        jobs.begin_attempt(job.job_id, dispatch_kind="external_synthetic", stage="s1", reserve_usd="0.10")
        report = self.build(corpus_ref, plan_ref)
        monetary = report["payload"]["monetary"]
        self.assertEqual(monetary["synthetic_jobs"]["virtual_reserve_usd"], "0.10")
        self.assertIn("never paid", monetary["synthetic_jobs"]["note"])
        # The shared exposure is the combined budget view, computed once.
        usage = jobs.budget_usage()
        self.assertEqual(monetary["shared_exposure"]["project_reserved_usd"], usage.project_reserved_usd)
        self.assertEqual(monetary["runtime_dispatch"]["totals"]["dispatches"], 0)

    def test_runtime_and_synthetic_shared_exposure_not_doubled(self) -> None:
        corpus_ref = self._corpus_ref()
        plan_ref = self.build_plan()
        jobs = JobRepository(self.store)
        job = jobs.submit("job-1", task_kind="synthetic_probe", parameters={"n": 1})
        jobs.begin_attempt(job.job_id, dispatch_kind="external_synthetic", stage="s1", reserve_usd="0.10")
        dispatch = RuntimeDispatchRepository(self.store)
        reserved = dispatch.reserve_dispatch(
            self._task(), request_id="call-1", route="opencode_session", stage="s1",
            input_digest=_DIG_A, version_digest=_DIG_B, reserve_usd="0.20",
            bound_evidence_digest=_DIG_C,
        )
        report = self.build(corpus_ref, plan_ref)
        monetary = report["payload"]["monetary"]
        # The combined ceiling is reported once by budget_usage, never re-summed.
        self.assertEqual(monetary["shared_exposure"]["project_reserved_usd"], "0.30")
        self.assertEqual(monetary["synthetic_jobs"]["virtual_reserve_usd"], "0.10")
        self.assertEqual(monetary["runtime_dispatch"]["opencode_session"]["reserve_usd"], "0.20")


# --------------------------------------------------------------------------- #
# Persistence, isolation, replay, non-mutation
# --------------------------------------------------------------------------- #
class PersistenceTests(ReportingTestBase):
    def _setup(self):
        a = self.observe("req_a", self.pubmed_run((self.rec(pmid="1", source_id="a"),)))
        revision = self.ingest_corpus([a.run_id], request_id="corpus-1")
        corpus_ref = self.ref(revision.artifact_id, revision.revision_id)
        plan_ref = self.build_plan()
        return corpus_ref, plan_ref

    def test_canonical_revision_and_dependencies(self) -> None:
        corpus_ref, plan_ref = self._setup()
        revision = self.record(corpus_ref, plan_ref, request_id="coverage-1")
        self.assertEqual(revision.artifact_id, DEFAULT_COVERAGE_ARTIFACT_ID)
        content = self.artifacts.read_content(revision.artifact_id, revision.revision_id)
        self.assertEqual(canonical_json(json.loads(content.decode("utf-8"))), content)
        deps = {
            (dep.artifact_id, dep.revision_id)
            for dep in self.artifacts.list_dependencies(revision.artifact_id, revision.revision_id)
        }
        self.assertIn((corpus_ref["artifact_id"], corpus_ref["revision_id"]), deps)
        self.assertIn((plan_ref["artifact_id"], plan_ref["revision_id"]), deps)
        report = self.coverage.get(
            {
                "project_id": self.project_id,
                "artifact_id": revision.artifact_id,
                "revision_id": revision.revision_id,
            }
        )
        self.assertEqual(report["document_type"], DOCUMENT_TYPE)
        self.assertEqual(report["payload"]["notice"], COVERAGE_NOTICE)

    def test_replay_returns_history_and_divergent_conflicts(self) -> None:
        corpus_ref, plan_ref = self._setup()
        rid = CoverageRepository.new_revision_id()
        rights = self.rights(DEFAULT_COVERAGE_ARTIFACT_ID, rid)
        first = self.coverage.record_report(
            corpus_ref, plan_ref=plan_ref, request_id="coverage-1",
            rights=rights, revision_id=rid,
        )
        again = self.coverage.record_report(
            corpus_ref, plan_ref=plan_ref, request_id="coverage-1",
            rights=rights, revision_id=rid,
        )
        self.assertEqual(again.revision_id, first.revision_id)
        with self.assertRaises(CoverageConflictError):
            self.coverage.record_report(
                corpus_ref, plan_ref=plan_ref, request_id="coverage-1",
                rights=rights, revision_id=rid,
                known_relevant=[{"namespace": "pmid", "value": "1"}],
            )

    def test_later_observations_do_not_mutate_old_report(self) -> None:
        corpus_ref, plan_ref = self._setup()
        revision = self.record(corpus_ref, plan_ref, request_id="coverage-1")
        before = self.coverage.get(
            {
                "project_id": self.project_id,
                "artifact_id": revision.artifact_id,
                "revision_id": revision.revision_id,
            }
        )
        # Growth in cumulative project work must not invalidate the frozen report.
        self.observe("req_late", self.pubmed_run((self.rec(pmid="88", source_id="late"),)))
        after = self.coverage.get(
            {
                "project_id": self.project_id,
                "artifact_id": revision.artifact_id,
                "revision_id": revision.revision_id,
            }
        )
        self.assertEqual(after, before)
        self.assertEqual(
            after["payload"]["work"]["totals"]["observations"],
            before["payload"]["work"]["totals"]["observations"],
        )
        # The frozen report still describes its own report-time work totals only.
        self.assertEqual(
            after["payload"]["work"]["totals"]["observations"],
            len(after["payload"]["work"]["observations"]),
        )

    def test_get_preserves_exact_ref_after_new_corpus_revision(self) -> None:
        corpus_ref, plan_ref = self._setup()
        revision = self.record(corpus_ref, plan_ref, request_id="coverage-1")
        before = self.coverage.get(
            {
                "project_id": self.project_id,
                "artifact_id": revision.artifact_id,
                "revision_id": revision.revision_id,
            }
        )
        # A newer corpus revision moves the corpus artifact's current revision.
        b = self.observe("req_b", self.pubmed_run((self.rec(pmid="2", source_id="b"),)))
        self.ingest_corpus([b.run_id], request_id="corpus-2")
        after = self.coverage.get(
            {
                "project_id": self.project_id,
                "artifact_id": revision.artifact_id,
                "revision_id": revision.revision_id,
            }
        )
        self.assertEqual(after, before)
        self.assertEqual(after["payload"]["corpus"]["ref"], corpus_ref)

    def test_reopen_preserves_current(self) -> None:
        corpus_ref, plan_ref = self._setup()
        self.record(corpus_ref, plan_ref, request_id="coverage-1")
        before = self.coverage.current()
        self._reopen()
        self.assertEqual(self.coverage.current(), before)

    def test_projects_are_isolated(self) -> None:
        corpus_ref, plan_ref = self._setup()
        revision = self.record(corpus_ref, plan_ref, request_id="coverage-1")

        other_id = paths.new_project_id()
        other_app = paths.app_dir_for(str(self.tmp / "other"))
        other_store = ProjectStore.create_at(other_app, other_id)
        self.addCleanup(other_store.close)
        other = CoverageRepository(other_store)
        self.assertIsNone(other.current())
        with self.assertRaises(CoverageValidationError):
            other.get(
                {
                    "project_id": other_id,
                    "artifact_id": revision.artifact_id,
                    "revision_id": revision.revision_id,
                }
            )

    def test_bad_references_are_refused(self) -> None:
        corpus_ref, plan_ref = self._setup()
        with self.assertRaises(CoverageValidationError):
            self.build(
                {"project_id": self.project_id, "artifact_id": "nope", "revision_id": "rev_x"},
                plan_ref,
            )
        with self.assertRaises(CoverageValidationError):
            self.build(
                corpus_ref,
                {"project_id": self.project_id, "artifact_id": "nope", "revision_id": "rev_x"},
            )

    def test_report_is_non_authorizing(self) -> None:
        corpus_ref, plan_ref = self._setup()
        report = self.build(corpus_ref, plan_ref)
        payload = report["payload"]
        self.assertIs(payload["authorizing"], False)
        self.assertIs(payload["completeness_claimed"], False)
        self.assertIs(payload["guarantees"]["literature_complete"], False)
        self.assertIs(payload["guarantees"]["scientific_eligibility_claimed"], False)
        self.assertIs(payload["guarantees"]["verified_support"], False)
        for forbidden in ("complete", "all_papers"):
            self.assertNotIn(forbidden, payload)

    def test_build_is_deterministic_and_writes_nothing(self) -> None:
        corpus_ref, plan_ref = self._setup()
        before = len(self.artifacts.list_revisions(DEFAULT_COVERAGE_ARTIFACT_ID))
        first = self.build(corpus_ref, plan_ref, known_relevant=[{"namespace": "pmid", "value": "1"}])
        second = self.build(corpus_ref, plan_ref, known_relevant=[{"namespace": "pmid", "value": "1"}])
        self.assertEqual(first, second)
        self.assertEqual(canonical_json(first), canonical_json(second))
        after = len(self.artifacts.list_revisions(DEFAULT_COVERAGE_ARTIFACT_ID))
        self.assertEqual(before, after)

    def test_forged_stored_payload_with_matching_digest_is_corruption(self) -> None:
        import hashlib
        import sqlite3

        corpus_ref, plan_ref = self._setup()
        revision = self.record(corpus_ref, plan_ref, request_id="coverage-1")
        content = self.artifacts.read_content(revision.artifact_id, revision.revision_id)
        envelope = json.loads(content.decode("utf-8"))
        envelope["payload"]["authorizing"] = True
        forged = canonical_json(envelope)
        conn = sqlite3.connect(paths.store_path_for(str(self.tmp / "project")))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='revision_no_update'"
            ).fetchone()
            saved = row[0] if row and row[0] else None
            conn.execute("DROP TRIGGER IF EXISTS revision_no_update")
            conn.execute(
                "UPDATE revision SET content = ?, content_sha256 = ?, byte_size = ? "
                "WHERE project_id = ? AND artifact_id = ? AND revision_id = ?",
                (
                    forged,
                    "sha256:" + hashlib.sha256(forged).hexdigest(),
                    len(forged),
                    self.project_id,
                    revision.artifact_id,
                    revision.revision_id,
                ),
            )
            if saved:
                conn.execute(saved)
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(CoverageCorruptionError):
            self.coverage.current()

    def test_forged_stored_source_row_is_corruption(self) -> None:
        import hashlib
        import sqlite3

        corpus_ref, plan_ref = self._setup()
        revision = self.record(corpus_ref, plan_ref, request_id="coverage-1")
        content = self.artifacts.read_content(revision.artifact_id, revision.revision_id)
        envelope = json.loads(content.decode("utf-8"))
        envelope["payload"]["sources"]["runs"][0]["query"] = "forged query"
        forged = canonical_json(envelope)
        conn = sqlite3.connect(paths.store_path_for(str(self.tmp / "project")))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='revision_no_update'"
            ).fetchone()
            saved = row[0] if row and row[0] else None
            conn.execute("DROP TRIGGER IF EXISTS revision_no_update")
            conn.execute(
                "UPDATE revision SET content = ?, content_sha256 = ?, byte_size = ? "
                "WHERE project_id = ? AND artifact_id = ? AND revision_id = ?",
                (
                    forged,
                    "sha256:" + hashlib.sha256(forged).hexdigest(),
                    len(forged),
                    self.project_id,
                    revision.artifact_id,
                    revision.revision_id,
                ),
            )
            if saved:
                conn.execute(saved)
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(CoverageCorruptionError):
            self.coverage.current()

    def test_forged_stored_payload_is_corruption(self) -> None:
        import sqlite3

        corpus_ref, plan_ref = self._setup()
        revision = self.record(corpus_ref, plan_ref, request_id="coverage-1")
        conn = sqlite3.connect(paths.store_path_for(str(self.tmp / "project")))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='revision_no_update'"
            ).fetchone()
            saved = row[0] if row and row[0] else None
            conn.execute("DROP TRIGGER IF EXISTS revision_no_update")
            conn.execute(
                "UPDATE revision SET content = ? WHERE project_id = ? AND artifact_id = ? AND revision_id = ?",
                (b"not-the-original", self.project_id, revision.artifact_id, revision.revision_id),
            )
            if saved:
                conn.execute(saved)
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(CoverageCorruptionError):
            self.coverage.current()


# --------------------------------------------------------------------------- #
# R1-R1 repair: trustworthy reusable historical report snapshots
# --------------------------------------------------------------------------- #
class HistorySnapshotRepairTests(ReportingTestBase):
    def _setup(self):
        a = self.observe("req_a", self.pubmed_run((self.rec(pmid="1", source_id="a"),)))
        revision = self.ingest_corpus([a.run_id], request_id="corpus-1")
        corpus_ref = self.ref(revision.artifact_id, revision.revision_id)
        plan_ref = self.build_plan()
        return corpus_ref, plan_ref

    def _forge(self, revision, mutate) -> None:
        """Rewrite a stored revision's envelope with a recomputed content hash."""

        import hashlib
        import sqlite3

        content = self.artifacts.read_content(revision.artifact_id, revision.revision_id)
        envelope = json.loads(content.decode("utf-8"))
        mutate(envelope)
        forged = canonical_json(envelope)
        conn = sqlite3.connect(paths.store_path_for(str(self.tmp / "project")))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='revision_no_update'"
            ).fetchone()
            saved = row[0] if row and row[0] else None
            conn.execute("DROP TRIGGER IF EXISTS revision_no_update")
            conn.execute(
                "UPDATE revision SET content = ?, content_sha256 = ?, byte_size = ? "
                "WHERE project_id = ? AND artifact_id = ? AND revision_id = ?",
                (
                    forged,
                    "sha256:" + hashlib.sha256(forged).hexdigest(),
                    len(forged),
                    self.project_id,
                    revision.artifact_id,
                    revision.revision_id,
                ),
            )
            if saved:
                conn.execute(saved)
            conn.commit()
        finally:
            conn.close()

    def _get(self, revision):
        return self.coverage.get(
            {
                "project_id": self.project_id,
                "artifact_id": revision.artifact_id,
                "revision_id": revision.revision_id,
            }
        )

    # -- defect 1: multiple report revisions under the default artifact ------ #
    def test_multiple_report_revisions_with_exact_parent(self) -> None:
        corpus_ref, plan_ref = self._setup()
        first = self.record(corpus_ref, plan_ref, request_id="coverage-1")
        b = self.observe("req_b", self.pubmed_run((self.rec(pmid="2", source_id="b"),)))
        second_corpus = self.ingest_corpus([b.run_id], request_id="corpus-2")
        second_ref = self.ref(second_corpus.artifact_id, second_corpus.revision_id)

        second = self.record(second_ref, plan_ref, request_id="coverage-2")
        self.assertNotEqual(second.revision_id, first.revision_id)
        self.assertEqual(second.parent_revision_id, first.revision_id)
        # Both revisions remain independently readable.
        self.assertEqual(self._get(first)["payload"]["corpus"]["ref"], corpus_ref)
        self.assertEqual(self._get(second)["payload"]["corpus"]["ref"], second_ref)

    # -- defect 2: replay compares the logical input binding, not work ------- #
    def _fixed_record(self, corpus_ref, plan_ref, **kwargs):
        rid = kwargs.pop("revision_id", None) or CoverageRepository.new_revision_id()
        artifact_id = kwargs.pop("artifact_id", DEFAULT_COVERAGE_ARTIFACT_ID)
        return self.coverage.record_report(
            corpus_ref,
            plan_ref=plan_ref,
            request_id=kwargs.pop("request_id", "coverage-1"),
            rights=self.rights(artifact_id, rid),
            revision_id=rid,
            artifact_id=artifact_id,
            **kwargs,
        )

    def test_replay_stable_after_new_work_and_cost(self) -> None:
        corpus_ref, plan_ref = self._setup()
        first = self._fixed_record(corpus_ref, plan_ref)
        before = self._get(first)
        content_before = self.artifacts.read_content(first.artifact_id, first.revision_id)
        # Unrelated mutable project state grows after the report.
        self.observe("req_late", self.pubmed_run((self.rec(pmid="99", source_id="late"),)))
        jobs = JobRepository(self.store)
        job = jobs.submit("job-late", task_kind="synthetic_probe", parameters={"n": 1})
        jobs.begin_attempt(job.job_id, dispatch_kind="external_synthetic", stage="s1", reserve_usd="0.30")

        again = self._fixed_record(corpus_ref, plan_ref)
        self.assertEqual(again.revision_id, first.revision_id)
        self.assertEqual(
            self.artifacts.read_content(first.artifact_id, first.revision_id), content_before
        )
        self.assertEqual(self._get(first), before)

    def test_replay_after_new_corpus_head(self) -> None:
        corpus_ref, plan_ref = self._setup()
        first = self._fixed_record(corpus_ref, plan_ref)
        # A new corpus revision moves the corpus artifact's current head.
        b = self.observe("req_b", self.pubmed_run((self.rec(pmid="2", source_id="b"),)))
        self.ingest_corpus([b.run_id], request_id="corpus-2")
        again = self._fixed_record(corpus_ref, plan_ref)
        self.assertEqual(again.revision_id, first.revision_id)

    def test_divergent_logical_binding_same_key_conflicts(self) -> None:
        corpus_ref, plan_ref = self._setup()
        self._fixed_record(corpus_ref, plan_ref)
        with self.assertRaises(CoverageConflictError):
            self._fixed_record(
                corpus_ref, plan_ref,
                known_relevant=[{"namespace": "pmid", "value": "1"}],
            )

    # -- defect 3: stored read rederives logical sections --------------------- #
    def test_forged_corpus_counts_and_totals_together_is_corruption(self) -> None:
        corpus_ref, plan_ref = self._setup()
        revision = self.record(corpus_ref, plan_ref, request_id="coverage-1")

        def mutate(envelope):
            payload = envelope["payload"]
            payload["corpus"]["counts"]["candidates"] += 1
            payload["totals"]["candidates"] += 1

        self._forge(revision, mutate)
        with self.assertRaises(CoverageCorruptionError):
            self.coverage.current()

    def test_forged_plan_boundaries_same_digest_is_corruption(self) -> None:
        corpus_ref, plan_ref = self._setup()
        revision = self.record(corpus_ref, plan_ref, request_id="coverage-1")

        def mutate(envelope):
            envelope["payload"]["plan"]["boundaries"]["expansion"]["max_hops"] = 99

        self._forge(revision, mutate)
        with self.assertRaises(CoverageCorruptionError):
            self.coverage.current()

    def test_forged_known_matching_together_is_corruption(self) -> None:
        corpus_ref, plan_ref = self._setup()
        revision = self.record(
            corpus_ref, plan_ref, request_id="coverage-1",
            known_relevant=[{"namespace": "pmid", "value": "1"}],
        )

        def mutate(envelope):
            token = envelope["payload"]["known_relevant"]["tokens"][0]
            token["present"] = False
            token["candidate_matches"] = []
            token["occurrence_count"] = 0
            envelope["payload"]["known_relevant"]["matched"] = 0
            envelope["payload"]["known_relevant"]["missing"] = 1

        self._forge(revision, mutate)
        with self.assertRaises(CoverageCorruptionError):
            self.coverage.current()

    def test_forged_known_fake_candidate_id_is_corruption(self) -> None:
        corpus_ref, plan_ref = self._setup()
        revision = self.record(
            corpus_ref, plan_ref, request_id="coverage-1",
            known_relevant=[{"namespace": "pmid", "value": "1"}],
        )

        def mutate(envelope):
            token = envelope["payload"]["known_relevant"]["tokens"][0]
            token["candidate_matches"] = ["cand_forged"]

        self._forge(revision, mutate)
        with self.assertRaises(CoverageCorruptionError):
            self.coverage.current()

    def test_removed_automatic_gap_is_corruption(self) -> None:
        # A corpus with a partial failure guarantees an automatic limitation line.
        ok_page = fx.page(records=(self.rec(pmid="5", source_id="5"),))
        failed_page = fx.page(
            page_index=1, records=(), total_count=1, complete=False,
            stop_reason="failure", failure="http_status",
            attempts=(fx.attempt(attempt_no=2, status=503, outcome="retryable"),),
        )
        partial = fx.result((ok_page, failed_page), stop_reason="failure", failure="http_status")
        a = self.observe("req_a", partial)
        revision_corpus = self.ingest_corpus([a.run_id], request_id="corpus-1")
        corpus_ref = self.ref(revision_corpus.artifact_id, revision_corpus.revision_id)
        plan_ref = self.build_plan()
        revision = self.record(corpus_ref, plan_ref, request_id="coverage-1")

        def mutate(envelope):
            summary = envelope["payload"]["limitations_summary"]
            envelope["payload"]["limitations_summary"] = [
                line for line in summary if "page limit" not in line
            ]

        self._forge(revision, mutate)
        with self.assertRaises(CoverageCorruptionError):
            self.coverage.current()

    # -- defect 4: each stored work row is revalidated against its run -------- #
    def test_forged_work_row_with_consistent_totals_is_corruption(self) -> None:
        corpus_ref, plan_ref = self._setup()
        revision = self.record(corpus_ref, plan_ref, request_id="coverage-1")

        def mutate(envelope):
            work = envelope["payload"]["work"]
            work["observations"][0]["attempt_count"] += 1
            work["totals"]["http_attempts"] += 1

        self._forge(revision, mutate)
        with self.assertRaises(CoverageCorruptionError):
            self.coverage.current()

    # -- defect 5: report-time monetary snapshot, immutable identity pins ----- #
    def _make_task(self):
        runtime = RuntimeRepository(self.store)
        artifact = self.artifacts.create_artifact("a-dispatch-src2", kind="document")
        self.artifacts.import_revision(
            artifact_id=artifact.artifact_id,
            content=b"dispatch-source-bytes-2",
            origin_locator="import:synthetic",
            rights=self.rights(artifact.artifact_id, "rev_d2"),
            revision_id="rev_d2",
        )
        config = RoleConfiguration.from_mapping(
            {
                "schema_version": 1,
                "roles": {
                    "writer": {
                        "provider_id": "openrouter",
                        "model_id": "anthropic/claude-sonnet-5-5",
                        "variant": "high",
                        "steps": 8,
                    }
                },
            }
        )
        mapping = {
            "project_id": self.project_id,
            "role": "writer",
            "target": {
                "project_id": self.project_id,
                "artifact_id": artifact.artifact_id,
                "revision_id": "rev_d2",
            },
            "input_refs": [],
            "scope": ["section:introduction"],
            "prompt_version": "writer.v1",
            "size_limit": 4096,
            "step_limit": 4,
        }
        bounded = BoundedTask.from_mapping(mapping, configuration=config)
        return runtime.record_task(bounded, request_id="task-req2", configuration=config)

    def test_settlement_after_report_does_not_invalidate(self) -> None:
        corpus_ref, plan_ref = self._setup()
        dispatch = RuntimeDispatchRepository(self.store)
        reserved = dispatch.reserve_dispatch(
            self._make_task(), request_id="call-1", route="direct_call", stage="s1",
            input_digest=_DIG_A, version_digest=_DIG_B, reserve_usd="0.20",
            bound_evidence_digest=_DIG_C,
        )
        dispatch.settle_cost(reserved.dispatch_id, cost_state="unknown")
        revision = self.record(corpus_ref, plan_ref, request_id="coverage-1")
        before = self._get(revision)
        # A later legitimate settlement must not invalidate the frozen report.
        dispatch.settle_cost(reserved.dispatch_id, cost_state="known", amount_usd="0.05")
        self.assertEqual(self._get(revision), before)

    def test_forged_immutable_pin_is_corruption(self) -> None:
        corpus_ref, plan_ref = self._setup()
        dispatch = RuntimeDispatchRepository(self.store)
        dispatch.reserve_dispatch(
            self._make_task(), request_id="call-1", route="direct_call", stage="s1",
            input_digest=_DIG_A, version_digest=_DIG_B, reserve_usd="0.20",
            bound_evidence_digest=_DIG_C,
        )
        revision = self.record(corpus_ref, plan_ref, request_id="coverage-1")

        def mutate(envelope):
            envelope["payload"]["monetary"]["pins"][0]["dispatch_id"] = "disp_missing"

        self._forge(revision, mutate)
        with self.assertRaises(CoverageCorruptionError):
            self.coverage.current()

    def test_forged_pin_route_is_corruption(self) -> None:
        corpus_ref, plan_ref = self._setup()
        dispatch = RuntimeDispatchRepository(self.store)
        dispatch.reserve_dispatch(
            self._make_task(), request_id="call-1", route="direct_call", stage="s1",
            input_digest=_DIG_A, version_digest=_DIG_B, reserve_usd="0.20",
            bound_evidence_digest=_DIG_C,
        )
        revision = self.record(corpus_ref, plan_ref, request_id="coverage-1")

        def mutate(envelope):
            monetary = envelope["payload"]["monetary"]
            direct = monetary["runtime_dispatch"]["direct_call"]
            empty = {key: (0 if isinstance(value, int) else "0") for key, value in direct.items()}
            for key, value in direct.items():
                monetary["runtime_dispatch"]["opencode_session"][key] = value
            monetary["runtime_dispatch"]["direct_call"] = empty
            monetary["pins"][0]["route"] = "opencode_session"

        self._forge(revision, mutate)
        with self.assertRaises(CoverageCorruptionError):
            self.coverage.current()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
