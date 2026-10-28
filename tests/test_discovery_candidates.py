"""Durable cumulative publication-candidate snapshot tests (Step 05, W2).

Covers ``src/review_article_agent/discovery/candidates.py`` against the W2
acceptance criteria: cumulative snapshots over saved observations, stable
candidate identity, auditable occurrence/dedup/conflict/alias decisions, exact
replay vs divergent-conflict, cross-provider overlap dedup without duplicate
resolved candidates, missing-identifier and conflict preservation, incremental
bridging aliases, partial failures, restart, byte-exact earlier revisions,
project isolation, corruption detection, transactional rollback, stale-parent
refusal, and backup/restore preservation. Metadata is asserted to be **not**
scientific support.

Every fixture is synthetic; no test performs a network, provider, or model call.

Run::

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_discovery_*.py'
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from review_article_agent.discovery.candidates import (  # noqa: E402
    CORPUS_NOTICE,
    CORPUS_PAYLOAD_VERSION,
    DEFAULT_CORPUS_ARTIFACT_ID,
    DOCUMENT_TYPE,
    INTEGRITY_NEVER_CLEAN,
    INTEGRITY_STATUS_UNREVIEWED,
    CandidateConflictError,
    CandidateCorruptionError,
    CandidateRepository,
    CandidateValidationError,
)
from review_article_agent.discovery.records import (  # noqa: E402
    SOURCE_EUROPEPMC,
    SOURCE_PUBMED,
)
from review_article_agent.domain.records import canonical_json  # noqa: E402
from review_article_agent.storage import (  # noqa: E402
    ArtifactRepository,
    ProjectStore,
    Registry,
    paths,
)
from review_article_agent.storage.backup import (  # noqa: E402
    create_backup,
    restore_backup,
)
from review_article_agent.storage.discovery import DiscoveryRepository  # noqa: E402
import test_discovery_serialization as fx  # noqa: E402

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _rights_record(project_id: str, artifact_id: str, revision_id: str, **overrides) -> dict:
    record = {
        "rights_id": f"RIGHTS-{revision_id}",
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
        "privacy": {
            "personal_data": False,
            "confidentiality": "public",
            "denial": False,
        },
        "recorded_by": "user",
        "recorded_at": "2026-10-27T00:00:00Z",
        "derived_status": "valid",
    }
    record.update(overrides)
    return record


class CandidateTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-candidates-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.project_id = paths.new_project_id()
        self.app_dir = paths.app_dir_for(str(self.tmp / "project"))
        self.store = ProjectStore.create_at(self.app_dir, self.project_id)
        self.addCleanup(self.store.close)
        self.artifacts = ArtifactRepository(self.store)
        self.discovery = DiscoveryRepository(self.store)
        self.corpus = CandidateRepository(self.store)

    def _reopen(self) -> ProjectStore:
        self.store.close()
        reopened = ProjectStore.open_at(
            self.app_dir, expected_project_id=self.project_id
        )
        self.addCleanup(reopened.close)
        self.store = reopened
        self.artifacts = ArtifactRepository(reopened)
        self.discovery = DiscoveryRepository(reopened)
        self.corpus = CandidateRepository(reopened)
        return reopened

    # -- fixtures ------------------------------------------------------------ #
    @staticmethod
    def rec(**overrides):
        """A record whose identifier tokens are only what the test names."""
        kwargs = {"pmcid": None, "doi": None}
        kwargs.update(overrides)
        return fx.record(**kwargs)

    def pubmed_run(self, records, **overrides):
        return fx.pubmed_result(pages=(fx.page(records=tuple(records)),), **overrides)

    def epmc_run(self, records, **overrides):
        page = fx.page(
            source=SOURCE_EUROPEPMC,
            records=tuple(records),
            page_size=100,
            total_count=len(records),
        )
        return fx.result(source=SOURCE_EUROPEPMC, pages=(page,), **overrides)

    def observe(self, request_id: str, result):
        return self.discovery.record_search(request_id=request_id, result=result)

    def rights(self, artifact_id: str, revision_id: str, **overrides) -> dict:
        return _rights_record(self.project_id, artifact_id, revision_id, **overrides)

    def ingest(
        self,
        run_ids,
        *,
        request_id,
        artifact_id=DEFAULT_CORPUS_ARTIFACT_ID,
        revision_id=None,
        rights=None,
        dependencies=(),
    ):
        rid = revision_id or CandidateRepository.new_revision_id()
        if rights is None:
            rights = self.rights(artifact_id, rid)
        return self.corpus.ingest(
            run_ids,
            request_id=request_id,
            rights=rights,
            revision_id=rid,
            artifact_id=artifact_id,
            dependencies=dependencies,
        )

    def _revision_count(self, artifact_id=DEFAULT_CORPUS_ARTIFACT_ID) -> int:
        return int(
            self.store.connection.execute(
                "SELECT COUNT(*) AS n FROM revision WHERE project_id = ? AND artifact_id = ?",
                (self.project_id, artifact_id),
            ).fetchone()["n"]
        )

    def _tamper_revision(self, fn) -> None:
        conn = sqlite3.connect(paths.store_path_for(str(self.tmp / "project")))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='revision_no_update'"
            ).fetchone()
            saved = row[0] if row and row[0] else None
            conn.execute("DROP TRIGGER IF EXISTS revision_no_update")
            fn(conn)
            if saved:
                conn.execute(saved)
            conn.commit()
        finally:
            conn.close()


class SnapshotShapeTests(CandidateTestBase):
    def test_first_ingest_builds_one_snapshot_from_a_run(self) -> None:
        record = self.observe(
            "req_a", self.pubmed_run((self.rec(pmid="123", source_id="1"),))
        )
        revision = self.ingest([record.run_id], request_id="corpus_1")

        snapshot = self.corpus.current()
        assert snapshot is not None
        self.assertEqual(snapshot["document_type"], DOCUMENT_TYPE)
        self.assertEqual(snapshot["payload_version"], CORPUS_PAYLOAD_VERSION)
        self.assertEqual(snapshot["artifact_id"], DEFAULT_CORPUS_ARTIFACT_ID)
        self.assertEqual(snapshot["revision_id"], revision.revision_id)
        self.assertEqual(snapshot["request_id"], "corpus_1")
        self.assertEqual(snapshot["input_run_ids"], [record.run_id])
        self.assertEqual(
            [entry["run_id"] for entry in snapshot["runs"]], [record.run_id]
        )
        self.assertEqual(
            snapshot["runs"][0]["result_sha256"], record.result_sha256
        )
        self.assertEqual(snapshot["occurrences"], [[record.run_id, 0]])
        self.assertEqual(len(snapshot["candidates"]), 1)
        candidate = snapshot["candidates"][0]
        self.assertTrue(candidate["resolved"])
        self.assertIsNone(candidate["unresolved_reason"])
        self.assertEqual(candidate["occurrences"], [[record.run_id, 0]])
        self.assertEqual(
            candidate["tokens"], [["native:pubmed", "1"], ["pmid", "123"]]
        )

    def test_metadata_is_never_scientific_support(self) -> None:
        record = self.observe(
            "req_a", self.pubmed_run((self.rec(pmid="123", source_id="1"),))
        )
        self.ingest([record.run_id], request_id="corpus_1")
        snapshot = self.corpus.current()
        assert snapshot is not None

        self.assertIs(snapshot["authorizing"], False)
        self.assertIs(snapshot["completeness_claimed"], False)
        self.assertEqual(snapshot["notice"], CORPUS_NOTICE)
        # No study/evidence/accepted-context vocabulary leaks into the snapshot.
        self.assertNotIn("studies", snapshot)
        self.assertNotIn("evidence", snapshot)
        self.assertNotIn("accepted", snapshot)
        for candidate in snapshot["candidates"]:
            integrity = candidate["integrity"]
            self.assertEqual(integrity["status"], INTEGRITY_STATUS_UNREVIEWED)
            self.assertNotIn(integrity["status"], INTEGRITY_NEVER_CLEAN)
            self.assertEqual(integrity["retraction"], "unknown")
            self.assertEqual(integrity["expression_of_concern"], "unknown")
            self.assertEqual(integrity["correction"], "unknown")
            self.assertEqual(integrity["publication_date"], "unknown")
            self.assertEqual(integrity["provenance"], "missing")

    def test_get_reads_exact_revision_by_same_project_ref(self) -> None:
        record = self.observe(
            "req_a", self.pubmed_run((self.rec(pmid="123", source_id="1"),))
        )
        revision = self.ingest([record.run_id], request_id="corpus_1")
        ref = {
            "project_id": self.project_id,
            "artifact_id": revision.artifact_id,
            "revision_id": revision.revision_id,
        }
        self.assertEqual(self.corpus.get(ref), self.corpus.current())

    def test_get_rejects_cross_project_and_missing_references(self) -> None:
        record = self.observe(
            "req_a", self.pubmed_run((self.rec(pmid="123", source_id="1"),))
        )
        revision = self.ingest([record.run_id], request_id="corpus_1")
        with self.assertRaises(CandidateValidationError):
            self.corpus.get(
                {
                    "project_id": "prj_someone_else",
                    "artifact_id": revision.artifact_id,
                    "revision_id": revision.revision_id,
                }
            )
        with self.assertRaises(CandidateValidationError):
            self.corpus.get(
                {
                    "project_id": self.project_id,
                    "artifact_id": revision.artifact_id,
                    "revision_id": "rev_missing",
                }
            )


class ReplayTests(CandidateTestBase):
    def test_same_run_replay_returns_historical_revision(self) -> None:
        record = self.observe(
            "req_a", self.pubmed_run((self.rec(pmid="123", source_id="1"),))
        )
        first = self.ingest([record.run_id], request_id="corpus_1")
        first_snapshot = self.corpus.current()

        again = self.ingest([record.run_id], request_id="corpus_1")
        self.assertEqual(again.revision_id, first.revision_id)
        self.assertEqual(self._revision_count(), 1)
        self.assertEqual(self.corpus.current(), first_snapshot)

    def test_replay_returns_history_even_after_later_current_changes(self) -> None:
        a = self.observe("req_a", self.pubmed_run((self.rec(pmid="1", source_id="a"),)))
        b = self.observe(
            "req_b", self.pubmed_run((self.rec(pmid="2", source_id="b"),))
        )
        first = self.ingest([a.run_id], request_id="corpus_1")
        second = self.ingest([b.run_id], request_id="corpus_2")
        self.assertNotEqual(first.revision_id, second.revision_id)
        current = self.corpus.current()
        assert current is not None
        self.assertEqual(current["revision_id"], second.revision_id)
        self.assertEqual(
            sorted(entry["run_id"] for entry in current["runs"]),
            sorted([a.run_id, b.run_id]),
        )

        replayed = self.ingest([a.run_id], request_id="corpus_1")
        self.assertEqual(replayed.revision_id, first.revision_id)
        # The replay does not move the current snapshot.
        self.assertEqual(self.corpus.current()["revision_id"], second.revision_id)
        self.assertEqual(self._revision_count(), 2)

    def test_divergent_reuse_is_static_conflict(self) -> None:
        a = self.observe("req_a", self.pubmed_run((self.rec(pmid="1", source_id="a"),)))
        b = self.observe("req_b", self.pubmed_run((self.rec(pmid="2", source_id="b"),)))
        self.ingest([a.run_id], request_id="corpus_1")
        with self.assertRaises(CandidateConflictError) as caught:
            self.ingest([b.run_id], request_id="corpus_1")
        self.assertNotIn(a.run_id, str(caught.exception))
        self.assertNotIn(b.run_id, str(caught.exception))
        self.assertEqual(self._revision_count(), 1)


class CumulativeDedupTests(CandidateTestBase):
    def test_overlapping_crossprovider_reruns_no_duplicate_resolved_candidates(
        self,
    ) -> None:
        a = self.observe("req_a", self.pubmed_run((self.rec(pmid="123", source_id="1"),)))
        b = self.observe(
            "req_b",
            self.epmc_run(
                (self.rec(source=SOURCE_EUROPEPMC, source_id="MED:123", pmid="123"),)
            ),
        )
        c = self.observe("req_c", self.pubmed_run((self.rec(pmid="123", source_id="1"),)))
        # A fresh rerun records a distinct run id but the same immutable digest.
        self.assertNotEqual(a.run_id, c.run_id)
        self.assertEqual(a.result_sha256, c.result_sha256)

        self.ingest([a.run_id], request_id="corpus_1")
        self.ingest([b.run_id], request_id="corpus_2")
        revision = self.ingest([c.run_id], request_id="corpus_3")
        snapshot = self.corpus.get(
            {
                "project_id": self.project_id,
                "artifact_id": revision.artifact_id,
                "revision_id": revision.revision_id,
            }
        )
        resolved = [doc for doc in snapshot["candidates"] if doc["resolved"]]
        self.assertEqual(len(resolved), 1, snapshot["candidates"])
        self.assertEqual(len(resolved[0]["occurrences"]), 3)
        self.assertEqual(
            sorted(entry["run_id"] for entry in snapshot["runs"]),
            sorted([a.run_id, b.run_id, c.run_id]),
        )

    def test_identical_run_replay_is_not_a_duplicate_occurrence(self) -> None:
        a = self.observe("req_a", self.pubmed_run((self.rec(pmid="7", source_id="x"),)))
        self.ingest([a.run_id], request_id="corpus_1")
        self.ingest([a.run_id, a.run_id], request_id="corpus_2")
        snapshot = self.corpus.current()
        assert snapshot is not None
        self.assertEqual(snapshot["occurrences"], [[a.run_id, 0]])

    def test_missing_identifier_remains_explicit_and_unresolved(self) -> None:
        missing = self.rec(
            source=SOURCE_EUROPEPMC,
            source_id=None,
            pmid=None,
            pmcid=None,
            doi=None,
            title="No identifiers here",
            publication_year=None,
        )
        a = self.observe("req_a", self.epmc_run((missing,)))
        # A second, independent fresh observation of the same missing-id payload.
        b = self.observe("req_b", self.epmc_run((missing,)))

        self.ingest([a.run_id], request_id="corpus_1")
        self.ingest([b.run_id], request_id="corpus_2")
        snapshot = self.corpus.current()
        assert snapshot is not None
        unresolved = [
            doc for doc in snapshot["candidates"] if not doc["resolved"]
        ]
        self.assertEqual(len(unresolved), 2)
        for doc in unresolved:
            self.assertEqual(doc["unresolved_reason"], "missing_identifiers")
            self.assertEqual(doc["tokens"], [])
            self.assertEqual(len(doc["occurrences"]), 1)

    def test_identifier_conflict_never_merges(self) -> None:
        a = self.observe(
            "req_a",
            self.pubmed_run(
                (self.rec(source=SOURCE_PUBMED, source_id="A", pmid="1", doi="x"),)
            ),
        )
        b = self.observe(
            "req_b",
            self.epmc_run(
                (
                    self.rec(
                        source=SOURCE_EUROPEPMC, source_id="MED:B", pmid="2", doi="x"
                    ),
                )
            ),
        )
        first = self.ingest([a.run_id], request_id="corpus_1")
        first_snapshot = self.corpus.current()
        self.assertEqual(len(first_snapshot["candidates"]), 1)

        self.ingest([b.run_id], request_id="corpus_2")
        snapshot = self.corpus.current()
        assert snapshot is not None
        resolved = [doc for doc in snapshot["candidates"] if doc["resolved"]]
        unresolved = [doc for doc in snapshot["candidates"] if not doc["resolved"]]
        # The prior candidate is preserved; the conflicting occurrence is an
        # explicit unresolved candidate -- no silent merge.
        self.assertEqual(len(resolved), 1)
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0]["unresolved_reason"], "identifier_conflict")
        self.assertEqual(len(snapshot["conflicts"]), 1)
        conflict = snapshot["conflicts"][0]
        self.assertEqual(conflict["namespaces"], {"pmid": ["1", "2"]})
        self.assertEqual(conflict["prior_candidate_ids"], [resolved[0]["candidate_id"]])
        self.assertEqual(
            conflict["unresolved_candidate_ids"], [unresolved[0]["candidate_id"]]
        )
        # The preserved candidate keeps its own occurrence and its identity.
        self.assertEqual(
            resolved[0]["candidate_id"], first_snapshot["candidates"][0]["candidate_id"]
        )
        self.assertEqual(self.corpus.get(
            {
                "project_id": self.project_id,
                "artifact_id": first.artifact_id,
                "revision_id": first.revision_id,
            }
        ), first_snapshot)

    def test_incremental_bridging_creates_survivor_and_alias(self) -> None:
        a = self.observe(
            "req_a",
            self.pubmed_run(
                (self.rec(source=SOURCE_PUBMED, source_id="P1", pmid="1", doi=None),)
            ),
        )
        b = self.observe(
            "req_b",
            self.epmc_run(
                (self.rec(source=SOURCE_EUROPEPMC, source_id="E1", pmid=None, doi="y"),)
            ),
        )
        bridge = self.observe(
            "req_bridge",
            self.pubmed_run(
                (self.rec(source=SOURCE_PUBMED, source_id="P1", pmid="1", doi="y"),)
            ),
        )
        self.ingest([a.run_id], request_id="corpus_1")
        self.ingest([b.run_id], request_id="corpus_2")
        two = self.corpus.current()
        assert two is not None
        self.assertEqual(len(two["candidates"]), 2)
        prior_ids = sorted(doc["candidate_id"] for doc in two["candidates"])

        self.ingest([bridge.run_id], request_id="corpus_3")
        snapshot = self.corpus.current()
        assert snapshot is not None
        self.assertEqual(len(snapshot["candidates"]), 1)
        survivor = snapshot["candidates"][0]
        self.assertTrue(survivor["resolved"])
        self.assertEqual(len(survivor["occurrences"]), 3)
        self.assertEqual(survivor["aliases"], sorted(prior_ids[1:]))
        self.assertEqual(snapshot["aliases"], [[prior_ids[1], prior_ids[0]]])
        self.assertEqual(snapshot["candidates"][0]["candidate_id"], prior_ids[0])
        merged = [
            decision
            for decision in snapshot["dedup_decisions"]
            if decision["kind"] == "merged_bridging"
        ]
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["survivor_id"], prior_ids[0])
        self.assertEqual(merged[0]["alias_ids"], [prior_ids[1]])

    def test_native_namespace_conflict_stays_conservative(self) -> None:
        # Two same-provider observations with a shared pmid but different native
        # source ids conflict in the provider-native namespace: never merged.
        a = self.observe(
            "req_a",
            self.pubmed_run((self.rec(source=SOURCE_PUBMED, source_id="A", pmid="1"),)),
        )
        b = self.observe(
            "req_b",
            self.pubmed_run((self.rec(source=SOURCE_PUBMED, source_id="B", pmid="1"),)),
        )
        self.ingest([a.run_id], request_id="corpus_1")
        self.ingest([b.run_id], request_id="corpus_2")
        snapshot = self.corpus.current()
        assert snapshot is not None
        resolved = [doc for doc in snapshot["candidates"] if doc["resolved"]]
        unresolved = [doc for doc in snapshot["candidates"] if not doc["resolved"]]
        self.assertEqual(len(resolved), 1)
        self.assertEqual(len(unresolved), 1)
        conflict = snapshot["conflicts"][0]
        self.assertEqual(conflict["namespaces"]["native:pubmed"], ["A", "B"])


class FailureAndRestartTests(CandidateTestBase):
    def test_partial_failure_run_is_kept_not_excluded(self) -> None:
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
        result = fx.result(
            (ok_page, failed_page), stop_reason="failure", failure="http_status"
        )
        a = self.observe("req_a", result)
        self.ingest([a.run_id], request_id="corpus_1")
        snapshot = self.corpus.current()
        assert snapshot is not None
        self.assertEqual(len(snapshot["runs"]), 1)
        self.assertFalse(snapshot["runs"][0]["complete"])
        self.assertEqual(snapshot["runs"][0]["failure"], "http_status")
        self.assertEqual(snapshot["runs"][0]["stop_reason"], "failure")
        self.assertEqual(snapshot["occurrences"], [[a.run_id, 0]])

    def test_restart_reconstructs_snapshot(self) -> None:
        a = self.observe("req_a", self.pubmed_run((self.rec(pmid="9", source_id="9"),)))
        revision = self.ingest([a.run_id], request_id="corpus_1")
        before = self.corpus.current()

        self._reopen()
        after = self.corpus.current()
        self.assertEqual(after, before)
        self.assertEqual(
            self.corpus.get(
                {
                    "project_id": self.project_id,
                    "artifact_id": revision.artifact_id,
                    "revision_id": revision.revision_id,
                }
            ),
            before,
        )

    def test_earlier_revision_is_byte_unchanged_after_later_ingest(self) -> None:
        a = self.observe("req_a", self.pubmed_run((self.rec(pmid="1", source_id="a"),)))
        b = self.observe("req_b", self.pubmed_run((self.rec(pmid="2", source_id="b"),)))
        first = self.ingest([a.run_id], request_id="corpus_1")
        before = self.corpus.current()
        content_before = self.artifacts.read_content(first.artifact_id, first.revision_id)

        self.ingest([b.run_id], request_id="corpus_2")
        current = self.corpus.current()
        assert current is not None
        self.assertNotEqual(current["revision_id"], first.revision_id)

        after = self.corpus.get(
            {
                "project_id": self.project_id,
                "artifact_id": first.artifact_id,
                "revision_id": first.revision_id,
            }
        )
        self.assertEqual(after, before)
        self.assertEqual(
            self.artifacts.read_content(first.artifact_id, first.revision_id),
            content_before,
        )
        self.assertEqual(self._revision_count(), 2)

    def test_reopen_keeps_candidate_ids_stable(self) -> None:
        a = self.observe("req_a", self.pubmed_run((self.rec(pmid="1", source_id="1"),)))
        self.ingest([a.run_id], request_id="corpus_1")
        before = self.corpus.current()
        self._reopen()
        b = self.observe("req_b", self.pubmed_run((self.rec(pmid="1", source_id="1"),)))
        self.ingest([b.run_id], request_id="corpus_2")
        after = self.corpus.current()
        assert after is not None
        self.assertEqual(len(after["candidates"]), 1)
        self.assertEqual(
            after["candidates"][0]["candidate_id"],
            before["candidates"][0]["candidate_id"],
        )


class IsolationAndRollbackTests(CandidateTestBase):
    def test_storage_not_allowed_rights_are_refused_without_rows(self) -> None:
        a = self.observe("req_a", self.pubmed_run((self.rec(pmid="1", source_id="1"),)))
        rid = CandidateRepository.new_revision_id()
        rights = self.rights(DEFAULT_CORPUS_ARTIFACT_ID, rid)
        rights["operations"]["storage"] = "not_allowed"
        rights["basis"]["storage"] = {
            "kind": "unknown",
            "evidence": {"ref": "n/a", "provenance": "n/a"},
        }
        with self.assertRaises(CandidateValidationError):
            self.corpus.ingest(
                [a.run_id], request_id="corpus_1", rights=rights, revision_id=rid
            )
        self.assertEqual(self._revision_count(), 0)
        self.assertIsNone(self.corpus.current())

    def test_rights_bound_to_a_different_revision_are_refused(self) -> None:
        a = self.observe("req_a", self.pubmed_run((self.rec(pmid="1", source_id="1"),)))
        rid = CandidateRepository.new_revision_id()
        # A rights record whose subject binds a different revision cannot
        # authorize this exact revision.
        wrong = self.rights(DEFAULT_CORPUS_ARTIFACT_ID, "rev_other")
        with self.assertRaises(CandidateValidationError):
            self.corpus.ingest(
                [a.run_id], request_id="corpus_1", rights=wrong, revision_id=rid
            )
        self.assertEqual(self._revision_count(), 0)

    def test_projects_are_isolated(self) -> None:
        a = self.observe("req_a", self.pubmed_run((self.rec(pmid="1", source_id="a"),)))
        revision = self.ingest([a.run_id], request_id="corpus_1")

        other_id = paths.new_project_id()
        other_app = paths.app_dir_for(str(self.tmp / "other"))
        other_store = ProjectStore.create_at(other_app, other_id)
        self.addCleanup(other_store.close)
        other = CandidateRepository(other_store)
        self.assertIsNone(other.current())
        with self.assertRaises(CandidateValidationError):
            other.get(
                {
                    "project_id": other_id,
                    "artifact_id": revision.artifact_id,
                    "revision_id": revision.revision_id,
                }
            )

        # The other project can record its own snapshot independently.
        other_discovery = DiscoveryRepository(other_store)
        other_record = other_discovery.record_search(
            request_id="req_a", result=self.pubmed_run((self.rec(pmid="1", source_id="a"),))
        )
        rid = CandidateRepository.new_revision_id()
        other.ingest(
            [other_record.run_id],
            request_id="corpus_1",
            rights=_rights_record(other_id, DEFAULT_CORPUS_ARTIFACT_ID, rid),
            revision_id=rid,
        )
        self.assertIsNotNone(other.current())
        self.assertEqual(len(self.discovery.list_searches()), 1)
        self.assertEqual(len(other_discovery.list_searches()), 1)

    def test_induced_write_failure_leaves_no_revision(self) -> None:
        a = self.observe("req_a", self.pubmed_run((self.rec(pmid="1", source_id="a"),)))
        with mock.patch.object(
            ArtifactRepository, "_insert_import_request", side_effect=RuntimeError("forced")
        ):
            with self.assertRaises(RuntimeError):
                self.ingest([a.run_id], request_id="corpus_1")
        self.assertEqual(self._revision_count(), 0)
        self.assertIsNone(self.corpus.current())

    def test_stale_parent_is_refused_and_does_not_overwrite(self) -> None:
        a = self.observe("req_a", self.pubmed_run((self.rec(pmid="1", source_id="a"),)))
        b = self.observe("req_b", self.pubmed_run((self.rec(pmid="2", source_id="b"),)))
        c = self.observe("req_c", self.pubmed_run((self.rec(pmid="3", source_id="c"),)))
        first = self.ingest([a.run_id], request_id="corpus_1")
        second = self.ingest([b.run_id], request_id="corpus_2")
        stale_revision = self.artifacts.get_revision(
            first.artifact_id, first.revision_id
        )
        assert stale_revision is not None

        with mock.patch.object(
            CandidateRepository, "_current_revision", return_value=stale_revision
        ):
            with self.assertRaises(CandidateValidationError):
                self.ingest([c.run_id], request_id="corpus_3")

        self.assertEqual(self._revision_count(), 2)
        current = self.corpus.current()
        assert current is not None
        self.assertEqual(current["revision_id"], second.revision_id)

    def test_deterministic_regardless_of_input_order(self) -> None:
        a = self.observe("req_a", self.pubmed_run((self.rec(pmid="1", source_id="a"),)))
        b = self.observe("req_b", self.pubmed_run((self.rec(pmid="2", source_id="b"),)))
        self.ingest([a.run_id, b.run_id], request_id="corpus_x", artifact_id="corpus.x")
        self.ingest([b.run_id, a.run_id], request_id="corpus_y", artifact_id="corpus.y")
        first = self.corpus.current("corpus.x")
        second = self.corpus.current("corpus.y")
        assert first is not None and second is not None
        self.assertEqual(first["candidates"], second["candidates"])
        self.assertEqual(first["occurrences"], second["occurrences"])
        self.assertEqual(first["conflicts"], second["conflicts"])
        self.assertEqual(first["aliases"], second["aliases"])
        self.assertEqual(first["dedup_decisions"], second["dedup_decisions"])
        self.assertEqual(first["runs"], second["runs"])


class CorruptionTests(CandidateTestBase):
    def _snapshot_revision(self):
        a = self.observe("req_a", self.pubmed_run((self.rec(pmid="1", source_id="a"),)))
        return self.ingest([a.run_id], request_id="corpus_1"), a

    def test_content_tampering_with_wrong_digest_is_corruption(self) -> None:
        revision, _a = self._snapshot_revision()
        self._tamper_revision(
            lambda conn: conn.execute(
                "UPDATE revision SET content = ? WHERE project_id = ? AND artifact_id = ? "
                "AND revision_id = ?",
                (b"not-the-original", self.project_id, revision.artifact_id, revision.revision_id),
            )
        )
        with self.assertRaises(CandidateCorruptionError):
            self.corpus.current()

    def test_payload_tampering_with_matching_digest_is_corruption(self) -> None:
        revision, _a = self._snapshot_revision()
        content = self.artifacts.read_content(revision.artifact_id, revision.revision_id)
        envelope = json.loads(content.decode("utf-8"))
        envelope["payload"]["authorizing"] = True
        forged = canonical_json(envelope)
        self._tamper_revision(
            lambda conn: conn.execute(
                "UPDATE revision SET content = ?, content_sha256 = ?, byte_size = ? "
                "WHERE project_id = ? AND artifact_id = ? AND revision_id = ?",
                (
                    forged,
                    "sha256:" + _sha(forged),
                    len(forged),
                    self.project_id,
                    revision.artifact_id,
                    revision.revision_id,
                ),
            )
        )
        with self.assertRaises(CandidateCorruptionError):
            self.corpus.current()

    def test_never_clean_integrity_is_refused_on_read(self) -> None:
        revision, _a = self._snapshot_revision()
        content = self.artifacts.read_content(revision.artifact_id, revision.revision_id)
        envelope = json.loads(content.decode("utf-8"))
        envelope["payload"]["candidates"][0]["integrity"]["status"] = "clean"
        forged = canonical_json(envelope)
        self._tamper_revision(
            lambda conn: conn.execute(
                "UPDATE revision SET content = ?, content_sha256 = ?, byte_size = ? "
                "WHERE project_id = ? AND artifact_id = ? AND revision_id = ?",
                (
                    forged,
                    "sha256:" + _sha(forged),
                    len(forged),
                    self.project_id,
                    revision.artifact_id,
                    revision.revision_id,
                ),
            )
        )
        with self.assertRaises(CandidateCorruptionError):
            self.corpus.current()

    def test_run_disagreement_is_corruption(self) -> None:
        revision, _a = self._snapshot_revision()
        content = self.artifacts.read_content(revision.artifact_id, revision.revision_id)
        envelope = json.loads(content.decode("utf-8"))
        envelope["payload"]["runs"][0]["result_sha256"] = "0" * 64
        forged = canonical_json(envelope)
        self._tamper_revision(
            lambda conn: conn.execute(
                "UPDATE revision SET content = ?, content_sha256 = ?, byte_size = ? "
                "WHERE project_id = ? AND artifact_id = ? AND revision_id = ?",
                (
                    forged,
                    "sha256:" + _sha(forged),
                    len(forged),
                    self.project_id,
                    revision.artifact_id,
                    revision.revision_id,
                ),
            )
        )
        with self.assertRaises(CandidateCorruptionError):
            self.corpus.current()

    def test_corrupt_observation_is_surfaced_as_snapshot_corruption(self) -> None:
        _revision, a = self._snapshot_revision()
        conn = sqlite3.connect(paths.store_path_for(str(self.tmp / "project")))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='discovery_search_run_no_update'"
            ).fetchone()
            saved = row[0] if row and row[0] else None
            conn.execute("DROP TRIGGER IF EXISTS discovery_search_run_no_update")
            conn.execute(
                "UPDATE discovery_search_run SET source = 'forged' "
                "WHERE project_id = ? AND run_id = ?",
                (self.project_id, a.run_id),
            )
            if saved:
                conn.execute(saved)
            conn.commit()
        finally:
            conn.close()
        with self.assertRaises(CandidateCorruptionError):
            self.corpus.current()

    def test_tampered_conflict_namespaces_is_corruption(self) -> None:
        a = self.observe(
            "req_a",
            self.pubmed_run((self.rec(source=SOURCE_PUBMED, source_id="A", pmid="1", doi="x"),)),
        )
        b = self.observe(
            "req_b",
            self.epmc_run(
                (self.rec(source=SOURCE_EUROPEPMC, source_id="MED:B", pmid="2", doi="x"),)
            ),
        )
        self.ingest([a.run_id], request_id="corpus_1")
        revision = self.ingest([b.run_id], request_id="corpus_2")
        content = self.artifacts.read_content(revision.artifact_id, revision.revision_id)
        envelope = json.loads(content.decode("utf-8"))
        self.assertEqual(len(envelope["payload"]["conflicts"]), 1)
        envelope["payload"]["conflicts"][0]["namespaces"] = {"pmid": ["1", "3"]}
        forged = canonical_json(envelope)
        self._tamper_revision(
            lambda conn: conn.execute(
                "UPDATE revision SET content = ?, content_sha256 = ?, byte_size = ? "
                "WHERE project_id = ? AND artifact_id = ? AND revision_id = ?",
                (
                    forged,
                    "sha256:" + _sha(forged),
                    len(forged),
                    self.project_id,
                    revision.artifact_id,
                    revision.revision_id,
                ),
            )
        )
        with self.assertRaises(CandidateCorruptionError):
            self.corpus.current()

    def test_unknown_run_id_and_bad_inputs_are_refused(self) -> None:
        with self.assertRaises(CandidateValidationError):
            self.ingest(["dsr_missing"], request_id="corpus_1")
        with self.assertRaises(CandidateValidationError):
            self.ingest([123], request_id="corpus_1")  # type: ignore[list-item]
        with self.assertRaises(CandidateValidationError):
            self.ingest(["has space"], request_id="corpus_1")
        with self.assertRaises(CandidateValidationError):
            self.ingest([], request_id="bad request id")
        self.assertEqual(self._revision_count(), 0)


class BackupRestoreTests(CandidateTestBase):
    def test_backup_restore_preserves_snapshot(self) -> None:
        a = self.observe("req_a", self.pubmed_run((self.rec(pmid="1", source_id="a"),)))
        self.ingest([a.run_id], request_id="corpus_1")
        before = self.corpus.current()

        dest = self.tmp / "backup"
        create_backup(self.store, dest)
        registry = Registry(self.tmp / "state")
        target = self.tmp / "restored"
        target.mkdir()
        info = restore_backup(dest, target, registry)
        restored_store = ProjectStore.open_at(
            paths.app_dir_for(str(target)), expected_project_id=info.project_id
        )
        self.addCleanup(restored_store.close)
        restored = CandidateRepository(restored_store)
        self.assertEqual(restored.current(), before)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
