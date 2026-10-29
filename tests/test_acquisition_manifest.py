"""Acquisition manifest + fail-closed human gate tests (Step 06, M1).

Covers ``src/review_article_agent/acquisition/manifest.py`` against
``docs/implementation/06-acquisition.md`` AC1/2/4/5/7 and specification workflow
step 6: a revision-specific manifest over the **exact** human-frozen roster whose
sources are independently re-validated from the stored bytes, whose gate is the
real ``artifact_revision``/``acquisition.complete`` owner flow, and which fails
closed on every missing / unapproved / stale / tampered state.

Every fixture is synthetic and uses a real disposable ``ProjectStore`` with the
real owner-capability approval flow (the tty seam is mocked as the freeze tests
do). No network, provider, or model call.

Run::

    PYTHONPATH=src python3 -m unittest tests.test_acquisition_manifest -v
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

import test_acquisition_validation as av  # noqa: E402
from test_discovery_freeze import FreezeTestBase  # noqa: E402

from review_article_agent.acquisition import manifest as manifest_mod  # noqa: E402
from review_article_agent.acquisition.manifest import (  # noqa: E402
    ACQUISITION_GATE_ID,
    ACQUISITION_GATE_KIND,
    DEFAULT_MANIFEST_ARTIFACT_ID,
    AcquisitionRepository,
    ManifestConflictError,
    ManifestCorruptionError,
    ManifestNotAcquiredError,
    ManifestValidationError,
)
from review_article_agent.storage import ArtifactRepository, ProjectStore, Registry, paths  # noqa: E402
from review_article_agent.storage.backup import (  # noqa: E402
    create_backup,
    restore_backup,
    verify_backup,
)

_HR = {
    "identity_confirmed": True,
    "full_text_readable": True,
    "complete_article": True,
    "checked_at": "2026-10-29T01:00:00+00:00",
    "provenance": "human:reviewer-1",
    "rationale": "opened the exact pinned bytes and read the article",
}


def _jats(pmid: str) -> bytes:
    return av.jats(pmid=pmid.encode(), pmcid=None, doi=None)


class ManifestBase(FreezeTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.acq = AcquisitionRepository(self.store)
        self._n = 0

    def _reopen(self) -> None:
        super()._reopen()
        self.acq = AcquisitionRepository(self.store)

    # -- fixtures ------------------------------------------------------------ #
    def setup_frozen(self, flagged=None):
        """Genuinely freeze the corpus; returns the frozen roster ``{pmid: id}``.

        ``flagged`` maps a pmid to integrity-status overrides for its freeze review.
        """
        corpus_ref, coverage_ref, _plan, snapshot = self.freeze_setup()
        overrides = {}
        for doc in snapshot["candidates"]:
            for ns, value in doc["tokens"]:
                if ns == "pmid" and flagged and value in flagged:
                    overrides[doc["candidate_id"]] = self.make_review(
                        doc["candidate_id"], statuses=flagged[value],
                        rationale="flag noted; historical use not banned",
                    )
        reviews = self.reviews_for(snapshot, overrides=overrides)
        rev = self.propose(corpus_ref, coverage_ref, snapshot, reviews=reviews, accept="accepted")
        self.approve_freeze(rev)
        self.freeze_ref = self.ref(rev.artifact_id, rev.revision_id)
        self.cands = {}
        for doc in snapshot["candidates"]:
            for ns, value in doc["tokens"]:
                if ns == "pmid":
                    self.cands[value] = doc["candidate_id"]
        self.snapshot = snapshot
        return self.cands

    def put_source(self, data, *, artifact_id=None, media_type=None, parent=None):
        artifact_id = artifact_id or f"src-{self._n}"
        self._n += 1
        rid = ArtifactRepository.new_revision_id()
        self.artifacts.create_artifact(artifact_id, kind="document")
        self.artifacts.import_revision(
            artifact_id=artifact_id,
            content=data,
            origin_locator="import:synthetic-source",
            rights=self.rights(artifact_id, rid),
            revision_id=rid,
            media_type=media_type,
            parent_revision_id=parent,
        )
        return self.ref(artifact_id, rid)

    def append_source(self, ref, data):
        return self.put_source(data, artifact_id=ref["artifact_id"], parent=ref["revision_id"])

    @staticmethod
    def entry(cid, disposition="use_full_text", *, source=None, human_review=None,
              assets=(), integrity=None, rationale="human disposition"):
        return {
            "candidate_id": cid,
            "disposition": disposition,
            "rationale": rationale,
            "source_ref": source,
            "human_review": human_review,
            "required_assets": list(assets),
            "integrity_review": integrity,
        }

    @staticmethod
    def asset(asset_id, kind="supplement", *, required=True, source=None, status="available",
              rationale="human checked asset"):
        return {
            "asset_id": asset_id, "kind": kind, "required": required,
            "source_ref": source, "status": status, "rationale": rationale,
        }

    def make(self, entries, *, request_id="man-1", artifact_id=DEFAULT_MANIFEST_ARTIFACT_ID,
             freeze_ref=None):
        rid = AcquisitionRepository.new_revision_id()
        return self.acq.propose(
            freeze_ref or self.freeze_ref,
            entries=entries,
            rights=self.rights(artifact_id, rid),
            revision_id=rid,
            request_id=request_id,
            artifact_id=artifact_id,
        )

    def gate(self, revision, *, decision="approved", gate_kind=ACQUISITION_GATE_KIND,
             gate_id=ACQUISITION_GATE_ID):
        return self.approve(revision.artifact_id, revision.revision_id, gate_kind, gate_id,
                            decision=decision)

    def ref_of(self, revision):
        return self.ref(revision.artifact_id, revision.revision_id)

    def standard(self, **kw):
        """A use_full_text JATS source for 111 plus an explicit exclusion for 222."""
        self.setup_frozen()
        a, b = self.cands["111"], self.cands["222"]
        src = self.put_source(_jats("111"))
        entries = [
            self.entry(a, source=src, **kw),
            self.entry(b, "exclude", rationale="out of scope after reading abstract"),
        ]
        return a, b, src, entries

    def forge(self, revision, mutate, *, drop_dependencies=False):
        data = json.loads(self.artifacts.read_content(revision.artifact_id, revision.revision_id))
        mutate(data)
        rid = ArtifactRepository.new_revision_id()
        deps = [] if drop_dependencies else list(
            self.artifacts.list_dependencies(revision.artifact_id, revision.revision_id)
        )
        return self.artifacts.import_revision(
            artifact_id=revision.artifact_id, content=data, origin_locator="import:forged",
            rights=self.rights(revision.artifact_id, rid), revision_id=rid,
            parent_revision_id=self.artifacts.current_revision(revision.artifact_id).revision_id,
            dependencies=deps,
        )


# --------------------------------------------------------------------------- #
# A. Success + gate authority
# --------------------------------------------------------------------------- #
class ManifestAuthorityTests(ManifestBase):
    def test_gated_jats_manifest_makes_acquisition_complete_and_survives_reopen(self):
        a, b, src, entries = self.standard()
        rev = self.make(entries)
        doc = self.acq.get(self.ref_of(rev))
        self.assertEqual(doc["payload"]["blockers"], [])
        self.assertFalse(doc["payload"]["authorizing"])
        self.assertIsNone(doc["approval"])
        with self.assertRaises(ManifestNotAcquiredError):
            self.acq.require_acquired()
        self.gate(rev)
        out = self.acq.require_acquired()
        self.assertTrue(out["acquired"])
        self.assertFalse(out["authorizes_scientific_processing"])
        self.assertEqual(out["acquired_candidate_ids"], [a])
        entry = next(e for e in out["payload"]["entries"] if e["candidate_id"] == a)
        self.assertEqual(entry["validation"]["status"], "ready")
        self.assertEqual(entry["expected_identifiers"], {"pmid": "111"})
        self.assertEqual(self.acq.queue(self.freeze_ref), [])
        deps = {(d.artifact_id, d.revision_id) for d in
                self.artifacts.list_dependencies(rev.artifact_id, rev.revision_id)}
        self.assertEqual(deps, {
            (self.freeze_ref["artifact_id"], self.freeze_ref["revision_id"]),
            (src["artifact_id"], src["revision_id"]),
        })
        self._reopen()
        again = self.acq.require_acquired()
        self.assertEqual(again["content_sha256"], out["content_sha256"])

    def test_unapproved_generic_rejected_wrong_kind_gates_do_not_acquire(self):
        _a, _b, _s, entries = self.standard()
        rev = self.make(entries)
        self.gate(rev, gate_id="tool.allowed")
        with self.assertRaises(ManifestNotAcquiredError):
            self.acq.require_acquired()
        self.gate(rev, gate_kind="pilot")
        with self.assertRaises(ManifestNotAcquiredError):
            self.acq.require_acquired()
        self.gate(rev, decision="rejected")
        with self.assertRaises(ManifestNotAcquiredError) as ctx:
            self.acq.require_acquired()
        self.assertIn("gate_not_active", ctx.exception.reasons)

    def test_gate_for_other_revision_is_not_authority_and_payload_cannot_mint_it(self):
        _a, _b, _s, entries = self.standard()
        first = self.make(entries, request_id="m1")
        self.gate(first)
        self.acq.require_acquired()
        second = self.make(entries, request_id="m2")
        # head moved to an ungated revision: the earlier gate never carries forward
        with self.assertRaises(ManifestNotAcquiredError):
            self.acq.require_acquired()
        bad = dict(entries[0], approved=True)
        with self.assertRaises(ManifestValidationError):
            self.make([bad, entries[1]], request_id="m3")

    def test_stale_gate_after_freeze_head_moves(self):
        _a, _b, _s, entries = self.standard()
        rev = self.make(entries)
        self.gate(rev)
        self.acq.require_acquired()
        # a newer freeze decision (search_again) supersedes the frozen roster
        corpus_ref = self.freeze.get(self.freeze_ref)["payload"]["corpus"]["ref"]
        cov_ref = self.freeze.get(self.freeze_ref)["payload"]["coverage"]["ref"]
        self.propose(corpus_ref, cov_ref, self.snapshot, request_id="freeze-2",
                     decision="search_again", rationale="search again")
        with self.assertRaises(ManifestNotAcquiredError):
            self.acq.require_acquired()


# --------------------------------------------------------------------------- #
# B. Exact roster and sources
# --------------------------------------------------------------------------- #
class ManifestRosterAndSourceTests(ManifestBase):
    def test_missing_extra_or_duplicate_candidate_is_refused(self):
        a, b, src, entries = self.standard()
        with self.assertRaises(ManifestValidationError):
            self.make(entries[:1], request_id="m-missing")
        unresolved = next(
            d["candidate_id"] for d in self.snapshot["candidates"] if not d["resolved"]
        )
        extra = self.entry(unresolved, "exclude", rationale="not frozen")
        with self.assertRaises(ManifestValidationError):
            self.make(entries + [extra], request_id="m-extra")
        with self.assertRaises(ManifestValidationError):
            self.make(entries + [entries[0]], request_id="m-dup")
        with self.assertRaises(ManifestValidationError):
            self.make(entries, request_id="m-nofreeze", freeze_ref=self.ref("nope", "nope"))

    def test_unapproved_freeze_cannot_be_manifest_base(self):
        corpus_ref, coverage_ref, _plan, snapshot = self.freeze_setup()
        rev = self.propose(corpus_ref, coverage_ref, snapshot, accept="accepted")
        with self.assertRaises(ManifestValidationError):
            self.make([], freeze_ref=self.ref_of(rev))

    def test_missing_source_is_persisted_blocker_and_never_acquires(self):
        self.setup_frozen()
        a, b = self.cands["111"], self.cands["222"]
        rev = self.make([self.entry(a), self.entry(b, "defer", rationale="later")])
        doc = self.acq.get(self.ref_of(rev))
        self.assertEqual([x["code"] for x in doc["payload"]["blockers"]], ["source_missing"])
        q = self.acq.queue(self.freeze_ref)
        self.assertEqual([(x["candidate_id"], x["category"]) for x in q], [(a, "missing")])
        self.gate(rev)  # a real gate over a blocked manifest still cannot acquire
        with self.assertRaises(ManifestNotAcquiredError) as ctx:
            self.acq.require_acquired()
        self.assertIn("blockers_present", ctx.exception.reasons)

    def test_queue_lists_frozen_candidates_without_manifest(self):
        self.setup_frozen()
        q = self.acq.queue(self.freeze_ref)
        self.assertEqual(sorted(x["candidate_id"] for x in q), sorted(self.cands.values()))
        self.assertTrue(all(x["code"] == "no_manifest_entry" for x in q))

    def test_wrong_paper_and_html_cannot_be_human_waived(self):
        self.setup_frozen()
        a, b = self.cands["111"], self.cands["222"]
        wrong = self.put_source(_jats("999"))
        html = self.put_source(av.LOGIN_HTML, media_type="application/pdf")
        rev = self.make([
            self.entry(a, source=wrong, human_review=_HR),
            self.entry(b, source=html, human_review=_HR),
        ])
        doc = self.acq.get(self.ref_of(rev))
        self.assertEqual(
            [(x["candidate_id"], x["code"]) for x in doc["payload"]["blockers"]],
            sorted([(a, "source_rejected"), (b, "source_rejected")]),
        )
        self.gate(rev)
        with self.assertRaises(ManifestNotAcquiredError):
            self.acq.require_acquired()

    def test_pdf_needs_exact_human_review(self):
        self.setup_frozen()
        a, b = self.cands["111"], self.cands["222"]
        pdf = self.put_source(av.GOOD_PDF)
        without = self.make([self.entry(a, source=pdf), self.entry(b, "exclude")], request_id="no-hr")
        self.assertEqual(
            [x["code"] for x in self.acq.get(self.ref_of(without))["payload"]["blockers"]],
            ["human_review_required"],
        )
        reviewed = self.make(
            [self.entry(a, source=pdf, human_review=_HR), self.entry(b, "exclude")],
            request_id="with-hr",
        )
        doc = self.acq.get(self.ref_of(reviewed))
        self.assertEqual(doc["payload"]["blockers"], [])
        ent = next(e for e in doc["payload"]["entries"] if e["candidate_id"] == a)
        self.assertEqual(ent["human_review_binding"]["source_ref"], pdf)
        self.assertEqual(ent["human_review_binding"]["content_sha256"], ent["source_pin"]["content_sha256"])
        self.gate(reviewed)
        self.assertTrue(self.acq.require_acquired()["acquired"])

    def test_human_review_must_be_structured_and_affirmative(self):
        self.setup_frozen()
        a, b = self.cands["111"], self.cands["222"]
        pdf = self.put_source(av.GOOD_PDF)
        for bad in (
            dict(_HR, identity_confirmed=False),
            dict(_HR, provenance=""),
            {k: v for k, v in _HR.items() if k != "rationale"},
            dict(_HR, checked_at="yesterday"),
        ):
            with self.assertRaises(ManifestValidationError):
                self.make([self.entry(a, source=pdf, human_review=bad), self.entry(b, "exclude")],
                          request_id="bad-hr")

    def test_source_must_be_current_same_project_document(self):
        self.setup_frozen()
        a, b = self.cands["111"], self.cands["222"]
        old = self.put_source(_jats("111"), artifact_id="src-old")
        self.append_source(old, _jats("111"))
        with self.assertRaises(ManifestValidationError):
            self.make([self.entry(a, source=old), self.entry(b, "exclude")])
        with self.assertRaises(ManifestValidationError):
            self.make([self.entry(a, source=self.ref("absent", "absent")), self.entry(b, "exclude")],
                      request_id="m-absent")
        with self.assertRaises(ManifestValidationError):
            self.make([self.entry(a, source={"project_id": "other", "artifact_id": "x", "revision_id": "y"}),
                       self.entry(b, "exclude")], request_id="m-xproj")
        # a non-document artifact (e.g. the freeze) is not a source
        with self.assertRaises(ManifestValidationError):
            self.make([self.entry(a, source=self.freeze_ref), self.entry(b, "exclude")],
                      request_id="m-kind")


# --------------------------------------------------------------------------- #
# C. Assets
# --------------------------------------------------------------------------- #
class ManifestAssetTests(ManifestBase):
    def test_invisible_required_supplement_queues_and_blocks_but_available_passes(self):
        self.setup_frozen()
        a, b = self.cands["111"], self.cands["222"]
        src = self.put_source(_jats("111"))
        for status, code in (("unknown", "required_asset_unknown"),
                             ("unavailable", "required_asset_unavailable")):
            rev = self.make(
                [self.entry(a, source=src, assets=[self.asset("s1", "supplement", status=status)]),
                 self.entry(b, "exclude")],
                request_id=f"m-{status}",
            )
            blockers = self.acq.get(self.ref_of(rev))["payload"]["blockers"]
            self.assertEqual([(x["category"], x["code"], x["asset_id"]) for x in blockers],
                             [("supplement", code, "s1")])
            self.gate(rev)
            with self.assertRaises(ManifestNotAcquiredError):
                self.acq.require_acquired()
        self.assertEqual([x["category"] for x in self.acq.queue(self.freeze_ref)], ["supplement"])
        # available without bytes is still a blocker; with bytes it passes
        nobytes = self.make(
            [self.entry(a, source=src, assets=[self.asset("f1", "figure")]), self.entry(b, "exclude")],
            request_id="m-nobytes",
        )
        self.assertEqual(
            [(x["category"], x["code"]) for x in self.acq.get(self.ref_of(nobytes))["payload"]["blockers"]],
            [("visual", "required_asset_bytes_missing")],
        )
        fig = self.put_source(b"\x89PNG synthetic figure bytes")
        ok = self.make(
            [self.entry(a, source=src, assets=[self.asset("f1", "figure", source=fig),
                                               self.asset("t9", "table", required=False, status="unknown")]),
             self.entry(b, "exclude")],
            request_id="m-ok",
        )
        self.assertEqual(self.acq.get(self.ref_of(ok))["payload"]["blockers"], [])
        self.gate(ok)
        self.acq.require_acquired()

    def test_contradictory_or_duplicate_assets_are_refused(self):
        self.setup_frozen()
        a, b = self.cands["111"], self.cands["222"]
        src = self.put_source(_jats("111"))
        fig = self.put_source(b"fig")
        for assets in (
            [self.asset("x", source=fig, status="unavailable")],
            [self.asset("x"), self.asset("x")],
            [self.asset("x", rationale="")],
        ):
            with self.assertRaises(ManifestValidationError):
                self.make([self.entry(a, source=src, assets=assets), self.entry(b, "exclude")],
                          request_id="bad-assets")


# --------------------------------------------------------------------------- #
# D. Dispositions and integrity (POL-P3)
# --------------------------------------------------------------------------- #
class ManifestDispositionIntegrityTests(ManifestBase):
    def test_explicit_nonuse_dispositions_need_only_rationale(self):
        self.setup_frozen()
        a, b = self.cands["111"], self.cands["222"]
        rev = self.make([self.entry(a, "no_scientific_use", rationale="background only"),
                         self.entry(b, "defer", rationale="paywalled, revisit")])
        self.assertEqual(self.acq.get(self.ref_of(rev))["payload"]["blockers"], [])
        self.gate(rev)
        out = self.acq.require_acquired()
        self.assertEqual(out["acquired_candidate_ids"], [])
        for bad in (
            self.entry(a, "exclude", rationale=""),
            self.entry(a, "exclude", source=self.put_source(_jats("111"))),
            self.entry(a, "exclude", human_review=_HR),
            self.entry(a, "exclude", assets=[self.asset("x")]),
        ):
            with self.assertRaises(ManifestValidationError):
                self.make([bad, self.entry(b, "exclude")], request_id="bad-nonuse")

    def test_unknown_integrity_blocks_use_and_is_never_clean(self):
        a, b, src, _e = self.standard()
        unknown = {
            "checked_at": "2026-10-30T00:00:00+00:00",
            "provenance": {"ref": "integrity:syn2", "source": "retraction-watch"},
            "retraction": "none", "expression_of_concern": "unknown",
            "correction": "none", "non_replication": "none",
            "rationale": "could not check concern status",
        }
        rev = self.make([self.entry(a, source=src, integrity=unknown),
                         self.entry(b, "exclude")])
        doc = self.acq.get(self.ref_of(rev))
        self.assertEqual([(x["category"], x["code"]) for x in doc["payload"]["blockers"]],
                         [("integrity", "integrity_unknown")])
        self.assertEqual(next(e for e in doc["payload"]["entries"] if e["candidate_id"] == a)["effective_integrity"]["basis"], "manifest_review")
        self.gate(rev)
        with self.assertRaises(ManifestNotAcquiredError):
            self.acq.require_acquired()
        # a manifest review dated before the frozen review cannot override it
        old = dict(unknown, expression_of_concern="none", checked_at="2020-01-01T00:00:00+00:00")
        with self.assertRaises(ManifestValidationError):
            self.make([self.entry(a, source=src, integrity=old), self.entry(b, "exclude")],
                      request_id="m-old")

    def test_flagged_status_needs_explicit_per_use_rationale_not_a_ban(self):
        self.setup_frozen(flagged={"111": {"correction": "flagged"}})
        a, b = self.cands["111"], self.cands["222"]
        src = self.put_source(_jats("111"))
        bare = self.make([self.entry(a, source=src), self.entry(b, "exclude")], request_id="bare")
        self.assertEqual([x["code"] for x in self.acq.get(self.ref_of(bare))["payload"]["blockers"]],
                         ["integrity_use_rationale_required"])
        use = {
            "checked_at": "2026-10-30T00:00:00+00:00",
            "provenance": {"ref": "integrity:syn3", "source": "journal-notice"},
            "retraction": "none", "expression_of_concern": "none",
            "correction": "flagged", "non_replication": "none",
            "rationale": "erratum affects an unrelated table; this use does not cite it",
        }
        ok = self.make([self.entry(a, source=src, integrity=use), self.entry(b, "exclude")],
                       request_id="use")
        self.assertEqual(self.acq.get(self.ref_of(ok))["payload"]["blockers"], [])
        self.gate(ok)
        self.assertTrue(self.acq.require_acquired()["acquired"])


# --------------------------------------------------------------------------- #
# E. Source append invalidates only the affected manifest
# --------------------------------------------------------------------------- #
class ManifestSupersessionTests(ManifestBase):
    def test_new_source_revision_stales_only_affected_manifest(self):
        self.setup_frozen()
        a, b = self.cands["111"], self.cands["222"]
        src1 = self.put_source(_jats("111"), artifact_id="src-one")
        src2 = self.put_source(_jats("111"), artifact_id="src-two")
        m1 = self.make([self.entry(a, source=src1), self.entry(b, "exclude")],
                       request_id="m1", artifact_id="acq.one")
        m2 = self.make([self.entry(a, source=src2), self.entry(b, "exclude")],
                       request_id="m2", artifact_id="acq.two")
        self.gate(m1)
        self.gate(m2)
        self.acq.require_acquired("acq.one")
        self.acq.require_acquired("acq.two")
        self.append_source(src1, _jats("111"))
        with self.assertRaises(ManifestNotAcquiredError):
            self.acq.require_acquired("acq.one")
        self.acq.require_acquired("acq.two")
        # earlier work is retained and still reads
        self.assertEqual(self.acq.get(self.ref_of(m1))["payload"]["blockers"], [])
        self.assertEqual(self.acq.queue(self.freeze_ref, artifact_id="acq.one")[0]["code"],
                         "source_superseded")

    def test_write_race_is_vetoed_inside_the_import_transaction(self):
        a, b, src, entries = self.standard()
        real = self.acq._artifacts.import_revision
        moved = []

        def racing_import(**kwargs):
            moved.append(self.append_source(src, _jats("111")))  # another writer wins first
            return real(**kwargs)

        self.acq._artifacts.import_revision = racing_import
        try:
            with self.assertRaises(ManifestValidationError):
                self.make(entries)
        finally:
            self.acq._artifacts.import_revision = real
        self.assertEqual(self.acq.history(), [])


# --------------------------------------------------------------------------- #
# F. Tamper / forged records
# --------------------------------------------------------------------------- #
class ManifestTamperTests(ManifestBase):
    def test_tampered_payload_and_forged_dependencies_fail_closed(self):
        a, b, src, entries = self.standard()
        rev = self.make(entries)
        self.gate(rev)
        self.acq.require_acquired()

        def flip(data):
            for e in data["payload"]["entries"]:
                if e["validation"] is not None:
                    e["validation"]["status"] = "needs_review"

        forged = self.forge(rev, flip)
        self.gate(forged)
        with self.assertRaises(ManifestCorruptionError):
            self.acq.get(self.ref_of(forged))
        with self.assertRaises(ManifestNotAcquiredError):
            self.acq.require_acquired()

        # forged missing-dependency bypass: payload identical, closure dropped
        nodeps = self.forge(rev, lambda d: None, drop_dependencies=True)
        self.gate(nodeps)
        with self.assertRaises(ManifestCorruptionError):
            self.acq.get(self.ref_of(nodeps))
        with self.assertRaises(ManifestNotAcquiredError):
            self.acq.require_acquired()

        # a forged blocker-free claim over a missing source
        def drop_source(data):
            for e in data["payload"]["entries"]:
                if e["disposition"] == "use_full_text":
                    e["source_ref"] = None
            data["payload"]["blockers"] = []

        bypass = self.forge(rev, drop_source)
        with self.assertRaises(ManifestCorruptionError):
            self.acq.get(self.ref_of(bypass))

    def test_source_byte_tamper_is_detected_on_read_and_require(self):
        a, b, src, entries = self.standard()
        rev = self.make(entries)
        self.gate(rev)
        self.acq.require_acquired()
        # Test-only DB tamper: drop the immutability trigger on the throwaway store.
        conn = self.store.connection
        conn.execute("DROP TRIGGER revision_no_update")
        conn.execute(
            "UPDATE revision SET content = ? WHERE artifact_id = ? AND revision_id = ?",
            (av.LOGIN_HTML, src["artifact_id"], src["revision_id"]),
        )
        conn.commit()
        with self.assertRaises(ManifestCorruptionError):
            self.acq.get(self.ref_of(rev))
        with self.assertRaises(ManifestNotAcquiredError):
            self.acq.require_acquired()


# --------------------------------------------------------------------------- #
# G. Replay / restart / backup
# --------------------------------------------------------------------------- #
class ManifestReplayBackupTests(ManifestBase):
    def test_exact_replay_is_historical_and_divergent_replay_conflicts(self):
        a, b, src, entries = self.standard()
        first = self.make(entries, request_id="same")
        again = self.make(entries, request_id="same")
        self.assertEqual(first.revision_id, again.revision_id)
        self.assertEqual(len(self.acq.history()), 1)
        diverged = [self.entry(a, "exclude", rationale="changed my mind"), entries[1]]
        with self.assertRaises(ManifestConflictError):
            self.make(diverged, request_id="same")
        # replay stays historical after a source supersession and a restart
        self.append_source(src, _jats("111"))
        self._reopen()
        replay = self.make(entries, request_id="same")
        self.assertEqual(replay.revision_id, first.revision_id)
        # ...but a genuinely new proposal over the stale source is refused
        with self.assertRaises(ManifestValidationError):
            self.make(entries, request_id="fresh")

    def test_authentic_manifest_survives_private_backup_and_restore(self):
        a, b, src, entries = self.standard()
        rev = self.make(entries)
        self.gate(rev)
        before = self.acq.require_acquired()["content_sha256"]
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
        out = AcquisitionRepository(restored).require_acquired()
        self.assertEqual(out["content_sha256"], before)
        self.assertEqual(out["acquired_candidate_ids"], [a])

    def test_module_constants_and_notice_are_public(self):
        self.assertEqual((ACQUISITION_GATE_KIND, ACQUISITION_GATE_ID),
                         ("artifact_revision", "acquisition.complete"))
        self.assertEqual(manifest_mod.DOCUMENT_TYPE, "acquisition_manifest")
        self.assertIn("authorizes no screening", manifest_mod.MANIFEST_NOTICE)


if __name__ == "__main__":
    unittest.main()
