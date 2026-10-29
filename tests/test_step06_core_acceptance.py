"""Step 06 core acceptance: AC1..AC7 over the integrated import -> manifest -> gate chain.

Every test composes the *actual* chain on a real disposable ``ProjectStore``:

    human-frozen roster (real freeze gate)
      -> ``AssetImportRepository.import_asset`` / ``record_missing`` (occurrence + exact source)
      -> ``AcquisitionRepository.propose`` over those exact source refs
      -> real named ``artifact_revision`` / ``acquisition.complete`` human gate
         (``prepare_gate_snapshot`` + genuine owner capability; only the tty seam is mocked)
      -> ``require_acquired`` -> restart / private backup / restore.

Fixture builders come from ``test_acquisition_manifest`` / ``test_discovery_freeze``
(whose tests are *not* re-run here). Synthetic self-authored bytes only; no live,
paid, or full-text network request.

Execution runner / atomic batch / worker tasks are not integrated and not covered.

Run::

    PYTHONPATH=src python3 -m unittest tests.test_step06_core_acceptance -v
"""

from __future__ import annotations

import socket
import sys
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

import test_acquisition_imports as ti  # noqa: E402
import test_acquisition_resolution as rs  # noqa: E402
import test_acquisition_validation as av  # noqa: E402
from test_acquisition_manifest import _HR, ManifestBase  # noqa: E402

from review_article_agent.acquisition import resolution  # noqa: E402
from review_article_agent.acquisition.impact import source_impact  # noqa: E402
from review_article_agent.acquisition.imports import (  # noqa: E402
    AssetImportError,
    AssetImportRepository,
)
from review_article_agent.acquisition.manifest import (  # noqa: E402
    AcquisitionRepository,
    ManifestNotAcquiredError,
    ManifestValidationError,
)
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


def _jats(pmid: str, body: bytes = b"<sec><p>Hello scientific world.</p></sec>") -> bytes:
    return av.jats(pmid=pmid.encode(), pmcid=None, doi=None, body=body)


class CoreBase(ManifestBase):
    """ManifestBase plus a real ``AssetImportRepository`` import front door."""

    def setUp(self) -> None:
        super().setUp()
        self.imports = AssetImportRepository(self.artifacts)
        self._req = 0

    def _reopen(self) -> None:
        super()._reopen()
        self.imports = AssetImportRepository(self.artifacts)

    def count(self, table: str) -> int:
        return self.store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def _occ_args(self, request_id: str) -> dict:
        report = self.imports.occurrence_artifact_id(request_id)
        rev = ArtifactRepository.new_revision_id()
        return {
            "request_id": request_id,
            "occurrence_revision_id": rev,
            "occurrence_rights": ti.rights(
                self.project_id, report, rev, rights_id=f"OCC-{request_id}"
            ),
        }

    def import_source(self, data, *, artifact, origin="file:///inbox/paper.bin", parent=None,
                      ops=None, deps=(), request_id=None):
        """Real import; returns the import result dict (source_ref + occurrence_ref)."""
        self._req += 1
        request_id = request_id or f"core-req-{self._req}"
        rev = ArtifactRepository.new_revision_id()
        return self.imports.import_asset(
            artifact_id=artifact,
            content=data,
            origin_locator=origin,
            rights=ti.rights(self.project_id, artifact, rev, rights_id=f"R-{rev}", ops=ops or {}),
            revision_id=rev,
            parent_revision_id=parent,
            dependencies=list(deps),
            **self._occ_args(request_id),
        )

    def record_missing(self, artifact, *, request_id=None):
        self._req += 1
        request_id = request_id or f"core-miss-{self._req}"
        return self.imports.record_missing(
            artifact_id=artifact,
            origin_locator="manual:publisher-request-pending",
            rights=ti.missing_rights(self.project_id, artifact, rights_id=f"MISS-{request_id}"),
            reason="paper not obtainable through authorized routes yet",
            **self._occ_args(request_id),
        )

    def blockers(self, revision):
        return self.acq.get(self.ref_of(revision))["payload"]["blockers"]

    def assert_not_acquired(self, artifact_id=None, code=None):
        args = (artifact_id,) if artifact_id else ()
        with self.assertRaises(ManifestNotAcquiredError) as ctx:
            self.acq.require_acquired(*args)
        if code is not None:
            self.assertIn(code, ctx.exception.reasons)
        return ctx.exception


