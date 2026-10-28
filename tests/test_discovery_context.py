"""Read-and-gate tests for typed discovery-context documents (Step 05, D1).

Covers ``src/review_article_agent/discovery/context.py`` against D1 acceptance
criteria 1 and 2 and the C01/C02 identity/gate contracts:

* static envelope/payload validation (wrong fields/types/bools/surrogates,
  unknown document type, forged payload, inverted amendment shape);
* reading an exact current revision with byte-integrity and canonical-envelope
  enforcement (missing, stale, cross-project, wrong type, forged/non-canonical);
* type-specific on-read cross-checks (seed run/index existence, rubric artifact
  kind, plan reference presence + dependency pins);
* gating: seeds only via their individual ``artifact_revision`` gate
  ``discovery.seed.<artifact_id>`` (partial seed approval fails closed and no
  abstract becomes primary support); a plan only via the combined protocol gate
  ``protocol``/``discovery.plan`` plus every seed gate plus the exact
  brief/interview association and ``ready_to_advance``;
* dependency changes and substantive amendments reopening/blocking approval.

Synthetic data only; no test performs a network, provider, or model call.

Run::

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_discovery_context.py'
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.discovery import context as ctx  # noqa: E402
from review_article_agent.discovery.context import (  # noqa: E402
    ARTIFACT_REVISION_GATE_KIND,
    PLAN_GATE_ID,
    PROTOCOL_GATE_KIND,
    DiscoveryContextError,
    read_context_document,
    require_approved_context,
    seed_context,
    validate_context_document,
)
from review_article_agent.discovery.records import (  # noqa: E402
    MetadataRecord,
    SearchPage,
    SearchResult,
)
from review_article_agent.runtime import owner_auth  # noqa: E402
from review_article_agent.runtime.gates import prepare_gate_snapshot  # noqa: E402
from review_article_agent.runtime.owner_auth import OwnerApproval  # noqa: E402
from review_article_agent.storage import ArtifactRepository, ProjectStore  # noqa: E402
from review_article_agent.storage import paths  # noqa: E402
from review_article_agent.storage.discovery import DiscoveryRepository  # noqa: E402
from review_article_agent.storage.human_approvals import HumanApprovalRepository  # noqa: E402

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")
_BASE_TIME = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
_FIXED_CHALLENGE = "TEST-CHAL-0001"
_OWNER_UID = 1000


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


def _brief_payload(**overrides):
    payload = {"objective": "obj", "starting_hypothesis": "hyp", "questions": []}
    payload.update(overrides)
    return payload


def _interview_payload(brief_ref, *, ready_to_advance=True, turns=None):
    return {
        "brief_ref": brief_ref,
        "turns": turns if turns is not None else [{"question": "q", "answer": "a"}],
        "ready_to_advance": ready_to_advance,
    }


def _framing_payload(**overrides):
    payload = {"alternatives": ["alt"], "counterevidence_queries": ["q"], "limitations": []}
    payload.update(overrides)
    return payload


def _scope_payload():
    return {
        "scope": "s", "date_boundary": "none", "language_boundary": "english",
        "source_boundary": ["none"], "intended_synthesis": "syn",
        "terminology": [], "exclusions": [],
    }


def _rubric_payload():
    return {"eligibility_rules": ["r"], "uncertain_action": "retain", "limitations": []}


def _envelope(document_type, payload):
    return {
        "schema_version": 1,
        "document_type": document_type,
        "payload": payload,
    }


class ContextTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-ctx-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.clock = [_BASE_TIME]
        self.mono = [1000.0]
        self.store = ProjectStore.create_at(
            paths.app_dir_for(str(self.tmp / "p")), paths.new_project_id()
        )
        self.addCleanup(self.store.close)
        self.repo = ArtifactRepository(self.store)
        self.project_id = self.store.project_id

    # -- artifacts / revisions ---------------------------------------------- #
    def ref(self, artifact_id, revision_id):
        return {
            "project_id": self.project_id,
            "artifact_id": artifact_id,
            "revision_id": revision_id,
        }

    def _put(self, artifact_id, kind, revision_id, document, *, dependencies=(), parent=None):
        self.repo.create_artifact(artifact_id, kind=kind)
        return self.repo.import_revision(
            artifact_id=artifact_id,
            content=document,
            origin_locator="import:synthetic",
            rights=_rights(self.project_id, artifact_id, revision_id),
            revision_id=revision_id,
            dependencies=list(dependencies),
            parent_revision_id=parent,
        )

    def put_brief(self, aid="a-brief", rid="rev-brief", payload=None, **kw):
        return self._put(aid, "report", rid, _envelope("discovery_brief", payload or _brief_payload()), **kw)

    def _corrupt_revision_bytes(self, artifact_id, revision_id, content=b'{"schema_version":1}'):
        """Overwrite a stored revision's bytes (dropping the immutability trigger)."""
        conn = sqlite3.connect(paths.store_path_for(str(self.tmp / "p")))
        try:
            conn.execute("DROP TRIGGER IF EXISTS revision_no_update")
            conn.execute(
                "UPDATE revision SET content = ? WHERE artifact_id = ? AND revision_id = ?",
                (content, artifact_id, revision_id),
            )
            conn.commit()
        finally:
            conn.close()

    # -- owner-auth seams (copied from the human-approval test pattern) ------ #
    def install(self, *, challenge=_FIXED_CHALLENGE, owner_uid=_OWNER_UID, real_uid=_OWNER_UID):
        patches = [
            mock.patch.object(owner_auth, "_new_challenge", lambda: challenge),
            mock.patch.object(owner_auth, "_current_owner_uid", lambda: owner_uid),
            mock.patch.object(owner_auth, "_real_uid", lambda: real_uid),
            mock.patch.object(owner_auth, "_utcnow", lambda: self.clock[0]),
            mock.patch.object(owner_auth, "_monotonic", lambda: self.mono[0]),
            mock.patch.object(
                owner_auth,
                "_open_controlling_tty",
                lambda: _FakeTty(replies=[challenge]),
            ),
        ]
        for patch in patches:
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
        return HumanApprovalRepository(self.store).record_decision(prepared, capability)

    def seed_gate(self, artifact_id, revision_id):
        return self.approve(
            artifact_id, revision_id, ARTIFACT_REVISION_GATE_KIND, ctx.SEED_GATE_PREFIX + artifact_id
        )

    # -- discovery run fixture ---------------------------------------------- #
    def put_run(self, request_id="req-1", n_records=1):
        records = tuple(
            MetadataRecord(
                source="pubmed",
                source_id=f"3145210{index}",
                pmid=f"3145210{index}",
                title=f"fixture {index}",
                publication_year=2020,
            )
            for index in range(n_records)
        )
        page = SearchPage(
            source="pubmed",
            query="q",
            requested_url="https://eutils.ncbi.nlm.nih.gov/fixture",
            retrieved_at="2026-01-01T00:00:00+00:00",
            page_index=0,
            records=records,
            page_size=20,
            total_count=n_records,
            complete=True,
            stop_reason="completed",
        )
        result = SearchResult.from_pages(
            source="pubmed",
            query="q",
            pages=[page],
            started_at="2026-01-01T00:00:00+00:00",
            completed_at="2026-01-01T00:00:01+00:00",
            stop_reason="completed",
            total_count=n_records,
        )
        return DiscoveryRepository(self.store).record_search(request_id=request_id, result=result)

    def put_seed(self, aid="a-seed0", rid="rev-seed0", *, record_index=0, abstract=None, run_id):
        payload = {
            "run_id": run_id,
            "record_index": record_index,
            "abstract": abstract,
            "abstract_context": "provisional",
            "limitations": ["synthetic"],
        }
        self._put(aid, "document", rid, _envelope("discovery_seed", payload))
        return self.ref(aid, rid)


