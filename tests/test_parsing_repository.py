"""Integration tests for the durable parsing repository (S7-STORE).

Covers ``src/review_article_agent/parsing/repository.py`` against
``docs/implementation/07-parsing.md`` AC16/AC20/AC21/AC22 and the
``docs/specification.md`` revision/rights invariants: a stored document parses
into a distinct immutable ``report`` revision, a bound locator keeps resolving
the historical bytes after a restart while reporting a changed current
source/report head or parser/normalization difference separately, and untrusted
generic-imported report content fails static errors.

All fixtures are synthetic and self-authored; no network, licensed source, or
scientific-correctness claim is used or made.

Run::

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_parsing*.py'
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from review_article_agent.parsing import (  # noqa: E402
    ParsingRepository,
    ParsingRepositoryError,
    make_locator,
    parse_document,
)
from review_article_agent.parsing.repository import REPORT_DOCUMENT_TYPE  # noqa: E402
from review_article_agent.storage import (  # noqa: E402
    ArtifactRepository,
    DependencyRef,
    ProjectStore,
)
from review_article_agent.storage.artifacts import sha256_digest  # noqa: E402
from review_article_agent.storage import paths  # noqa: E402

_OPS = ("acquisition", "storage", "redistribution", "hosted_transfer")

JATS = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<!DOCTYPE article PUBLIC "-//NLM//DTD JATS (Z39.96) Journal Archiving '
    'and Interchange DTD v1.3 20210610//EN" "JATS-archivearticle1-3.dtd">\n'
    '<article xmlns:xlink="http://www.w3.org/1999/xlink">\n'
    "  <front><article-meta><abstract>\n"
    '    <p>Clearance was 5 mg not 10 mg<xref ref-type="bibr" rid="ref1">1</xref>.</p>\n'
    "  </abstract></article-meta></front>\n"
    "  <body><sec>\n"
    "    <title>Results</title>\n"
    '    <p>Treatment improved<xref ref-type="bibr" rid="ref1">1</xref>.</p>\n'
    '    <p>Unresolved<xref ref-type="bibr" rid="ref9">9</xref>.</p>\n'
    "  </sec><sec>\n"
    "    <title>Discussion</title>\n"
    "    <p>A well-known e\ufb00ect was not reproduced.</p>\n"
    "  </sec></body>\n"
    '  <back><ref-list><ref id="ref1"><label>1</label>'
    "<mixed-citation>Alpha et al.</mixed-citation></ref></ref-list></back>\n"
    "</article>\n"
)
JATS_BYTES = JATS.encode("utf-8")


def _rights(project_id, artifact_id, revision_id, *, rights_id=None, record_revision=1, **overrides):
    """A synthetic, fully valid RightsRecord v1 bound to one revision."""
    operations = overrides.pop("operations", {op: "allowed" for op in _OPS})
    privacy = overrides.pop(
        "privacy",
        {"personal_data": False, "confidentiality": "public", "denial": False},
    )
    record = {
        "rights_id": rights_id or f"RIGHTS-{revision_id}",
        "project_id": project_id,
        "record_revision": record_revision,
        "subject": {
            "kind": "source_revision",
            "project_id": project_id,
            "artifact_id": artifact_id,
            "revision_id": revision_id,
        },
        "operations": operations,
        "basis": {
            op: {"kind": "public_domain", "evidence": {"ref": "lic:syn", "provenance": "publisher:syn"}}
            for op in _OPS
        },
        "authorization": None,
        "restrictions": [],
        "privacy": privacy,
        "recorded_by": "user",
        "recorded_at": "2026-10-27T00:00:00Z",
        "derived_status": "valid",
    }
    record.update(overrides)
    return record


class ParsingRepositoryTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-parsing-repo-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def new_store(self, name: str = "p") -> ProjectStore:
        return ProjectStore.create_at(paths.app_dir_for(str(self.root / name)), paths.new_project_id())

    def add_document(self, store, artifact_id, data, revision_id, *, media_type=None):
        repo = ArtifactRepository(store)
        repo.create_artifact(artifact_id, kind="document", created_by="import")
        repo.import_revision(
            artifact_id=artifact_id,
            revision_id=revision_id,
            content=data,
            origin_locator="import:synthetic",
            media_type=media_type,
            rights=_rights(store.project_id, artifact_id, revision_id),
            acquired_by="import",
        )
        ref = {"project_id": store.project_id, "artifact_id": artifact_id, "revision_id": revision_id}
        return ref, repo

    def write_generic_report(self, store, artifact_id, revision_id, envelope, source_ref=None):
        """Import a caller-built envelope through the generic artifact store."""
        repo = ArtifactRepository(store)
        repo.create_artifact(artifact_id, kind="report", created_by="import")
        deps = (
            []
            if source_ref is None
            else [DependencyRef(store.project_id, source_ref["artifact_id"], source_ref["revision_id"], "derived_from")]
        )
        repo.import_revision(
            artifact_id=artifact_id,
            revision_id=revision_id,
            content=envelope,
            origin_locator="generic:untrusted",
            rights=_rights(store.project_id, artifact_id, revision_id),
            dependencies=deps,
            acquired_by="import",
        )
        return {"project_id": store.project_id, "artifact_id": artifact_id, "revision_id": revision_id}

    def assert_code(self, code, func, *args, **kwargs):
        with self.assertRaises(ParsingRepositoryError) as ctx:
            func(*args, **kwargs)
        self.assertEqual(ctx.exception.code, code)
        return ctx.exception


class ParseAndReadbackTests(ParsingRepositoryTestBase):
    def test_parse_persists_distinct_report_and_readback(self):
        with self.new_store() as store:
            pid = store.project_id
            src_ref, repo = self.add_document(store, "art-src", JATS_BYTES, "rev-src")
            pr = ParsingRepository(store)
            out = pr.parse_stored_document(
                src_ref,
                report_artifact_id="art-rep",
                report_revision_id="rev-rep",
                rights=_rights(pid, "art-rep", "rev-rep"),
            )
            self.assertEqual(out["status"], "parsed")
            self.assertFalse(out["human_action_required"])
            self.assertEqual(out["source_ref"], src_ref)
            self.assertEqual(out["report_ref"], {"project_id": pid, "artifact_id": "art-rep", "revision_id": "rev-rep"})
            # report is a distinct artifact/revision from the source document
            self.assertNotEqual(out["report_ref"]["artifact_id"], src_ref["artifact_id"])
            self.assertEqual(repo.get_artifact("art-rep").kind, "report")
            self.assertEqual(repo.get_artifact("art-src").kind, "document")

            record = pr.read_report(out["report_ref"])
            self.assertEqual(record["source_ref"], src_ref)
            expected = parse_document(JATS_BYTES, source_digest=hashlib.sha256(JATS_BYTES).hexdigest())
            self.assertEqual(record["payload"], expected)

            deps = repo.list_dependencies("art-rep", "rev-rep")
            self.assertEqual(
                [(d.artifact_id, d.revision_id, d.role) for d in deps],
                [("art-src", "rev-src", "derived_from")],
            )
            # the source bytes are untouched by parsing
            self.assertEqual(repo.read_content("art-src", "rev-src"), JATS_BYTES)
            self.assertEqual(repo.current_revision("art-src").revision_id, "rev-src")

    def test_source_bytes_unchanged_and_raw_is_a_separate_revision(self):
        with self.new_store() as store:
            pid = store.project_id
            src_ref, repo = self.add_document(store, "art-src", JATS_BYTES, "rev-src")
            pr = ParsingRepository(store)
            out = pr.parse_stored_document(
                src_ref, report_artifact_id="art-rep", report_revision_id="rev-rep",
                rights=_rights(pid, "art-rep", "rev-rep"),
            )
            revision = repo.get_revision("art-src", "rev-src")
            self.assertEqual(repo.read_content("art-src", "rev-src"), JATS_BYTES)
            self.assertEqual(revision.content_sha256, sha256_digest(JATS_BYTES))
            self.assertEqual(revision.byte_size, len(JATS_BYTES))
            record = pr.read_report(out["report_ref"])
            passage = next(p for p in record["payload"]["passages"] if p["passage_id"] == "p0004")
            self.assertNotEqual(passage["raw_text"], passage["text"])  # raw vs normalized
            self.assertIn("effect", passage["text"])


class RestartAndStalenessTests(ParsingRepositoryTestBase):
    def test_restart_historical_resolution_with_separate_staleness(self):
        app = paths.app_dir_for(str(self.root / "restart"))
        with ProjectStore.create_at(app, paths.new_project_id()) as store:
            pid = store.project_id
            src_ref, _ = self.add_document(store, "art-src", JATS_BYTES, "rev-src")
            pr = ParsingRepository(store)
            out = pr.parse_stored_document(
                src_ref, report_artifact_id="art-rep", report_revision_id="rev-rep",
                rights=_rights(pid, "art-rep", "rev-rep"),
            )
            locator = pr.make_passage_locator(out["report_ref"], "p0002")
            self.assertEqual(locator["report_ref"], out["report_ref"])
            self.assertEqual(locator["source_ref"], src_ref)

        # restart: reopen the store, then move BOTH current heads forward
        with ProjectStore.open_at(app) as store:
            pid = store.project_id
            src_ref = {"project_id": pid, "artifact_id": "art-src", "revision_id": "rev-src"}
            pr = ParsingRepository(store)
            pr.parse_stored_document(
                src_ref, report_artifact_id="art-rep", report_revision_id="rev-rep2",
                parent_revision_id="rev-rep", rights=_rights(pid, "art-rep", "rev-rep2"),
            )
            repo = ArtifactRepository(store)
            repo.import_revision(
                artifact_id="art-src", revision_id="rev-src2",
                content=JATS.replace("Alpha", "Gamma").encode("utf-8"),
                parent_revision_id="rev-src", origin_locator="import:synthetic",
                rights=_rights(pid, "art-src", "rev-src2"), acquired_by="import",
            )

            resolved = pr.resolve_passage(locator)
            # the OLD locator still resolves its exact historical report
            self.assertEqual(resolved["status"], "resolved")
            self.assertTrue(resolved["historical_resolved"])
            self.assertEqual(resolved["source_ref"], src_ref)
            # current heads changed, reported separately, not rebound
            self.assertTrue(resolved["current"]["source_head_changed"])
            self.assertTrue(resolved["current"]["report_head_changed"])
            self.assertFalse(resolved["current"]["parser_version_changed"])
            self.assertFalse(resolved["current"]["normalization_version_changed"])
            self.assertIn("source_head_changed", resolved["current"]["reasons"])
            self.assertIn("report_head_changed", resolved["current"]["reasons"])
            self.assertEqual(resolved["current"]["source_head"], "rev-src2")
            self.assertEqual(resolved["current"]["report_head"], "rev-rep2")
            # the resolved passage text is the historical one, never the latest
            historical = pr.read_report(locator["report_ref"])
            self.assertEqual(historical["source_ref"]["revision_id"], "rev-src")
            passage = next(p for p in historical["payload"]["passages"] if p["passage_id"] == "p0002")
            self.assertIn("Treatment improved", passage["text"])


class TamperTests(ParsingRepositoryTestBase):
    def test_parser_version_and_text_tamper(self):
        with self.new_store() as store:
            pid = store.project_id
            src_ref, _ = self.add_document(store, "art-src", JATS_BYTES, "rev-src")
            pr = ParsingRepository(store)
            out = pr.parse_stored_document(
                src_ref, report_artifact_id="art-rep", report_revision_id="rev-rep",
                rights=_rights(pid, "art-rep", "rev-rep"),
            )
            base = json.loads(json.dumps(pr.read_report(out["report_ref"])["payload"]))

            # (a) text tamper carrying the CURRENT parser/normalization label:
            # a deterministic reparse must catch it.
            tampered = json.loads(json.dumps(base))
            tampered["passages"][1]["text"] += " TAMPER"
            tampered["passages"][1]["raw_text"] = tampered["passages"][1]["text"]
            tampered["passages"][1]["text_sha256"] = hashlib.sha256(
                tampered["passages"][1]["text"].encode("utf-8")
            ).hexdigest()
            bad_text_ref = self.write_generic_report(
                store, "art-bad-text", "rev-bad-text",
                {"schema_version": 1, "document_type": REPORT_DOCUMENT_TYPE, "payload": tampered},
                src_ref,
            )
            self.assert_code("report_payload_mismatch", pr.read_report, bad_text_ref)

            # (b) a bumped parser_version is accepted as a non-reproducible
            # historical report and flagged SEPARATELY, never trusted as current.
            bumped = json.loads(json.dumps(base))
            bumped["parser_version"] = "step07-jats-tei/0"
            bad_parser_ref = self.write_generic_report(
                store, "art-bad-parser", "rev-bad-parser",
                {"schema_version": 1, "document_type": REPORT_DOCUMENT_TYPE, "payload": bumped},
                src_ref,
            )
            record = pr.read_report(bad_parser_ref)
            self.assertTrue(record["parser_version_changed"])
            loc = dict(make_locator(bumped, "p0002"))
            loc["report_ref"] = dict(bad_parser_ref)
            loc["source_ref"] = dict(src_ref)
            resolved = pr.resolve_passage(loc)
            self.assertEqual(resolved["status"], "resolved")
            self.assertIn("parser_version_changed", resolved["current"]["reasons"])


class SourceFailureTests(ParsingRepositoryTestBase):
    def test_source_kind_project_rights_and_ref_failures(self):
        with self.new_store() as store:
            pid = store.project_id
            src_ref, repo = self.add_document(store, "art-src", JATS_BYTES, "rev-src")
            pr = ParsingRepository(store)

            # a non-document (report) artifact is never a source
            repo.create_artifact("art-other", kind="report", created_by="import")
            self.assert_code(
                "source_not_document", pr.parse_stored_document,
                {"project_id": pid, "artifact_id": "art-other", "revision_id": "rev-other"},
                report_artifact_id="art-r1", report_revision_id="rev-r1",
                rights=_rights(pid, "art-r1", "rev-r1"),
            )
            # a missing revision of a real document
            self.assert_code(
                "source_not_found", pr.parse_stored_document,
                {"project_id": pid, "artifact_id": "art-src", "revision_id": "rev-missing"},
                report_artifact_id="art-r2", report_revision_id="rev-r2",
                rights=_rights(pid, "art-r2", "rev-r2"),
            )
            # a cross-project reference is rejected before any read
            self.assert_code(
                "invalid_input", pr.parse_stored_document,
                {"project_id": "other-project", "artifact_id": "art-src", "revision_id": "rev-src"},
                report_artifact_id="art-r3", report_revision_id="rev-r3",
                rights=_rights(pid, "art-r3", "rev-r3"),
            )
            # the report must not overwrite the source artifact
            self.assert_code(
                "report_overwrites_source", pr.parse_stored_document,
                src_ref, report_artifact_id="art-src", report_revision_id="rev-r4",
                rights=_rights(pid, "art-src", "rev-r4"),
            )
            # a newer rights revision that denies privacy blocks derivatives
            repo.store_rights_record(
                _rights(
                    pid, "art-src", "rev-src", rights_id="RIGHTS-rev-src",
                    record_revision=2,
                    privacy={"personal_data": False, "confidentiality": "public", "denial": True},
                )
            )
            self.assert_code(
                "source_rights_blocked", pr.parse_stored_document,
                src_ref, report_artifact_id="art-r5", report_revision_id="rev-r5",
                rights=_rights(pid, "art-r5", "rev-r5"),
            )


class InvalidGenericReportTests(ParsingRepositoryTestBase):
    def test_untrusted_generic_report_content_is_rejected(self):
        with self.new_store() as store:
            pid = store.project_id
            src_ref, _ = self.add_document(store, "art-src", JATS_BYTES, "rev-src")
            pr = ParsingRepository(store)
            good = parse_document(JATS_BYTES, source_digest=hashlib.sha256(JATS_BYTES).hexdigest())

            # wrong envelope document_type
            wrong_type = self.write_generic_report(
                store, "art-e1", "rev-e1",
                {"schema_version": 1, "document_type": "something_else", "payload": good}, src_ref,
            )
            self.assert_code("report_envelope_invalid", pr.read_report, wrong_type)

            # right envelope type, garbage payload
            bad_payload = self.write_generic_report(
                store, "art-e2", "rev-e2",
                {"schema_version": 1, "document_type": REPORT_DOCUMENT_TYPE, "payload": {"nope": True}},
                src_ref,
            )
            self.assert_code("report_payload_invalid", pr.read_report, bad_payload)

            # no derived_from dependency at all
            no_dep = self.write_generic_report(
                store, "art-e3", "rev-e3",
                {"schema_version": 1, "document_type": REPORT_DOCUMENT_TYPE, "payload": good},
                None,
            )
            self.assert_code("report_source_dependency_invalid", pr.read_report, no_dep)

            # payload source digest does not match the stored source bytes
            bad_hash = json.loads(json.dumps(good))
            bad_hash["source"]["sha256"] = "0" * 64
            mismatch = self.write_generic_report(
                store, "art-e4", "rev-e4",
                {"schema_version": 1, "document_type": REPORT_DOCUMENT_TYPE, "payload": bad_hash},
                src_ref,
            )
            self.assert_code("report_source_hash_mismatch", pr.read_report, mismatch)

            # an exclusion claim is never trusted
            excluded = json.loads(json.dumps(good))
            excluded["excludes_candidate"] = True
            bad_excl = self.write_generic_report(
                store, "art-e5", "rev-e5",
                {"schema_version": 1, "document_type": REPORT_DOCUMENT_TYPE, "payload": excluded},
                src_ref,
            )
            self.assert_code("report_payload_invalid", pr.read_report, bad_excl)


class FailedParseTests(ParsingRepositoryTestBase):
    def test_failed_xml_parse_persists_human_action(self):
        with self.new_store() as store:
            pid = store.project_id
            src_ref, repo = self.add_document(store, "art-src", b"<article><body>", "rev-src")
            pr = ParsingRepository(store)
            out = pr.parse_stored_document(
                src_ref, report_artifact_id="art-rep", report_revision_id="rev-rep",
                rights=_rights(pid, "art-rep", "rev-rep"),
            )
            self.assertEqual(out["status"], "rejected")
            self.assertTrue(out["human_action_required"])
            self.assertFalse(out["excludes_candidate"])
            self.assertEqual(repo.get_artifact("art-src").kind, "document")
            record = pr.read_report(out["report_ref"])
            self.assertEqual(record["payload"]["passages"], [])
            self.assertFalse(record["payload"]["authorizes_scientific_processing"])
            self.assertFalse(record["excludes_candidate"])

    def test_pdf_report_stays_human_review(self):
        with self.new_store() as store:
            pid = store.project_id
            src_ref, _ = self.add_document(
                store, "art-pdf", b"%PDF-1.4 synthetic", "rev-pdf", media_type="application/pdf"
            )
            pr = ParsingRepository(store)
            out = pr.parse_stored_document(
                src_ref, report_artifact_id="art-pr", report_revision_id="rev-pr",
                rights=_rights(pid, "art-pr", "rev-pr"),
                inspector=lambda data: {"status": "unavailable", "failure_code": "dependency_missing"},
            )
            self.assertEqual(out["status"], "needs_review")
            self.assertTrue(out["human_action_required"])
            record = pr.read_report(out["report_ref"])
            self.assertEqual(record["format"], "pdf")
            self.assertEqual(record["status"], "needs_review")
            self.assertEqual(record["payload"]["passages"], [])
            self.assertIn("ocr_or_parser_unavailable", record["payload"]["reasons"])
            self.assertFalse(record["excludes_candidate"])


if __name__ == "__main__":
    unittest.main()