# --------------------------------------------------------------------------- #
# AC1 - a frozen candidate without full text or a disposition blocks the revision
# --------------------------------------------------------------------------- #
class Ac1FrozenCandidateTests(CoreBase):
    def test_ac1_missing_candidate_or_source_blocks_even_with_generic_approval(self):
        self.setup_frozen()
        a, b = self.cands["111"], self.cands["222"]
        # no manifest at all: the whole frozen roster is queued and nothing is acquired
        self.assert_not_acquired()
        self.assertTrue(all(q["code"] == "no_manifest_entry" for q in self.acq.queue(self.freeze_ref)))
        # a manifest that skips a frozen candidate cannot even be stored
        with self.assertRaises(ManifestValidationError):
            self.make([self.entry(b, "exclude")], request_id="skip-a")
        # a recorded "no full text" event holds no bytes and no source revision
        miss = self.record_missing("paper-111")
        self.assertEqual(miss["disposition"], "no_full_text_acquired")
        self.assertIsNone(miss["source_ref"])
        self.assertEqual(self.artifacts.list_revisions("paper-111"), [])
        # use_full_text with no source -> persisted blocker; generic AND named gates both fail
        rev = self.make([self.entry(a), self.entry(b, "defer", rationale="revisit later")])
        self.assertEqual([x["code"] for x in self.blockers(rev)], ["source_missing"])
        self.gate(rev, gate_id="tool.allowed")  # generic approval is not the named gate
        self.assert_not_acquired()
        self.gate(rev)  # genuine named gate over a blocked manifest still cannot acquire
        self.assert_not_acquired(code="blockers_present")
        self.assertEqual([(q["candidate_id"], q["category"]) for q in self.acq.queue(self.freeze_ref)],
                         [(a, "missing")])

    def test_ac1_explicit_disposition_is_not_scientific_authorization(self):
        self.setup_frozen()
        a, b = self.cands["111"], self.cands["222"]
        rev = self.make([self.entry(a, "no_scientific_use", rationale="background only"),
                         self.entry(b, "defer", rationale="paywalled, revisit")])
        self.gate(rev)
        out = self.acq.require_acquired()
        self.assertEqual(out["acquired_candidate_ids"], [])
        self.assertFalse(out["authorizes_scientific_processing"])

    def test_ac1_jats_import_to_named_gate_acquires_but_never_authorizes_processing(self):
        self.setup_frozen()
        a, b = self.cands["111"], self.cands["222"]
        res = self.import_source(_jats("111"), artifact="paper-111", origin="file:///inbox/111.xml")
        occ = self.imports.get_occurrence(res["occurrence_ref"])
        self.assertTrue(occ.proposal_only)
        self.assertFalse(occ.authorizes_use)
        self.assertEqual(occ.source_ref.as_dict(), res["source_ref"])
        rev = self.make([self.entry(a, source=res["source_ref"]), self.entry(b, "exclude")])
        self.assertEqual(self.blockers(rev), [])
        self.assert_not_acquired()  # proposal alone, before any gate
        self.gate(rev)
        out = self.acq.require_acquired()
        self.assertTrue(out["acquired"])
        self.assertFalse(out["authorizes_scientific_processing"])
        self.assertEqual(out["acquired_candidate_ids"], [a])
        entry = next(e for e in out["payload"]["entries"] if e["candidate_id"] == a)
        self.assertEqual(entry["source_ref"], res["source_ref"])
        self.assertEqual(entry["validation"]["status"], "ready")