# --------------------------------------------------------------------------- #
# Static validation
# --------------------------------------------------------------------------- #
class StaticValidationTest(ContextTestBase):
    def test_valid_document_returns_independent_plain_copy(self) -> None:
        source = _envelope("discovery_brief", _brief_payload(questions=["a?"]))
        validated = validate_context_document(source)
        self.assertEqual(
            validated,
            {
                "schema_version": 1,
                "document_type": "discovery_brief",
                "payload": {"objective": "obj", "starting_hypothesis": "hyp", "questions": ["a?"]},
            },
        )
        source["payload"]["questions"].append("mutated")
        self.assertEqual(validated["payload"]["questions"], ["a?"])

    def test_rejects_unknown_and_missing_envelope_and_payload_fields(self) -> None:
        for bad in (
            {"schema_version": 1, "document_type": "discovery_brief", "payload": {}, "extra": 1},
            {"schema_version": 1, "document_type": "discovery_brief"},
            _envelope("discovery_brief", {"objective": "o", "starting_hypothesis": "h"}),
            _envelope("discovery_brief", {**_brief_payload(), "approved": True}),
            _envelope("discovery_brief", {**_brief_payload(), "actor": "user"}),
        ):
            with self.assertRaises(DiscoveryContextError):
                validate_context_document(bad)

    def test_rejects_wrong_schema_version_and_type(self) -> None:
        with self.assertRaises(DiscoveryContextError):
            validate_context_document(_envelope("discovery_brief", _brief_payload()) | {"schema_version": 2})
        with self.assertRaises(DiscoveryContextError):
            validate_context_document(_envelope("discovery_brief", _brief_payload()) | {"schema_version": True})
        with self.assertRaises(DiscoveryContextError):
            validate_context_document(_envelope("discovery_unknown", _brief_payload()))

    def test_rejects_wrong_types_bools_and_surrogates(self) -> None:
        with self.assertRaises(DiscoveryContextError):
            validate_context_document(_envelope("discovery_brief", {**_brief_payload(), "objective": 5}))
        with self.assertRaises(DiscoveryContextError):
            validate_context_document(_envelope("discovery_brief", {**_brief_payload(), "questions": "q"}))
        with self.assertRaises(DiscoveryContextError):
            validate_context_document(
                _envelope("discovery_seed", {
                    "run_id": "dsr_x", "record_index": True, "abstract": None,
                    "abstract_context": "provisional", "limitations": [],
                })
            )
        with self.assertRaises(DiscoveryContextError):
            validate_context_document(
                _envelope("discovery_scope", {
                    "scope": "a\ud800b", "date_boundary": "x", "language_boundary": "y",
                    "source_boundary": [], "intended_synthesis": "z",
                    "terminology": [], "exclusions": [],
                })
            )

    def test_seed_abstract_is_null_or_nonempty_and_context_is_provisional(self) -> None:
        good = _envelope("discovery_seed", {
            "run_id": "dsr_x", "record_index": 0, "abstract": None,
            "abstract_context": "provisional", "limitations": [],
        })
        self.assertEqual(validate_context_document(good)["payload"]["abstract"], None)
        for bad_abstract in ("", 5):
            with self.assertRaises(DiscoveryContextError):
                validate_context_document({**good, "payload": {**good["payload"], "abstract": bad_abstract}})
        with self.assertRaises(DiscoveryContextError):
            validate_context_document(
                {**good, "payload": {**good["payload"], "abstract_context": "accepted"}}
            )

    def test_framing_requires_an_alternative_and_a_counterevidence_query(self) -> None:
        base = {"alternatives": ["alt"], "counterevidence_queries": ["q"], "limitations": []}
        validate_context_document(_envelope("discovery_framing", base))
        for field in ("alternatives", "counterevidence_queries"):
            with self.assertRaises(DiscoveryContextError):
                validate_context_document(_envelope("discovery_framing", {**base, field: []}))

    def test_rubric_requires_the_retain_constant(self) -> None:
        good = {"eligibility_rules": ["r"], "uncertain_action": "retain", "limitations": []}
        validate_context_document(_envelope("discovery_rubric", good))
        with self.assertRaises(DiscoveryContextError):
            validate_context_document(_envelope("discovery_rubric", {**good, "uncertain_action": "exclude"}))

    def test_plan_static_rules(self) -> None:
        def plan_payload(**overrides):
            payload = {
                "protocol": "p",
                "brief_ref": self.ref("a-brief", "rev-brief"),
                "interview_ref": self.ref("a-interview", "rev-interview"),
                "seed_refs": [self.ref("a-seed0", "rev-seed0")],
                "framing_ref": self.ref("a-framing", "rev-framing"),
                "scope_ref": self.ref("a-scope", "rev-scope"),
                "rubric_ref": self.ref("a-rubric", "rev-rubric"),
                "queries": [{"source": "pubmed", "query": "q", "page_size": 20, "max_pages": 2}],
                "complementary_sources": [
                    {"source": "regulatory", "mode": "metadata_import", "rationale": "r"}
                ],
                "expansion": {
                    "max_hops": 1, "max_sources": 5, "max_requests": 10,
                    "page_size": 20, "max_pages": 3,
                },
                "impact_review": {"previous_plan": None, "affected": [], "rationale": ""},
            }
            payload.update(overrides)
            return payload

        validate_context_document(_envelope("discovery_plan", plan_payload()))
        # Only the two supported query APIs; only the ratified import mode.
        with self.assertRaises(DiscoveryContextError):
            validate_context_document(
                _envelope("discovery_plan", plan_payload(queries=[{"source": "scholar", "query": "q", "page_size": 20, "max_pages": 2}]))
            )
        with self.assertRaises(DiscoveryContextError):
            validate_context_document(
                _envelope("discovery_plan", plan_payload(complementary_sources=[{"source": "x", "mode": "scrape", "rationale": "r"}]))
            )
        # Expansion bounds are exact integers, never booleans.
        with self.assertRaises(DiscoveryContextError):
            validate_context_document(
                _envelope("discovery_plan", plan_payload(expansion={"max_hops": True, "max_sources": 1, "max_requests": 1, "page_size": 1, "max_pages": 1}))
            )
        # Rubric must be distinct from scope/framing.
        with self.assertRaises(DiscoveryContextError):
            validate_context_document(
                _envelope("discovery_plan", plan_payload(rubric_ref=self.ref("a-scope", "rev-scope")))
            )
        # A mixed-project document is refused.
        with self.assertRaises(DiscoveryContextError):
            validate_context_document(
                _envelope("discovery_plan", plan_payload(rubric_ref={"project_id": "other", "artifact_id": "a-rubric", "revision_id": "rev-rubric"}))
            )
        # An amendment shape requires affected + rationale.
        amendment = {"previous_plan": self.ref("a-plan", "rev1"), "affected": [], "rationale": ""}
        with self.assertRaises(DiscoveryContextError):
            validate_context_document(_envelope("discovery_plan", plan_payload(impact_review=amendment)))


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #
class ReadContextTest(ContextTestBase):
    def test_read_current_returns_validated_document(self) -> None:
        self.put_brief()
        document = read_context_document(self.store, self.ref("a-brief", "rev-brief"))
        self.assertEqual(document["document_type"], "discovery_brief")
        self.assertEqual(document["payload"]["objective"], "obj")

    def test_read_missing_revision_fails(self) -> None:
        self.put_brief()
        with self.assertRaises(DiscoveryContextError):
            read_context_document(self.store, self.ref("a-brief", "rev-absent"))

    def test_read_stale_revision_requires_current(self) -> None:
        self.put_brief()
        self._put("a-brief", "report", "rev-brief2", _envelope("discovery_brief", _brief_payload()), parent="rev-brief")
        with self.assertRaises(DiscoveryContextError):
            read_context_document(self.store, self.ref("a-brief", "rev-brief"))
        historical = read_context_document(
            self.store, self.ref("a-brief", "rev-brief"), require_current=False
        )
        self.assertEqual(historical["document_type"], "discovery_brief")

    def test_read_cross_project_ref_fails(self) -> None:
        self.put_brief()
        with self.assertRaises(DiscoveryContextError):
            read_context_document(
                self.store,
                {"project_id": "other", "artifact_id": "a-brief", "revision_id": "rev-brief"},
            )

    def test_read_wrong_expected_type_fails(self) -> None:
        self.put_brief()
        with self.assertRaises(DiscoveryContextError):
            read_context_document(
                self.store, self.ref("a-brief", "rev-brief"), expected_type="discovery_scope"
            )
        with self.assertRaises(DiscoveryContextError):
            read_context_document(
                self.store, self.ref("a-brief", "rev-brief"), expected_type="not_a_type"
            )

    def test_read_forged_payload_field_fails(self) -> None:
        self._put(
            "a-brief", "report", "rev-forged",
            _envelope("discovery_brief", {**_brief_payload(), "approved": True}),
        )
        with self.assertRaises(DiscoveryContextError):
            read_context_document(self.store, self.ref("a-brief", "rev-forged"))

    def test_read_non_canonical_bytes_fails(self) -> None:
        raw = json.dumps(_envelope("discovery_brief", _brief_payload()), indent=2).encode("utf-8")
        self.repo.create_artifact("a-brief", kind="report")
        self.repo.import_revision(
            artifact_id="a-brief",
            content=raw,
            origin_locator="import:synthetic",
            rights=_rights(self.project_id, "a-brief", "rev-raw"),
            revision_id="rev-raw",
        )
        with self.assertRaises(DiscoveryContextError):
            read_context_document(self.store, self.ref("a-brief", "rev-raw"))

    def test_rubric_artifact_kind_must_be_rubric(self) -> None:
        payload = {"eligibility_rules": ["r"], "uncertain_action": "retain", "limitations": []}
        self._put("a-rubric-bad", "report", "rev-r", _envelope("discovery_rubric", payload))
        with self.assertRaises(DiscoveryContextError):
            read_context_document(self.store, self.ref("a-rubric-bad", "rev-r"))
        self._put("a-rubric", "rubric", "rev-r2", _envelope("discovery_rubric", payload))
        document = read_context_document(self.store, self.ref("a-rubric", "rev-r2"))
        self.assertEqual(document["payload"]["uncertain_action"], "retain")

    def test_read_interview_requires_typed_pinned_brief(self) -> None:
        self.put_brief(aid="a-brief", rid="rev-brief")
        self._put("a-scope", "framework", "rev-scope",
                  _envelope("discovery_scope", _scope_payload()))
        brief_ref = self.ref("a-brief", "rev-brief")
        # The interview must pin its brief as an exact dependency.
        self._put("a-interview-unpinned", "report", "rev-i0",
                  _envelope("discovery_interview", _interview_payload(brief_ref)))
        with self.assertRaises(DiscoveryContextError):
            read_context_document(self.store, self.ref("a-interview-unpinned", "rev-i0"))
        # The referenced brief must itself be a typed discovery_brief.
        self._put("a-interview-wrong", "report", "rev-i1",
                  _envelope("discovery_interview", _interview_payload(self.ref("a-scope", "rev-scope"))),
                  dependencies=[self.ref("a-scope", "rev-scope")])
        with self.assertRaises(DiscoveryContextError):
            read_context_document(self.store, self.ref("a-interview-wrong", "rev-i1"))
        # A correctly pinned, typed brief reads.
        self._put("a-interview", "report", "rev-i2",
                  _envelope("discovery_interview", _interview_payload(brief_ref)),
                  dependencies=[brief_ref])
        document = read_context_document(self.store, self.ref("a-interview", "rev-i2"))
        self.assertEqual(document["document_type"], "discovery_interview")


