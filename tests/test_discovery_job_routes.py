"""Owner-controlled discovery background-job **routes** (Step 05, J1).

Extends the J0 ``discovery_submit`` operation
(``src/review_article_agent/worker/discovery.py`` + the narrow
``worker/engine.py`` hooks) with two further modes:

* ``expand`` — a bounded bidirectional citation traversal whose only roots are
  **observed** candidate identities of the exact current, validated corpus and
  whose only limits are the approved plan's ``payload.expansion`` (never a caller
  value); the full verified :class:`ExpansionReport.to_mapping` is retained in the
  ``JobRepository`` accepted result while the status projection shows only a
  bounded summary.
* ``lead`` — one scoped hosted lead-proposal request through the existing
  revision-scoped runner, reading the credential from the single named
  environment variable at dispatch, reserving on the runtime dispatch ledger
  before the one POST, and never re-POSTing an unknown outcome.

Everything runs against a real ``Registry``/``ProjectStore`` with an injected
deterministic low-level transport and injected owner-auth seams: no test performs
a network, provider, model, upload, or paid call.

Run::

    PYTHONPATH=src python3 -m unittest tests.test_discovery_job_routes
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from _tiers import slow

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from review_article_agent.discovery import execution as ex  # noqa: E402
from review_article_agent.discovery import lead_runner as runner  # noqa: E402
from review_article_agent.discovery.candidates import (  # noqa: E402
    DEFAULT_CORPUS_ARTIFACT_ID,
    CandidateRepository,
)
from review_article_agent.discovery.records import SOURCE_EUROPEPMC  # noqa: E402
from review_article_agent.discovery.transport import TransportResponse  # noqa: E402
from review_article_agent.runtime import owner_auth  # noqa: E402
from review_article_agent.runtime.dispatch import (  # noqa: E402
    RuntimeDispatchRepository,
    runtime_dispatch_remote_state,
)
from review_article_agent.runtime.gates import prepare_gate_snapshot  # noqa: E402
from review_article_agent.runtime.jobs import JobRepository  # noqa: E402
from review_article_agent.runtime.lead import (  # noqa: E402
    LeadConfig,
)
from review_article_agent.runtime.lead import TransportResponse as LeadTransportResponse  # noqa: E402
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
import test_discovery_serialization as fx  # noqa: E402

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")
_BASE_TIME = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
_FIXED_CHALLENGE = "TEST-CHAL-DISC-J1"
_OWNER_UID = 1000
_MODEL_ID = "anthropic/claude-sonnet-5-5"
_EVIDENCE = {"ref": "evidence:synthetic", "verified_at": "2026-10-28T00:00:00Z"}


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


def _lead_rights(project_id, artifact_id, revision_id):
    """A source-rights record that permits a hosted transfer (with authorization)."""
    record = _rights(project_id, artifact_id, revision_id)
    subject = record["subject"]
    for op in _OPS:
        record["basis"][op] = {
            "kind": "license",
            "evidence": {"ref": "lic:syn", "provenance": "publisher:syn"},
        }
    record["authorization"] = {
        "actor": "user",
        "method": "out_of_band_authenticated",
        "authorized_at": "2026-10-28T00:00:00Z",
        "against_subject": dict(subject),
        "against_rights_revision": 1,
        "basis": {
            "kind": "license",
            "evidence": {"ref": "lic:syn", "provenance": "publisher:syn"},
        },
    }
    return record


def _config_map(**overrides):
    config = {
        "model_id": _MODEL_ID,
        "upstream_provider": "anthropic",
        "max_output_tokens": 1024,
        "context_tokens": 128000,
        "input_price_per_million": "0.5",
        "output_price_per_million": "1.5",
        "pricing_ref": "pricing:synthetic:v1",
        "instructions_revision": "lead.instr.v1",
        "timeout_seconds": 60.0,
        "max_response_bytes": 1048576,
    }
    config.update(overrides)
    return LeadConfig.from_mapping(config).to_mapping()


def _hosted_use_document(purpose, source_refs, config=None):
    return {
        "schema_version": 1,
        "document_type": "discovery_hosted_use",
        "payload": {
            "purpose": purpose,
            "config": config or _config_map(),
            "source_refs": source_refs,
            "provider_terms": dict(_EVIDENCE),
            "privacy_review": dict(_EVIDENCE),
            "pricing_evidence": dict(_EVIDENCE),
        },
    }


def _brief_envelope():
    return {
        "schema_version": 1,
        "document_type": "discovery_brief",
        "payload": {
            "objective": "synthetic objective",
            "starting_hypothesis": "synthetic hypothesis",
            "questions": ["q1"],
        },
    }


def _completion_body(text="synthetic lead proposal", *, model=_MODEL_ID):
    return json.dumps(
        {
            "id": "gen-synthetic-1",
            "model": model,
            "provider": "anthropic",
            "choices": [
                {"finish_reason": "stop", "message": {"role": "assistant", "content": text}}
            ],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 8,
                "total_tokens": 20,
                "cost": 0.0002,
            },
        }
    )


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


def _ref_payload(hit_count, items):
    return {"version": "6.9", "hitCount": hit_count, "referenceList": {"reference": list(items)}}


def _cit_payload(hit_count, items):
    return {"version": "6.9", "hitCount": hit_count, "citationList": {"citation": list(items)}}


def _cite_entry(rec_id, source="MED", **extra):
    item = {"source": source, "id": rec_id}
    item.update(extra)
    return item


class _CitationProvider:
    """A deterministic low-level citation provider keyed by (source, id, relation)."""

    def __init__(self, script=None, *, on_call=None):
        self._script = {key: list(value) for key, value in (script or {}).items()}
        self.calls: list[str] = []
        self.on_call = on_call

    def __call__(self, url, *, timeout, max_response_bytes):
        self.calls.append(url)
        if self.on_call is not None:
            self.on_call(len(self.calls))
        parts = url.split("?")[0].rstrip("/").split("/")
        source, rec_id, relation = parts[-3], parts[-2], parts[-1]
        items = self._script.get((source, rec_id, relation))
        if not items:
            payload = _ref_payload(0, []) if relation == "references" else _cit_payload(0, [])
            return TransportResponse(status=200, body=json.dumps(payload).encode("utf-8"))
        item = items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    @staticmethod
    def response(payload):
        return TransportResponse(status=200, body=json.dumps(payload).encode("utf-8"))


class _LeadTransport:
    """A deterministic lead transport double (never touches the network)."""

    def __init__(self, responses=(), observer=None):
        self._responses = list(responses)
        self.calls: list[dict] = []
        self.observer = observer

    def __call__(self, body, api_key, *, endpoint, timeout, max_response_bytes):
        self.calls.append({"body": body, "api_key": api_key, "endpoint": endpoint})
        if self.observer is not None:
            self.observer()
        if not self._responses:
            raise RuntimeError("synthetic lead transport has no queued response")
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, str):
            return LeadTransportResponse(status=200, body_text=response)
        return response

    @property
    def call_count(self):
        return len(self.calls)


class DiscoveryJobRoutesBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-disc-j1-")
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
        self.discovery = DiscoveryRepository(self.store)

        patcher = mock.patch.object(ex, "_SLEEP", lambda _s: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- owner auth ---------------------------------------------------------- #
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

    # -- fixtures ------------------------------------------------------------ #
    def ref(self, artifact_id, revision_id):
        return {
            "project_id": self.project_id,
            "artifact_id": artifact_id,
            "revision_id": revision_id,
        }

    def _put(self, artifact_id, kind, revision_id, document, *, rights=None, dependencies=(), parent=None):
        self.artifact_repo.create_artifact(artifact_id, kind=kind)
        return self.artifact_repo.import_revision(
            artifact_id=artifact_id,
            content=document,
            origin_locator="import:synthetic",
            rights=rights or _rights(self.project_id, artifact_id, revision_id),
            revision_id=revision_id,
            dependencies=list(dependencies),
            parent_revision_id=parent,
        )

    @staticmethod
    def _env(document_type, payload):
        return {"schema_version": 1, "document_type": document_type, "payload": payload}

    def build_plan(self, *, plan_artifact="a-plan", plan_revision="rev-plan", expansion=None):
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
            "queries": [
                {"source": "europepmc", "query": "asthma AND review", "page_size": 5, "max_pages": 1}
            ],
            "complementary_sources": [],
            "expansion": expansion or {
                "max_hops": 2, "max_sources": 10, "max_requests": 10,
                "page_size": 20, "max_pages": 1,
            },
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

    def observe_epmc(self, records, *, request_id="run-epmc"):
        page = fx.page(
            source=SOURCE_EUROPEPMC,
            records=tuple(records),
            page_size=100,
            total_count=len(records),
        )
        result = fx.result(source=SOURCE_EUROPEPMC, pages=(page,))
        return self.discovery.record_search(request_id=request_id, result=result)

    def ingest_corpus(self, run_ids, *, request_id="corpus-1"):
        revision_id = CandidateRepository.new_revision_id()
        rights = _rights(self.project_id, DEFAULT_CORPUS_ARTIFACT_ID, revision_id)
        CandidateRepository(self.store).ingest(
            run_ids, request_id=request_id, rights=rights, revision_id=revision_id
        )
        return self.ref(DEFAULT_CORPUS_ARTIFACT_ID, revision_id)

    def build_lead_batch(self, *, source_artifact="a-src", source_revision="rev-src"):
        self._put(
            source_artifact, "report", source_revision, _brief_envelope(),
            rights=_lead_rights(self.project_id, source_artifact, source_revision),
        )
        source_ref = self.ref(source_artifact, source_revision)
        batch_artifact, batch_revision = "a-batch-int", "rev-batch-int"
        document = _hosted_use_document("interview", [source_ref])
        self._put(
            batch_artifact, "report", batch_revision, document,
            rights=_lead_rights(self.project_id, batch_artifact, batch_revision),
            dependencies=[source_ref],
        )
        return self.ref(batch_artifact, batch_revision)

    def approve_batch(self, batch_ref, purpose="interview"):
        return self.approve(
            batch_ref["artifact_id"],
            batch_ref["revision_id"],
            runner.HOSTED_USE_GATE_KIND,
            runner.hosted_use_gate_id(purpose),
        )

    # -- engine helpers ------------------------------------------------------ #
    def engine(self, transport=None, *, lead_transport=None, lead_api_key=None,
               registry=None, data_dir=None):
        engine = WorkerEngine(
            str(data_dir or self.data_dir),
            registry=registry or self.registry,
            discovery_transport=transport,
            lead_transport=lead_transport,
            lead_api_key=lead_api_key,
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


# --------------------------------------------------------------------------- #
# Strict submission validation for the new modes
# --------------------------------------------------------------------------- #
class NewModeValidationTest(DiscoveryJobRoutesBase):
    def test_expand_and_lead_parameter_keys_are_exact(self) -> None:
        engine = self.engine()
        plan_ref = self.build_plan()
        self.approve_plan(plan_ref)
        corpus_ref = self.ingest_corpus([self.observe_epmc(
            [fx.record(source=SOURCE_EUROPEPMC, source_id="MED:1", pmid="1", pmcid=None, doi=None)]
        ).run_id])

        # A caller-supplied limits override is refused before any write.
        injected = self._submit(
            engine, request_id="x-limits", mode="expand",
            parameters={"plan_ref": plan_ref, "corpus_ref": corpus_ref,
                        "roots": [{"source": "MED", "id": "1"}],
                        "limits": {"max_requests": 999}},
        )
        self.assertFalse(injected["ok"])
        self.assertEqual(injected["code"], "bad_value")

        # An unsafe root is refused purely (no store access needed).
        unsafe = self._submit(
            engine, request_id="x-unsafe", mode="expand",
            parameters={"plan_ref": plan_ref, "corpus_ref": corpus_ref,
                        "roots": [{"source": "MED", "id": "a b"}]},
        )
        self.assertEqual(unsafe["code"], "bad_value")

        # A full URL / traversal root is refused.
        traversal = self._submit(
            engine, request_id="x-trav", mode="expand",
            parameters={"plan_ref": plan_ref, "corpus_ref": corpus_ref,
                        "roots": [{"source": "MED", "id": "..%2f"}]},
        )
        self.assertEqual(traversal["code"], "bad_value")

        # credential_env must be the one fixed, approved name.
        bad_env = self._submit(
            engine, request_id="lead-env", mode="lead",
            parameters={"batch_ref": plan_ref, "credential_env": "OTHER_KEY"},
        )
        self.assertEqual(bad_env["code"], "bad_value")

        with engine.open_repository(self.project_id) as repo:
            self.assertEqual(repo.list(), [])


# --------------------------------------------------------------------------- #
# Expansion route
# --------------------------------------------------------------------------- #
class ExpandRoutesTest(DiscoveryJobRoutesBase):
    def _observed_corpus(self):
        run = self.observe_epmc(
            [fx.record(source=SOURCE_EUROPEPMC, source_id="MED:1", pmid="1", pmcid=None, doi=None)]
        )
        return self.ingest_corpus([run.run_id])

    def test_unapproved_plan_zero_http_and_escalates(self) -> None:
        fake = _CitationProvider()
        engine = self.engine(fake)
        plan_ref = self.build_plan()  # never approved
        corpus_ref = self._observed_corpus()
        res = self._submit(
            engine, request_id="exp-unapproved", mode="expand",
            parameters={"plan_ref": plan_ref, "corpus_ref": corpus_ref,
                        "roots": [{"source": "MED", "id": "1"}]},
        )
        self.assertTrue(res["ok"])
        job_id = res["job"]["job_id"]
        self._drive(engine)
        self.assertEqual(fake.calls, [])
        # No expansion relation run was recorded (only the corpus ingest run exists).
        self.assertEqual(
            [r for r in self.discovery.list_searches() if r.request_id.startswith("dsc_")],
            [],
        )
        self.assertIn(self._job(engine, job_id)["state"], {"waiting_human"})

    def test_out_of_root_zero_http_and_escalates(self) -> None:
        fake = _CitationProvider()
        engine = self.engine(fake)
        plan_ref = self.build_plan()
        self.approve_plan(plan_ref)
        corpus_ref = self._observed_corpus()
        res = self._submit(
            engine, request_id="exp-unobserved", mode="expand",
            parameters={"plan_ref": plan_ref, "corpus_ref": corpus_ref,
                        "roots": [{"source": "MED", "id": "999999"}]},
        )
        self.assertTrue(res["ok"])
        job_id = res["job"]["job_id"]
        self._drive(engine)
        self.assertEqual(fake.calls, [])
        self.assertIn(self._job(engine, job_id)["state"], {"waiting_human"})

    def test_expand_bidirectional_lifecycle_and_summary(self) -> None:
        provider = _CitationProvider({
            ("MED", "1", "references"): [
                _CitationProvider.response(_ref_payload(2, [_cite_entry("2"), _cite_entry("3")]))
            ],
            ("MED", "1", "citations"): [
                _CitationProvider.response(_cit_payload(1, [_cite_entry("4")]))
            ],
            ("MED", "2", "references"): [
                _CitationProvider.response(_ref_payload(1, [_cite_entry("1")]))  # cycle
            ],
            ("MED", "2", "citations"): [
                _CitationProvider.response(_cit_payload(0, []))
            ],
            ("MED", "3", "references"): [
                _CitationProvider.response(_ref_payload(0, []))
            ],
            ("MED", "3", "citations"): [
                _CitationProvider.response(_cit_payload(0, []))
            ],
            ("MED", "4", "references"): [
                _CitationProvider.response(_ref_payload(0, []))
            ],
            ("MED", "4", "citations"): [
                _CitationProvider.response(_cit_payload(0, []))
            ],
        })
        engine = self.engine(provider)
        plan_ref = self.build_plan()
        self.approve_plan(plan_ref)
        corpus_ref = self._observed_corpus()
        res = self._submit(
            engine, request_id="exp-1", mode="expand",
            parameters={"plan_ref": plan_ref, "corpus_ref": corpus_ref,
                        "roots": [{"source": "MED", "id": "1"}]},
        )
        job_id = res["job"]["job_id"]
        self._drive(engine)

        job = self._job(engine, job_id)
        self.assertEqual(job["state"], "completed")
        accepted = job["accepted_result"]
        self.assertEqual(accepted["result_kind"], "discovery_expansion")
        summary = accepted["result"]
        # The status projection is a bounded summary, never the full graph.
        self.assertNotIn("edges", summary)
        self.assertNotIn("relations", summary)
        self.assertEqual(summary["mode"], "expand")
        self.assertEqual(summary["roots_count"], 1)
        self.assertEqual(summary["guarantees"],
                         {"completeness_claimed": False, "verified_support": False})
        self.assertEqual(summary["counts"]["nodes_visited"], 4)
        self.assertEqual(len(summary["report_sha256"]), 64)
        self.assertIn("empty_relation", summary["stop_reasons"])

        # The **full** verified mapping is retained in the job repository and is
        # readable through the explicit helper (never via minimal status).
        with engine.open_store(self.project_id) as store:
            full = discovery_jobs.persisted_expansion_report(store, request_id="exp-1")
        self.assertIsNotNone(full)
        self.assertEqual(full["guarantees"],
                         {"completeness_claimed": False, "verified_support": False})
        self.assertTrue(full["relations"])
        self.assertTrue(full["edges"])
        self.assertEqual(full["limits"]["max_requests"], 10)
        # Both directions were traversed.
        self.assertEqual({r["direction"] for r in full["relations"]}, {"references", "citations"})

    def test_expand_limits_come_only_from_plan(self) -> None:
        # A plan with max_requests=1 allows exactly one low-level request.
        provider = _CitationProvider({
            ("MED", "1", "references"): [
                _CitationProvider.response(_ref_payload(2, [_cite_entry("2"), _cite_entry("3")]))
            ],
        })
        engine = self.engine(provider)
        plan_ref = self.build_plan(expansion={
            "max_hops": 3, "max_sources": 10, "max_requests": 1,
            "page_size": 20, "max_pages": 1,
        })
        self.approve_plan(plan_ref)
        corpus_ref = self._observed_corpus()
        res = self._submit(
            engine, request_id="exp-limits", mode="expand",
            parameters={"plan_ref": plan_ref, "corpus_ref": corpus_ref,
                        "roots": [{"source": "MED", "id": "1"}]},
        )
        job_id = res["job"]["job_id"]
        self._drive(engine)
        self.assertEqual(len(provider.calls), 1)
        job = self._job(engine, job_id)
        self.assertEqual(job["state"], "completed")
        self.assertIn("request_limit", job["accepted_result"]["result"]["stop_reasons"])

    def test_post_start_plan_change_stops_further_requests(self) -> None:
        first_called, release = threading.Event(), threading.Event()

        def on_call(n: int) -> None:
            if n == 1:
                first_called.set()
                release.wait(timeout=10.0)

        provider = _CitationProvider({
            ("MED", "1", "references"): [
                _CitationProvider.response(_ref_payload(1, [_cite_entry("2")]))
            ],
        }, on_call=on_call)
        engine = self.engine(provider)
        plan_ref = self.build_plan()
        self.approve_plan(plan_ref)
        corpus_ref = self._observed_corpus()
        res = self._submit(
            engine, request_id="exp-change", mode="expand",
            parameters={"plan_ref": plan_ref, "corpus_ref": corpus_ref,
                        "roots": [{"source": "MED", "id": "1"}]},
        )
        job_id = res["job"]["job_id"]
        engine.tick()
        try:
            self.assertTrue(first_called.wait(timeout=10.0))
            # Amend a pinned dependency so the approved plan becomes stale while
            # the first provider call is in flight.
            self._put("a-brief", "report", "rev-brief2", self._env("discovery_brief", {
                "objective": "revised", "starting_hypothesis": "h", "questions": [],
            }), parent="rev-brief")
        finally:
            release.set()
        self._drive(engine)
        # The second relation (citations for MED:1) was refused before any request.
        self.assertEqual(len(provider.calls), 1)
        job = self._job(engine, job_id)
        self.assertEqual(job["state"], "completed")
        self.assertIn("before_request_refused",
                      job["accepted_result"]["result"]["stop_reasons"])

    def test_replay_and_reopen_perform_no_new_io(self) -> None:
        provider = _CitationProvider({
            ("MED", "1", "references"): [_CitationProvider.response(_ref_payload(0, []))],
        })
        engine = self.engine(provider)
        plan_ref = self.build_plan()
        self.approve_plan(plan_ref)
        corpus_ref = self._observed_corpus()
        first = self._submit(
            engine, request_id="exp-replay", mode="expand",
            parameters={"plan_ref": plan_ref, "corpus_ref": corpus_ref,
                        "roots": [{"source": "MED", "id": "1"}]},
        )
        job_id = first["job"]["job_id"]
        self._drive(engine)
        calls = len(provider.calls)

        # Same request id + identical parameters: same job; replay sends nothing.
        second = self._submit(
            engine, request_id="exp-replay", mode="expand",
            parameters={"plan_ref": plan_ref, "corpus_ref": corpus_ref,
                        "roots": [{"source": "MED", "id": "1"}]},
        )
        self.assertTrue(second["ok"])
        self.assertEqual(second["job"]["job_id"], job_id)
        self._drive(engine)
        self.assertEqual(len(provider.calls), calls)

        # Divergent parameters under the same request id are a conflict.
        divergent = self._submit(
            engine, request_id="exp-replay", mode="expand",
            parameters={"plan_ref": plan_ref, "corpus_ref": corpus_ref,
                        "roots": [{"source": "MED", "id": "2"}]},
        )
        self.assertEqual(divergent["code"], "conflict")

        # A fresh engine over the same store (recovery/reopen) issues no request.
        exploding = _CitationProvider()
        engine.close()
        engine2 = self.engine(exploding, registry=Registry(self.data_dir))
        engine2.reconcile_startup()
        self._drive(engine2)
        self.assertEqual(exploding.calls, [])
        self.assertEqual(self._job(engine2, job_id)["state"], "completed")

    def test_restart_with_missing_remainder_escalates_never_completes(self) -> None:
        # Persist only the root's relations, then crash before the children: a
        # restart cannot reconstruct the full traversal and must escalate.
        plan_ref = self.build_plan()
        self.approve_plan(plan_ref)
        corpus_ref = self._observed_corpus()
        engine = self.engine(_CitationProvider())
        res = self._submit(
            engine, request_id="exp-partial", mode="expand",
            parameters={"plan_ref": plan_ref, "corpus_ref": corpus_ref,
                        "roots": [{"source": "MED", "id": "1"}]},
        )
        job_id = res["job"]["job_id"]
        engine.close()  # no future was ever started

        token = self.registry.activation().activation_token
        store = ProjectStore.open_at(
            paths.app_dir_for(str(self.root)), expected_project_id=self.project_id
        )

        def transport(url, *, timeout, max_response_bytes):
            parts = url.split("?")[0].rstrip("/").split("/")
            if parts[-2] == "1":
                payload = _ref_payload(1, [_cite_entry("2")]) if parts[-1] == "references" \
                    else _cit_payload(0, [])
                return TransportResponse(status=200, body=json.dumps(payload).encode("utf-8"))
            raise RuntimeError("crash before the child relation was persisted")

        try:
            repo = JobRepository(store)
            job = repo.get(job_id)
            attempt = repo.begin_attempt(job_id, dispatch_kind="local")
            repo.record_checkpoint(
                attempt.attempt_id, discovery_jobs.predispatch_checkpoint("expand")
            )
            with self.assertRaises(RuntimeError):
                discovery_jobs.dispatch_expand(store, self.registry, token, job, transport)
        finally:
            store.close()

        exploding = _CitationProvider()
        engine2 = self.engine(exploding, registry=Registry(self.data_dir))
        engine2.reconcile_startup()
        self._drive(engine2)
        self.assertEqual(exploding.calls, [])
        job2 = self._job(engine2, job_id)
        self.assertEqual(job2["state"], "waiting_human")
        self.assertNotEqual(job2["state"], "completed")
        self.assertIsNone(job2.get("accepted_result"))

    def test_running_expand_blocks_another_project(self) -> None:
        first_called, release = threading.Event(), threading.Event()

        def on_call(n: int) -> None:
            if n == 1:
                first_called.set()
                release.wait(timeout=10.0)

        provider = _CitationProvider({
            ("MED", "1", "references"): [_CitationProvider.response(_ref_payload(0, []))],
        }, on_call=on_call)
        engine = self.engine(provider)
        plan_ref = self.build_plan()
        self.approve_plan(plan_ref)
        corpus_ref = self._observed_corpus()
        res = self._submit(
            engine, request_id="exp-slot", mode="expand",
            parameters={"plan_ref": plan_ref, "corpus_ref": corpus_ref,
                        "roots": [{"source": "MED", "id": "1"}]},
        )
        try:
            engine.tick()
            self.assertTrue(first_called.wait(timeout=10.0))
            blocked = engine.handle(
                _req("submit", project_id=self.other_id, request_id="b-1",
                     task_kind="synthetic_numeric", steps=1, delay_ms=0,
                     outcome="complete", stage="s", reserve_usd="0.10")
            )
            self.assertFalse(blocked["ok"])
            self.assertEqual(blocked["code"], "switch_blocked")
        finally:
            release.set()
        self._drive(engine)
        self.assertEqual(self._job(engine, res["job"]["job_id"])["state"], "completed")

    @slow
    def test_large_graph_status_is_summary_only(self) -> None:
        # Every visited node contributes 2 relations; the status projection must
        # stay bounded and never include the edge/relation graph.
        children = [str(1000 + i) for i in range(40)]
        script = {
            ("MED", "1", "references"): [
                _CitationProvider.response(_ref_payload(len(children), [_cite_entry(c) for c in children]))
            ],
            ("MED", "1", "citations"): [_CitationProvider.response(_cit_payload(0, []))],
        }
        for child in children:
            script[("MED", child, "references")] = [
                _CitationProvider.response(_ref_payload(0, []))
            ]
            script[("MED", child, "citations")] = [
                _CitationProvider.response(_cit_payload(0, []))
            ]
        provider = _CitationProvider(script)
        engine = self.engine(provider)
        plan_ref = self.build_plan(expansion={
            "max_hops": 3, "max_sources": 100, "max_requests": 200,
            "page_size": 100, "max_pages": 1,
        })
        self.approve_plan(plan_ref)
        corpus_ref = self._observed_corpus()
        res = self._submit(
            engine, request_id="exp-large", mode="expand",
            parameters={"plan_ref": plan_ref, "corpus_ref": corpus_ref,
                        "roots": [{"source": "MED", "id": "1"}]},
        )
        job_id = res["job"]["job_id"]
        self._drive(engine)
        job = self._job(engine, job_id)
        summary = job["accepted_result"]["result"]
        self.assertEqual(summary["counts"]["edges"], 40)
        self.assertGreaterEqual(summary["counts"]["relations"], 41)
        # The status JSON is small (a bounded summary), not the full graph.
        self.assertLess(len(json.dumps(job)), 6000)
        self.assertNotIn("edges", summary)


# --------------------------------------------------------------------------- #
# Hosted lead route
# --------------------------------------------------------------------------- #
@slow
class LeadRoutesTest(DiscoveryJobRoutesBase):
    def _prepare(self, *, approved=True):
        batch_ref = self.build_lead_batch()
        if approved:
            self.approve_batch(batch_ref)
        return batch_ref

    def test_single_post_with_reserve_visible_before_send(self) -> None:
        seen = {}

        def observer():
            with ProjectStore.open_at(
                paths.app_dir_for(str(self.root)), expected_project_id=self.project_id
            ) as store:
                records = RuntimeDispatchRepository(store).list_dispatches()
            seen["count"] = len(records)
            seen["states"] = [r.state for r in records]
            seen["reserved"] = [r.reserve_usd for r in records]

        lead_transport = _LeadTransport([_completion_body()], observer=observer)
        engine = self.engine(lead_transport=lead_transport, lead_api_key="sk-or-test")
        batch_ref = self._prepare()
        res = self._submit(
            engine, request_id="lead-1", mode="lead",
            parameters={"batch_ref": batch_ref, "credential_env": "OPENROUTER_API_KEY"},
        )
        job_id = res["job"]["job_id"]
        self._drive(engine)

        self.assertEqual(lead_transport.call_count, 1)
        # The reserve + submitted dispatch existed **before** the single POST.
        self.assertEqual(seen["count"], 1)
        self.assertEqual(seen["states"], ["submitted"])
        self.assertIsNotNone(seen["reserved"][0])

        job = self._job(engine, job_id)
        self.assertEqual(job["state"], "completed")
        accepted = job["accepted_result"]
        self.assertEqual(accepted["result_kind"], "discovery_lead")
        result = accepted["result"]
        self.assertEqual(result["status"], "completed")
        self.assertIsNotNone(result["dispatch_id"])
        self.assertEqual(result["cost_state"], "known")
        self.assertEqual(result["usage"]["total_tokens"], 20)
        # No proposal text, no API key.
        self.assertNotIn("synthetic lead proposal", json.dumps(job))
        self.assertNotIn("sk-or-test", json.dumps(job))
        self.assertNotIn("proposal_text", result)

    def test_credential_not_persisted_and_read_from_named_env(self) -> None:
        lead_transport = _LeadTransport([_completion_body()])
        engine = self.engine(lead_transport=lead_transport)
        batch_ref = self._prepare()
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-env-secret"}):
            res = self._submit(
                engine, request_id="lead-env-1", mode="lead",
                parameters={"batch_ref": batch_ref, "credential_env": "OPENROUTER_API_KEY"},
            )
            job_id = res["job"]["job_id"]
            self._drive(engine)
        self.assertEqual(lead_transport.call_count, 1)
        # The key was passed to the transport but never persisted anywhere.
        self.assertEqual(lead_transport.calls[0]["api_key"], "sk-or-env-secret")
        job = self._job(engine, job_id)
        self.assertNotIn("sk-or-env-secret", json.dumps(job))
        with engine.open_store(self.project_id) as store:
            repo = JobRepository(store)
            accepted = repo.accepted_result(job_id)
            self.assertNotIn("sk-or-env-secret", json.dumps(accepted.result))

    def test_rejected_batch_and_missing_key_zero_post(self) -> None:
        lead_transport = _LeadTransport([_completion_body()])
        engine = self.engine(lead_transport=lead_transport, lead_api_key="sk-or-test")
        batch_ref = self._prepare(approved=False)  # gate never approved
        res = self._submit(
            engine, request_id="lead-unapproved", mode="lead",
            parameters={"batch_ref": batch_ref, "credential_env": "OPENROUTER_API_KEY"},
        )
        job_id = res["job"]["job_id"]
        self._drive(engine)
        self.assertEqual(lead_transport.call_count, 0)
        self.assertIn(self._job(engine, job_id)["state"], {"waiting_human"})

        # A missing credential is refused with zero POST, even on a valid batch.
        lead2 = _LeadTransport([_completion_body()])
        engine2 = self.engine(lead_transport=lead2)  # no key injected and none in env
        with mock.patch.dict(os.environ, {}, clear=True):
            batch_ref2 = self._prepare()
            res2 = self._submit(
                engine2, request_id="lead-nokey", mode="lead",
                parameters={"batch_ref": batch_ref2, "credential_env": "OPENROUTER_API_KEY"},
            )
            job_id2 = res2["job"]["job_id"]
            self._drive(engine2)
        self.assertEqual(lead2.call_count, 0)
        self.assertIn(self._job(engine2, job_id2)["state"], {"waiting_human"})

    def test_unknown_retains_occupancy_and_never_reposts(self) -> None:
        lead_transport = _LeadTransport([RuntimeError("ambiguous transport failure")])
        engine = self.engine(lead_transport=lead_transport, lead_api_key="sk-or-test")
        batch_ref = self._prepare()
        res = self._submit(
            engine, request_id="lead-unknown", mode="lead",
            parameters={"batch_ref": batch_ref, "credential_env": "OPENROUTER_API_KEY"},
        )
        job_id = res["job"]["job_id"]
        self._drive(engine)
        self.assertEqual(lead_transport.call_count, 1)

        job = self._job(engine, job_id)
        self.assertIn(job["state"], {"waiting_human", "reconciliation_required"})
        self.assertTrue(job["outcome_unresolved"])
        self.assertIsNone(job.get("accepted_result"))

        # The reserve and the runtime occupancy are retained.
        with engine.open_store(self.project_id) as store:
            state = runtime_dispatch_remote_state(store.connection, self.project_id)
            dispatch = RuntimeDispatchRepository(store).get_dispatch_by_request("lead-unknown")
        self.assertEqual(state, "remote")
        self.assertEqual(dispatch.state, "unknown")
        self.assertIsNotNone(dispatch.reserve_usd)

        # A restart never re-POSTs; the outcome is read back without a credential.
        replay_transport = _LeadTransport([_completion_body()])
        engine.close()
        engine2 = self.engine(lead_transport=replay_transport, registry=Registry(self.data_dir))
        engine2.reconcile_startup()
        self._drive(engine2)
        self.assertEqual(replay_transport.call_count, 0)
        self.assertIn(self._job(engine2, job_id)["state"],
                      {"waiting_human", "reconciliation_required"})

    def test_success_recovery_reads_back_without_credential(self) -> None:
        lead_transport = _LeadTransport([_completion_body()])
        engine = self.engine(lead_transport=lead_transport, lead_api_key="sk-or-test")
        batch_ref = self._prepare()
        res = self._submit(
            engine, request_id="lead-recover", mode="lead",
            parameters={"batch_ref": batch_ref, "credential_env": "OPENROUTER_API_KEY"},
        )
        job_id = res["job"]["job_id"]
        self._drive(engine)
        self.assertEqual(lead_transport.call_count, 1)

        replay_transport = _LeadTransport([_completion_body()])
        engine.close()
        engine2 = self.engine(lead_transport=replay_transport, registry=Registry(self.data_dir))
        engine2.reconcile_startup()
        self._drive(engine2)
        self.assertEqual(replay_transport.call_count, 0)
        job = self._job(engine2, job_id)
        self.assertEqual(job["state"], "completed")
        self.assertEqual(job["accepted_result"]["result"]["status"], "completed")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
