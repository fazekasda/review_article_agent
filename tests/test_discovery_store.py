"""Durable inert search-observation storage tests (Step 05, T2).

Covers ``src/review_article_agent/storage/discovery.py`` against the T2
acceptance criteria: exact query/source/time/count/failure reconstruction,
lossless PubMed ``query_translation``, partial/failure/missing-ID/overlap
preservation, reopen, identical/divergent replay, distinct reruns, project
isolation, two-connection concurrent retry, transactional rollback, SQL
immutability, schema 10 -> 11 additive migration, and shared read-only
integrity validation.

Every fixture is synthetic; no test performs a network, provider, or model call.

Run::

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_discovery_*.py'
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from review_article_agent.discovery.pubmed import PubMedSearchResult  # noqa: E402
from review_article_agent.discovery.records import (  # noqa: E402
    SOURCE_EUROPEPMC,
    SOURCE_PUBMED,
    SearchResult,
)
from review_article_agent.discovery.serialization import (  # noqa: E402
    encode_search_result,
)
from review_article_agent.domain.records import canonical_json  # noqa: E402
from review_article_agent.storage import ProjectStore, paths, schema  # noqa: E402
from review_article_agent.storage.discovery import (  # noqa: E402
    DISCOVERY_SEARCH_SCHEMA_VERSION,
    DiscoveryRepository,
    DiscoverySearchConflictError,
    DiscoverySearchCorruptionError,
    DiscoverySearchValidationError,
    discovery_integrity_problems,
)
import test_discovery_serialization as fixtures  # noqa: E402

_TRIGGERS = (
    "discovery_search_run_no_update",
    "discovery_search_run_no_delete",
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _run_id(project_id: str, request_id: str) -> str:
    material = canonical_json([project_id, request_id])
    return "dsr_" + hashlib.sha256(material).hexdigest()[:32]


class DiscoveryStoreBase(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory(prefix="raa-discovery-store-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.project_id = paths.new_project_id()
        self.app_dir = paths.app_dir_for(str(self.tmp / "project"))
        self.store = ProjectStore.create_at(self.app_dir, self.project_id)
        self.addCleanup(self.store.close)
        self.repo = DiscoveryRepository(self.store)

    # -- fixtures ------------------------------------------------------------ #
    @staticmethod
    def pubmed_completed(**overrides) -> PubMedSearchResult:
        kwargs = dict(query_translation="((cancer therapy[MeSH Terms]))")
        kwargs.update(overrides)
        return fixtures.pubmed_result(pages=(fixtures.page(),), **kwargs)

    @staticmethod
    def epmc_completed(**overrides) -> SearchResult:
        epmc_page = fixtures.page(
            source=SOURCE_EUROPEPMC,
            records=(
                fixtures.record(
                    source=SOURCE_EUROPEPMC,
                    source_id="MED:31452104",
                    pmid="31452104",
                    pmcid=None,
                    doi="10.1000/epmc",
                    title="Europe PMC fixture",
                    publication_year=2021,
                ),
            ),
            page_size=25,
            total_count=1,
        )
        kwargs = dict(pages=(epmc_page,), source=SOURCE_EUROPEPMC)
        kwargs.update(overrides)
        return fixtures.result(**kwargs)

    def _reopen(self) -> tuple[ProjectStore, DiscoveryRepository]:
        self.store.close()
        reopened = ProjectStore.open_at(self.app_dir, expected_project_id=self.project_id)
        self.addCleanup(reopened.close)
        self.store = reopened
        self.repo = DiscoveryRepository(reopened)
        return reopened, self.repo

    def _drop_triggers_run(self, fn) -> None:
        conn = sqlite3.connect(paths.store_path_for(str(self.tmp / "project")))
        conn.row_factory = sqlite3.Row
        try:
            saved: dict[str, str] = {}
            for name in _TRIGGERS:
                row = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                    (name,),
                ).fetchone()
                if row and row[0]:
                    saved[name] = row[0]
                conn.execute(f"DROP TRIGGER IF EXISTS {name}")
            fn(conn)
            for sql in saved.values():
                conn.execute(sql)
            conn.commit()
        finally:
            conn.close()

    def _row_count(self) -> int:
        return int(
            self.store.connection.execute(
                "SELECT COUNT(*) AS n FROM discovery_search_run"
            ).fetchone()["n"]
        )


class CompletedObservationTests(DiscoveryStoreBase):
    def test_completed_pubmed_result_preserves_translation_and_provenance(self) -> None:
        result = self.pubmed_completed()
        record = self.repo.record_search(request_id="req_pubmed_1", result=result)

        self.assertEqual(record.project_id, self.project_id)
        self.assertEqual(record.request_id, "req_pubmed_1")
        self.assertEqual(record.run_id, _run_id(self.project_id, "req_pubmed_1"))
        self.assertEqual(record.schema_version, DISCOVERY_SEARCH_SCHEMA_VERSION)
        self.assertEqual(record.result_type, "pubmed_search_result")
        self.assertEqual(record.source, SOURCE_PUBMED)
        self.assertEqual(record.query, result.query)
        self.assertEqual(record.started_at, result.started_at)
        self.assertEqual(record.completed_at, result.completed_at)
        self.assertEqual(record.stop_reason, "completed")
        self.assertTrue(record.complete)
        self.assertIsNone(record.failure)
        self.assertEqual(record.total_count, result.total_count)
        self.assertEqual(record.page_count, 1)
        self.assertEqual(record.record_count, 1)
        self.assertEqual(record.attempt_count, 1)
        self.assertIsInstance(record.result, PubMedSearchResult)
        self.assertEqual(record.result, result)
        self.assertEqual(record.result.query_translation, "((cancer therapy[MeSH Terms]))")
        self.assertEqual(record.canonical, encode_search_result(result))
        self.assertEqual(record.result_bytes, len(record.canonical))
        self.assertEqual(record.result_sha256, _sha(record.canonical))
        self.assertEqual(record.binding_sha256, _sha(record.binding))
        self.assertEqual(record.binding_bytes, len(record.binding))

    def test_completed_epmc_result_preserves_exact_source_and_urls(self) -> None:
        result = self.epmc_completed()
        record = self.repo.record_search(request_id="req_epmc_1", result=result)

        self.assertEqual(record.result_type, "search_result")
        self.assertEqual(record.source, SOURCE_EUROPEPMC)
        self.assertEqual(record.result, result)
        self.assertEqual(record.result.pages[0].requested_url, result.pages[0].requested_url)
        self.assertEqual(record.result.pages[0].retrieved_at, result.pages[0].retrieved_at)
        self.assertEqual(record.result.records[0].source_id, "MED:31452104")
        self.assertTrue(record.complete)

    def test_partial_failure_missing_id_and_overlap_are_preserved(self) -> None:
        missing = fixtures.record(
            source=SOURCE_EUROPEPMC,
            source_id=None,
            pmid=None,
            pmcid=None,
            doi=None,
            title=None,
            publication_year=None,
        )
        overlap = fixtures.record(
            source=SOURCE_EUROPEPMC,
            source_id="MED:1",
            pmid=None,
            pmcid="PMC1",
            doi=None,
            title="Overlapping record",
            publication_year=None,
        )
        page0 = fixtures.page(
            source=SOURCE_EUROPEPMC,
            page_index=0,
            records=(missing, overlap),
            page_size=2,
            total_count=3,
            complete=True,
            stop_reason="completed",
        )
        page1 = fixtures.page(
            source=SOURCE_EUROPEPMC,
            page_index=1,
            records=(overlap,),
            page_size=2,
            total_count=3,
            complete=False,
            stop_reason="failure",
            failure="http_status",
            attempts=(
                fixtures.attempt(status=503, outcome="retryable", body_sha256=None),
            ),
        )
        result = fixtures.result(
            pages=(page0, page1),
            source=SOURCE_EUROPEPMC,
            stop_reason="failure",
            failure="http_status",
            total_count=3,
        )
        record = self.repo.record_search(request_id="req_partial", result=result)

        self.assertFalse(record.complete)
        self.assertEqual(record.stop_reason, "failure")
        self.assertEqual(record.failure, "http_status")
        self.assertEqual(record.total_count, 3)
        self.assertEqual(record.page_count, 2)
        self.assertEqual(record.record_count, 3)
        self.assertEqual(record.attempt_count, 2)
        self.assertEqual(record.result, result)
        self.assertEqual(len(record.result.pages), 2)
        self.assertIs(record.result.pages[0].records[0].source_id, None)
        self.assertEqual(record.result.pages[1].records[0].source_id, "MED:1")
        self.assertEqual(record.result.pages[1].failure, "http_status")
        self.assertEqual(record.result.pages[1].attempts[0].status, 503)

    def test_reopen_reconstructs_exact_record_and_result(self) -> None:
        result = self.pubmed_completed()
        created = self.repo.record_search(request_id="req_reopen", result=result)
        recorded_at = created.recorded_at_utc
        canonical = created.canonical

        _store, repo = self._reopen()
        loaded = repo.get_search(created.run_id)

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.recorded_at_utc, recorded_at)
        self.assertEqual(loaded.canonical, canonical)
        self.assertEqual(loaded.result, result)
        self.assertEqual(loaded.result.query_translation, result.query_translation)
        self.assertEqual(loaded.binding, created.binding)
        self.assertEqual([item.run_id for item in repo.list_searches()], [created.run_id])


class ReplayTests(DiscoveryStoreBase):
    def test_identical_replay_returns_historical_row_even_after_reopen(self) -> None:
        result = self.pubmed_completed()
        first = self.repo.record_search(request_id="req_same", result=result)
        _store, repo = self._reopen()
        second = repo.record_search(request_id="req_same", result=result)

        self.assertEqual(second.run_id, first.run_id)
        self.assertEqual(second.recorded_at_utc, first.recorded_at_utc)
        self.assertEqual(second.canonical, first.canonical)
        self.assertEqual(second.binding, first.binding)
        self.assertEqual(second.result, first.result)
        self.assertEqual(len(repo.list_searches()), 1)

    def test_divergent_replay_is_static_conflict_and_never_overwrites(self) -> None:
        first = self.repo.record_search(request_id="req_diverge", result=self.pubmed_completed())
        divergent = fixtures.pubmed_result(
            pages=(fixtures.page(),),
            query_translation="different translation",
        )
        with self.assertRaises(DiscoverySearchConflictError) as caught:
            self.repo.record_search(request_id="req_diverge", result=divergent)
        message = str(caught.exception)
        self.assertNotIn("cancer", message)
        self.assertNotIn("different translation", message)

        loaded = self.repo.get_search(first.run_id)
        assert loaded is not None
        self.assertEqual(loaded.canonical, first.canonical)
        self.assertEqual(loaded.result, first.result)
        self.assertEqual(len(self.repo.list_searches()), 1)

    def test_distinct_request_keys_are_separate_runs_for_same_payload(self) -> None:
        result = self.pubmed_completed()
        first = self.repo.record_search(request_id="req_a", result=result)
        second = self.repo.record_search(request_id="req_b", result=result)

        self.assertNotEqual(first.run_id, second.run_id)
        self.assertEqual(first.canonical, second.canonical)
        self.assertEqual(first.result_sha256, second.result_sha256)
        self.assertEqual(
            sorted(item.request_id for item in self.repo.list_searches()),
            ["req_a", "req_b"],
        )
        self.assertEqual(self.repo.get_search(first.run_id), first)
        self.assertEqual(self.repo.get_search(second.run_id), second)

    def test_projects_are_isolated_even_with_identical_keys_and_payload(self) -> None:
        result = self.pubmed_completed()
        first = self.repo.record_search(request_id="req_shared", result=result)

        other_id = paths.new_project_id()
        other_app = paths.app_dir_for(str(self.tmp / "other"))
        other = ProjectStore.create_at(other_app, other_id)
        self.addCleanup(other.close)
        other_repo = DiscoveryRepository(other)
        second = other_repo.record_search(request_id="req_shared", result=result)

        self.assertEqual(first.request_id, second.request_id)
        self.assertNotEqual(first.run_id, second.run_id)
        self.assertIsNone(self.repo.get_search(second.run_id))
        self.assertIsNone(other_repo.get_search(first.run_id))
        self.assertEqual(len(self.repo.list_searches()), 1)
        self.assertEqual(len(other_repo.list_searches()), 1)
        self.assertEqual(other_repo.list_searches()[0].project_id, other_id)


class AtomicityTests(DiscoveryStoreBase):
    def test_two_connections_concurrently_insert_same_key_once(self) -> None:
        result = self.pubmed_completed()
        barrier = threading.Barrier(2)
        outcomes: list[object] = []
        lock = threading.Lock()

        def worker() -> None:
            store = ProjectStore.open_at(self.app_dir, expected_project_id=self.project_id)
            try:
                barrier.wait(timeout=10)
                record = DiscoveryRepository(store).record_search(
                    request_id="req_concurrent", result=result
                )
                with lock:
                    outcomes.append(record)
            except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion
                with lock:
                    outcomes.append(exc)
            finally:
                store.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(len(outcomes), 2)
        self.assertTrue(all(not isinstance(item, BaseException) for item in outcomes), outcomes)
        first, second = outcomes  # type: ignore[misc]
        self.assertEqual(first.run_id, second.run_id)
        self.assertEqual(first.recorded_at_utc, second.recorded_at_utc)
        self.assertEqual(first.canonical, second.canonical)
        self.assertEqual(self._row_count(), 1)

    def test_induced_failure_after_insert_rolls_back_to_zero_rows(self) -> None:
        with mock.patch.object(
            self.repo, "_validate_row", side_effect=RuntimeError("forced")
        ):
            with self.assertRaises(RuntimeError):
                self.repo.record_search(request_id="req_rollback", result=self.pubmed_completed())

        self.assertEqual(self._row_count(), 0)
        self.assertEqual(self.repo.list_searches(), [])
        self.assertEqual(discovery_integrity_problems(self.store.connection, self.project_id), [])

    def test_sql_row_is_immutable_and_cannot_be_deleted(self) -> None:
        record = self.repo.record_search(request_id="req_immutable", result=self.pubmed_completed())
        conn = self.store.connection

        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE discovery_search_run SET source = 'forged' WHERE project_id = ? AND run_id = ?",
                (self.project_id, record.run_id),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "DELETE FROM discovery_search_run WHERE project_id = ? AND run_id = ?",
                (self.project_id, record.run_id),
            )

        loaded = self.repo.get_search(record.run_id)
        assert loaded is not None
        self.assertEqual(loaded.source, SOURCE_PUBMED)
        self.assertEqual(loaded.canonical, record.canonical)


class CorruptionTests(DiscoveryStoreBase):
    def test_tampered_projection_is_corruption_and_replay_does_not_overwrite(self) -> None:
        result = self.pubmed_completed()
        record = self.repo.record_search(request_id="req_projection", result=result)
        self._drop_triggers_run(
            lambda conn: conn.execute(
                "UPDATE discovery_search_run SET source = 'forged' "
                "WHERE project_id = ? AND run_id = ?",
                (self.project_id, record.run_id),
            )
        )

        with self.assertRaises(DiscoverySearchCorruptionError) as caught:
            self.repo.get_search(record.run_id)
        self.assertNotIn("cancer", str(caught.exception))
        with self.assertRaises(DiscoverySearchCorruptionError):
            self.repo.record_search(request_id="req_projection", result=result)
        self.assertEqual(self._row_count(), 1)

    def test_tampered_payload_with_matching_hash_is_refused(self) -> None:
        record = self.repo.record_search(request_id="req_payload", result=self.pubmed_completed())
        unsupported = dict(json.loads(record.canonical.decode("utf-8")))
        unsupported["format_version"] = 99
        forged = canonical_json(unsupported)

        self._drop_triggers_run(
            lambda conn: conn.execute(
                "UPDATE discovery_search_run SET canonical = ?, result_sha256 = ?, "
                "result_bytes = ? WHERE project_id = ? AND run_id = ?",
                (forged, _sha(forged), len(forged), self.project_id, record.run_id),
            )
        )

        with self.assertRaises(DiscoverySearchCorruptionError):
            self.repo.get_search(record.run_id)

    def test_noncanonical_but_decodable_blob_is_refused(self) -> None:
        record = self.repo.record_search(request_id="req_noncanonical", result=self.pubmed_completed())
        noncanonical = b" " + record.canonical  # valid JSON, but not canonical bytes
        self._drop_triggers_run(
            lambda conn: conn.execute(
                "UPDATE discovery_search_run SET canonical = ?, result_sha256 = ?, "
                "result_bytes = ? WHERE project_id = ? AND run_id = ?",
                (
                    noncanonical,
                    _sha(noncanonical),
                    len(noncanonical),
                    self.project_id,
                    record.run_id,
                ),
            )
        )
        with self.assertRaises(DiscoverySearchCorruptionError):
            self.repo.get_search(record.run_id)

    def test_bad_request_identity_metadata_is_refused(self) -> None:
        record = self.repo.record_search(request_id="req_metadata", result=self.pubmed_completed())
        # ``schema_version`` is additionally pinned by a table CHECK; a rewritten
        # request id is accepted by SQL but must fail the independent identity
        # re-derivation on read.
        self._drop_triggers_run(
            lambda conn: conn.execute(
                "UPDATE discovery_search_run SET request_id = 'req_replaced' "
                "WHERE project_id = ? AND run_id = ?",
                (self.project_id, record.run_id),
            )
        )
        with self.assertRaises(DiscoverySearchCorruptionError):
            self.repo.get_search(record.run_id)

    def test_tampered_binding_hash_and_timestamp_are_refused(self) -> None:
        record = self.repo.record_search(request_id="req_binding", result=self.pubmed_completed())
        self._drop_triggers_run(
            lambda conn: conn.execute(
                "UPDATE discovery_search_run SET recorded_at_utc = 'not-a-time' "
                "WHERE project_id = ? AND run_id = ?",
                (self.project_id, record.run_id),
            )
        )
        with self.assertRaises(DiscoverySearchCorruptionError):
            self.repo.get_search(record.run_id)

        self._drop_triggers_run(
            lambda conn: conn.execute(
                "UPDATE discovery_search_run SET recorded_at_utc = ?, "
                "binding_sha256 = ? WHERE project_id = ? AND run_id = ?",
                (record.recorded_at_utc, "0" * 64, self.project_id, record.run_id),
            )
        )
        with self.assertRaises(DiscoverySearchCorruptionError):
            self.repo.get_search(record.run_id)

    def test_mutated_or_unsupported_input_contracts_are_refused_without_rows(self) -> None:
        mutated = self.pubmed_completed()
        object.__setattr__(mutated, "records", ())
        with self.assertRaises(DiscoverySearchValidationError) as caught:
            self.repo.record_search(request_id="req_mutated", result=mutated)
        self.assertNotIn("cancer", str(caught.exception))
        with self.assertRaises(DiscoverySearchValidationError):
            self.repo.record_search(request_id="req_dict", result={"query": "secret"})  # type: ignore[arg-type]
        with self.assertRaises(DiscoverySearchValidationError):
            self.repo.record_search(request_id="bad id with space", result=self.pubmed_completed())
        with self.assertRaises(DiscoverySearchValidationError):
            self.repo.record_search(request_id="a" * 513, result=self.pubmed_completed())
        self.assertEqual(self._row_count(), 0)

    def test_shared_integrity_sweep_is_read_only_and_detects_foreign_rows(self) -> None:
        record = self.repo.record_search(request_id="req_sweep", result=self.pubmed_completed())
        conn = self.store.connection
        conn.execute("PRAGMA query_only = ON")
        try:
            self.assertEqual(discovery_integrity_problems(conn, self.project_id), [])
            foreign = discovery_integrity_problems(conn, "prj_other")
        finally:
            conn.execute("PRAGMA query_only = OFF")

        self.assertEqual(len(foreign), 1)
        self.assertIn("foreign project row", foreign[0])
        self.assertNotIn("cancer", foreign[0])
        self.assertEqual(self.repo.get_search(record.run_id), record)


class MigrationTests(DiscoveryStoreBase):
    def test_schema10_to_11_is_additive_and_preserves_predecessor_data(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory(prefix="raa-discovery-migration-") as raw:
            db_path = str(Path(raw) / "store.sqlite3")
            conn = schema.open_connection(db_path)
            try:
                schema.migrate(
                    conn,
                    target_version=10,
                    migrations=schema.STORE_MIGRATIONS,
                    label="project store",
                )
                self.assertEqual(schema.read_user_version(conn), 10)
                conn.execute(
                    "INSERT INTO project (project_id, name, created_at_utc, app_version) "
                    "VALUES (?, 'predecessor', '2026-01-01T00:00:00+00:00', '0.0.0')",
                    (self.project_id,),
                )
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('predecessor', 'kept')"
                )
                schema.migrate(
                    conn,
                    target_version=11,
                    migrations=schema.STORE_MIGRATIONS,
                    label="project store",
                )
                self.assertEqual(schema.read_user_version(conn), 11)
                self.assertEqual(
                    conn.execute("SELECT project_id FROM project").fetchone()[0],
                    self.project_id,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT value FROM meta WHERE key = 'predecessor'"
                    ).fetchone()[0],
                    "kept",
                )
                names = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type IN ('table','trigger')"
                    )
                }
                self.assertIn("discovery_search_run", names)
                for trigger in _TRIGGERS:
                    self.assertIn(trigger, names)
                self.assertEqual(discovery_integrity_problems(conn, self.project_id), [])
            finally:
                conn.close()

    def test_schema_version_and_migration_map_are_current(self) -> None:
        self.assertEqual(schema.STORE_SCHEMA_VERSION, max(schema.STORE_MIGRATIONS))
        self.assertEqual(schema.STORE_SCHEMA_VERSION, 11)
        self.assertEqual(set(schema.STORE_MIGRATIONS), set(range(1, 12)))
        self.assertEqual(self.store.schema_version, schema.STORE_SCHEMA_VERSION)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()