# --------------------------------------------------------------------------- #
# AC2 - extension alone never establishes success
# --------------------------------------------------------------------------- #
class Ac2ExtensionNeverSuccessTests(CoreBase):
    def test_ac2_html_wrong_paper_and_corrupt_sources_cannot_pass_even_with_human_review(self):
        self.setup_frozen()
        a, b = self.cands["111"], self.cands["222"]
        truncated_pdf = av.GOOD_PDF.split(b"startxref")[0]  # no EOF marker
        cases = {
            "login-html": (av.LOGIN_HTML, "file:///inbox/paper.pdf"),
            "plain-html": (av.PLAIN_HTML, "file:///inbox/paper.pdf"),
            "wrong-paper": (_jats("999"), "file:///inbox/paper.xml"),
            "corrupt-pdf": (truncated_pdf, "file:///inbox/paper.pdf"),
        }
        for name, (data, origin) in cases.items():
            res = self.import_source(data, artifact=f"paper-{name}", origin=origin)
            rev = self.make(
                [self.entry(a, source=res["source_ref"], human_review=_HR),
                 self.entry(b, "exclude")],
                request_id=f"m-{name}",
            )
            self.assertEqual([(x["candidate_id"], x["code"]) for x in self.blockers(rev)],
                             [(a, "source_rejected")], name)
            self.gate(rev)
            self.assert_not_acquired(code="blockers_present")

    def test_ac2_pdf_variants_need_exact_source_human_review_never_automatic(self):
        self.setup_frozen()
        a, b = self.cands["111"], self.cands["222"]
        scan = av.GOOD_PDF.replace(
            b"<< /Type /Pages /Kids [] /Count 0 >>",
            b"<< /Type /Pages /Kids [] /Count 0 /XObject <</Im1 3 0 R>> /Subtype /Image >>",
        )
        for name, data in (("good", av.GOOD_PDF), ("scan", scan)):
            res = self.import_source(data, artifact=f"pdf-{name}", origin="file:///inbox/p.pdf")
            bare = self.make([self.entry(a, source=res["source_ref"]), self.entry(b, "exclude")],
                             request_id=f"bare-{name}")
            self.assertEqual([x["code"] for x in self.blockers(bare)], ["human_review_required"], name)
            self.gate(bare)
            self.assert_not_acquired()
            # a review that does not confirm identity / readable / complete is refused outright
            for flag in ("identity_confirmed", "full_text_readable", "complete_article"):
                with self.assertRaises(ManifestValidationError, msg=f"{name}:{flag}"):
                    self.make([self.entry(a, source=res["source_ref"], human_review=dict(_HR, **{flag: False})),
                               self.entry(b, "exclude")], request_id=f"nohr-{name}-{flag}")
            reviewed = self.make(
                [self.entry(a, source=res["source_ref"], human_review=_HR), self.entry(b, "exclude")],
                request_id=f"hr-{name}",
            )
            doc = self.acq.get(self.ref_of(reviewed))
            ent = next(e for e in doc["payload"]["entries"] if e["candidate_id"] == a)
            self.assertEqual(ent["validation"]["status"], "needs_review")
            # the human review is bound to the exact source revision and bytes
            self.assertEqual(ent["human_review_binding"]["source_ref"], res["source_ref"])
            self.assertEqual(ent["human_review_binding"]["content_sha256"],
                             ent["source_pin"]["content_sha256"])
            self.gate(reviewed)
            self.assertFalse(self.acq.require_acquired()["authorizes_scientific_processing"])


