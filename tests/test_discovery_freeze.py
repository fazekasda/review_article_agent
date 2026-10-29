"""Corpus freeze / search-again decision tests (Step 05, W3).

Covers ``src/review_article_agent/discovery/freeze.py`` against Step 05 AC6
(human approval freezes a specific corpus revision; subsequent imports/searches
do not mutate it silently), AC5 (reporting never claims absolute completeness),
``POL-OPS-COVERAGE`` (the exact candidate revision is frozen by explicit human
approval; no universal numeric stop rule) and ``POL-P3`` (entry integrity
statuses with date/provenance; an unknown status is not clean; non-replication is
never conflated with retraction; historical discussion is not automatically
prohibited).

Every fixture is synthetic and uses a **real** disposable ``ProjectStore`` with
the **real** owner-capability approval flow (the controlling-terminal seam is
mocked exactly as the other human-gate tests do). No test performs a network,
provider, or model call.

Run::

    PYTHONPATH=src python3 -m unittest tests.test_discovery_freeze -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

import test_discovery_serialization as fx  # noqa: E402

from review_article_agent.discovery import context as ctx  # noqa: E402
from review_article_agent.discovery import freeze as freeze_mod  # noqa: E402
from review_article_agent.discovery.candidates import (  # noqa: E402
    DEFAULT_CORPUS_ARTIFACT_ID,
    CandidateRepository,
    CandidateValidationError,
)
from review_article_agent.discovery.freeze import (  # noqa: E402
    DEFAULT_FREEZE_ARTIFACT_ID,
    FREEZE_GATE_ID,
    FREEZE_GATE_KIND,
    FREEZE_NOTICE,
    INTEGRITY_FIELDS,
    FreezeConflictError,
    FreezeCorruptionError,
    FreezeNotAuthorizedError,
    FreezeRepository,
    FreezeValidationError,
    corpus_has_authentic_freeze,
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
from review_article_agent.storage.backup import (  # noqa: E402
    create_backup,
    restore_backup,
    verify_backup,
)
from review_article_agent.storage.discovery import DiscoveryRepository  # noqa: E402
from review_article_agent.storage.human_approvals import (  # noqa: E402
    HumanApprovalRepository,
)

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")
_BASE_TIME = datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc)
_FIXED_CHALLENGE = "TEST-CHAL-FREEZE"
_OWNER_UID = 1000
_CHECKED_AT = "2026-10-29T00:00:00+00:00"


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
        "recorded_at": "2026-10-29T00:00:00Z",
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


def _envelope(document_type, payload):
    return {"schema_version": 1, "document_type": document_type, "payload": payload}


class FreezeTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-freeze-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.project_id = paths.new_project_id()
        self.app_dir = paths.app_dir_for(str(self.tmp / "project"))
        self.store = ProjectStore.create_at(self.app_dir, self.project_id)
        self.addCleanup(self._close_store)
        self._bind(self.store)
        self.freeze = FreezeRepository(self.store)
        self._install_owner_auth()

    # -- harness ------------------------------------------------------------- #
    def _close_store(self) -> None:
        try:
            self.store.close()
        except Exception:  # pragma: no cover - defensive
            pass

    def _bind(self, store) -> None:
        self.artifacts = ArtifactRepository(store)
        self.discovery = DiscoveryRepository(store)
        self.corpus = CandidateRepository(store)
        self.coverage = CoverageRepository(store)

    def _install_owner_auth(self) -> None:
        patches = [
            mock.patch.object(owner_auth, "_new_challenge", lambda: _FIXED_CHALLENGE),
            mock.patch.object(owner_auth, "_current_owner_uid", lambda: _OWNER_UID),
            mock.patch.object(owner_auth, "_real_uid", lambda: _OWNER_UID),
            mock.patch.object(owner_auth, "_utcnow", lambda: _BASE_TIME),
            mock.patch.object(owner_auth, "_monotonic", lambda: 1000.0),
            mock.patch.object(
                owner_auth,
                "_open_controlling_tty",
                lambda: _FakeTty(replies=[_FIXED_CHALLENGE]),
            ),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _reopen(self) -> None:
        self.store.close()
        self.store = ProjectStore.open_at(
            self.app_dir, expected_project_id=self.project_id
        )
        self.addCleanup(self._close_store)
        self._bind(self.store)
        self.freeze = FreezeRepository(self.store)

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

    def rights(self, artifact_id, revision_id, **overrides) -> dict:
        return _rights(self.project_id, artifact_id, revision_id, **overrides)

    def ref(self, artifact_id, revision_id):
        return {
            "project_id": self.project_id,
            "artifact_id": artifact_id,
            "revision_id": revision_id,
        }

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
            self.store,
            self.ref(artifact_id, revision_id),
            gate_kind=gate_kind,
            gate_id=gate_id,
            decision=decision,
            rationale="synthetic human gate",
        )
        capability = owner_auth.request_owner_confirmation(prepared.owner_action())
        assert isinstance(capability, OwnerApproval)
        return HumanApprovalRepository(self.store).record_decision(prepared, capability)

    # -- approved plan ------------------------------------------------------- #
    def build_plan(self, *, queries=None, approve=True, seed_run=None):
        records = (self.rec(pmid="999", source_id="seed0"),)
        run = seed_run or self.observe("seed-run", self.pubmed_run(records))
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
        deps = [
            brief_ref,
            self.ref("a-interview", "rev-interview"),
            seed_ref,
            self.ref("a-framing", "rev-framing"),
            self.ref("a-scope", "rev-scope"),
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
                "queries": queries or [
                    {"source": "pubmed", "query": "cancer therapy", "page_size": 20, "max_pages": 2}
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

    # -- corpus + coverage --------------------------------------------------- #
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
            [a.run_id, b.run_id],
            request_id=request_id,
            rights=self.rights(artifact_id, rid),
            revision_id=rid,
            artifact_id=artifact_id,
        )
        self._corpus_run_ids = [a.run_id, b.run_id]
        return self.ref(rev.artifact_id, rev.revision_id), self.corpus.current()

    def record_coverage(self, corpus_ref, plan_ref, *, request_id="cov-1", **kwargs):
        rid = CoverageRepository.new_revision_id()
        rev = self.coverage.record_report(
            corpus_ref,
            plan_ref=plan_ref,
            request_id=request_id,
            rights=self.rights(DEFAULT_COVERAGE_ARTIFACT_ID, rid),
            revision_id=rid,
            **kwargs,
        )
        return self.ref(rev.artifact_id, rev.revision_id)

    # -- entry reviews ------------------------------------------------------- #
    def make_review(
        self,
        candidate_id,
        *,
        disposition="include",
        statuses=None,
        rationale="reviewed at entry",
        checked_at=_CHECKED_AT,
    ):
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
                out.append(
                    self.make_review(
                        cid,
                        disposition="exclude",
                        statuses={name: "unknown" for name in INTEGRITY_FIELDS},
                    )
                )
        return out

    def propose(self, corpus_ref, coverage_ref, snapshot, *, request_id="freeze-1", decision="freeze", reviews=None, accept=None, rationale="human decision"):
        rid = FreezeRepository.new_revision_id()
        return self.freeze.propose(
            corpus_ref,
            coverage_ref,
            decision=decision,
            entry_reviews=reviews if reviews is not None else self.reviews_for(snapshot),
            rationale=rationale,
            request_id=request_id,
            rights=self.rights(DEFAULT_FREEZE_ARTIFACT_ID, rid),
            revision_id=rid,
            accept_coverage_limitations=accept,
        )

    def approve_freeze(self, revision, *, decision="approved", gate_kind=FREEZE_GATE_KIND, gate_id=FREEZE_GATE_ID):
        return self.approve(revision.artifact_id, revision.revision_id, gate_kind, gate_id, decision=decision)

    def freeze_setup(self, *, approve_plan=True):
        """Return ``(corpus_ref, coverage_ref, plan_ref, snapshot)``."""
        corpus_ref, snapshot = self.build_corpus()
        plan_ref = self.build_plan(approve=approve_plan)
        coverage_ref = self.record_coverage(corpus_ref, plan_ref)
        return corpus_ref, coverage_ref, plan_ref, snapshot

    # -- forging ------------------------------------------------------------- #
    def forge_freeze_payload(self, revision, mutate):
        """Append a new revision whose payload is a forged copy of ``revision``.

        ``revision`` rows are immutable at the store, so a "forged field" is
        simulated by importing a new revision (with the same exact dependency
        closure) whose canonical payload has been altered; it can still be given a
        real human gate, which is exactly the case the read path must refuse.
        """
        data = json.loads(
            self.artifacts.read_content(revision.artifact_id, revision.revision_id).decode("utf-8")
        )
        mutate(data)
        new_rid = ArtifactRepository.new_revision_id()
        deps = self.artifacts.list_dependencies(revision.artifact_id, revision.revision_id)
        return self.artifacts.import_revision(
            artifact_id=revision.artifact_id,
            content=data,
            origin_locator="import:forged",
            rights=self.rights(revision.artifact_id, new_rid),
            revision_id=new_rid,
            parent_revision_id=revision.revision_id,
            dependencies=list(deps),
        )

    def candidate_ids_included(self, document):
        return document["payload"]["selection"]["included"]


# --------------------------------------------------------------------------- #
# A. Inert proposal / authentic freeze
# --------------------------------------------------------------------------- #
class FreezeAuthorityTests(FreezeTestBase):
    def test_unapproved_proposal_is_not_a_freeze(self):
        corpus_ref, coverage_ref, _plan, snapshot = self.freeze_setup()
        revision = self.propose(corpus_ref, coverage_ref, snapshot, accept="accepted")
        # The proposal exists and reads back, but freezes nothing.
        stored = self.freeze.get(self.ref(revision.artifact_id, revision.revision_id))
        self.assertFalse(stored["payload"]["authorizing"])
        self.assertFalse(stored["payload"]["completeness_claimed"])
        self.assertIsNone(stored["approval"])
        self.assertIsNone(self.freeze.current_decision())
        self.assertIsNone(self.freeze.frozen_corpus())
        with self.assertRaises(FreezeNotAuthorizedError):
            self.freeze.require_frozen()

    def test_exact_approved_freeze_is_authoritative_and_survives_reopen(self):
        corpus_ref, coverage_ref, _plan, snapshot = self.freeze_setup()
        revision = self.propose(corpus_ref, coverage_ref, snapshot, accept="accepted")
        self.approve_freeze(revision)

        doc = self.freeze.require_frozen()
        self.assertTrue(doc["frozen"])
        self.assertEqual(doc["payload"]["decision"], "freeze")
        self.assertEqual(
            doc["included_candidate_ids"], self.candidate_ids_included(doc)
        )
        self.assertEqual(sorted(doc["included_candidate_ids"]), sorted(
            c["candidate_id"] for c in snapshot["candidates"] if c["resolved"]
        ))
        self.assertEqual(self.freeze.current_decision()["payload"]["decision"], "freeze")

        self._reopen()
        again = self.freeze.require_frozen()
        self.assertEqual(again["included_candidate_ids"], doc["included_candidate_ids"])
        self.assertEqual(again["content_sha256"], doc["content_sha256"])

    def test_wrong_gate_kind_or_id_does_not_freeze(self):
        corpus_ref, coverage_ref, _plan, snapshot = self.freeze_setup()
        revision = self.propose(corpus_ref, coverage_ref, snapshot, accept="accepted")
        # A generic/different named gate instance over the same revision.
        self.approve_freeze(revision, gate_id="tool.allowed")
        with self.assertRaises(FreezeNotAuthorizedError):
            self.freeze.require_frozen()
        self.assertIsNone(self.freeze.current_decision())
        # A different gate kind is equally insufficient.
        self.approve_freeze(revision, gate_kind="pilot")
        with self.assertRaises(FreezeNotAuthorizedError):
            self.freeze.require_frozen()

    def test_rejected_decision_does_not_freeze(self):
        corpus_ref, coverage_ref, _plan, snapshot = self.freeze_setup()
        revision = self.propose(corpus_ref, coverage_ref, snapshot, accept="accepted")
        self.approve_freeze(revision, decision="rejected")
        with self.assertRaises(FreezeNotAuthorizedError):
            self.freeze.require_frozen()
        self.assertIsNone(self.freeze.current_decision())
        self.assertIsNone(self.freeze.frozen_corpus())

    def test_forged_authorizing_field_is_refused(self):
        corpus_ref, coverage_ref, _plan, snapshot = self.freeze_setup()
        revision = self.propose(corpus_ref, coverage_ref, snapshot, accept="accepted")
        self.approve_freeze(revision)
        self.freeze.require_frozen()  # authentic before tampering

        def mutate(data):
            data["payload"]["authorizing"] = True

        forged = self.forge_freeze_payload(revision, mutate)
        # A real gate over the forged revision still cannot authorize a freeze.
        self.approve_freeze(forged)
        with self.assertRaises(FreezeCorruptionError):
            self.freeze.get(self.ref(forged.artifact_id, forged.revision_id))
        with self.assertRaises(FreezeNotAuthorizedError):
            self.freeze.require_frozen()

    def test_forged_reference_to_missing_coverage_does_not_freeze(self):
        corpus_ref, coverage_ref, _plan, snapshot = self.freeze_setup()
        revision = self.propose(corpus_ref, coverage_ref, snapshot, accept="accepted")
        self.approve_freeze(revision)

        def mutate(data):
            data["payload"]["coverage"]["ref"]["revision_id"] = "rev_missing_coverage"

        forged = self.forge_freeze_payload(revision, mutate)
        self.approve_freeze(forged)
        with self.assertRaises(FreezeNotAuthorizedError):
            self.freeze.require_frozen()

    def test_unapproved_plan_blocks_a_freeze_proposal(self):
        corpus_ref, snapshot = self.build_corpus()
        plan_ref = self.build_plan(approve=False)
        coverage_ref = self.record_coverage(corpus_ref, plan_ref)
        with self.assertRaises(FreezeValidationError):
            self.propose(corpus_ref, coverage_ref, snapshot, accept="accepted")


# --------------------------------------------------------------------------- #
# B. Exact corpus/coverage pinning
# --------------------------------------------------------------------------- #
class FreezePinTests(FreezeTestBase):
    def test_mismatched_corpus_and_coverage_is_refused(self):
        corpus_ref, snapshot = self.build_corpus()
        other_corpus_ref, _other_snapshot = self.build_corpus(
            request_id="corpus-2", artifact_id="discovery.corpus.other"
        )
        plan_ref = self.build_plan()
        # Coverage built on the OTHER corpus artifact.
        coverage_ref = self.record_coverage(other_corpus_ref, plan_ref)
        with self.assertRaises(FreezeValidationError):
            self.propose(corpus_ref, coverage_ref, snapshot, accept="accepted")

    def test_cross_project_reference_is_refused(self):
        corpus_ref, coverage_ref, _plan, snapshot = self.freeze_setup()
        revision = self.propose(corpus_ref, coverage_ref, snapshot, accept="accepted")
        foreign = {
            "project_id": "prj_other",
            "artifact_id": revision.artifact_id,
            "revision_id": revision.revision_id,
        }
        with self.assertRaises(FreezeValidationError):
            self.freeze.get(foreign)
        with self.assertRaises(FreezeValidationError):
            self.freeze.propose(
                {**corpus_ref, "project_id": "prj_other"},
                coverage_ref,
                decision="freeze",
                entry_reviews=self.reviews_for(snapshot),
                rationale="r",
                request_id="x",
                rights=self.rights(DEFAULT_FREEZE_ARTIFACT_ID, "rev_x"),
                accept_coverage_limitations="accepted",
            )

    def test_a_freeze_is_isolated_to_its_own_project(self):
        corpus_ref, coverage_ref, _plan, snapshot = self.freeze_setup()
        revision = self.propose(corpus_ref, coverage_ref, snapshot, accept="accepted")
        self.approve_freeze(revision)
        self.freeze.require_frozen()

        # A separate project store never sees the first project's freeze.
        other_pid = paths.new_project_id()
        other_dir = paths.app_dir_for(str(self.tmp / "project-other"))
        other = ProjectStore.create_at(other_dir, other_pid)
        self.addCleanup(other.close)
        other_freeze = FreezeRepository(other)
        self.assertIsNone(other_freeze.current_decision())
        self.assertIsNone(other_freeze.frozen_corpus())
        with self.assertRaises(FreezeNotAuthorizedError):
            other_freeze.require_frozen()
        with self.assertRaises(FreezeValidationError):
            other_freeze.get(self.ref(revision.artifact_id, revision.revision_id))
        self.assertFalse(corpus_has_authentic_freeze(other, DEFAULT_CORPUS_ARTIFACT_ID))

    def test_replay_and_parent_chain(self):
        corpus_ref, coverage_ref, _plan, snapshot = self.freeze_setup()
        first = self.propose(corpus_ref, coverage_ref, snapshot, request_id="freeze-1", accept="accepted")
        replay = self.propose(corpus_ref, coverage_ref, snapshot, request_id="freeze-1", accept="accepted")
        self.assertEqual(first.revision_id, replay.revision_id)

        with self.assertRaises(FreezeConflictError):
            self.propose(
                corpus_ref, coverage_ref, snapshot, request_id="freeze-1",
                accept="different acknowledgement",
            )

        second = self.propose(
            corpus_ref, coverage_ref, snapshot, request_id="freeze-2",
            decision="search_again", rationale="search again please",
        )
        self.assertNotEqual(first.revision_id, second.revision_id)
        child = self.artifacts.get_revision(second.artifact_id, second.revision_id)
        self.assertEqual(child.parent_revision_id, first.revision_id)


# --------------------------------------------------------------------------- #
# C. Entry-review integrity + dispositions (POL-P3)
# --------------------------------------------------------------------------- #
class EntryReviewTests(FreezeTestBase):
    def _resolved_id(self, snapshot):
        return next(c["candidate_id"] for c in snapshot["candidates"] if c["resolved"])

    def _unresolved_id(self, snapshot):
        return next(c["candidate_id"] for c in snapshot["candidates"] if not c["resolved"])

    def test_missing_or_extra_review_is_blocked(self):
        corpus_ref, coverage_ref, _plan, snapshot = self.freeze_setup()
        reviews = self.reviews_for(snapshot)
        with self.assertRaises(FreezeValidationError):
            self.propose(corpus_ref, coverage_ref, snapshot, reviews=reviews[:-1])
        with self.assertRaises(FreezeValidationError):
            self.propose(corpus_ref, coverage_ref, snapshot, reviews=[*reviews, reviews[0]])

    def test_unknown_integrity_cannot_be_included(self):
        corpus_ref, coverage_ref, _plan, snapshot = self.freeze_setup()
        cid = self._resolved_id(snapshot)
        reviews = self.reviews_for(
            snapshot,
            overrides={cid: self.make_review(cid, disposition="include", statuses={"retraction": "unknown"})},
        )
        with self.assertRaises(FreezeValidationError):
            self.propose(corpus_ref, coverage_ref, snapshot, reviews=reviews, accept="accepted")

    def test_unresolved_and_conflict_candidates_cannot_be_included(self):
        corpus_ref, coverage_ref, _plan, snapshot = self.freeze_setup()
        unresolved = self._unresolved_id(snapshot)
        reviews = self.reviews_for(
            snapshot,
            overrides={unresolved: self.make_review(unresolved, disposition="include")},
        )
        with self.assertRaises(FreezeValidationError):
            self.propose(corpus_ref, coverage_ref, snapshot, reviews=reviews, accept="accepted")

        # identifier conflict: run X resolves a PMID, run Y asserts a different DOI.
        run_x = self.observe("run-x", self.pubmed_run((self.rec(pmid="7", doi="10.1000/x", source_id="7"),)))
        rid1 = CandidateRepository.new_revision_id()
        self.corpus.ingest(
            [run_x.run_id], request_id="cx1",
            rights=self.rights("discovery.corpus.conflict", rid1), revision_id=rid1,
            artifact_id="discovery.corpus.conflict",
        )
        run_y = self.observe("run-y", self.pubmed_run((self.rec(pmid="7", doi="10.1000/y", source_id="8"),)))
        rid2 = CandidateRepository.new_revision_id()
        self.corpus.ingest(
            [run_x.run_id, run_y.run_id], request_id="cx2",
            rights=self.rights("discovery.corpus.conflict", rid2), revision_id=rid2,
            artifact_id="discovery.corpus.conflict",
        )
        conflict_snapshot = self.corpus.current("discovery.corpus.conflict")
        conflict_ref = self.ref("discovery.corpus.conflict", conflict_snapshot["revision_id"])
        plan_ref = self.build_plan()
        conflict_coverage = self.record_coverage(conflict_ref, plan_ref, request_id="cov-conflict")
        conflict_reviews = self.reviews_for(conflict_snapshot)
        # Force the unresolved conflict candidate to "include".
        conflict_unresolved = next(
            c["candidate_id"] for c in conflict_snapshot["candidates"]
            if c["unresolved_reason"] == "identifier_conflict"
        )
        conflict_reviews = [
            (self.make_review(conflict_unresolved, disposition="include")
             if r["candidate_id"] == conflict_unresolved else r)
            for r in conflict_reviews
        ]
        with self.assertRaises(FreezeValidationError):
            self.propose(conflict_ref, conflict_coverage, conflict_snapshot, reviews=conflict_reviews, accept="accepted")

    def test_no_scientific_use_is_an_explicit_safe_disposition(self):
        corpus_ref, coverage_ref, _plan, snapshot = self.freeze_setup()
        ids = sorted(c["candidate_id"] for c in snapshot["candidates"])
        reviews = [
            self.make_review(cid, disposition="no_scientific_use", rationale="not usable downstream")
            for cid in ids
        ]
        revision = self.propose(corpus_ref, coverage_ref, snapshot, reviews=reviews, accept="accepted")
        self.approve_freeze(revision)
        doc = self.freeze.require_frozen()
        self.assertEqual(doc["included_candidate_ids"], [])
        self.assertEqual(
            doc["payload"]["selection"]["no_scientific_use"], ids
        )

    def test_flagged_status_requires_rationale_but_is_not_auto_excluded(self):
        corpus_ref, coverage_ref, _plan, snapshot = self.freeze_setup()
        cid = self._resolved_id(snapshot)
        flagged = self.make_review(cid, disposition="include", statuses={"expression_of_concern": "flagged"})
        # An empty rationale is refused for every entry.
        bad = dict(flagged, rationale="")
        reviews = self.reviews_for(snapshot, overrides={cid: bad})
        with self.assertRaises(FreezeValidationError):
            self.propose(corpus_ref, coverage_ref, snapshot, reviews=reviews, accept="accepted")
        # A flagged candidate with an explicit rationale may still be included
        # (historical discussion is not automatically prohibited).
        reviews = self.reviews_for(
            snapshot,
            overrides={cid: self.make_review(cid, disposition="include", statuses={"expression_of_concern": "flagged"})},
        )
        revision = self.propose(corpus_ref, coverage_ref, snapshot, reviews=reviews, accept="accepted")
        self.approve_freeze(revision)
        self.assertIn(cid, self.freeze.require_frozen()["included_candidate_ids"])

    def test_non_replication_is_a_separate_status(self):
        corpus_ref, coverage_ref, _plan, snapshot = self.freeze_setup()
        cid = self._resolved_id(snapshot)
        review = self.make_review(cid, disposition="include", statuses={"non_replication": "flagged"})
        self.assertEqual(review["retraction"], "none")
        self.assertNotIn("retraction", ("flagged",))
        self.assertEqual(review["non_replication"], "flagged")


# --------------------------------------------------------------------------- #
# D. Coverage-gap acknowledgement (AC5 / POL-OPS-COVERAGE)
# --------------------------------------------------------------------------- #
class CoverageAcknowledgementTests(FreezeTestBase):
    def test_freeze_without_gap_acknowledgement_is_refused(self):
        corpus_ref, coverage_ref, _plan, snapshot = self.freeze_setup()
        with self.assertRaises(FreezeValidationError):
            self.propose(corpus_ref, coverage_ref, snapshot, accept=None)

    def test_explicit_acknowledgement_freezes_without_a_completeness_claim(self):
        corpus_ref, coverage_ref, _plan, snapshot = self.freeze_setup()
        revision = self.propose(
            corpus_ref, coverage_ref, snapshot, accept="accepted: expansion not performed"
        )
        self.approve_freeze(revision)
        doc = self.freeze.require_frozen()
        acceptance = doc["payload"]["coverage_acceptance"]
        self.assertTrue(acceptance["acknowledged"])
        self.assertTrue(acceptance["gaps"]["expansion_not_performed"])
        self.assertFalse(doc["payload"]["completeness_claimed"])
        self.assertEqual(doc["payload"]["notice"], FREEZE_NOTICE)
        self.assertFalse(doc["payload"]["authorizing"])

    def test_search_again_carries_gaps_without_acknowledgement(self):
        corpus_ref, coverage_ref, _plan, snapshot = self.freeze_setup()
        revision = self.propose(
            corpus_ref, coverage_ref, snapshot,
            decision="search_again", rationale="next search planned", request_id="sa-1",
        )
        self.approve_freeze(revision)
        doc = self.freeze.get(self.ref(revision.artifact_id, revision.revision_id))
        self.assertEqual(doc["payload"]["decision"], "search_again")
        self.assertFalse(doc["payload"]["coverage_acceptance"]["acknowledged"])
        # No freeze is authorized by the search-again decision.
        with self.assertRaises(FreezeNotAuthorizedError):
            self.freeze.require_frozen()


# --------------------------------------------------------------------------- #
# E. Search-again reopens; history preserved; staleness
# --------------------------------------------------------------------------- #
class SearchAgainAndStalenessTests(FreezeTestBase):
    def test_search_again_reopens_and_preserves_history(self):
        corpus_ref, coverage_ref, _plan, snapshot = self.freeze_setup()
        freeze_rev = self.propose(corpus_ref, coverage_ref, snapshot, request_id="f1", accept="accepted")
        self.approve_freeze(freeze_rev)
        frozen_ids = self.freeze.require_frozen()["included_candidate_ids"]

        again_rev = self.propose(
            corpus_ref, coverage_ref, snapshot,
            decision="search_again", rationale="more search", request_id="sa1",
        )
        self.approve_freeze(again_rev)
        # The current decision reopens the search; the old membership is retained.
        with self.assertRaises(FreezeNotAuthorizedError):
            self.freeze.require_frozen()
        self.assertEqual(self.freeze.current_decision()["payload"]["decision"], "search_again")
        old = self.freeze.frozen_corpus()
        self.assertIsNotNone(old)
        self.assertEqual(old["included_candidate_ids"], frozen_ids)
        self.assertFalse(old["frozen"])
        history = self.freeze.history()
        self.assertEqual(
            [d["payload"]["decision"] for d in history], ["search_again", "freeze"]
        )

    def test_coverage_amendment_stales_instead_of_silently_refreezing(self):
        corpus_ref, coverage_ref, plan_ref, snapshot = self.freeze_setup()
        freeze_rev = self.propose(corpus_ref, coverage_ref, snapshot, request_id="f1", accept="accepted")
        self.approve_freeze(freeze_rev)
        self.freeze.require_frozen()

        # A later coverage revision on the SAME artifact moves the live input head.
        self.record_coverage(corpus_ref, plan_ref, request_id="cov-2")
        with self.assertRaises(FreezeNotAuthorizedError):
            self.freeze.require_frozen()
        self.assertIsNone(self.freeze.current_decision())
        stale = self.freeze.frozen_corpus()
        self.assertIsNotNone(stale)
        self.assertEqual(stale["approval"]["status"], "stale")
        self.assertFalse(stale["frozen"])
        # The frozen membership is retained exactly (never erased).
        self.assertEqual(
            stale["included_candidate_ids"],
            sorted(c["candidate_id"] for c in snapshot["candidates"] if c["resolved"]),
        )

    def test_plan_amendment_blocks_require_frozen(self):
        corpus_ref, coverage_ref, plan_ref, snapshot = self.freeze_setup()
        freeze_rev = self.propose(corpus_ref, coverage_ref, snapshot, request_id="f1", accept="accepted")
        self.approve_freeze(freeze_rev)

        # A substantive plan amendment (new plan head) invalidates the pinned plan.
        previous = plan_ref
        new_payload = json.loads(
            self.artifacts.read_content("a-plan", "rev-plan").decode("utf-8")
        )["payload"]
        new_payload["queries"] = [
            {"source": "pubmed", "query": "amended", "page_size": 20, "max_pages": 2}
        ]
        new_payload["impact_review"] = {
            "previous_plan": previous,
            "affected": ["queries"],
            "rationale": "broader query",
        }
        deps = [
            self.ref("a-brief", "rev-brief"),
            self.ref("a-interview", "rev-interview"),
            self.ref("a-seed0", "rev-seed0"),
            self.ref("a-framing", "rev-framing"),
            self.ref("a-scope", "rev-scope"),
            self.ref("a-rubric", "rev-rubric"),
        ]
        self._put(
            "a-plan", "report", "rev-plan-2",
            _envelope("discovery_plan", new_payload),
            dependencies=deps,
            parent="rev-plan",
        )
        with self.assertRaises(FreezeNotAuthorizedError):
            self.freeze.require_frozen()


# --------------------------------------------------------------------------- #
# F. Freeze immutability on the corpus side
# --------------------------------------------------------------------------- #
class CorpusFreezeImmutabilityTests(FreezeTestBase):
    def test_frozen_corpus_artifact_refuses_new_snapshots(self):
        corpus_ref, coverage_ref, _plan, snapshot = self.freeze_setup()
        revision = self.propose(corpus_ref, coverage_ref, snapshot, request_id="f1", accept="accepted")
        self.approve_freeze(revision)
        frozen_ids = self.freeze.require_frozen()["included_candidate_ids"]
        self.assertTrue(corpus_has_authentic_freeze(self.store, DEFAULT_CORPUS_ARTIFACT_ID))

        # A brand-new observation still records (never blocked).
        new_run = self.observe("req-new", self.pubmed_run((self.rec(pmid="333", source_id="c"),)))
        # But a new cumulative snapshot into the frozen artifact is refused.
        rid = CandidateRepository.new_revision_id()
        with self.assertRaises(CandidateValidationError):
            self.corpus.ingest(
                [new_run.run_id],
                request_id="corpus-new",
                rights=self.rights(DEFAULT_CORPUS_ARTIFACT_ID, rid),
                revision_id=rid,
            )

        # A NEW corpus artifact reconciles freely; the old frozen roster is untouched.
        rid2 = CandidateRepository.new_revision_id()
        self.corpus.ingest(
            [new_run.run_id],
            request_id="corpus-new-2",
            rights=self.rights("discovery.corpus.2", rid2),
            revision_id=rid2,
            artifact_id="discovery.corpus.2",
        )
        self.assertEqual(
            self.freeze.require_frozen()["included_candidate_ids"], frozen_ids
        )
        old_snapshot = self.corpus.current(DEFAULT_CORPUS_ARTIFACT_ID)
        self.assertEqual(
            sorted(c["candidate_id"] for c in old_snapshot["candidates"]),
            sorted(c["candidate_id"] for c in snapshot["candidates"]),
        )

    def test_unapproved_proposal_does_not_block_ingest(self):
        corpus_ref, _coverage_ref, _plan, _snapshot = self.freeze_setup()
        self.assertFalse(corpus_has_authentic_freeze(self.store, DEFAULT_CORPUS_ARTIFACT_ID))


# --------------------------------------------------------------------------- #
# G. Durable backup / restore
# --------------------------------------------------------------------------- #
class FreezeBackupTests(FreezeTestBase):
    def test_authentic_freeze_survives_backup_and_restore(self):
        corpus_ref, coverage_ref, _plan, snapshot = self.freeze_setup()
        revision = self.propose(corpus_ref, coverage_ref, snapshot, request_id="f1", accept="accepted")
        self.approve_freeze(revision)
        included = self.freeze.require_frozen()["included_candidate_ids"]

        dest = self.tmp / "backup"
        create_backup(self.store, dest)
        verify_backup(dest)
        registry = Registry(self.tmp / "state")
        target = self.tmp / "restored"
        target.mkdir()
        info = restore_backup(dest, target, registry)
        restored = ProjectStore.open_at(
            paths.app_dir_for(str(target)), expected_project_id=info.project_id
        )
        self.addCleanup(restored.close)
        doc = FreezeRepository(restored).require_frozen()
        self.assertEqual(doc["included_candidate_ids"], included)


# --------------------------------------------------------------------------- #
# H. Locator-independent authority + serialized write boundary (repair)
# --------------------------------------------------------------------------- #
class FreezeAuthorityDerivationTests(FreezeTestBase):
    def _import_manual_freeze(self, corpus_ref, coverage_ref, snapshot, *, locator):
        """Store an authentic-shaped freeze through the *generic* artifact path.

        The document is built by the module's own payload builder but written with
        an honest, non-``propose`` origin locator, so it exercises exactly the case
        where a locator/provenance string must not stand in for authority.
        """
        repo = FreezeRepository(self.store)
        prepared = repo._prepare_logical(
            "freeze-manual", corpus_ref, coverage_ref, "freeze",
            self.reviews_for(snapshot), "manual freeze", "accepted",
        )
        payload = prepared["payload"]
        document = {
            "schema_version": 1,
            "document_type": "discovery_freeze_decision",
            "payload": payload,
        }
        rid = ArtifactRepository.new_revision_id()
        self.artifacts.create_artifact(DEFAULT_FREEZE_ARTIFACT_ID, kind="report")
        deps = [
            {**payload["corpus"]["ref"], "role": "derived_from"},
            {**payload["coverage"]["ref"], "role": "derived_from"},
            {**payload["plan"]["ref"], "role": "derived_from"},
        ]
        return self.artifacts.import_revision(
            artifact_id=DEFAULT_FREEZE_ARTIFACT_ID,
            content=document,
            origin_locator=locator,
            rights=self.rights(DEFAULT_FREEZE_ARTIFACT_ID, rid),
            revision_id=rid,
            dependencies=deps,
        )

    def test_approved_freeze_without_the_propose_locator_protects_the_corpus(self):
        corpus_ref, coverage_ref, _plan, snapshot = self.freeze_setup()
        revision = self._import_manual_freeze(
            corpus_ref, coverage_ref, snapshot, locator="import:honest-freeze"
        )
        # The imported document was NOT written through propose's locator.
        self.assertFalse(revision.origin_locator.startswith("discovery-freeze:"))
        # Before the human gate it is not a freeze (no false freeze from data).
        self.assertFalse(corpus_has_authentic_freeze(self.store, DEFAULT_CORPUS_ARTIFACT_ID))
        self.assertIsNone(self.freeze.current_decision())

        # The real human gate over the exact imported revision is the authority.
        self.approve_freeze(revision)
        self.assertTrue(corpus_has_authentic_freeze(self.store, DEFAULT_CORPUS_ARTIFACT_ID))
        # Detected by the read helpers too (they never consulted the locator).
        self.assertEqual(self.freeze.current_decision()["payload"]["decision"], "freeze")
        self.assertEqual(
            self.freeze.frozen_corpus()["revision_id"], revision.revision_id
        )
        self.assertEqual(
            self.freeze.require_frozen()["revision_id"], revision.revision_id
        )
        self.assertEqual(
            [d["revision_id"] for d in self.freeze.history()], [revision.revision_id]
        )

        # A new cumulative snapshot into the frozen corpus artifact is refused...
        before = len(self.artifacts.list_revisions(DEFAULT_CORPUS_ARTIFACT_ID))
        new_run = self.observe("req-new", self.pubmed_run((self.rec(pmid="333", source_id="c"),)))
        rid = CandidateRepository.new_revision_id()
        with self.assertRaises(CandidateValidationError):
            self.corpus.ingest(
                [new_run.run_id],
                request_id="corpus-new",
                rights=self.rights(DEFAULT_CORPUS_ARTIFACT_ID, rid),
                revision_id=rid,
            )
        self.assertEqual(
            len(self.artifacts.list_revisions(DEFAULT_CORPUS_ARTIFACT_ID)), before
        )
        # ...while a genuinely NEW corpus artifact reconciles freely.
        rid2 = CandidateRepository.new_revision_id()
        self.corpus.ingest(
            [new_run.run_id],
            request_id="corpus-new-2",
            rights=self.rights("discovery.corpus.2", rid2),
            revision_id=rid2,
            artifact_id="discovery.corpus.2",
        )

    def test_forged_payload_under_the_propose_locator_is_not_a_freeze(self):
        # An unapproved *forged* document with the propose prefix is still not a
        # freeze: the guard derives authority from approvals, not the locator.
        corpus_ref, coverage_ref, _plan, snapshot = self.freeze_setup()
        revision = self._import_manual_freeze(
            corpus_ref, coverage_ref, snapshot, locator="discovery-freeze:forged"
        )
        self.assertTrue(revision.origin_locator.startswith("discovery-freeze:"))
        self.assertFalse(corpus_has_authentic_freeze(self.store, DEFAULT_CORPUS_ARTIFACT_ID))


class FreezeWriteBoundaryTests(FreezeTestBase):
    def test_frozen_guard_runs_inside_the_append_transaction_and_refuses(self):
        corpus_ref, coverage_ref, _plan, _snapshot = self.freeze_setup()
        revision = self.propose(corpus_ref, coverage_ref, _snapshot, request_id="f1", accept="accepted")
        self.approve_freeze(revision)
        self.freeze.require_frozen()

        before = len(self.artifacts.list_revisions(DEFAULT_CORPUS_ARTIFACT_ID))
        new_run = self.observe("req-new", self.pubmed_run((self.rec(pmid="333", source_id="c"),)))

        real = freeze_mod.corpus_has_authentic_freeze
        seen = []

        def wrapper(store, artifact_id):
            seen.append(store.connection.in_transaction)
            return real(store, artifact_id)

        with mock.patch.object(freeze_mod, "corpus_has_authentic_freeze", wrapper):
            rid = CandidateRepository.new_revision_id()
            with self.assertRaises(CandidateValidationError):
                self.corpus.ingest(
                    [new_run.run_id],
                    request_id="corpus-new",
                    rights=self.rights(DEFAULT_CORPUS_ARTIFACT_ID, rid),
                    revision_id=rid,
                )
            # An exact replay of an older request is still historical and never
            # reaches the guard (it is a shortcut inside the same transaction).
            replay = self.corpus.ingest(
                self._corpus_run_ids,
                request_id="corpus-1",
                rights=self.rights(DEFAULT_CORPUS_ARTIFACT_ID, CandidateRepository.new_revision_id()),
            )
        # The guard ran exactly once, inside an active write transaction.
        self.assertEqual(seen, [True])
        self.assertEqual(
            len(self.artifacts.list_revisions(DEFAULT_CORPUS_ARTIFACT_ID)), before
        )
        self.assertEqual(
            replay.revision_id,
            self.corpus.current(DEFAULT_CORPUS_ARTIFACT_ID)["revision_id"],
        )

    def test_identical_propose_replay_survives_plan_and_coverage_amendment(self):
        corpus_ref, coverage_ref, plan_ref, snapshot = self.freeze_setup()
        first = self.propose(corpus_ref, coverage_ref, snapshot, request_id="f1", accept="accepted")

        # A substantive plan amendment moves the plan head (the pinned plan is no
        # longer current) and a later coverage revision moves the coverage head.
        new_payload = json.loads(
            self.artifacts.read_content("a-plan", "rev-plan").decode("utf-8")
        )["payload"]
        new_payload["queries"] = [
            {"source": "pubmed", "query": "amended", "page_size": 20, "max_pages": 2}
        ]
        new_payload["impact_review"] = {
            "previous_plan": plan_ref,
            "affected": ["queries"],
            "rationale": "broader query",
        }
        deps = [
            self.ref("a-brief", "rev-brief"),
            self.ref("a-interview", "rev-interview"),
            self.ref("a-seed0", "rev-seed0"),
            self.ref("a-framing", "rev-framing"),
            self.ref("a-scope", "rev-scope"),
            self.ref("a-rubric", "rev-rubric"),
        ]
        self._put(
            "a-plan", "report", "rev-plan-2",
            _envelope("discovery_plan", new_payload),
            dependencies=deps,
            parent="rev-plan",
        )

        # An identical replay stays historical: no new authority is needed to
        # re-read an already-recorded decision.
        replay = self.propose(corpus_ref, coverage_ref, snapshot, request_id="f1", accept="accepted")
        self.assertEqual(replay.revision_id, first.revision_id)
        self.assertEqual(
            len(self.artifacts.list_revisions(DEFAULT_FREEZE_ARTIFACT_ID)), 1
        )
        # A divergent reuse still conflicts.
        with self.assertRaises(FreezeConflictError):
            self.propose(corpus_ref, coverage_ref, snapshot, request_id="f1", accept="other")
        # A NEW proposal is refused because the pinned plan is no longer approved.
        with self.assertRaises(FreezeValidationError):
            self.propose(corpus_ref, coverage_ref, snapshot, request_id="f2", accept="accepted")

        # The same holds after the coverage head also moves.
        self.record_coverage(corpus_ref, plan_ref, request_id="cov-2")
        replay2 = self.propose(corpus_ref, coverage_ref, snapshot, request_id="f1", accept="accepted")
        self.assertEqual(replay2.revision_id, first.revision_id)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()