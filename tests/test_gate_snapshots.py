"""Acceptance tests for read-only human-gate snapshot preparation/revalidation.

Synthetic data only (no secrets, no licensed source text, no credentials, no
network, no model calls). These tests assert the **format/pin validation**
behavior of ``review_article_agent.runtime.gates``:

* a valid exact target closure prepares an immutable, canonical, deterministic
  :class:`PreparedGate`;
* missing / corrupt-digest / byte-size / rights-identity / rights-bytes /
  rights-subject / superseded-rights / superseded-live-input / cross-project
  pins are blocked with static typed errors and write nothing;
* a later edit to the target, a live dependency, or a pinned rights record makes
  revalidation ``stale``; a digest/bytes corruption makes it ``invalid``; a
  cross-project object is ``denied``;
* the preparation digest binds the gate kind/id, decision, and rationale, and
  the yielded ``OwnerAction`` binds that exact digest;
* no generic saved permission, in-band authority keyword, or hand-built object
  can be converted into a validated snapshot.

They claim **no** scientific validity and no authority: preparation authorizes
no execution, no export, and no scientific gate.

Run with::

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_gate_snapshots.py'
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.domain.records import canonical_json  # noqa: E402
from review_article_agent.runtime import gates  # noqa: E402
from review_article_agent.runtime.gates import (  # noqa: E402
    GateContentIntegrityError,
    GateDefinitionError,
    GateDependencyError,
    GateRightsError,
    GateSnapshotError,
    GateSupersededInputError,
    GateTargetError,
    PreparedGate,
    PreparedGateDeniedError,
    PreparedGateInvalidError,
    PreparedGateStaleError,
    prepare_gate_snapshot,
    revalidate_prepared,
)
from review_article_agent.runtime.owner_auth import action_digest  # noqa: E402
from review_article_agent.storage import ArtifactRepository, ProjectStore  # noqa: E402
from review_article_agent.storage import paths  # noqa: E402
from review_article_agent.storage.approvals import ApprovalRepository  # noqa: E402
from review_article_agent.storage.artifacts import DependencyRef, sha256_digest  # noqa: E402

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")
_GATE_KW = dict(gate_kind="artifact_revision", gate_id="gate-1", decision="approved")


def _rights(
    project_id: str,
    artifact_id: str,
    revision_id: str,
    *,
    rights_id: str | None = None,
    record_revision: int = 1,
) -> dict:
    """A synthetic, fully valid RightsRecord v1 bound to one exact revision."""
    return {
        "rights_id": rights_id or f"RIGHTS-{revision_id}",
        "project_id": project_id,
        "record_revision": record_revision,
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


def _table_counts(store) -> dict[str, int]:
    conn = store.connection
    tables = [
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    ]
    return {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in tables
    }


class GateSnapshotTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-gate-snap-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def new_store(self, name: str = "p") -> ProjectStore:
        store = ProjectStore.create_at(
            paths.app_dir_for(str(self.tmp / name)), paths.new_project_id()
        )
        self.addCleanup(store.close)
        return store

    def _import(
        self,
        repo: ArtifactRepository,
        artifact_id: str,
        content: bytes,
        revision_id: str,
        *,
        dependencies=(),
        parent_revision_id: str | None = None,
        rights: dict | None = None,
    ):
        return repo.import_revision(
            artifact_id=artifact_id,
            content=content,
            origin_locator="import:synthetic",
            rights=rights
            if rights is not None
            else _rights(repo.project_id, artifact_id, revision_id),
            revision_id=revision_id,
            dependencies=list(dependencies),
            parent_revision_id=parent_revision_id,
        )

    def _target(self, store, artifact_id: str, revision_id: str) -> dict:
        return {
            "project_id": store.project_id,
            "artifact_id": artifact_id,
            "revision_id": revision_id,
        }

    def _chain(self, store) -> ArtifactRepository:
        """base document <- finding <- report (a transitive live closure)."""
        repo = ArtifactRepository(store)
        pid = store.project_id
        repo.create_artifact("a-base", kind="document")
        self._import(repo, "a-base", b"base-bytes", "rev-base")
        repo.create_artifact("a-find", kind="finding")
        self._import(
            repo,
            "a-find",
            b"find-bytes",
            "rev-find",
            dependencies=[self._target(store, "a-base", "rev-base")],
        )
        repo.create_artifact("a-rep", kind="report")
        self._import(
            repo,
            "a-rep",
            b"rep-bytes",
            "rev-rep",
            dependencies=[self._target(store, "a-find", "rev-find")],
        )
        return repo

    # -- corruption helpers (test-only; simulate on-disk corruption) --------- #
    def _drop_update_trigger(self, store, table: str) -> None:
        store.connection.execute(f"DROP TRIGGER IF EXISTS {table}_no_update")

    def _corrupt_revision(
        self,
        store,
        artifact_id: str,
        revision_id: str,
        *,
        content: bytes | None = None,
        byte_size: int | None = None,
        content_sha256: str | None = None,
    ) -> None:
        self._drop_update_trigger(store, "revision")
        where = "project_id = ? AND artifact_id = ? AND revision_id = ?"
        params = (store.project_id, artifact_id, revision_id)
        for column, value in (
            ("content", content),
            ("byte_size", byte_size),
            ("content_sha256", content_sha256),
        ):
            if value is not None:
                store.connection.execute(
                    f"UPDATE revision SET {column} = ? WHERE {where}", (value, *params)
                )

    def _rewrite_rights(self, store, rights_id: str, mutate) -> None:
        """Rewrite a pinned rights record's canonical bytes (and its digest)."""
        self._drop_update_trigger(store, "rights_record")
        row = store.connection.execute(
            "SELECT canonical FROM rights_record WHERE project_id = ? "
            "AND rights_id = ? AND record_revision = 1",
            (store.project_id, rights_id),
        ).fetchone()
        record = json.loads(bytes(row["canonical"]).decode("utf-8"))
        mutate(record)
        new_canonical = canonical_json(record)
        store.connection.execute(
            "UPDATE rights_record SET canonical = ?, content_sha256 = ? "
            "WHERE project_id = ? AND rights_id = ? AND record_revision = 1",
            (new_canonical, sha256_digest(new_canonical), store.project_id, rights_id),
        )

    def _corrupt_rights_bytes(self, store, rights_id: str) -> None:
        self._drop_update_trigger(store, "rights_record")
        store.connection.execute(
            "UPDATE rights_record SET content_sha256 = ? WHERE project_id = ? "
            "AND rights_id = ? AND record_revision = 1",
            ("sha256:" + "0" * 64, store.project_id, rights_id),
        )

    def _corrupt_rights_row_subject(self, store, rights_id: str) -> None:
        self._drop_update_trigger(store, "rights_record")
        store.connection.execute(
            "UPDATE rights_record SET subject_revision_id = ? WHERE project_id = ? "
            "AND rights_id = ? AND record_revision = 1",
            ("rev-other", store.project_id, rights_id),
        )

    def _insert_newer_rights(self, store, rights_id: str, record_revision: int = 2) -> None:
        pid = store.project_id
        canonical = canonical_json(
            {"rights_id": rights_id, "project_id": pid, "record_revision": record_revision}
        )
        store.connection.execute(
            "INSERT INTO rights_record "
            "(project_id, rights_id, record_revision, subject_kind, subject_project_id, "
            " subject_artifact_id, subject_revision_id, canonical, content_sha256, "
            " recorded_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                pid,
                rights_id,
                record_revision,
                "source_revision",
                pid,
                "a-tampered",
                None,
                canonical,
                sha256_digest(canonical),
                "2026-10-28T00:00:00+00:00",
            ),
        )