# --------------------------------------------------------------------------- #
# AC3 - duplicates, versions, shared studies; no fuzzy merge
# --------------------------------------------------------------------------- #
class Ac3DuplicateVersionTests(CoreBase):
    def test_ac3_exact_duplicate_other_path_reuses_source_with_separate_occurrence(self):
        self.setup_frozen()
        a, b = self.cands["111"], self.cands["222"]
        data = _jats("111")
        first = self.import_source(data, artifact="paper-111", origin="file:///inbox/a/111.xml")
        dup = self.import_source(data, artifact="paper-111", origin="file:///elsewhere/copy.xml")
        self.assertEqual(first["disposition"], "created_new_revision")
        self.assertEqual(dup["disposition"], "duplicate_bytes_of_existing_revision")
        self.assertEqual(dup["source_ref"], first["source_ref"])
        self.assertNotEqual(dup["occurrence_ref"], first["occurrence_ref"])
        self.assertEqual(len(self.artifacts.list_revisions("paper-111")), 1)
        self.assertEqual(
            self.imports.get_occurrence(dup["occurrence_ref"]).origin_locator,
            "file:///elsewhere/copy.xml",
        )
        # the manifest consumes the one reused source
        rev = self.make([self.entry(a, source=dup["source_ref"]), self.entry(b, "exclude")])
        self.gate(rev)
        self.assertEqual(self.acq.require_acquired()["acquired_candidate_ids"], [a])
        # C01 6.1 R1: identical bytes under a DIFFERENT artifact id must not
        # create a second independent source revision; the import is vetoed and
        # the caller is shown the existing canonical bytes to reconcile.
        with self.assertRaises(AssetImportError) as caught:
            self.import_source(data, artifact="paper-other", origin="file:///inbox/other.xml")
        self.assertEqual(caught.exception.code, "cross_artifact_duplicate")
        self.assertEqual(
            [r.artifact_id for r in caught.exception.matching_refs], ["paper-111"]
        )
        self.assertIsNone(self.artifacts.get_artifact("paper-other"))
        self.assertEqual(len(self.artifacts.list_revisions("paper-111")), 1)

    def test_ac3_alternate_versions_stay_distinct_and_shared_study_links_are_retained(self):
        self.setup_frozen()
        a, b = self.cands["111"], self.cands["222"]
        v1 = self.import_source(_jats("111"), artifact="paper-111")
        v2 = self.import_source(
            _jats("111", body=b"<sec><p>Corrected version text.</p></sec>"),
            artifact="paper-111", parent=v1["source_ref"]["revision_id"],
        )
        self.assertEqual(v2["disposition"], "created_new_revision")
        self.assertNotEqual(v2["source_ref"], v1["source_ref"])
        self.assertEqual([r.revision_id for r in self.artifacts.list_revisions("paper-111")],
                         [v1["source_ref"]["revision_id"], v2["source_ref"]["revision_id"]])
        # two reports of one study are linked through an explicit study edge, not merged
        self.artifacts.create_artifact("study-1", kind="study")
        study_rev = ArtifactRepository.new_revision_id()
        self.artifacts.import_revision(
            artifact_id="study-1", content=b"study metadata", origin_locator="note:study",
            rights=self.rights("study-1", study_rev), revision_id=study_rev,
        )
        link = dict(self.ref("study-1", study_rev), role="reports")
        companion = self.import_source(_jats("222"), artifact="paper-222", deps=[link])
        roles = {(d.artifact_id, d.revision_id, d.role) for d in
                 self.artifacts.list_dependencies(companion["source_ref"]["artifact_id"],
                                                  companion["source_ref"]["revision_id"])}
        self.assertEqual(roles, {("study-1", study_rev, "reports")})
        sibling = self.import_source(_jats("111", body=b"<sec><p>Preprint wording.</p></sec>"),
                                     artifact="paper-111-preprint", deps=[link])
        reporters = {(e["artifact_id"], e["revision_id"]) for e in
                     self.artifacts.dependents("study-1", study_rev) if e["role"] == "reports"}
        self.assertEqual(reporters, {
            (companion["source_ref"]["artifact_id"], companion["source_ref"]["revision_id"]),
            (sibling["source_ref"]["artifact_id"], sibling["source_ref"]["revision_id"]),
        })
        lineage = {(d.revision_id, d.role) for d in
                   self.artifacts.list_dependencies("paper-111", v2["source_ref"]["revision_id"])}
        self.assertEqual(lineage, {(v1["source_ref"]["revision_id"], "version_of")})
        self.assertNotEqual(companion["source_ref"]["artifact_id"], v2["source_ref"]["artifact_id"])
        # both reports remain separate acquisition sources for the two frozen candidates
        rev = self.make([self.entry(a, source=v2["source_ref"]),
                         self.entry(b, source=companion["source_ref"])])
        self.assertEqual(self.blockers(rev), [])
        self.gate(rev)
        self.assertEqual(self.acq.require_acquired()["acquired_candidate_ids"], sorted([a, b]))
        # the superseded version is not an acceptable manifest source
        with self.assertRaises(ManifestValidationError):
            self.make([self.entry(a, source=v1["source_ref"]), self.entry(b, "exclude")],
                      request_id="old-version")