# --------------------------------------------------------------------------- #
# Seed reading + individual seed gates
# --------------------------------------------------------------------------- #
class SeedContextTest(ContextTestBase):
    def test_seed_read_cross_checks_run_and_index(self) -> None:
        self.put_run(request_id="req-1", n_records=1)
        self.put_seed(run_id="dsr_missing", rid="rev-miss")
        with self.assertRaises(DiscoveryContextError):
            read_context_document(self.store, self.ref("a-seed0", "rev-miss"))
        run = self.put_run(request_id="req-2", n_records=1)
        self.put_seed(aid="a-seed1", rid="rev-s1", run_id=run.run_id, record_index=5)
        with self.assertRaises(DiscoveryContextError):
            read_context_document(self.store, self.ref("a-seed1", "rev-s1"))
        self.put_seed(aid="a-seed2", rid="rev-s2", run_id=run.run_id, record_index=0)
        self.assertIsNotNone(read_context_document(self.store, self.ref("a-seed2", "rev-s2")))

    def test_seed_context_only_returns_individually_approved_seeds(self) -> None:
        run = self.put_run(request_id="req-3", n_records=2)
        s0 = self.put_seed(aid="a-seed0", rid="rev-s0", run_id=run.run_id, record_index=0)
        s1 = self.put_seed(aid="a-seed1", rid="rev-s1", run_id=run.run_id, record_index=1)
        # Nothing approved yet.
        with self.assertRaises(DiscoveryContextError):
            seed_context(self.store, [s0, s1])
        # Partial approval fails closed and returns nothing.
        self.seed_gate("a-seed0", "rev-s0")
        with self.assertRaises(DiscoveryContextError):
            seed_context(self.store, [s0, s1])
        self.seed_gate("a-seed1", "rev-s1")
        documents = seed_context(self.store, [s0, s1])
        self.assertEqual(len(documents), 2)
        self.assertEqual(seed_context(self.store, []), ())

    def test_seed_stays_provisional_and_never_primary_support(self) -> None:
        run = self.put_run(request_id="req-4", n_records=1)
        s0 = self.put_seed(aid="a-seed0", rid="rev-s0", run_id=run.run_id, abstract="synthetic abstract")
        self.seed_gate("a-seed0", "rev-s0")
        (document,) = seed_context(self.store, [s0])
        self.assertEqual(document["payload"]["abstract_context"], "provisional")

    def test_seed_gate_target_must_be_the_seed_artifact(self) -> None:
        run = self.put_run(request_id="req-5", n_records=1)
        s0 = self.put_seed(aid="a-seed0", rid="rev-s0", run_id=run.run_id)
        # A gate under the wrong gate id does not satisfy the seed gate.
        self.approve("a-seed0", "rev-s0", ARTIFACT_REVISION_GATE_KIND, "discovery.seed.other")
        with self.assertRaises(DiscoveryContextError):
            seed_context(self.store, [s0])