# --------------------------------------------------------------------------- #
# Preparation
# --------------------------------------------------------------------------- #
class PrepareTest(GateSnapshotTestBase):
    def test_valid_target_closure_prepares_exact_snapshot(self) -> None:
        with self.new_store() as store:
            repo = self._chain(store)
            pid = store.project_id
            prepared = prepare_gate_snapshot(
                store,
                self._target(store, "a-rep", "rev-rep"),
                rationale="human review of the report revision",
                **_GATE_KW,
            )
            self.assertEqual(prepared.project_id, pid)
            self.assertEqual(prepared.gate_kind, "artifact_revision")
            self.assertEqual(prepared.gate_id, "gate-1")
            self.assertEqual(prepared.decision, "approved")
            self.assertEqual(dict(prepared.target), self._target(store, "a-rep", "rev-rep"))

            # Exact closure: target + finding + source document, all one project.
            self.assertIsInstance(prepared.entries, tuple)
            self.assertEqual(
                [(e.artifact_id, e.revision_id, e.role) for e in prepared.entries],
                [
                    ("a-rep", "rev-rep", "target"),
                    ("a-find", "rev-find", "finding"),
                    ("a-base", "rev-base", "source_document"),
                ],
            )
            for entry in prepared.entries:
                self.assertEqual(entry.project_id, pid)
                self.assertEqual(entry.byte_size, len(repo.read_content(entry.artifact_id, entry.revision_id)))
                self.assertEqual(
                    entry.content_sha256,
                    sha256_digest(repo.read_content(entry.artifact_id, entry.revision_id)),
                )
                self.assertTrue(entry.rights_digest.startswith("sha256:"))

            # The rights digest pins the exact stored canonical rights bytes.
            rights_row = store.connection.execute(
                "SELECT content_sha256 FROM rights_record WHERE project_id = ? "
                "AND rights_id = ? AND record_revision = 1",
                (pid, "RIGHTS-rev-base"),
            ).fetchone()
            base_entry = prepared.entries[2]
            self.assertEqual(base_entry.rights_id, "RIGHTS-rev-base")
            self.assertEqual(base_entry.rights_digest, rights_row["content_sha256"])

            # Canonical digest binds the exact canonical bytes; ids are deterministic.
            self.assertEqual(
                prepared.digest, hashlib.sha256(prepared.canonical_bytes).hexdigest()
            )
            self.assertEqual(prepared.snapshot_id, "psnap_" + prepared.digest)
            # The yielded owner action binds this exact snapshot digest.
            self.assertEqual(prepared.owner_action().snapshot_digest, prepared.digest)
            # Preparing authorizes nothing.
            self.assertFalse(prepared.authorizes_execution)
            self.assertFalse(prepared.authorizes_export)

    def test_preparation_is_deterministic_and_canonical(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            target = self._target(store, "a-rep", "rev-rep")
            first = prepare_gate_snapshot(store, target, rationale="r", **_GATE_KW)
            second = prepare_gate_snapshot(store, dict(target), rationale="r", **_GATE_KW)
            self.assertEqual(first.canonical_bytes, second.canonical_bytes)
            self.assertEqual(first.digest, second.digest)
            self.assertEqual(first.snapshot_id, second.snapshot_id)
            # No random/time value is in the hashed structure.
            self.assertNotIn("snapshot_id", json.loads(first.canonical_bytes))

    def test_caller_target_and_entries_are_immutable(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            target = self._target(store, "a-rep", "rev-rep")
            prepared = prepare_gate_snapshot(store, target, rationale="r", **_GATE_KW)
            # Mutating the caller's dict must not change the prepared target.
            target["artifact_id"] = "a-mutated"
            self.assertEqual(prepared.target["artifact_id"], "a-rep")
            with self.assertRaises(TypeError):
                prepared.target["artifact_id"] = "x"  # mapping proxy
            with self.assertRaises(dataclasses.FrozenInstanceError):
                prepared.entries[0].artifact_id = "x"
            with self.assertRaises(AttributeError):
                prepared._digest = "x"  # noqa: SLF001 - immutability check
            with self.assertRaises(TypeError):
                prepared.version_set["context_kind"] = "mixed"

    def test_repr_and_errors_never_leak_source_bytes(self) -> None:
        with self.new_store() as store:
            repo = ArtifactRepository(store)
            repo.create_artifact("a-doc", kind="document")
            canary = b"SECRET-CANARY-9931-source-bytes"
            self._import(repo, "a-doc", canary, "rev-1")
            target = self._target(store, "a-doc", "rev-1")
            prepared = prepare_gate_snapshot(store, target, rationale="r", **_GATE_KW)
            self.assertNotIn(b"SECRET-CANARY", prepared.canonical_bytes)
            self.assertNotIn("SECRET-CANARY", repr(prepared))
            self.assertNotIn("SECRET-CANARY", repr(prepared.entries))

            # A failing preparation never echoes the source bytes either.
            self._corrupt_revision(store, "a-doc", "rev-1", content_sha256="sha256:" + "0" * 64)
            try:
                prepare_gate_snapshot(store, target, rationale="r", **_GATE_KW)
            except GateContentIntegrityError as exc:
                self.assertNotIn("SECRET-CANARY", str(exc))
                self.assertNotIn(canary.decode(), str(exc))
            else:  # pragma: no cover - the corruption must fail closed
                self.fail("corrupt content digest did not block preparation")

    def test_wrong_target_shape_rejected(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            for bad in (
                {"artifact_id": "a-rep", "revision_id": "rev-rep"},  # no project
                {"project_id": store.project_id, "artifact_id": "a-rep"},  # no revision
                {
                    "project_id": store.project_id,
                    "artifact_id": "a-rep",
                    "revision_id": "rev-rep",
                    "extra": "x",
                },
                "a-rep/rev-rep",
                None,
            ):
                with self.assertRaises(GateTargetError):
                    prepare_gate_snapshot(store, bad, rationale="r", **_GATE_KW)

    def test_cross_project_target_rejected(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            foreign = {
                "project_id": "prj_" + "f" * 32,
                "artifact_id": "a-rep",
                "revision_id": "rev-rep",
            }
            with self.assertRaises(GateTargetError):
                prepare_gate_snapshot(store, foreign, rationale="r", **_GATE_KW)

    def test_missing_target_revision_rejected(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            with self.assertRaises(GateDependencyError):
                prepare_gate_snapshot(
                    store,
                    self._target(store, "a-rep", "rev-does-not-exist"),
                    rationale="r",
                    **_GATE_KW,
                )
            # A dangling dependency ref is blocked at the same boundary (a
            # dependency edge can only be recorded to an existing revision, so a
            # reachable-but-missing revision is exercised through the target).
            with self.assertRaises(GateDependencyError):
                prepare_gate_snapshot(
                    store,
                    self._target(store, "a-find", "rev-find-missing"),
                    rationale="r",
                    **_GATE_KW,
                )

    def test_bytes_corruption_rejected(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            self._corrupt_revision(store, "a-base", "rev-base", content=b"tampered")
            with self.assertRaises(GateContentIntegrityError):
                prepare_gate_snapshot(
                    store, self._target(store, "a-rep", "rev-rep"), rationale="r", **_GATE_KW
                )

    def test_byte_size_corruption_rejected(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            self._corrupt_revision(store, "a-base", "rev-base", byte_size=999999)
            with self.assertRaises(GateContentIntegrityError):
                prepare_gate_snapshot(
                    store, self._target(store, "a-rep", "rev-rep"), rationale="r", **_GATE_KW
                )

    def test_superseded_live_input_rejected(self) -> None:
        with self.new_store() as store:
            repo = ArtifactRepository(store)
            repo.create_artifact("a-doc", kind="document")
            self._import(repo, "a-doc", b"v1", "rev-1")
            # A later revision supersedes rev-1; a live input must be current.
            self._import(repo, "a-doc", b"v2", "rev-2", parent_revision_id="rev-1")
            with self.assertRaises(GateSupersededInputError):
                prepare_gate_snapshot(
                    store, self._target(store, "a-doc", "rev-1"), rationale="r", **_GATE_KW
                )
            # The current head still prepares cleanly.
            prepared = prepare_gate_snapshot(
                store, self._target(store, "a-doc", "rev-2"), rationale="r", **_GATE_KW
            )
            self.assertEqual(prepared.entries[0].revision_id, "rev-2")

    def test_historical_version_of_lineage_is_pinned_and_not_head_compared(self) -> None:
        with self.new_store() as store:
            repo = ArtifactRepository(store)
            pid = store.project_id
            repo.create_artifact("a-chain", kind="report")
            self._import(repo, "a-chain", b"v1", "rev-1")
            self._import(
                repo,
                "a-chain",
                b"v2",
                "rev-2",
                parent_revision_id="rev-1",
                dependencies=[
                    {
                        "project_id": pid,
                        "artifact_id": "a-chain",
                        "revision_id": "rev-1",
                        "role": "version_of",
                    }
                ],
            )
            # rev-1 is not the current head, but as a version_of ancestor it is a
            # historical pin, so preparation still succeeds.
            prepared = prepare_gate_snapshot(
                store, self._target(store, "a-chain", "rev-2"), rationale="r", **_GATE_KW
            )
            self.assertEqual(
                [(e.revision_id, e.role) for e in prepared.entries],
                [("rev-2", "target"), ("rev-1", "version_lineage")],
            )
            # The lineage pin is still verified exactly (rights digest present).
            self.assertTrue(prepared.entries[1].rights_digest.startswith("sha256:"))
            self.assertEqual(revalidate_prepared(store, prepared).status, "unchanged")

    def test_live_edge_to_ancestor_wins_and_is_head_checked(self) -> None:
        with self.new_store() as store:
            repo = ArtifactRepository(store)
            pid = store.project_id
            repo.create_artifact("a-chain", kind="report")
            self._import(repo, "a-chain", b"v1", "rev-1")
            # rev-1 is reachable through BOTH an ancestry and a live edge. The
            # live edge wins, so rev-1 is head-checked and (being stale) blocked.
            self._import(
                repo,
                "a-chain",
                b"v2",
                "rev-2",
                parent_revision_id="rev-1",
                dependencies=[
                    {
                        "project_id": pid,
                        "artifact_id": "a-chain",
                        "revision_id": "rev-1",
                        "role": "version_of",
                    },
                    {
                        "project_id": pid,
                        "artifact_id": "a-chain",
                        "revision_id": "rev-1",
                        "role": "derived_from",
                    },
                ],
            )
            with self.assertRaises(GateSupersededInputError):
                prepare_gate_snapshot(
                    store, self._target(store, "a-chain", "rev-2"), rationale="r", **_GATE_KW
                )

    def test_rights_identity_mismatch_rejected(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            self._rewrite_rights(
                store, "RIGHTS-rev-base", lambda rec: rec.__setitem__("rights_id", "RIGHTS-other")
            )
            with self.assertRaises(GateRightsError):
                prepare_gate_snapshot(
                    store, self._target(store, "a-rep", "rev-rep"), rationale="r", **_GATE_KW
                )

    def test_rights_row_subject_columns_inconsistency_rejected(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            self._corrupt_rights_row_subject(store, "RIGHTS-rev-base")
            with self.assertRaises(GateRightsError):
                prepare_gate_snapshot(
                    store, self._target(store, "a-rep", "rev-rep"), rationale="r", **_GATE_KW
                )

    def test_rights_bytes_corruption_rejected(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            self._corrupt_rights_bytes(store, "RIGHTS-rev-base")
            with self.assertRaises(GateRightsError):
                prepare_gate_snapshot(
                    store, self._target(store, "a-rep", "rev-rep"), rationale="r", **_GATE_KW
                )

    def test_rights_subject_mismatch_rejected(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            self._rewrite_rights(
                store,
                "RIGHTS-rev-base",
                lambda rec: rec["subject"].__setitem__("revision_id", "rev-other"),
            )
            with self.assertRaises(GateRightsError):
                prepare_gate_snapshot(
                    store, self._target(store, "a-rep", "rev-rep"), rationale="r", **_GATE_KW
                )

    def test_superseded_rights_record_rejected(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            self._insert_newer_rights(store, "RIGHTS-rev-base", 2)
            with self.assertRaises(GateRightsError):
                prepare_gate_snapshot(
                    store, self._target(store, "a-rep", "rev-rep"), rationale="r", **_GATE_KW
                )

    def test_unapproved_or_fixture_gate_kind_rejected(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            target = self._target(store, "a-rep", "rev-rep")
            for bad_kind in ("fixture_artifact_revision", "fixture_batch", "not_a_gate", "", None, {"x": 1}):
                with self.assertRaises(GateDefinitionError):
                    prepare_gate_snapshot(
                        store, target, rationale="r", gate_kind=bad_kind, gate_id="g", decision="approved"
                    )

    def test_bad_decision_gate_id_and_rationale_rejected(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            target = self._target(store, "a-rep", "rev-rep")
            with self.assertRaises(GateDefinitionError):
                prepare_gate_snapshot(
                    store, target, rationale="r", gate_kind="protocol", gate_id="g", decision="maybe"
                )
            for bad_gate_id in ("", "a/b", "../x", None):
                with self.assertRaises(GateDefinitionError):
                    prepare_gate_snapshot(
                        store, target, rationale="r", gate_kind="protocol", gate_id=bad_gate_id,
                        decision="approved",
                    )
            for bad_rationale in ("", None, 5):
                with self.assertRaises(GateDefinitionError):
                    prepare_gate_snapshot(
                        store, target, rationale=bad_rationale, gate_kind="protocol",
                        gate_id="g", decision="approved",
                    )

    def test_generic_saved_permission_and_in_band_authority_rejected(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            target = self._target(store, "a-rep", "rev-rep")
            # A generic saved-permission/tool payload is not a gate target.
            with self.assertRaises(GateTargetError):
                prepare_gate_snapshot(
                    store,
                    {"type": "saved_permission", "scope": "tools", "allowed": True},
                    rationale="r",
                    **_GATE_KW,
                )
            # In-band authority/authentication keywords are never accepted.
            for extra in (
                {"authority": "owner_authenticated"},
                {"actor": {"actor_kind": "human"}},
                {"authenticated": True},
                {"owner_auth_ref": "auth_x"},
                {"granted_by": {"actor_kind": "human"}},
            ):
                with self.assertRaises(TypeError):
                    prepare_gate_snapshot(store, target, rationale="r", **_GATE_KW, **extra)

    def test_prepared_gate_cannot_be_hand_constructed(self) -> None:
        with self.assertRaises(GateSnapshotError):
            PreparedGate(
                None,  # a forged/absent mint guard
                project_id="prj_" + "0" * 32,
                target={"project_id": "prj_" + "0" * 32, "artifact_id": "a", "revision_id": "r"},
                gate_kind="protocol",
                gate_id="g",
                decision="approved",
                rationale="r",
                snapshot_id="psnap_x",
                digest="0" * 64,
                canonical_bytes=b"{}",
                entries=(),
                version_set={},
                storage_schema_version=4,
                snapshot_schema_version=1,
            )

    def test_cyclic_dependency_edge_terminates(self) -> None:
        with self.new_store() as store:
            repo = ArtifactRepository(store)
            repo.create_artifact("a-doc", kind="document")
            self._import(repo, "a-doc", b"v1", "rev-1")
            target = self._target(store, "a-doc", "rev-1")

            def _self_edge(self, artifact_id, revision_id):
                # A (forged) edge from the revision back to itself must not loop.
                return [
                    DependencyRef(store.project_id, artifact_id, revision_id, "derived_from")
                ]

            with mock.patch.object(
                ArtifactRepository, "list_dependencies", autospec=True, side_effect=_self_edge
            ):
                prepared = prepare_gate_snapshot(store, target, rationale="r", **_GATE_KW)
            self.assertEqual([e.revision_id for e in prepared.entries], ["rev-1"])

    def test_closure_bound_is_enforced(self) -> None:
        with self.new_store() as store:
            repo = ArtifactRepository(store)
            repo.create_artifact("a-doc", kind="document")
            self._import(repo, "a-doc", b"v1", "rev-1")
            oversized = [object()] * (gates.MAX_GATE_SNAPSHOT_ENTRIES + 1)
            with mock.patch.object(
                ApprovalRepository, "_build_snapshot", return_value=oversized
            ):
                with self.assertRaises(GateDependencyError):
                    prepare_gate_snapshot(
                        store, self._target(store, "a-doc", "rev-1"), rationale="r", **_GATE_KW
                    )

    def test_prepare_writes_nothing(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            before = _table_counts(store)
            self.assertEqual(before.get("fixture_approval", 0), 0)
            prepare_gate_snapshot(
                store, self._target(store, "a-rep", "rev-rep"), rationale="r", **_GATE_KW
            )
            after = _table_counts(store)
            self.assertEqual(after, before)


# --------------------------------------------------------------------------- #
# Digest binding and versions
# --------------------------------------------------------------------------- #
class DigestBindingTest(GateSnapshotTestBase):
    def test_preparation_digest_binds_gate_identity_and_rationale(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            target = self._target(store, "a-rep", "rev-rep")
            baseline = prepare_gate_snapshot(store, target, rationale="r", **_GATE_KW)
            variants = [
                prepare_gate_snapshot(store, target, rationale="r", gate_kind="artifact_revision", gate_id="gate-2", decision="approved"),
                prepare_gate_snapshot(store, target, rationale="r", gate_kind="artifact_revision", gate_id="gate-1", decision="rejected"),
                prepare_gate_snapshot(store, target, rationale="different", **_GATE_KW),
                prepare_gate_snapshot(store, target, rationale="r", gate_kind="pilot", gate_id="gate-1", decision="approved"),
            ]
            digests = {baseline.digest} | {v.digest for v in variants}
            self.assertEqual(len(digests), 5)
            for variant in variants:
                self.assertNotEqual(variant.canonical_bytes, baseline.canonical_bytes)
                self.assertNotEqual(
                    action_digest(variant.owner_action()),
                    action_digest(baseline.owner_action()),
                )
            # The digest is stable for the same inputs and binds the action.
            again = prepare_gate_snapshot(store, target, rationale="r", **_GATE_KW)
            self.assertEqual(again.digest, baseline.digest)
            self.assertEqual(
                action_digest(again.owner_action()), action_digest(baseline.owner_action())
            )

    def test_versions_recorded_without_fake_model_version(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            prepared = prepare_gate_snapshot(
                store, self._target(store, "a-rep", "rev-rep"), rationale="r", **_GATE_KW
            )
            versions = dict(prepared.version_set)
            self.assertEqual(versions["context_kind"], "human_authored")
            self.assertEqual(versions["route"], "not_applicable")
            for slot in ("model", "prompt", "rubric", "parser", "adapter"):
                self.assertEqual(dict(versions[slot]), {"state": "not_applicable"})
                self.assertNotIn("ref", dict(versions[slot]))
            expected_input_digest = sha256_digest(
                canonical_json([entry.as_dict() for entry in prepared.entries])
            )
            self.assertEqual(versions["input_digest"], expected_input_digest)
            self.assertEqual(prepared.snapshot_schema_version, 1)
            self.assertEqual(prepared.storage_schema_version, store.schema_version)


# --------------------------------------------------------------------------- #
# Revalidation
# --------------------------------------------------------------------------- #
class RevalidationTest(GateSnapshotTestBase):
    def _prepared_report(self, store) -> PreparedGate:
        return prepare_gate_snapshot(
            store, self._target(store, "a-rep", "rev-rep"), rationale="r", **_GATE_KW
        )

    def test_revalidate_unchanged_returns_same_digest(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            prepared = self._prepared_report(store)
            result = revalidate_prepared(store, prepared)
            self.assertEqual(result.status, "unchanged")
            self.assertTrue(result.ok)
            self.assertEqual(result.recomputed_digest, prepared.digest)
            self.assertEqual(result.problems, ())
            result.raise_for_status()  # unchanged never raises

    def test_revalidate_after_target_edit_is_stale(self) -> None:
        with self.new_store() as store:
            repo = self._chain(store)
            prepared = self._prepared_report(store)
            self._import(repo, "a-rep", b"rep-v2", "rev-rep-2", parent_revision_id="rev-rep")
            result = revalidate_prepared(store, prepared)
            self.assertEqual(result.status, "stale")
            self.assertFalse(result.ok)
            self.assertTrue(result.problems)
            with self.assertRaises(PreparedGateStaleError):
                result.raise_for_status()

    def test_revalidate_after_live_dependency_edit_is_stale(self) -> None:
        with self.new_store() as store:
            repo = self._chain(store)
            prepared = self._prepared_report(store)
            # The finding is unchanged; its live input (the base document) moved.
            self._import(repo, "a-base", b"base-v2", "rev-base-2", parent_revision_id="rev-base")
            result = revalidate_prepared(store, prepared)
            self.assertEqual(result.status, "stale")

    def test_revalidate_after_rights_amendment_is_stale(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            prepared = self._prepared_report(store)
            self._insert_newer_rights(store, "RIGHTS-rev-base", 2)
            result = revalidate_prepared(store, prepared)
            self.assertEqual(result.status, "stale")

    def test_revalidate_after_content_corruption_is_invalid(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            prepared = self._prepared_report(store)
            self._corrupt_revision(store, "a-base", "rev-base", content=b"tampered")
            result = revalidate_prepared(store, prepared)
            self.assertEqual(result.status, "invalid")
            self.assertFalse(result.ok)
            with self.assertRaises(PreparedGateInvalidError):
                result.raise_for_status()

    def test_revalidate_after_rights_bytes_corruption_is_invalid(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            prepared = self._prepared_report(store)
            self._corrupt_rights_bytes(store, "RIGHTS-rev-base")
            result = revalidate_prepared(store, prepared)
            self.assertEqual(result.status, "invalid")

    def test_revalidate_cross_project_is_denied(self) -> None:
        with self.new_store("a") as store_a, self.new_store("b") as store_b:
            self._chain(store_a)
            prepared = self._prepared_report(store_a)
            result = revalidate_prepared(store_b, prepared)
            self.assertEqual(result.status, "denied")
            self.assertFalse(result.ok)
            with self.assertRaises(PreparedGateDeniedError):
                result.raise_for_status()

    def test_revalidate_rejects_non_prepared_object(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            prepared = self._prepared_report(store)
            for not_prepared in (
                {"project_id": store.project_id},
                prepared.digest,
                prepared.canonical_bytes,
                None,
                object(),
            ):
                with self.assertRaises(GateSnapshotError):
                    revalidate_prepared(store, not_prepared)

    def test_revalidate_writes_nothing(self) -> None:
        with self.new_store() as store:
            self._chain(store)
            prepared = self._prepared_report(store)
            before = _table_counts(store)
            revalidate_prepared(store, prepared)
            self._insert_newer_rights(store, "RIGHTS-rev-base", 2)
            revalidate_prepared(store, prepared)
            after = _table_counts(store)
            # The only DB change is the rights row this test inserted itself.
            self.assertEqual(
                {k: v for k, v in after.items() if k != "rights_record"},
                {k: v for k, v in before.items() if k != "rights_record"},
            )
            self.assertEqual(after["rights_record"], before["rights_record"] + 1)
            self.assertEqual(after.get("fixture_approval", 0), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