# --------------------------------------------------------------------------- #
# AC4 - imports do not bypass screening; source append stales only dependents
# --------------------------------------------------------------------------- #
class Ac4ImportsAndImpactTests(CoreBase):
    def test_ac4_imports_never_seed_screen_or_approve_and_cannot_extend_the_roster(self):
        self.setup_frozen()
        a, b = self.cands["111"], self.cands["222"]
        corpus_art = self.freeze.get(self.freeze_ref)["payload"]["corpus"]["ref"]["artifact_id"]
        corpus_head = self.artifacts.current_revision(corpus_art).revision_id
        approvals, kinds = self.count("human_approval"), {x.artifact_id: x.kind for x in self.artifacts.list_artifacts()}
        res = self.import_source(_jats("111"), artifact="paper-111")
        extra = self.import_source(_jats("333"), artifact="paper-333")  # a paper nobody froze
        self.record_missing("paper-444")
        self.assertEqual(self.count("human_approval"), approvals)
        self.assertEqual(self.artifacts.current_revision(corpus_art).revision_id, corpus_head)
        new_kinds = {x.kind for x in self.artifacts.list_artifacts() if x.artifact_id not in kinds}
        self.assertLessEqual(new_kinds, {"document", "report"})
        for ref in (res["occurrence_ref"], extra["occurrence_ref"]):
            occ = self.imports.get_occurrence(ref)
            self.assertTrue(occ.proposal_only)
            self.assertFalse(occ.authorizes_use)
        # an imported but never-frozen paper cannot enter the manifest roster
        unresolved = next(d["candidate_id"] for d in self.snapshot["candidates"] if not d["resolved"])
        with self.assertRaises(ManifestValidationError):
            self.make([self.entry(a, source=res["source_ref"]), self.entry(b, "exclude"),
                       self.entry(unresolved, source=extra["source_ref"])], request_id="extra")

    def test_ac4_source_append_stales_only_dependent_manifest_and_unrelated_gate_stays_active(self):
        self.setup_frozen()
        a, b = self.cands["111"], self.cands["222"]
        s1 = self.import_source(_jats("111"), artifact="paper-one")
        s2 = self.import_source(_jats("222"), artifact="paper-two")
        m1 = self.make([self.entry(a, source=s1["source_ref"]), self.entry(b, "exclude")],
                       request_id="m1", artifact_id="acq.one")
        m2 = self.make([self.entry(b, source=s2["source_ref"]), self.entry(a, "exclude")],
                       request_id="m2", artifact_id="acq.two")
        self.gate(m1)
        self.gate(m2)
        self.acq.require_acquired("acq.one")
        self.acq.require_acquired("acq.two")
        appended = self.import_source(
            _jats("111", body=b"<sec><p>New release of the same report.</p></sec>"),
            artifact="paper-one", parent=s1["source_ref"]["revision_id"],
        )
        self.assert_not_acquired("acq.one")
        self.acq.require_acquired("acq.two")  # unrelated, genuinely gated manifest stays active
        report = source_impact(self.store, appended["source_ref"])
        self.assertTrue(report["claims_change"])
        self.assertFalse(report["authorizing"])
        affected = {e["ref"]["artifact_id"] for e in report["affected"]}
        self.assertIn("acq.one", affected)
        self.assertNotIn("acq.two", affected)
        self.assertNotIn(self.freeze_ref["artifact_id"], affected)
        self.assertIn(self.ref_of(m2), [self.ref(r["artifact_id"], r["revision_id"])
                                        for r in report["unaffected"]])
        # earlier work is retained and still reads
        self.assertEqual(self.blockers(m1), [])
        self.assertTrue(report["historical_work_retained"])


# --------------------------------------------------------------------------- #
# AC5 - necessary supplements/figures are visible; integrity unknown is not clean
# --------------------------------------------------------------------------- #
class Ac5SupplementIntegrityTests(CoreBase):
    def test_ac5_unavailable_unknown_or_login_supplement_is_visible_and_blocks(self):
        self.setup_frozen()
        a, b = self.cands["111"], self.cands["222"]
        src = self.import_source(_jats("111"), artifact="paper-111")["source_ref"]
        login = self.import_source(av.LOGIN_HTML, artifact="supp-login",
                                   origin="file:///inbox/supplement.pdf")["source_ref"]
        cases = [
            ("unavailable", self.asset("s1", "supplement", status="unavailable"),
             "supplement", "required_asset_unavailable"),
            ("unknown", self.asset("s1", "supplement", status="unknown"),
             "supplement", "required_asset_unknown"),
            ("login", self.asset("s1", "supplement", source=login), "supplement", "required_asset_rejected"),
            ("fig", self.asset("f1", "figure"), "visual", "required_asset_bytes_missing"),
        ]
        for name, asset, category, code in cases:
            rev = self.make([self.entry(a, source=src, assets=[asset]), self.entry(b, "exclude")],
                            request_id=f"m-{name}")
            self.assertEqual([(x["category"], x["code"], x["asset_id"]) for x in self.blockers(rev)],
                             [(category, code, asset["asset_id"])], name)
            self.gate(rev)
            self.assert_not_acquired(code="blockers_present")
            self.assertIn(code, [q["code"] for q in self.acq.queue(self.freeze_ref)])
            self.assertFalse(self.acq.get(self.ref_of(rev))["payload"]["authorizing"])
        # the same supplement imported with real bytes clears the visible blocker
        supp = self.import_source(b"PK\x03\x04 opaque supplement archive",
                                  artifact="supp-real")["source_ref"]
        ok = self.make([self.entry(a, source=src, assets=[self.asset("s1", "supplement", source=supp)]),
                        self.entry(b, "exclude")], request_id="m-ok")
        self.assertEqual(self.blockers(ok), [])
        self.gate(ok)
        self.assertTrue(self.acq.require_acquired()["acquired"])

    def test_ac5_unknown_integrity_blocks_and_is_never_asserted_clean(self):
        self.setup_frozen()
        a, b = self.cands["111"], self.cands["222"]
        src = self.import_source(_jats("111"), artifact="paper-111")["source_ref"]
        unknown = {
            "checked_at": "2026-10-30T00:00:00+00:00",
            "provenance": {"ref": "integrity:syn-core", "source": "retraction-watch"},
            "retraction": "none", "expression_of_concern": "unknown",
            "correction": "none", "non_replication": "none",
            "rationale": "could not check concern status",
        }
        rev = self.make([self.entry(a, source=src, integrity=unknown), self.entry(b, "exclude")])
        self.assertEqual([(x["category"], x["code"]) for x in self.blockers(rev)],
                         [("integrity", "integrity_unknown")])
        self.gate(rev)
        self.assert_not_acquired()


