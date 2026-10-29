"""Execution tests for the approved-plan search/import library (Step 05, D2/D3).

Covers ``src/review_article_agent/discovery/execution.py`` against D2/D3
acceptance criteria 1-4/7 and the specification's workflow steps 2-5 plus the
gates/safety invariants:

* ``execute_query`` reads the exact query/source/budgets from the currently
  **approved** plan only (no caller override) and executes nothing for a missing,
  rejected, stale, wrongly-gated ("fake"), or cross-project plan;
* the plan and the execution activation are revalidated before **every**
  low-level attempt and again before persisting, so a changed gate/dependency or
  a drain/pause stops further attempts and accepts no result under stale scope;
* an identical replay returns the original observation without any network;
* a bounded exploratory ``seed_search`` honours hard ceilings, needs an active
  token, keeps the exact query, and never creates accepted context;
* ``import_metadata`` requires an exact project-approved ``metadata_import``
  complementary source and records an honest, attempt-free, local
  metadata-only observation (duplicates and missing identifiers preserved).

Synthetic data only; the injected low-level transport is a deterministic fake, so
no test performs a network, provider, or model call.

Run::

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_discovery_execution.py'
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

from review_article_agent.discovery import execution as ex  # noqa: E402
from review_article_agent.discovery.execution import (  # noqa: E402
    DiscoveryExecutionBlockedError,
    DiscoveryExecutionError,
    DiscoveryExecutor,
    query_fingerprint,
)
from review_article_agent.discovery.records import MetadataRecord  # noqa: E402
from review_article_agent.discovery.transport import TransportResponse  # noqa: E402
from review_article_agent.runtime import owner_auth  # noqa: E402
from review_article_agent.runtime.gates import prepare_gate_snapshot  # noqa: E402
from review_article_agent.runtime.owner_auth import OwnerApproval  # noqa: E402
from review_article_agent.storage import (  # noqa: E402
    ArtifactRepository,
    ProjectStore,
    Registry,
)
from review_article_agent.storage import paths  # noqa: E402
from review_article_agent.storage.discovery import DiscoveryRepository  # noqa: E402

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")
_BASE_TIME = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
_FIXED_CHALLENGE = "TEST-CHAL-EXEC-1"
_OWNER_UID = 1000
_OBSERVED_AT = "2026-10-28T12:00:00+00:00"


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


class _FakeLowlevel:
    """Deterministic low-level transport fake (``(url, *, timeout, ...)``).

    Returns the indexed ``(status, body)`` response for each call and records the
    number of real low-level invocations, so a test can prove a blocked guard made
    **zero** or **one** attempt.
    """

    def __init__(self, responses, on_call=None):
        self._responses = list(responses)
        self.on_call = on_call
        self.calls = 0

    def __call__(self, url, *, timeout, max_response_bytes):
        index = self.calls
        self.calls += 1
        if self.on_call is not None:
            self.on_call(index)
        status, body = self._responses[min(index, len(self._responses) - 1)]
        return TransportResponse(status=status, body=body)


def _epmc_page(*ids, hit_count=None, next_cursor=None):
    results = [
        {"source": "MED", "id": pid, "pmid": pid, "title": f"t{pid}", "pubYear": "2020"}
        for pid in ids
    ]
    payload = {"hitCount": hit_count if hit_count is not None else len(ids),
               "resultList": {"result": results}}
    if next_cursor is not None:
        payload["nextCursorMark"] = next_cursor
    return json.dumps(payload).encode("utf-8")


class ExecBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-exec-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.clock = [_BASE_TIME]
        self.mono = [1000.0]

        self.registry = Registry(self.tmp / "state")
        self.root = self.tmp / "p"
        self.info = self.registry.create_project(self.root, name="P")
        self.other = self.registry.create_project(self.tmp / "q", name="Q")
        self.store = ProjectStore.open_at(paths.app_dir_for(str(self.root)))
        self.addCleanup(self.store.close)
        self.repo = ArtifactRepository(self.store)
        self.project_id = self.store.project_id
        # No real sleeps between transport attempts.
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
        parent=None,
        previous_plan=None,
        affected=None,
        rationale="",
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
            "impact_review": {
                "previous_plan": previous_plan,
                "affected": affected if affected is not None else [],
                "rationale": rationale,
            },
        }
        self._put(plan_artifact, "report", plan_revision,
                  self._env("discovery_plan", payload), dependencies=refs,
                  parent=parent)
        return self.ref(plan_artifact, plan_revision)

    def approve_plan(self, plan_ref, *, gate_kind=ex.PROTOCOL_GATE_KIND, gate_id=ex.PLAN_GATE_ID,
                     decision="approved"):
        self.approve(plan_ref["artifact_id"], plan_ref["revision_id"], gate_kind, gate_id,
                     decision=decision)

    def activate(self, project_id=None):
        return self.registry.activate_execution(project_id or self.project_id)

    def executor(self, *, token=None, transport=None, project=None):
        return DiscoveryExecutor(
            self.store,
            registry=self.registry,
            activation_token=token if token is not None else self._token,
            transport=transport,
        )

    def set_token(self, token):
        self._token = token

    def rows(self):
        return DiscoveryRepository(self.store).list_searches()


# --------------------------------------------------------------------------- #
# execute_query: approval/scope gates and zero-I/O refusals
# --------------------------------------------------------------------------- #
class ExecuteQueryGateTest(ExecBase):
    def setUp(self) -> None:
        super().setUp()
        self.set_token(self.activate().activation_token)

    def _no_io(self, plan_ref, *, query_index=0):
        fake = _FakeLowlevel([(200, _epmc_page("1"))])
        with self.assertRaises(DiscoveryExecutionBlockedError):
            self.executor(transport=fake).execute_query(plan_ref, query_index, request_id="req-1")
        self.assertEqual(fake.calls, 0)
        self.assertEqual(self.rows(), [])

    def test_construct_performs_no_io(self) -> None:
        fake = _FakeLowlevel([(200, _epmc_page("1"))])
        DiscoveryExecutor(self.store, registry=self.registry, activation_token="x", transport=fake)
        self.assertEqual(fake.calls, 0)

    def test_missing_plan_is_blocked_with_zero_io(self) -> None:
        plan_ref = self.build_plan()
        self._no_io(plan_ref)

    def test_rejected_plan_is_blocked(self) -> None:
        plan_ref = self.build_plan()
        self.approve_plan(plan_ref, decision="rejected")
        self._no_io(plan_ref)

    def test_wrong_gate_kind_is_blocked(self) -> None:
        plan_ref = self.build_plan()
        self.approve_plan(plan_ref, gate_kind="pilot")
        self._no_io(plan_ref)

    def test_stale_dependency_is_blocked(self) -> None:
        plan_ref = self.build_plan()
        self.approve_plan(plan_ref)
        self._put("a-brief", "report", "rev-brief2", self._env("discovery_brief", {
            "objective": "revised", "starting_hypothesis": "h", "questions": [],
        }), parent="rev-brief")
        self._no_io(plan_ref)

    def test_cross_project_plan_ref_is_blocked(self) -> None:
        plan_ref = self.build_plan()
        self.approve_plan(plan_ref)
        foreign = {"project_id": "other-project", "artifact_id": "a-plan", "revision_id": "rev-plan"}
        fake = _FakeLowlevel([(200, _epmc_page("1"))])
        # A cross-project ref is refused during argument validation, before any
        # plan read or provider I/O (no provider call, no stored row).
        with self.assertRaises(DiscoveryExecutionError):
            self.executor(transport=fake).execute_query(foreign, 0, request_id="req-1")
        self.assertEqual(fake.calls, 0)
        self.assertEqual(self.rows(), [])

    def test_out_of_range_query_index_refused_before_io(self) -> None:
        plan_ref = self.build_plan()
        self.approve_plan(plan_ref)
        fake = _FakeLowlevel([(200, _epmc_page("1"))])
        for bad in (1, -1, True, "0"):
            with self.assertRaises(DiscoveryExecutionError):
                self.executor(transport=fake).execute_query(plan_ref, bad, request_id="req-1")
        self.assertEqual(fake.calls, 0)


# --------------------------------------------------------------------------- #
# execute_query: exact execution, provenance, replay, revalidation
# --------------------------------------------------------------------------- #
class ExecuteQueryRunTest(ExecBase):
    def setUp(self) -> None:
        super().setUp()
        self.set_token(self.activate().activation_token)
        self.plan_ref = self.build_plan()
        self.approve_plan(self.plan_ref)

    def test_executes_exact_plan_query_and_preserves_provenance(self) -> None:
        fake = _FakeLowlevel([(200, _epmc_page("31452104"))])
        record = self.executor(transport=fake).execute_query(self.plan_ref, 0, request_id="req-1")
        self.assertEqual(fake.calls, 1)
        self.assertEqual(record.query, "asthma AND review")  # exact, no hidden binding
        self.assertNotIn("dxq", record.query)
        self.assertEqual(record.source, "europepmc")
        self.assertEqual(record.record_count, 1)
        self.assertEqual(record.total_count, 1)
        self.assertEqual(record.page_count, 1)
        self.assertTrue(record.complete)
        self.assertEqual(record.attempt_count, 1)
        self.assertEqual([r.source_id for r in record.result.records], ["MED:31452104"])

    def test_replay_returns_original_without_network(self) -> None:
        fake = _FakeLowlevel([(200, _epmc_page("31452104"))])
        executor = self.executor(transport=fake)
        first = executor.execute_query(self.plan_ref, 0, request_id="req-1")
        second = executor.execute_query(self.plan_ref, 0, request_id="req-1")
        self.assertEqual(first.run_id, second.run_id)
        self.assertEqual(first.recorded_at_utc, second.recorded_at_utc)
        self.assertEqual(fake.calls, 1)
        self.assertEqual(len(self.rows()), 1)

    def test_replay_survives_reopen(self) -> None:
        fake = _FakeLowlevel([(200, _epmc_page("31452104"))])
        first = self.executor(transport=fake).execute_query(self.plan_ref, 0, request_id="req-1")
        self.store.close()
        self.store = ProjectStore.open_at(paths.app_dir_for(str(self.root)))
        self.addCleanup(self.store.close)
        replay_fake = _FakeLowlevel([(200, _epmc_page("999"))])
        second = self.executor(transport=replay_fake).execute_query(
            self.plan_ref, 0, request_id="req-1"
        )
        self.assertEqual(first.run_id, second.run_id)
        self.assertEqual(replay_fake.calls, 0)

    def test_partial_provider_failure_is_stored_exactly(self) -> None:
        fake = _FakeLowlevel([(429, b"")])
        record = self.executor(transport=fake).execute_query(self.plan_ref, 0, request_id="req-fail")
        self.assertFalse(record.complete)
        self.assertEqual(record.stop_reason, "failure")
        self.assertEqual(record.failure, "attempts_exhausted")
        self.assertEqual(record.attempt_count, 3)
        self.assertEqual(record.record_count, 0)

    def test_different_query_indices_are_separate_provenance(self) -> None:
        ref = self.build_plan(
            plan_artifact="a-plan2", plan_revision="rev-plan2",
            queries=[
                {"source": "europepmc", "query": "alpha", "page_size": 5, "max_pages": 1},
                {"source": "europepmc", "query": "beta", "page_size": 5, "max_pages": 1},
            ],
        )
        self.approve_plan(ref)
        fake = _FakeLowlevel([(200, _epmc_page("1"))])
        executor = self.executor(transport=fake)
        a = executor.execute_query(ref, 0, request_id="req-a")
        b = executor.execute_query(ref, 1, request_id="req-b")
        self.assertNotEqual(a.run_id, b.run_id)
        self.assertEqual(a.query, "alpha")
        self.assertEqual(b.query, "beta")


class ExecuteQueryRevalidationTest(ExecBase):
    def setUp(self) -> None:
        super().setUp()
        self.set_token(self.activate().activation_token)

    def test_gate_changed_between_pages_stops_further_attempts(self) -> None:
        plan_ref = self.build_plan(queries=[
            {"source": "europepmc", "query": "asthma", "page_size": 1, "max_pages": 2}
        ])
        self.approve_plan(plan_ref)

        def on_call(index):
            if index == 0:
                # After the first page, amend a pinned dependency so the plan is stale.
                self._put("a-brief", "report", "rev-briefX", self._env("discovery_brief", {
                    "objective": "amended", "starting_hypothesis": "h", "questions": [],
                }), parent="rev-brief")

        fake = _FakeLowlevel(
            [(200, _epmc_page("1", hit_count=2, next_cursor="C1"))], on_call=on_call
        )
        with self.assertRaises(DiscoveryExecutionBlockedError):
            self.executor(transport=fake).execute_query(plan_ref, 0, request_id="req-1")
        self.assertEqual(fake.calls, 1)  # page 2 never issued
        self.assertEqual(self.rows(), [])

    def test_drain_between_retries_stops_further_attempts_and_keeps_partial(self) -> None:
        plan_ref = self.build_plan()
        self.approve_plan(plan_ref)
        token = self._token

        def on_call(index):
            if index == 0:
                self.registry.begin_drain(token)

        fake = _FakeLowlevel([(429, b""), (200, _epmc_page("1"))], on_call=on_call)
        # A drain after the first attempt stops the retry before its HTTP request;
        # the received (failed) page is still committed as an explicit partial
        # observation under the drained token (allow_draining reconciliation).
        record = self.executor(transport=fake).execute_query(
            plan_ref, 0, request_id="req-1"
        )
        self.assertEqual(fake.calls, 1)  # retry refused before its HTTP attempt
        self.assertFalse(record.complete)
        self.assertEqual(record.stop_reason, "failure")
        self.assertEqual(record.failure, "attempts_exhausted")
        self.assertEqual(record.attempt_count, 1)  # the one real attempt
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].run_id, record.run_id)

    def test_drain_between_pages_preserves_observed_page(self) -> None:
        # A drain after page 1 stops page 2's HTTP attempt (the pre-attempt guard
        # is active-only) yet the already-observed page 1 is preserved: the opt-in
        # interrupt yields an explicit partial result and the completion path
        # commits it under the drained token.
        plan_ref = self.build_plan(queries=[
            {"source": "europepmc", "query": "asthma", "page_size": 1, "max_pages": 2}
        ])
        self.approve_plan(plan_ref)
        token = self._token

        def on_call(index):
            if index == 0:
                self.registry.begin_drain(token)

        fake = _FakeLowlevel(
            [(200, _epmc_page("1", hit_count=2, next_cursor="C1"))], on_call=on_call
        )
        record = self.executor(transport=fake).execute_query(
            plan_ref, 0, request_id="req-drain-page"
        )
        self.assertEqual(fake.calls, 1)  # page 2 refused before its HTTP attempt
        self.assertFalse(record.complete)
        self.assertEqual(record.stop_reason, "failure")
        self.assertEqual(record.failure, "attempts_exhausted")
        self.assertEqual([r.source_id for r in record.result.records], ["MED:1"])
        self.assertEqual(record.attempt_count, 1)  # only the real page-1 attempt
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].run_id, record.run_id)

    def test_scope_change_after_response_not_accepted(self) -> None:
        plan_ref = self.build_plan()
        self.approve_plan(plan_ref)

        def on_call(index):
            if index == 0:
                self._put("a-brief", "report", "rev-briefZ", self._env("discovery_brief", {
                    "objective": "post-response", "starting_hypothesis": "h", "questions": [],
                }), parent="rev-brief")

        fake = _FakeLowlevel([(200, _epmc_page("1"))], on_call=on_call)
        with self.assertRaises(DiscoveryExecutionBlockedError):
            self.executor(transport=fake).execute_query(plan_ref, 0, request_id="req-1")
        self.assertEqual(fake.calls, 1)
        self.assertEqual(self.rows(), [])


# --------------------------------------------------------------------------- #
# Activation fencing
# --------------------------------------------------------------------------- #
class ActivationFenceTest(ExecBase):
    def test_wrong_token_is_blocked(self) -> None:
        plan_ref = self.build_plan()
        self.approve_plan(plan_ref)
        self.activate()
        fake = _FakeLowlevel([(200, _epmc_page("1"))])
        with self.assertRaises(DiscoveryExecutionBlockedError):
            self.executor(token="act_deadbeef", transport=fake).execute_query(
                plan_ref, 0, request_id="req-1"
            )
        self.assertEqual(fake.calls, 0)

    def test_other_project_active_is_blocked(self) -> None:
        plan_ref = self.build_plan()
        self.approve_plan(plan_ref)
        self.registry.activate_execution(self.other.project_id)
        token = self.registry.activation().activation_token
        fake = _FakeLowlevel([(200, _epmc_page("1"))])
        with self.assertRaises(DiscoveryExecutionBlockedError):
            self.executor(token=token, transport=fake).execute_query(plan_ref, 0, request_id="req-1")
        self.assertEqual(fake.calls, 0)

    def test_draining_is_blocked_before_io(self) -> None:
        plan_ref = self.build_plan()
        self.approve_plan(plan_ref)
        token = self.activate().activation_token
        self.registry.begin_drain(token)
        fake = _FakeLowlevel([(200, _epmc_page("1"))])
        with self.assertRaises(DiscoveryExecutionBlockedError):
            self.executor(token=token, transport=fake).execute_query(plan_ref, 0, request_id="req-1")
        self.assertEqual(fake.calls, 0)
        self.assertEqual(self.rows(), [])


# --------------------------------------------------------------------------- #
# seed_search
# --------------------------------------------------------------------------- #
class SeedSearchTest(ExecBase):
    def setUp(self) -> None:
        super().setUp()
        self.set_token(self.activate().activation_token)

    def test_bounded_seed_search_is_provisional_with_exact_query(self) -> None:
        fake = _FakeLowlevel([(200, _epmc_page("1"))])
        record = self.executor(transport=fake).seed_search(
            "europepmc", "seed query", request_id="seed-1", page_size=5, max_pages=1
        )
        self.assertEqual(fake.calls, 1)
        self.assertEqual(record.query, "seed query")
        self.assertEqual(record.record_count, 1)
        # No plan and no accepted context: only an inert observation exists.
        self.assertEqual(len(self.rows()), 1)

    def test_hard_ceiling_refused_before_io(self) -> None:
        fake = _FakeLowlevel([(200, _epmc_page("1"))])
        executor = self.executor(transport=fake)
        for page_size, max_pages in ((21, 1), (5, 2), (11, 2), (0, 1), (1, 0), (5, True)):
            with self.assertRaises(DiscoveryExecutionError):
                executor.seed_search(
                    "europepmc", "q", request_id="seed-x",
                    page_size=page_size, max_pages=max_pages,
                )
        self.assertEqual(fake.calls, 0)
        self.assertEqual(self.rows(), [])

    def test_unsupported_source_refused(self) -> None:
        fake = _FakeLowlevel([(200, _epmc_page("1"))])
        with self.assertRaises(DiscoveryExecutionError):
            self.executor(transport=fake).seed_search(
                "scholar", "q", request_id="seed-x", page_size=5, max_pages=1
            )
        self.assertEqual(fake.calls, 0)

    def test_seed_search_requires_active_token(self) -> None:
        fake = _FakeLowlevel([(200, _epmc_page("1"))])
        executor = DiscoveryExecutor(
            self.store, registry=self.registry, activation_token="bad", transport=fake
        )
        with self.assertRaises(DiscoveryExecutionBlockedError):
            executor.seed_search("europepmc", "q", request_id="seed-x", page_size=5, max_pages=1)
        self.assertEqual(fake.calls, 0)

    def test_seed_replay_no_network(self) -> None:
        fake = _FakeLowlevel([(200, _epmc_page("1"))])
        executor = self.executor(transport=fake)
        first = executor.seed_search("europepmc", "q", request_id="seed-1", page_size=5, max_pages=1)
        second = executor.seed_search("europepmc", "q", request_id="seed-1", page_size=5, max_pages=1)
        self.assertEqual(first.run_id, second.run_id)
        self.assertEqual(fake.calls, 1)


# --------------------------------------------------------------------------- #
# import_metadata
# --------------------------------------------------------------------------- #
class ImportMetadataTest(ExecBase):
    def setUp(self) -> None:
        super().setUp()
        self.set_token(self.activate().activation_token)
        self.plan_ref = self.build_plan(comp=[
            {"source": "regulatory", "mode": "metadata_import", "rationale": "project opt-in"}
        ])
        self.approve_plan(self.plan_ref)

    def test_unratified_source_is_blocked_with_zero_records(self) -> None:
        with self.assertRaises(DiscoveryExecutionBlockedError):
            self.executor().import_metadata(
                self.plan_ref, source="not-in-plan",
                records=[{"source": "not-in-plan", "pmid": "1"}],
                origin_locator="note:batch", observed_at=_OBSERVED_AT, request_id="imp-1",
            )
        self.assertEqual(self.rows(), [])

    def test_approved_source_records_honest_local_observation(self) -> None:
        records = [
            MetadataRecord(source="regulatory", source_id="R-1", pmid="111", title="a"),
            {"source": "regulatory", "source_id": "R-2", "title": "b"},  # strict mapping
            {"source": "regulatory", "source_id": "R-1", "pmid": "111", "title": "a"},  # duplicate
            {"source": "regulatory", "title": "no identifiers"},  # missing identifiers
        ]
        record = self.executor().import_metadata(
            self.plan_ref, source="regulatory", records=records,
            origin_locator="note:batch-1", observed_at=_OBSERVED_AT, request_id="imp-1",
        )
        self.assertEqual(record.source, "regulatory")
        self.assertEqual(record.query, "metadata_import:regulatory")
        self.assertEqual(record.result.pages[0].requested_url, "note:batch-1")
        self.assertEqual(record.result.pages[0].retrieved_at, _OBSERVED_AT)
        self.assertEqual(record.attempt_count, 0)
        self.assertEqual(record.result.attempts, ())
        self.assertEqual(record.result.pages[0].attempts, ())
        self.assertIsNone(record.total_count)
        self.assertEqual(record.record_count, 4)
        # Duplicates and missing identifiers are preserved verbatim.
        self.assertEqual([r.source_id for r in record.result.records],
                         ["R-1", "R-2", "R-1", None])
        self.assertTrue(record.complete)

    def test_record_source_mismatch_refused(self) -> None:
        with self.assertRaises(DiscoveryExecutionError):
            self.executor().import_metadata(
                self.plan_ref, source="regulatory",
                records=[{"source": "pubmed", "pmid": "1"}],
                origin_locator="note:batch", observed_at=_OBSERVED_AT, request_id="imp-2",
            )
        self.assertEqual(self.rows(), [])

    def test_unknown_record_field_refused(self) -> None:
        with self.assertRaises(DiscoveryExecutionError):
            self.executor().import_metadata(
                self.plan_ref, source="regulatory",
                records=[{"source": "regulatory", "pmid": "1", "extra": True}],
                origin_locator="note:batch", observed_at=_OBSERVED_AT, request_id="imp-3",
            )
        self.assertEqual(self.rows(), [])

    def test_bad_observed_at_refused(self) -> None:
        for bad in ("2026-10-28", "not-a-time", "2026-10-28T12:00:00"):
            with self.assertRaises(DiscoveryExecutionError):
                self.executor().import_metadata(
                    self.plan_ref, source="regulatory",
                    records=[{"source": "regulatory", "pmid": "1"}],
                    origin_locator="note:batch", observed_at=bad, request_id="imp-4",
                )
        self.assertEqual(self.rows(), [])

    def test_technical_record_bound_is_explicit(self) -> None:
        self.assertEqual(ex.MAX_IMPORT_RECORDS, 100_000)
        with mock.patch.object(ex, "MAX_IMPORT_RECORDS", 2):
            with self.assertRaises(DiscoveryExecutionError):
                self.executor().import_metadata(
                    self.plan_ref, source="regulatory",
                    records=[{"source": "regulatory", "pmid": str(i)} for i in range(3)],
                    origin_locator="note:batch", observed_at=_OBSERVED_AT, request_id="imp-5",
                )
        self.assertEqual(self.rows(), [])

    def test_non_portable_record_refused_before_store(self) -> None:
        with self.assertRaises(DiscoveryExecutionError):
            self.executor().import_metadata(
                self.plan_ref, source="regulatory",
                records=[{"source": "regulatory", "source_id": "R-1", "title": "bad\ud800"}],
                origin_locator="note:batch", observed_at=_OBSERVED_AT, request_id="imp-7",
            )
        self.assertEqual(self.rows(), [])

    def test_import_replay_no_duplicate(self) -> None:
        records = [{"source": "regulatory", "source_id": "R-1", "pmid": "111"}]
        executor = self.executor()
        first = executor.import_metadata(
            self.plan_ref, source="regulatory", records=records,
            origin_locator="note:batch-1", observed_at=_OBSERVED_AT, request_id="imp-1",
        )
        second = executor.import_metadata(
            self.plan_ref, source="regulatory", records=records,
            origin_locator="note:batch-1", observed_at=_OBSERVED_AT, request_id="imp-1",
        )
        self.assertEqual(first.run_id, second.run_id)
        self.assertEqual(len(self.rows()), 1)

    def test_import_requires_active_token(self) -> None:
        executor = DiscoveryExecutor(
            self.store, registry=self.registry, activation_token="bad"
        )
        with self.assertRaises(DiscoveryExecutionBlockedError):
            executor.import_metadata(
                self.plan_ref, source="regulatory",
                records=[{"source": "regulatory", "pmid": "1"}],
                origin_locator="note:batch", observed_at=_OBSERVED_AT, request_id="imp-6",
            )
        self.assertEqual(self.rows(), [])


# --------------------------------------------------------------------------- #
# query_fingerprint helper
# --------------------------------------------------------------------------- #
class QueryFingerprintTest(unittest.TestCase):
    def test_stable_and_distinct(self) -> None:
        a = query_fingerprint(source="europepmc", query="q", page_size=5, max_pages=1)
        b = query_fingerprint(source="europepmc", query="q", page_size=5, max_pages=1)
        c = query_fingerprint(source="europepmc", query="q2", page_size=5, max_pages=1)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(len(a), 64)
        self.assertNotIn("q", a)  # hashed, never echoed


if __name__ == "__main__":
    unittest.main()
