"""Acceptance tests for the durable pre-transport session-turn intent (task T7).

Covers ``src/review_article_agent/storage/session_intents.py`` against the T7
requirements and ``docs/implementation/04-agent-runtime.md`` deliverable 2 /
acceptance 4 and 7, plus ``docs/contracts/02-gates-and-runtime.md`` §6.1 and §8.1,
over a **real** temporary project store and a **real** planner-produced
:class:`SessionTurnSpec` (the same fixture as ``test_session_turns``):

* one immutable correlation row per dispatch, storing the exact spec canonical
  bytes, exact message bytes, their digests, IDs, fence, and time;
* restart reload reconstructs the identical spec/message/admission IDs without a
  live service or manager;
* identical replay is idempotent (even after submission and a moved head), while a
  divergent replay is refused;
* a wrong fence and a non-reserved new write are refused and write nothing;
* a relabelled/cross-project spec and forged-but-self-consistent projections
  (role/scope/target/input/model/config/version/message) are refused against the
  independent persisted parents;
* historical read survives a moved task input head (no currentness check);
* corrupt bytes/hash/columns/message, foreign, and orphan rows fail the read and/or
  the read-only integrity sweep;
* a forced validation failure leaves zero rows and the dispatch's state/cost
  untouched;
* the append-only triggers and the unique session/admission tuple are enforced;
* a schema 9 -> 10 upgrade preserves predecessor rows and leaves versions 1..9 in
  place.

No network, no credential, no paid call. Run::

    PYTHONPATH=src:tests python3 -m unittest test_session_intents
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from _tiers import slow

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.domain.records import canonical_json  # noqa: E402
from review_article_agent.runtime.session_turns import SessionTurnSpec  # noqa: E402
from review_article_agent.storage import (  # noqa: E402
    ArtifactRepository,
    ProjectStore,
    paths,
    schema,
)
from review_article_agent.storage.session_intents import (  # noqa: E402
    SESSION_INTENT_SCHEMA_VERSION,
    SessionIntentConflictError,
    SessionIntentCorruptionError,
    SessionIntentError,
    SessionIntentFenceError,
    SessionIntentRepository,
    SessionIntentStateError,
    SessionIntentValidationError,
    session_intent_integrity_problems,
)
from test_session_turns import SessionTurnPlanTestBase  # noqa: E402


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _intent_id(project_id: str, dispatch_id: str) -> str:
    material = canonical_json([project_id, dispatch_id])
    return "sint_" + hashlib.sha256(material).hexdigest()[:32]


def _forge_spec(spec: SessionTurnSpec, mutate) -> SessionTurnSpec:
    """Build an internally self-consistent forged spec (parents are NOT checked).

    ``mutate(plan, message)`` edits the canonical plan/message projections; the
    helper then re-synchronises the projections the spec validator cross-checks,
    recomputes the version/message digests, and constructs the strict value object.
    """
    plan = spec.to_mapping()
    message = json.loads(spec.message.decode("utf-8"))
    mutate(plan, message)
    message.update(
        {
            "kind": "session_turn_input",
            "project_id": plan["project_id"],
            "role": plan["role"],
            "task_id": plan["task"]["task_id"],
            "task_fingerprint": plan["task"]["fingerprint"],
            "dispatch_id": plan["dispatch"]["dispatch_id"],
            "mapping_id": plan["session"]["mapping_id"],
            "session_id": plan["session"]["session_id"],
            "scope": plan["task"]["scope"],
            "target": plan["task"]["target"],
            "input_refs": plan["task"]["input_refs"],
            "prompt_version": plan["task"]["prompt_version"],
            "size_limit": plan["task"]["size_limit"],
            "step_limit": plan["task"]["step_limit"],
            "versions": plan["versions"],
        }
    )
    plan["dispatch"]["version_digest"] = _sha(canonical_json(plan["versions"]))
    message_bytes = canonical_json(message)
    plan["message_digest"] = _sha(message_bytes)
    canonical = canonical_json(plan)
    return SessionTurnSpec(
        project_id=plan["project_id"],
        role=plan["role"],
        task_id=plan["task"]["task_id"],
        task_fingerprint=plan["task"]["fingerprint"],
        dispatch_id=plan["dispatch"]["dispatch_id"],
        dispatch_request_id=plan["dispatch"]["request_id"],
        mapping_id=plan["session"]["mapping_id"],
        session_id=plan["session"]["session_id"],
        service_instance_id=plan["session"]["service_instance_id"],
        clean_location=plan["session"]["clean_location"],
        config_snapshot_id=plan["session"]["config_snapshot_id"],
        config_digest=plan["session"]["config_digest"],
        model=plan["session"]["model"],
        admission_id=plan["admission_id"],
        input_digest=plan["dispatch"]["input_digest"],
        version_digest=plan["dispatch"]["version_digest"],
        prompt_version=plan["task"]["prompt_version"],
        step_limit=plan["task"]["step_limit"],
        size_limit=plan["task"]["size_limit"],
        fresh_context_required=plan["fresh_context_required"],
        message_digest=plan["message_digest"],
        canonical=canonical,
        message=message_bytes,
    )


class SessionIntentTestBase(SessionTurnPlanTestBase):
    def _record(self, scenario, spec=None, *, fence=1):
        spec = self._prepare(scenario) if spec is None else spec
        return SessionIntentRepository(scenario.store).record_intent(spec, fence=fence)

    def _count(self, scenario) -> int:
        return int(
            scenario.store.connection.execute(
                "SELECT COUNT(*) FROM session_turn_intent"
            ).fetchone()[0]
        )

    def _fixture(self):
        scenario = self._scenario()
        spec = self._prepare(scenario)
        record = SessionIntentRepository(scenario.store).record_intent(spec, fence=1)
        return scenario, spec, record

    def _drop_trigger(self, scenario, name: str) -> None:
        scenario.store.connection.execute(f"DROP TRIGGER {name}")


@slow
class RecordAndRestartTest(SessionIntentTestBase):
    def test_records_immutable_correlation_and_reloads_after_restart(self) -> None:
        scenario = self._scenario()
        spec = self._prepare(scenario)
        repo = SessionIntentRepository(scenario.store)
        record = repo.record_intent(spec, fence=1)

        self.assertEqual(record.canonical, spec.canonical)
        self.assertEqual(record.message, spec.message)
        self.assertEqual(record.admission_id, spec.admission_id)
        self.assertEqual(record.session_id, spec.session_id)
        self.assertEqual(record.task_id, spec.task_id)
        self.assertEqual(record.dispatch_id, spec.dispatch_id)
        self.assertEqual(record.spec_digest, spec.digest)
        self.assertEqual(record.message_digest, spec.message_digest)
        self.assertEqual(record.message_bytes, len(spec.message))
        self.assertEqual(record.fence, 1)
        self.assertEqual(record.schema_version, SESSION_INTENT_SCHEMA_VERSION)
        # The reconstructed spec needs no service/manager and is byte-identical.
        rebuilt = record.spec()
        self.assertIsInstance(rebuilt, SessionTurnSpec)
        self.assertEqual(rebuilt.canonical, spec.canonical)
        self.assertEqual(rebuilt.message, spec.message)
        self.assertEqual(rebuilt.admission_id, spec.admission_id)
        self.assertEqual(rebuilt.digest, spec.digest)
        # The dispatch's operational state and cost are untouched (no authority).
        unchanged = scenario.dispatch_repo.get_dispatch(spec.dispatch_id)
        self.assertEqual(unchanged.state, "reserved")
        self.assertEqual(unchanged.cost_state, "not_incurred")
        self.assertIsNone(unchanged.admission_id)

        # Restart: close and reopen the real store, then reload the same correlation.
        app_dir = scenario.store.app_dir
        pid = scenario.store.project_id
        scenario.store.close()
        reopened = ProjectStore.open_at(app_dir, expected_project_id=pid)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.schema_version, schema.STORE_SCHEMA_VERSION)
        reloaded = SessionIntentRepository(reopened).get_intent(spec.dispatch_id)
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.canonical, record.canonical)
        self.assertEqual(reloaded.message, record.message)
        self.assertEqual(reloaded.admission_id, record.admission_id)
        self.assertEqual(reloaded.session_id, record.session_id)
        self.assertEqual(reloaded.created_at_utc, record.created_at_utc)
        self.assertEqual(reloaded.spec().to_mapping(), spec.to_mapping())

        self.assertEqual(len(SessionIntentRepository(reopened).list_intents()), 1)
        self.assertEqual(SessionIntentRepository(reopened).verify_all_integrity(), [])
        self.assertEqual(session_intent_integrity_problems(reopened.connection, pid), [])


@slow
class ReplayTest(SessionIntentTestBase):
    def test_identical_replay_is_idempotent_and_divergent_replay_refused(self) -> None:
        scenario = self._scenario()
        spec = self._prepare(scenario)
        repo = SessionIntentRepository(scenario.store)
        first = repo.record_intent(spec, fence=1)
        second = repo.record_intent(spec, fence=1)
        self.assertEqual(second.canonical, first.canonical)
        self.assertEqual(second.message, first.message)
        self.assertEqual(second.created_at_utc, first.created_at_utc)
        self.assertEqual(self._count(scenario), 1)

        divergent = self._prepare(scenario, text="A different but valid application text.")
        with self.assertRaises(SessionIntentConflictError):
            repo.record_intent(divergent, fence=1)
        self.assertEqual(self._count(scenario), 1)

        # Replay remains idempotent after submission and remains fenced exactly.
        scenario.dispatch_repo.mark_submitted(spec.dispatch_id)
        replay = repo.record_intent(spec, fence=1)
        self.assertEqual(replay.canonical, first.canonical)
        with self.assertRaises(SessionIntentFenceError):
            repo.record_intent(spec, fence=2)
        self.assertEqual(self._count(scenario), 1)
        self.assertEqual(repo.verify_all_integrity(), [])


@slow
class RefusalTest(SessionIntentTestBase):
    def test_wrong_fence_is_refused_and_writes_nothing(self) -> None:
        scenario = self._scenario()
        spec = self._prepare(scenario)
        repo = SessionIntentRepository(scenario.store)
        for bad in (0, -1, True, "1", 1.0, 2):
            with self.subTest(fence=bad):
                with self.assertRaises(SessionIntentFenceError):
                    repo.record_intent(spec, fence=bad)
                self.assertEqual(self._count(scenario), 0)
                self.assertEqual(
                    scenario.dispatch_repo.get_dispatch(spec.dispatch_id).state,
                    "reserved",
                )

    def test_non_reserved_new_write_is_refused(self) -> None:
        scenario = self._scenario()
        spec = self._prepare(scenario)
        scenario.dispatch_repo.mark_submitted(spec.dispatch_id)
        repo = SessionIntentRepository(scenario.store)
        with self.assertRaises(SessionIntentStateError):
            repo.record_intent(spec, fence=1)
        self.assertEqual(self._count(scenario), 0)

    def test_cross_project_relabelled_spec_is_refused(self) -> None:
        scenario_a = self._scenario()
        scenario_b = self._scenario()
        spec_a = self._prepare(scenario_a)
        repo_b = SessionIntentRepository(scenario_b.store)
        with self.assertRaises(SessionIntentValidationError):
            repo_b.record_intent(spec_a, fence=1)
        self.assertEqual(self._count(scenario_b), 0)

    def test_forced_insert_validation_failure_leaves_zero_rows(self) -> None:
        scenario = self._scenario()
        spec = self._prepare(scenario)
        conn = scenario.store.connection
        self._drop_trigger(scenario, "revision_no_update")
        conn.execute(
            "UPDATE revision SET content = ? WHERE project_id = ? AND artifact_id = ? "
            "AND revision_id = ?",
            (
                b"tampered-bytes",
                scenario.store.project_id,
                scenario.input_artifact.artifact_id,
                "rev_in",
            ),
        )
        repo = SessionIntentRepository(scenario.store)
        with self.assertRaises(SessionIntentError):
            repo.record_intent(spec, fence=1)
        self.assertEqual(self._count(scenario), 0)
        self.assertEqual(repo.verify_all_integrity(), [])


@slow
class ForgedProjectionTest(SessionIntentTestBase):
    def test_forged_self_consistent_projections_are_refused(self) -> None:
        scenario = self._scenario()
        spec = self._prepare(scenario)
        repo = SessionIntentRepository(scenario.store)
        pid = scenario.store.project_id

        def _target(plan, message):
            plan["task"]["target"] = {
                "project_id": pid,
                "artifact_id": scenario.input_artifact.artifact_id,
                "revision_id": "rev_in",
            }

        def _scope(plan, message):
            plan["task"]["scope"] = ["section:introduction", "section:methods"]

        def _inputs(plan, message):
            plan["task"]["input_refs"][0]["digest"] = "0" * 64

        def _role(plan, message):
            plan["role"] = "section_critic"
            plan["fresh_context_required"] = True

        def _model(plan, message):
            plan["session"]["model"] = "openrouter/other/model#high"
            plan["versions"]["model"]["ref"]["version"] = "openrouter/other/model#high"

        def _config(plan, message):
            plan["session"]["config_snapshot_id"] = "cfg_forged"
            plan["session"]["config_digest"] = "f" * 64

        def _prompt_version(plan, message):
            plan["task"]["prompt_version"] = "other.v1"
            plan["versions"]["prompt"]["ref"]["version"] = "other.v1"

        def _version_hash(plan, message):
            plan["versions"]["rubric"] = {
                "state": "known",
                "ref": {"version": "rubric.v1", "digest": "d" * 64},
            }

        def _dispatch_stage(plan, message):
            plan["dispatch"]["stage"] = "section_critique"

        def _task_request(plan, message):
            plan["task"]["request_id"] = "forged-request"

        for mutate in (
            _target,
            _scope,
            _inputs,
            _role,
            _model,
            _config,
            _prompt_version,
            _version_hash,
            _dispatch_stage,
            _task_request,
        ):
            with self.subTest(mutation=mutate.__name__):
                forged = _forge_spec(spec, mutate)
                # The forged value object is internally self-consistent: only the
                # independent persisted-parent binding can refuse it.
                self.assertNotEqual(forged.canonical, spec.canonical)
                with self.assertRaises(SessionIntentValidationError):
                    repo.record_intent(forged, fence=1)
                self.assertEqual(self._count(scenario), 0)


@slow
class HistoricalReadTest(SessionIntentTestBase):
    def test_historical_read_survives_a_moved_input_head(self) -> None:
        scenario = self._scenario()
        spec = self._prepare(scenario)
        repo = SessionIntentRepository(scenario.store)
        first = repo.record_intent(spec, fence=1)

        artifacts = ArtifactRepository(scenario.store)
        artifacts.import_revision(
            artifact_id=scenario.input_artifact.artifact_id,
            content=b"moved-input-head-bytes",
            origin_locator="import:synthetic",
            rights={
                "rights_id": f"RIGHTS-{scenario.input_artifact.artifact_id}-rev_in2",
                "project_id": scenario.store.project_id,
                "record_revision": 1,
                "subject": {
                    "kind": "source_revision",
                    "project_id": scenario.store.project_id,
                    "artifact_id": scenario.input_artifact.artifact_id,
                    "revision_id": "rev_in2",
                },
                "operations": {
                    op: "allowed"
                    for op in ("acquisition", "storage", "redistribution", "hosted_transfer")
                },
                "basis": {
                    op: {
                        "kind": "public_domain",
                        "evidence": {"ref": "lic:pd-syn", "provenance": "publisher:pd-list"},
                    }
                    for op in ("acquisition", "storage", "redistribution", "hosted_transfer")
                },
                "authorization": None,
                "restrictions": [],
                "privacy": {
                    "personal_data": False,
                    "confidentiality": "public",
                    "denial": False,
                },
                "recorded_by": "user",
                "recorded_at": "2026-10-27T00:00:00Z",
                "derived_status": "valid",
            },
            revision_id="rev_in2",
            parent_revision_id="rev_in",
        )

        reloaded = repo.get_intent(spec.dispatch_id)
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.canonical, first.canonical)
        self.assertEqual(reloaded.spec().message, spec.message)
        self.assertEqual(repo.verify_all_integrity(), [])
        # An identical replay after the head moved is still idempotent.
        replay = repo.record_intent(spec, fence=1)
        self.assertEqual(replay.canonical, first.canonical)
        self.assertEqual(self._count(scenario), 1)


@slow
class CorruptionSweepTest(SessionIntentTestBase):
    def _assert_read_and_sweep_fail(self, scenario, dispatch_id, fragment=None) -> None:
        repo = SessionIntentRepository(scenario.store)
        with self.assertRaises(SessionIntentCorruptionError):
            repo.get_intent(dispatch_id)
        problems = repo.verify_all_integrity()
        self.assertTrue(problems)
        module_problems = session_intent_integrity_problems(
            scenario.store.connection, scenario.store.project_id
        )
        self.assertTrue(module_problems)
        if fragment is not None:
            self.assertTrue(any(fragment in problem for problem in module_problems))

    def test_corrupt_canonical_bytes_fail_read_and_sweep(self) -> None:
        scenario, spec, _ = self._fixture()
        self._drop_trigger(scenario, "session_turn_intent_no_update")
        scenario.store.connection.execute(
            "UPDATE session_turn_intent SET canonical = ? WHERE project_id = ? AND "
            "dispatch_id = ?",
            (canonical_json({"tampered": True}), scenario.store.project_id, spec.dispatch_id),
        )
        self._assert_read_and_sweep_fail(scenario, spec.dispatch_id)

    def test_corrupt_digest_fails_read_and_sweep(self) -> None:
        scenario, spec, _ = self._fixture()
        self._drop_trigger(scenario, "session_turn_intent_no_update")
        scenario.store.connection.execute(
            "UPDATE session_turn_intent SET spec_digest = ? WHERE project_id = ? AND "
            "dispatch_id = ?",
            ("0" * 64, scenario.store.project_id, spec.dispatch_id),
        )
        self._assert_read_and_sweep_fail(scenario, spec.dispatch_id)

    def test_corrupt_column_fails_read_and_sweep(self) -> None:
        scenario, spec, _ = self._fixture()
        self._drop_trigger(scenario, "session_turn_intent_no_update")
        scenario.store.connection.execute(
            "UPDATE session_turn_intent SET role = ? WHERE project_id = ? AND "
            "dispatch_id = ?",
            ("not_a_role", scenario.store.project_id, spec.dispatch_id),
        )
        self._assert_read_and_sweep_fail(scenario, spec.dispatch_id)

    def test_corrupt_message_bytes_fail_read_and_sweep(self) -> None:
        scenario, spec, _ = self._fixture()
        self._drop_trigger(scenario, "session_turn_intent_no_update")
        scenario.store.connection.execute(
            "UPDATE session_turn_intent SET message = ? WHERE project_id = ? AND "
            "dispatch_id = ?",
            (canonical_json({"tampered": True}), scenario.store.project_id, spec.dispatch_id),
        )
        self._assert_read_and_sweep_fail(scenario, spec.dispatch_id)

    def test_foreign_row_fails_sweep(self) -> None:
        scenario, spec, _ = self._fixture()
        conn = scenario.store.connection
        conn.execute("PRAGMA foreign_keys = OFF")
        self._drop_trigger(scenario, "session_turn_intent_no_update")
        conn.execute(
            "UPDATE session_turn_intent SET project_id = ? WHERE project_id = ? AND "
            "dispatch_id = ?",
            ("prj_" + "f" * 32, scenario.store.project_id, spec.dispatch_id),
        )
        problems = session_intent_integrity_problems(
            conn, scenario.store.project_id
        )
        self.assertTrue(any("foreign project row" in problem for problem in problems))

    def test_orphan_row_fails_read_and_sweep(self) -> None:
        scenario, spec, _ = self._fixture()
        conn = scenario.store.connection
        conn.execute("PRAGMA foreign_keys = OFF")
        self._drop_trigger(scenario, "runtime_dispatch_no_delete")
        conn.execute(
            "DELETE FROM runtime_dispatch_state WHERE project_id = ? AND dispatch_id = ?",
            (scenario.store.project_id, spec.dispatch_id),
        )
        conn.execute(
            "DELETE FROM runtime_dispatch WHERE project_id = ? AND dispatch_id = ?",
            (scenario.store.project_id, spec.dispatch_id),
        )
        self._assert_read_and_sweep_fail(
            scenario, spec.dispatch_id, fragment="missing dispatch"
        )


@slow
class ImmutabilityAndUniquenessTest(SessionIntentTestBase):
    def test_rows_are_append_only(self) -> None:
        scenario, spec, _ = self._fixture()
        conn = scenario.store.connection
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE session_turn_intent SET role = role WHERE project_id = ? AND "
                "dispatch_id = ?",
                (scenario.store.project_id, spec.dispatch_id),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "DELETE FROM session_turn_intent WHERE project_id = ? AND dispatch_id = ?",
                (scenario.store.project_id, spec.dispatch_id),
            )
        self.assertEqual(self._count(scenario), 1)

    def test_second_row_for_same_dispatch_is_refused(self) -> None:
        scenario, spec, _ = self._fixture()
        conn = scenario.store.connection
        row = conn.execute(
            "SELECT * FROM session_turn_intent WHERE project_id = ? AND dispatch_id = ?",
            (scenario.store.project_id, spec.dispatch_id),
        ).fetchone()
        data = {key: row[key] for key in row.keys()}
        columns = ", ".join(data)
        placeholders = ", ".join("?" for _ in data)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                f"INSERT INTO session_turn_intent ({columns}) VALUES ({placeholders})",
                tuple(data.values()),
            )
        self.assertEqual(self._count(scenario), 1)

    def test_unique_project_session_admission_tuple_is_enforced(self) -> None:
        scenario, spec, _ = self._fixture()
        conn = scenario.store.connection
        row = conn.execute(
            "SELECT * FROM session_turn_intent WHERE project_id = ? AND dispatch_id = ?",
            (scenario.store.project_id, spec.dispatch_id),
        ).fetchone()
        data = {key: row[key] for key in row.keys()}
        # A *different* dispatch key and intent id, but the same session/admission
        # tuple: only the composite UNIQUE constraint can refuse this.
        data["dispatch_id"] = "disp_" + "e" * 32
        data["intent_id"] = _intent_id(
            scenario.store.project_id, data["dispatch_id"]
        )
        columns = ", ".join(data)
        placeholders = ", ".join("?" for _ in data)
        conn.execute("PRAGMA foreign_keys = OFF")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                f"INSERT INTO session_turn_intent ({columns}) VALUES ({placeholders})",
                tuple(data.values()),
            )
        self.assertEqual(self._count(scenario), 1)


@slow
class MigrationTest(SessionIntentTestBase):
    def test_schema9_to_10_preserves_old_rows(self) -> None:
        root = str(self.tmp / "p9")
        app_dir = paths.app_dir_for(root)
        db_path = paths.store_path_for(root)
        paths.ensure_owner_private_dir(app_dir)
        conn = schema.open_connection(db_path)
        try:
            schema.migrate(
                conn,
                target_version=9,
                migrations=schema.STORE_MIGRATIONS,
                label="project store",
            )
            self.assertEqual(schema.read_user_version(conn), 9)
            tables_before = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertNotIn("session_turn_intent", tables_before)
            pid = paths.new_project_id()
            conn.execute(
                "INSERT INTO project (project_id, name, created_at_utc, app_version) "
                "VALUES (?, 's9', '2026-01-01T00:00:00+00:00', '0.0.0')",
                (pid,),
            )
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('created_at_utc', ?)",
                ("2026-01-01T00:00:00+00:00",),
            )
            store = ProjectStore(conn, app_dir, pid, schema_version=9, app_version="0.0.0")
            repo = ArtifactRepository(store)
            artifact = repo.create_artifact(kind="document")
            repo.import_revision(
                artifact_id=artifact.artifact_id,
                content=b"predecessor-session-intent-bytes",
                origin_locator="import:synthetic",
                rights={
                    "rights_id": f"RIGHTS-{artifact.artifact_id}-rev_1",
                    "project_id": pid,
                    "record_revision": 1,
                    "subject": {
                        "kind": "source_revision",
                        "project_id": pid,
                        "artifact_id": artifact.artifact_id,
                        "revision_id": "rev_1",
                    },
                    "operations": {
                        op: "allowed"
                        for op in (
                            "acquisition",
                            "storage",
                            "redistribution",
                            "hosted_transfer",
                        )
                    },
                    "basis": {
                        op: {
                            "kind": "public_domain",
                            "evidence": {
                                "ref": "lic:pd-syn",
                                "provenance": "publisher:pd-list",
                            },
                        }
                        for op in (
                            "acquisition",
                            "storage",
                            "redistribution",
                            "hosted_transfer",
                        )
                    },
                    "authorization": None,
                    "restrictions": [],
                    "privacy": {
                        "personal_data": False,
                        "confidentiality": "public",
                        "denial": False,
                    },
                    "recorded_by": "user",
                    "recorded_at": "2026-10-27T00:00:00Z",
                    "derived_status": "valid",
                },
                revision_id="rev_1",
            )
        finally:
            conn.close()

        reopened = ProjectStore.open_at(app_dir, expected_project_id=pid)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.schema_version, schema.STORE_SCHEMA_VERSION)
        names = {
            row[0]
            for row in reopened.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertIn("session_turn_intent", names)
        # Predecessor rows and bytes are untouched by the additive migration.
        self.assertEqual(
            ArtifactRepository(reopened).read_content(artifact.artifact_id, "rev_1"),
            b"predecessor-session-intent-bytes",
        )
        # The new table starts empty and revalidates.
        self.assertEqual(SessionIntentRepository(reopened).list_intents(), [])
        self.assertEqual(
            session_intent_integrity_problems(reopened.connection, pid), []
        )

    def test_schema_version_is_current_and_additive(self) -> None:
        self.assertEqual(schema.STORE_SCHEMA_VERSION, max(schema.STORE_MIGRATIONS))
        self.assertGreaterEqual(schema.STORE_SCHEMA_VERSION, 10)
        self.assertTrue(set(range(1, 11)).issubset(set(schema.STORE_MIGRATIONS)))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