# --------------------------------------------------------------------------- #
# AC6 - unofficial acquisition is manual only; official resolver is allowlisted
# --------------------------------------------------------------------------- #
class Ac6ManualAndOfficialRoutesTests(CoreBase):
    def test_ac6_manual_missing_locator_never_contacts_the_network(self):
        self.setup_frozen()

        def boom(*args, **kwargs):
            raise AssertionError("network contacted during manual record")

        with mock.patch.object(socket, "create_connection", boom), \
                mock.patch.object(socket.socket, "connect", boom), \
                mock.patch.object(urllib.request, "urlopen", boom):
            res = self.imports.record_missing(
                artifact_id="paper-111",
                origin_locator="https://sci-hub.example/paper-111.pdf",
                rights=ti.missing_rights(self.project_id, "paper-111"),
                reason="user will fetch this manually outside the tool",
                **self._occ_args("manual-1"),
            )
        self.assertEqual(res["disposition"], "no_full_text_acquired")
        occ = self.imports.get_occurrence(res["occurrence_ref"])
        self.assertEqual(occ.origin_locator, "https://sci-hub.example/paper-111.pdf")
        self.assertIsNone(occ.source_ref)
        self.assertFalse(occ.authorizes_use)
        self.assertEqual(self.artifacts.list_revisions("paper-111"), [])

    def test_ac6_official_resolver_stub_requests_only_allowlisted_routes(self):
        self.setup_frozen()
        a, b = self.cands["111"], self.cands["222"]
        arbitrary = "https://mirror.example/PMC3257301.pdf"
        # failing routes: only the two fixed official URLs are ever requested
        result, fake = rs.resolve(
            [rs.response(404), rs.response(404)],
            identifiers={"pmcid": "PMC3257301", "url": arbitrary, "publisher_url": arbitrary},
        )
        self.assertEqual(result["status"], "manual_required")
        self.assertEqual(fake.calls, [rs.EPMC_URL, rs.EFETCH_URL])
        self.assertTrue(result["unofficial_locator_present"])
        # the transport itself refuses a non-allowlisted URL
        tx, tfake = rs.make_transport([])
        with self.assertRaises(Exception):
            tx.get(arbitrary)
        self.assertEqual(tfake.calls, [])
        # retrieved official bytes enter the same import path; retrieval is not acquisition
        got, _ = rs.resolve([rs.xml_response(rs.jats_fulltext())])
        self.assertEqual(got["status"], "retrieved")
        import base64

        data = base64.b64decode(got["content"])
        res = self.import_source(data, artifact="paper-111", origin=got["origin_locator"])
        rev = self.make([self.entry(a, source=res["source_ref"]), self.entry(b, "exclude")])
        self.assertEqual([x["code"] for x in self.blockers(rev)], ["human_review_required"])
        self.gate(rev)
        self.assert_not_acquired()
        # the resolver never ran an unauthorized request
        blocked = resolution.resolve_full_text(
            {"pmcid": "PMC3257301"}, authorization=None, transport=rs.make_transport([])[0]
        )
        self.assertEqual(blocked["status"], "blocked")