# --------------------------------------------------------------------------- #
# Plan reading + combined protocol approval
# --------------------------------------------------------------------------- #
class PlanApprovalTest(ContextTestBase):
    def build_plan_fixture(self, *, ready_to_advance=True, interview_brief=None, plan_artifact="a-plan", plan_revision="rev-plan", parent=None, previous_plan=None, affected=None, rationale="", include_seed=True, override=None, seed_refs_override=None):
        brief_ref = self.ref("a-brief", "rev-brief")
        self.put_brief(aid="a-brief", rid="rev-brief")
        # The interview pins its exact brief as a dependency (typed on read).
        self._put(
            "a-interview", "report", "rev-interview",
            _envelope("discovery_interview", _interview_payload(
                interview_brief or brief_ref, ready_to_advance=ready_to_advance
            )),
            dependencies=[interview_brief or brief_ref],
        )
        run = self.put_run(request_id="req-plan", n_records=1)
        seed = self.put_seed(aid="a-seed0", rid="rev-seed0", run_id=run.run_id)
        self._put("a-framing", "framework", "rev-framing",
                  _envelope("discovery_framing", _framing_payload()))
        self._put("a-scope", "framework", "rev-scope",
                  _envelope("discovery_scope", _scope_payload()))
        self._put("a-rubric", "rubric", "rev-rubric",
                  _envelope("discovery_rubric", _rubric_payload()))
        good = {
            "brief_ref": brief_ref,
            "interview_ref": self.ref("a-interview", "rev-interview"),
            "framing_ref": self.ref("a-framing", "rev-framing"),
            "scope_ref": self.ref("a-scope", "rev-scope"),
            "rubric_ref": self.ref("a-rubric", "rev-rubric"),
        }
        if override:
            good.update(override)
        if seed_refs_override is not None:
            seeds = list(seed_refs_override)
        elif include_seed:
            seeds = [seed]
        else:
            seeds = []
        refs = [
            good["brief_ref"],
            good["interview_ref"],
            *seeds,
            good["framing_ref"],
            good["scope_ref"],
            good["rubric_ref"],
        ]
        payload = {
            "protocol": "protocol",
            "brief_ref": good["brief_ref"],
            "interview_ref": good["interview_ref"],
            "seed_refs": seeds,
            "framing_ref": good["framing_ref"],
            "scope_ref": good["scope_ref"],
            "rubric_ref": good["rubric_ref"],
            "queries": [{"source": "pubmed", "query": "q", "page_size": 20, "max_pages": 2}],
            "complementary_sources": [],
            "expansion": {"max_hops": 1, "max_sources": 5, "max_requests": 10, "page_size": 20, "max_pages": 3},
            "impact_review": {
                "previous_plan": previous_plan,
                "affected": affected if affected is not None else [],
                "rationale": rationale,
            },
        }
        self._put(
            plan_artifact, "report", plan_revision, _envelope("discovery_plan", payload),
            dependencies=refs, parent=parent,
        )
        return self.ref(plan_artifact, plan_revision), seed

    def approve_all(self, seed_ref, plan_ref):
        self.seed_gate(seed_ref["artifact_id"], seed_ref["revision_id"])
        self.approve("a-plan", plan_ref["revision_id"], PROTOCOL_GATE_KIND, PLAN_GATE_ID)

    def test_plan_requires_protocol_gate_and_seed_gate(self) -> None:
        plan_ref, seed_ref = self.build_plan_fixture()
        with self.assertRaises(DiscoveryContextError):
            require_approved_context(self.store, plan_ref, gate_kind=PROTOCOL_GATE_KIND, gate_id=PLAN_GATE_ID)
        self.approve_all(seed_ref, plan_ref)
        document = require_approved_context(
            self.store, plan_ref, gate_kind=PROTOCOL_GATE_KIND, gate_id=PLAN_GATE_ID
        )
        self.assertEqual(document["document_type"], "discovery_plan")

    def test_plan_with_no_seeds_approves_via_protocol_gate_only(self) -> None:
        # An empty seed list is valid when the user explicitly advances with none.
        plan_ref, _seed_ref = self.build_plan_fixture(include_seed=False)
        self.approve("a-plan", plan_ref["revision_id"], PROTOCOL_GATE_KIND, PLAN_GATE_ID)
        document = require_approved_context(
            self.store, plan_ref, gate_kind=PROTOCOL_GATE_KIND, gate_id=PLAN_GATE_ID
        )
        self.assertEqual(document["payload"]["seed_refs"], [])

    def test_plan_missing_seed_gate_fails(self) -> None:
        plan_ref, _seed_ref = self.build_plan_fixture()
        self.approve("a-plan", plan_ref["revision_id"], PROTOCOL_GATE_KIND, PLAN_GATE_ID)
        with self.assertRaises(DiscoveryContextError):
            require_approved_context(self.store, plan_ref, gate_kind=PROTOCOL_GATE_KIND, gate_id=PLAN_GATE_ID)

    def test_plan_wrong_gate_kind_fails(self) -> None:
        plan_ref, seed_ref = self.build_plan_fixture()
        self.seed_gate(seed_ref["artifact_id"], seed_ref["revision_id"])
        # A plan approved under a pilot gate is not the required protocol gate.
        self.approve("a-plan", plan_ref["revision_id"], "pilot", PLAN_GATE_ID)
        with self.assertRaises(DiscoveryContextError):
            require_approved_context(self.store, plan_ref, gate_kind="pilot", gate_id=PLAN_GATE_ID)

    def test_plan_requires_ready_to_advance_interview(self) -> None:
        plan_ref, seed_ref = self.build_plan_fixture(ready_to_advance=False)
        self.approve_all(seed_ref, plan_ref)
        with self.assertRaises(DiscoveryContextError):
            require_approved_context(self.store, plan_ref, gate_kind=PROTOCOL_GATE_KIND, gate_id=PLAN_GATE_ID)

    def test_plan_requires_exact_brief_interview_association(self) -> None:
        self.put_brief(aid="a-brief", rid="rev-brief")
        self.put_brief(aid="a-other-brief", rid="rev-other", payload=_brief_payload(objective="other"))
        plan_ref, seed_ref = self.build_plan_fixture(
            interview_brief=self.ref("a-other-brief", "rev-other")
        )
        self.approve_all(seed_ref, plan_ref)
        with self.assertRaises(DiscoveryContextError):
            require_approved_context(self.store, plan_ref, gate_kind=PROTOCOL_GATE_KIND, gate_id=PLAN_GATE_ID)

    def test_plan_dependency_change_makes_approval_stale(self) -> None:
        plan_ref, seed_ref = self.build_plan_fixture()
        self.approve_all(seed_ref, plan_ref)
        self.assertIsNotNone(
            require_approved_context(self.store, plan_ref, gate_kind=PROTOCOL_GATE_KIND, gate_id=PLAN_GATE_ID)
        )
        # A substantive change to a pinned dependency reopens the approval.
        self._put("a-brief", "report", "rev-brief2", _envelope("discovery_brief", _brief_payload(objective="revised")), parent="rev-brief")
        with self.assertRaises(DiscoveryContextError):
            require_approved_context(self.store, plan_ref, gate_kind=PROTOCOL_GATE_KIND, gate_id=PLAN_GATE_ID)

    def test_plan_missing_dependency_pin_fails_read(self) -> None:
        self.put_brief(aid="a-brief", rid="rev-brief")
        self._put("a-interview", "report", "rev-interview", _envelope("discovery_interview", {
            "brief_ref": self.ref("a-brief", "rev-brief"),
            "turns": [], "ready_to_advance": True,
        }))
        self._put("a-framing", "framework", "rev-framing", _envelope("discovery_framing", {
            "alternatives": ["a"], "counterevidence_queries": ["q"], "limitations": [],
        }))
        self._put("a-scope", "framework", "rev-scope", _envelope("discovery_scope", {
            "scope": "s", "date_boundary": "none", "language_boundary": "english",
            "source_boundary": [], "intended_synthesis": "syn", "terminology": [], "exclusions": [],
        }))
        self._put("a-rubric", "rubric", "rev-rubric", _envelope("discovery_rubric", {
            "eligibility_rules": ["r"], "uncertain_action": "retain", "limitations": [],
        }))
        payload = {
            "protocol": "p",
            "brief_ref": self.ref("a-brief", "rev-brief"),
            "interview_ref": self.ref("a-interview", "rev-interview"),
            "seed_refs": [],
            "framing_ref": self.ref("a-framing", "rev-framing"),
            "scope_ref": self.ref("a-scope", "rev-scope"),
            "rubric_ref": self.ref("a-rubric", "rev-rubric"),
            "queries": [],
            "complementary_sources": [],
            "expansion": {"max_hops": 0, "max_sources": 0, "max_requests": 0, "page_size": 1, "max_pages": 1},
            "impact_review": {"previous_plan": None, "affected": [], "rationale": ""},
        }
        # Persisted WITHOUT the exact dependency pins.
        self._put("a-plan", "report", "rev-plan", _envelope("discovery_plan", payload))
        with self.assertRaises(DiscoveryContextError):
            read_context_document(self.store, self.ref("a-plan", "rev-plan"))

    def test_plan_read_rejects_wrong_typed_direct_reference(self) -> None:
        wrong = {
            "brief_ref": ("a-w-brief", "report", "discovery_scope", _scope_payload()),
            "interview_ref": ("a-w-interview", "report", "discovery_brief", _brief_payload()),
            "framing_ref": ("a-w-framing", "framework", "discovery_brief", _brief_payload()),
            "scope_ref": ("a-w-scope", "framework", "discovery_framing", _framing_payload()),
            "rubric_ref": ("a-w-rubric", "report", "discovery_brief", _brief_payload()),
        }
        for aid, kind, dtype, payload in wrong.values():
            self._put(aid, kind, "rev-w", _envelope(dtype, payload))
        for field, (aid, _kind, _dtype, _payload) in wrong.items():
            plan_ref, _seed = self.build_plan_fixture(
                plan_artifact=f"a-plan-{field}",
                plan_revision="rev-wrong",
                override={field: self.ref(aid, "rev-w")},
            )
            with self.assertRaises(DiscoveryContextError):
                read_context_document(self.store, plan_ref)

    def test_plan_read_rejects_invalid_referenced_payload(self) -> None:
        invalid = {
            "brief_ref": ("a-i-brief", "report", "discovery_brief", {"objective": "o"}),
            "interview_ref": ("a-i-interview", "report", "discovery_interview", {
                "brief_ref": self.ref("a-brief", "rev-brief"), "turns": [],
                "ready_to_advance": "yes",
            }),
            "framing_ref": ("a-i-framing", "framework", "discovery_framing", {
                "alternatives": [], "counterevidence_queries": [], "limitations": [],
            }),
            "scope_ref": ("a-i-scope", "framework", "discovery_scope", {"scope": "s"}),
            "rubric_ref": ("a-i-rubric", "rubric", "discovery_rubric", {
                "eligibility_rules": [], "uncertain_action": "exclude", "limitations": [],
            }),
        }
        for aid, kind, dtype, payload in invalid.values():
            self._put(aid, kind, "rev-i", _envelope(dtype, payload))
        for field, (aid, _kind, _dtype, _payload) in invalid.items():
            plan_ref, _seed = self.build_plan_fixture(
                plan_artifact=f"a-plan-{field}",
                plan_revision="rev-invalid",
                override={field: self.ref(aid, "rev-i")},
            )
            with self.assertRaises(DiscoveryContextError):
                read_context_document(self.store, plan_ref)

    def test_plan_read_rejects_fake_seed_approval_on_non_seed(self) -> None:
        # A generic artifact_revision gate named discovery.seed.<id> over a
        # non-seed artifact satisfies the *name* but must NOT satisfy a plan.
        self.put_brief(aid="a-brief", rid="rev-brief")
        self._put("a-fake-seed", "report", "rev-fake",
                  _envelope("discovery_brief", _brief_payload()))
        self.approve("a-fake-seed", "rev-fake", ARTIFACT_REVISION_GATE_KIND,
                     ctx.SEED_GATE_PREFIX + "a-fake-seed")
        approvals = HumanApprovalRepository(self.store)
        self.assertTrue(
            approvals.named_gate_satisfied(
                "a-fake-seed", gate_kind=ARTIFACT_REVISION_GATE_KIND,
                gate_id=ctx.SEED_GATE_PREFIX + "a-fake-seed",
            )
        )
        fake_seed = self.ref("a-fake-seed", "rev-fake")
        plan_ref, _seed = self.build_plan_fixture(
            plan_revision="rev-fake-seed", seed_refs_override=[fake_seed],
        )
        with self.assertRaises(DiscoveryContextError):
            read_context_document(self.store, plan_ref)

    def test_plan_read_requires_current_direct_reference(self) -> None:
        plan_ref, _seed = self.build_plan_fixture()
        self.assertIsNotNone(read_context_document(self.store, plan_ref))
        # A substantive change to a pinned direct reference makes the current read stale.
        self._put("a-framing", "framework", "rev-framing2",
                  _envelope("discovery_framing", _framing_payload(alternatives=["alt2"])),
                  parent="rev-framing")
        with self.assertRaises(DiscoveryContextError):
            read_context_document(self.store, plan_ref)
        historical = read_context_document(self.store, plan_ref, require_current=False)
        self.assertEqual(historical["document_type"], "discovery_plan")

    def test_plan_historic_read_of_amended_plan(self) -> None:
        plan_ref, _seed = self.build_plan_fixture()
        self.build_plan_fixture(
            plan_revision="rev-plan2", parent="rev-plan", previous_plan=plan_ref,
            affected=["scope"], rationale="substantive",
        )
        # The superseded plan revision is not current ...
        with self.assertRaises(DiscoveryContextError):
            read_context_document(self.store, plan_ref)
        # ... but a historical read of the exact revision resolves.
        historical = read_context_document(self.store, plan_ref, require_current=False)
        self.assertEqual(historical["document_type"], "discovery_plan")

    def test_plan_read_rejects_corrupted_referenced_bytes(self) -> None:
        plan_ref, _seed = self.build_plan_fixture()
        self.assertIsNotNone(read_context_document(self.store, plan_ref))
        self._corrupt_revision_bytes("a-scope", "rev-scope", b'{"schema_version":1}')
        with self.assertRaises(DiscoveryContextError):
            read_context_document(self.store, plan_ref)

    def test_plan_amendment_requires_prior_plan_reference(self) -> None:
        # Initial plan (no parent) must have a null previous_plan.
        plan_ref, _seed_ref = self.build_plan_fixture()
        self.assertIsNotNone(read_context_document(self.store, plan_ref))
        # A revision with a parent must reference the exact parent as previous_plan.
        self.build_plan_fixture(
            plan_revision="rev-plan2", parent="rev-plan", previous_plan=None,
            affected=["scope"], rationale="r",
        )
        with self.assertRaises(DiscoveryContextError):
            read_context_document(self.store, self.ref("a-plan", "rev-plan2"))
        # A previous_plan that is not the exact parent is refused.
        self.build_plan_fixture(
            plan_revision="rev-plan3", parent="rev-plan2",
            previous_plan=self.ref("a-plan", "rev-plan"),
            affected=["scope"], rationale="r",
        )
        with self.assertRaises(DiscoveryContextError):
            read_context_document(self.store, self.ref("a-plan", "rev-plan3"))
        # A correct substantive amendment (prior ref + affected + rationale) reads.
        self.build_plan_fixture(
            plan_revision="rev-plan4", parent="rev-plan3",
            previous_plan=self.ref("a-plan", "rev-plan3"),
            affected=["brief"], rationale="substantive change",
        )
        document = read_context_document(self.store, self.ref("a-plan", "rev-plan4"))
        self.assertEqual(
            document["payload"]["impact_review"]["previous_plan"]["revision_id"], "rev-plan3"
        )


if __name__ == "__main__":
    unittest.main()