# --------------------------------------------------------------------------- #
# AC7 - storage / redistribution / hosted transfer stay distinct
# --------------------------------------------------------------------------- #
class Ac7RightsDistinctTests(CoreBase):
    def test_ac7_unknown_storage_retains_no_bytes_and_blocks_the_chain(self):
        self.setup_frozen()
        a, b = self.cands["111"], self.cands["222"]
        revisions, artifacts_n = self.count("revision"), self.count("artifact")
        with self.assertRaises(AssetImportError) as ctx:
            self.import_source(_jats("111"), artifact="paper-111", ops={"storage": "unknown"})
        self.assertEqual(ctx.exception.code, "storage_not_allowed")
        self.assertEqual((self.count("revision"), self.count("artifact")), (revisions, artifacts_n))
        self.assertEqual(self.artifacts.list_revisions("paper-111"), [])
        rev = self.make([self.entry(a), self.entry(b, "exclude")], request_id="nosrc")
        self.gate(rev)
        self.assert_not_acquired(code="blockers_present")

    def test_ac7_unknown_redistribution_and_hosted_transfer_do_not_authorize_hosted_use(self):
        self.setup_frozen()
        a, b = self.cands["111"], self.cands["222"]
        res = self.import_source(
            _jats("111"), artifact="paper-111",
            ops={"redistribution": "unknown", "hosted_transfer": "unknown"},
        )
        revision = self.artifacts.get_revision("paper-111", res["source_ref"]["revision_id"])
        record = self.artifacts.get_rights_record(revision.rights_ref)
        self.assertEqual(record["operations"]["storage"], "allowed")
        self.assertEqual(record["operations"]["acquisition"], "allowed")
        self.assertEqual(record["operations"]["redistribution"], "unknown")
        self.assertEqual(record["operations"]["hosted_transfer"], "unknown")
        rev = self.make([self.entry(a, source=res["source_ref"]), self.entry(b, "exclude")])
        self.gate(rev)
        out = self.acq.require_acquired()  # local acquisition + storage is enough for the prerequisite
        self.assertTrue(out["acquired"])
        self.assertFalse(out["authorizes_scientific_processing"])
        # acquisition success did not rewrite the distinct, still-unknown rights
        again = self.artifacts.get_rights_record(
            self.artifacts.get_revision("paper-111", res["source_ref"]["revision_id"]).rights_ref
        )
        self.assertEqual(again["operations"]["hosted_transfer"], "unknown")
        self.assertEqual(again["operations"]["redistribution"], "unknown")
        occ = self.imports.get_occurrence(res["occurrence_ref"])
        self.assertFalse(occ.authorizes_use)


# --------------------------------------------------------------------------- #
# Restart + private backup/restore preserve the exact chain
# --------------------------------------------------------------------------- #
class RestartBackupChainTests(CoreBase):
    def test_restart_and_private_backup_preserve_occurrence_source_manifest_and_gate(self):
        self.setup_frozen()
        a, b = self.cands["111"], self.cands["222"]
        data = _jats("111")
        res = self.import_source(data, artifact="paper-111", origin="file:///inbox/111.xml")
        dup = self.import_source(data, artifact="paper-111", origin="file:///elsewhere/111.xml")
        rev = self.make([self.entry(a, source=res["source_ref"]), self.entry(b, "exclude")])
        self.gate(rev)
        before = self.acq.require_acquired()
        self._reopen()
        after = self.acq.require_acquired()
        self.assertEqual(after["content_sha256"], before["content_sha256"])

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
        self.assertEqual(out["content_sha256"], before["content_sha256"])
        self.assertEqual(out["acquired_candidate_ids"], [a])
        self.assertFalse(out["authorizes_scientific_processing"])
        r_arts = ArtifactRepository(restored)
        self.assertEqual(
            r_arts.read_content("paper-111", res["source_ref"]["revision_id"]), data
        )
        r_imports = AssetImportRepository(r_arts)
        for original in (res, dup):
            occ = r_imports.get_occurrence(original["occurrence_ref"])
            self.assertEqual(occ.source_ref.as_dict(), res["source_ref"])
            self.assertEqual(occ.content_hash, res["sha256"])
            self.assertFalse(occ.authorizes_use)
        self.assertEqual(
            r_imports.get_occurrence(dup["occurrence_ref"]).origin_locator,
            "file:///elsewhere/111.xml",
        )


if __name__ == "__main__":
    unittest.main()
