"""In-process CLI tests for the ``parsing`` command group (Step 07, S7-CLI).

Covers ``src/review_article_agent/cli/parsing.py`` against Step 07
(``docs/implementation/07-parsing.md`` lines 9-22) over the integrated
S7-CORE/S7-STORE repository:

* ``parsing parse`` turns one exact stored ``document`` revision into a distinct
  immutable ``report`` revision from explicit ids + an explicit caller rights
  record; the default output carries no private passage/section text;
* ``parsing show`` reads one exact report back -- a safe summary by default and
  the private parsed passages only with ``--content``;
* ``parsing locator`` summarises a bound locator safely by default and prints the
  full locator (the section-path headings and the exact JSON to save) only with
  ``--content``;
* after a restart, ``parsing resolve`` re-checks a saved locator against its exact
  historical report revision, reports safe citation counts by default, and the raw
  per-passage markers only with ``--content``;
* invalid/malformed references, rights and JSON inputs fail with a static
  ``validation`` error, and a failed XML parse is a human action, never an
  exclusion.

Synthetic data only: a real temporary registry and project store. No network,
model, provider, or approval call is performed. This is a minimal MVP -- it makes
no scientific-correctness, OCR, PDF/text-extraction, or full Step 07 claim.

Run::

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_cli_parsing.py'
"""

from __future__ import annotations

import io
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import review_article_agent.cli.main as cli_main  # noqa: E402
from review_article_agent.storage import (  # noqa: E402
    ArtifactRepository,
    DependencyRef,
    ProjectStore,
    Registry,
    paths,
)

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


class ParsingCliBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-cli-parsing-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.state = self.tmp / "state"
        self.registry = Registry(self.state)
        root = self.tmp / "project"
        info = self.registry.create_project(root, name="P")
        self.app_dir = paths.app_dir_for(str(root))
        self.store = ProjectStore.open_at(self.app_dir)
        self.project_id = self.store.project_id
        self.assertEqual(self.project_id, info.project_id)

    def tearDown(self) -> None:
        try:
            self.store.close()
        except Exception:  # pragma: no cover - already closed
            pass

    # -- driving ------------------------------------------------------------- #
    def _run(self, *args: str):
        out, err = io.StringIO(), io.StringIO()
        code = cli_main.main(
            ["--data-dir", str(self.state), *args], env={}, stdout=out, stderr=err
        )
        return code, out.getvalue(), err.getvalue()

    def cli(self, *args: str, expect: int = 0) -> dict:
        code, out, err = self._run(*args)
        self.assertEqual(code, expect, f"args={args} rc={code}\n{out}\n{err}")
        if expect == 0:
            return json.loads(out)
        self.assertNotIn("Traceback", err)
        return json.loads(err)

    def write_json(self, name: str, data: object) -> str:
        path = self.tmp / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return str(path)

    def write_bytes(self, name: str, data: bytes) -> str:
        path = self.tmp / name
        path.write_bytes(data)
        return str(path)

    # -- fixtures ------------------------------------------------------------ #
    def add_document(self, artifact_id="art-src", revision_id="rev-src", data=JATS_BYTES):
        repo = ArtifactRepository(self.store)
        repo.create_artifact(artifact_id, kind="document", created_by="import")
        repo.import_revision(
            artifact_id=artifact_id,
            revision_id=revision_id,
            content=data,
            origin_locator="import:synthetic",
            rights=_rights(self.project_id, artifact_id, revision_id),
            acquired_by="import",
        )
        return repo

    def parse(self, *, source="art-src", revision="rev-src", rights_file=None, expect=0, **extra):
        if rights_file is None:
            rights_file = self.write_json(
                "report-rights.json",
                _rights(self.project_id, "art-rep", "rev-rep"),
            )
        return self.cli(
            "parsing", "parse", "--project", self.project_id,
            "--source-artifact-id", source, "--source-revision-id", revision,
            "--report-artifact-id", "art-rep", "--report-revision-id", "rev-rep",
            "--rights-file", rights_file, *self._flags(extra), expect=expect,
        )

    @staticmethod
    def _flags(extra: dict) -> list:
        flags: list = []
        for key, value in extra.items():
            flags.append("--" + key.replace("_", "-"))
            if value is not True:
                flags.append(str(value))
        return flags


# --------------------------------------------------------------------------- #
# end-to-end: parse -> show -> locator -> restart -> resolve, with privacy
# --------------------------------------------------------------------------- #
class ParseReadbackAndRestartTest(ParsingCliBase):
    def test_success_restart_resolve_and_privacy(self):
        self.add_document()
        parsed = self.parse()
        self.assertEqual(parsed["status"], "parsed")
        self.assertFalse(parsed["human_action_required"])
        self.assertEqual(
            parsed["report_ref"],
            {"project_id": self.project_id, "artifact_id": "art-rep", "revision_id": "rev-rep"},
        )
        self.assertFalse(parsed["authorizes_scientific_processing"])
        # the default parse output carries no private passage/section text
        blob = json.dumps(parsed)
        self.assertNotIn("Clearance", blob)
        self.assertNotIn("Results", blob)

        # show: safe summary by default, private passages only with --content
        show = self.cli(
            "parsing", "show", "--project", self.project_id,
            "--report-artifact-id", "art-rep", "--report-revision-id", "rev-rep",
        )
        self.assertEqual(show["passage_count"], 4)
        self.assertEqual(show["reference_count"], 1)
        self.assertEqual(show["citation_counts"]["resolved"], 2)
        self.assertEqual(show["citation_counts"]["unresolved"], 1)
        self.assertFalse(show["private_content_included"])
        self.assertNotIn("Clearance", json.dumps(show))
        self.assertNotIn("Results", json.dumps(show))
        content = self.cli(
            "parsing", "show", "--project", self.project_id,
            "--report-artifact-id", "art-rep", "--report-revision-id", "rev-rep",
            "--content",
        )
        self.assertTrue(content["private_content_included"])
        self.assertIn("Clearance", json.dumps(content))
        self.assertIn("Results", json.dumps(content))

        # locator: safe summary by default, full locator only with --content
        loc = self.cli(
            "parsing", "locator", "--project", self.project_id,
            "--report-artifact-id", "art-rep", "--report-revision-id", "rev-rep",
            "--passage-id", "p0002",
        )
        self.assertEqual(loc["passage_id"], "p0002")
        self.assertEqual(loc["section_path_depth"], 1)
        self.assertNotIn("Results", json.dumps(loc))
        self.assertNotIn("locator", loc)
        loc_full = self.cli(
            "parsing", "locator", "--project", self.project_id,
            "--report-artifact-id", "art-rep", "--report-revision-id", "rev-rep",
            "--passage-id", "p0002", "--content",
        )
        self.assertEqual(loc_full["section_path"], ["Results"])
        locator_path = self.write_json("locator.json", loc_full["locator"])
        # a second locator for a passage that carries an unresolved citation marker
        unresolved_full = self.cli(
            "parsing", "locator", "--project", self.project_id,
            "--report-artifact-id", "art-rep", "--report-revision-id", "rev-rep",
            "--passage-id", "p0003", "--content",
        )
        unresolved_path = self.write_json("locator-unresolved.json", unresolved_full["locator"])

        # restart: reopen the store before resolving the saved locator
        self.store.close()
        self.store = ProjectStore.open_at(self.app_dir)

        resolved = self.cli(
            "parsing", "resolve", "--project", self.project_id,
            "--locator-file", locator_path,
        )
        self.assertEqual(resolved["status"], "resolved")
        self.assertTrue(resolved["locator_ok"])
        self.assertTrue(resolved["historical_resolved"])
        self.assertEqual(
            set(resolved["citation_check"]),
            {"passage_id", "ok", "total", "unresolved_count", "ambiguous_count", "malformed_count"},
        )
        # safe resolve output exposes counts/states only, never marker strings
        self.assertNotIn("Clearance", json.dumps(resolved))
        resolved_content = self.cli(
            "parsing", "resolve", "--project", self.project_id,
            "--locator-file", locator_path, "--content",
        )
        self.assertTrue(resolved_content["private_content_included"])
        self.assertIsInstance(resolved_content["citation_check"]["unresolved"], list)
        # the raw per-passage link detail (including the marker string) is opt-in
        unresolved = self.cli(
            "parsing", "resolve", "--project", self.project_id,
            "--locator-file", unresolved_path, "--content",
        )
        self.assertTrue(unresolved["citation_check"]["unresolved"])
        self.assertIn("9", json.dumps(unresolved["citation_check"]["unresolved"]))


# --------------------------------------------------------------------------- #
# invalid references / rights / malformed inputs / unknown project
# --------------------------------------------------------------------------- #
class ParseValidationTest(ParsingCliBase):
    def test_missing_source_bad_rights_and_malformed_inputs(self):
        self.add_document()
        # an unknown source revision is a validation failure, nothing written
        missing = self.cli(
            "parsing", "parse", "--project", self.project_id,
            "--source-artifact-id", "art-src", "--source-revision-id", "rev-missing",
            "--report-artifact-id", "art-x", "--report-revision-id", "rev-x",
            "--rights-file", self.write_json("r1.json", _rights(self.project_id, "art-x", "rev-x")),
            expect=cli_main.EXIT_VALIDATION,
        )
        self.assertEqual(missing["error"]["category"], "validation")
        # a non-document source is refused
        repo = ArtifactRepository(self.store)
        repo.create_artifact("art-other", kind="report", created_by="import")
        not_doc = self.cli(
            "parsing", "parse", "--project", self.project_id,
            "--source-artifact-id", "art-other", "--source-revision-id", "rev-other",
            "--report-artifact-id", "art-y", "--report-revision-id", "rev-y",
            "--rights-file", self.write_json("r2.json", _rights(self.project_id, "art-y", "rev-y")),
            expect=cli_main.EXIT_VALIDATION,
        )
        self.assertEqual(not_doc["error"]["category"], "validation")
        # a rights input that is not a JSON object
        bad_shape = self.parse(
            rights_file=self.write_json("r3.json", [1, 2, 3]),
            expect=cli_main.EXIT_VALIDATION,
        )
        self.assertEqual(bad_shape["error"]["category"], "validation")
        # a malformed JSON rights file
        malformed = self.parse(
            rights_file=self.write_bytes("r4.json", b"{"),
            expect=cli_main.EXIT_VALIDATION,
        )
        self.assertEqual(malformed["error"]["category"], "validation")
        # an unknown project
        unknown = self.cli(
            "parsing", "show", "--project", "no-such-project",
            "--report-artifact-id", "art-rep", "--report-revision-id", "rev-rep",
            expect=cli_main.EXIT_VALIDATION,
        )
        self.assertEqual(unknown["error"]["category"], "validation")

    def test_rights_denial_blocks_derivative(self):
        repo = self.add_document()
        # a newer rights revision with a privacy denial blocks derivatives
        repo.store_rights_record(
            _rights(
                self.project_id, "art-src", "rev-src",
                rights_id="RIGHTS-rev-src", record_revision=2,
                privacy={"personal_data": False, "confidentiality": "public", "denial": True},
            )
        )
        blocked = self.parse(expect=cli_main.EXIT_VALIDATION)
        self.assertEqual(blocked["error"]["category"], "validation")
        self.assertIn("rights", blocked["error"]["message"])


# --------------------------------------------------------------------------- #
# failed XML parse is a human action, never an exclusion
# --------------------------------------------------------------------------- #
class FailedParseCliTest(ParsingCliBase):
    def test_failed_xml_is_human_action_not_exclusion(self):
        self.add_document(data=b"<article><body>")
        parsed = self.parse()
        self.assertEqual(parsed["status"], "rejected")
        self.assertTrue(parsed["human_action_required"])
        self.assertFalse(parsed["excludes_candidate"])
        shown = self.cli(
            "parsing", "show", "--project", self.project_id,
            "--report-artifact-id", "art-rep", "--report-revision-id", "rev-rep",
        )
        self.assertEqual(shown["status"], "rejected")
        self.assertFalse(shown["excludes_candidate"])
        self.assertIn("xml_malformed", shown["reasons"])


# --------------------------------------------------------------------------- #
# locator / resolve invalid inputs
# --------------------------------------------------------------------------- #
class LocatorResolveValidationTest(ParsingCliBase):
    def test_invalid_locator_and_resolve_inputs(self):
        self.add_document()
        self.parse()
        # an unknown passage is a validation failure
        bad_passage = self.cli(
            "parsing", "locator", "--project", self.project_id,
            "--report-artifact-id", "art-rep", "--report-revision-id", "rev-rep",
            "--passage-id", "p9999", expect=cli_main.EXIT_VALIDATION,
        )
        self.assertEqual(bad_passage["error"]["category"], "validation")
        # a locator file that is not a JSON object
        bad_json = self.cli(
            "parsing", "resolve", "--project", self.project_id,
            "--locator-file", self.write_json("bad-loc.json", "nope"),
            expect=cli_main.EXIT_VALIDATION,
        )
        self.assertEqual(bad_json["error"]["category"], "validation")
        # a locator referencing an unknown report
        bogus = {
            "passage_id": "p0002",
            "section_path": ["Results"],
            "text_sha256": "0" * 64,
            "source_sha256": "0" * 64,
            "parser_version": "step07-jats-tei/1",
            "normalization_version": "nfc-ligature-ws/1",
            "locator_sha256": "0" * 64,
            "report_ref": {"project_id": self.project_id, "artifact_id": "art-none", "revision_id": "rev-none"},
            "source_ref": {"project_id": self.project_id, "artifact_id": "art-src", "revision_id": "rev-src"},
        }
        unknown_report = self.cli(
            "parsing", "resolve", "--project", self.project_id,
            "--locator-file", self.write_json("bogus-loc.json", bogus),
            expect=cli_main.EXIT_VALIDATION,
        )
        self.assertEqual(unknown_report["error"]["category"], "validation")
        # a non-existent locator file is unreadable (validation)
        unreadable = self.cli(
            "parsing", "resolve", "--project", self.project_id,
            "--locator-file", str(self.tmp / "does-not-exist.json"),
            expect=cli_main.EXIT_VALIDATION,
        )
        self.assertEqual(unreadable["error"]["category"], "validation")


# --------------------------------------------------------------------------- #
# an unverifiable (unknown-version) generic report must not leak its payload
# --------------------------------------------------------------------------- #
class UnverifiedGenericReportPrivacyTest(ParsingCliBase):
    """An unknown-version report is readable as unverified metadata only.

    The repository legitimately keeps a generic, non-reproducible report readable
    (``verifiable`` false), but its payload is *not* bound by a deterministic
    reparse, so the default ``show``/``resolve`` output must project a safe static
    summary and never the payload's arbitrary reasons/citations/counts (which
    could carry private raw source or citation text).  Only an explicit
    ``--content`` opt-in exposes the private payload.
    """

    def _add_generic_report(self):
        self.add_document()
        secret = "SECRET-payload-text"
        src_sha = hashlib.sha256(JATS_BYTES).hexdigest()
        payload = {
            "schema_version": 1,
            "document_type": "parsed_passages",
            # prefix-valid but unknown family version (arbitrary free-text suffix)
            # -> not reproducible here
            "parser_version": "step07-jats-tei/SECRET",
            "normalization_version": "SECRET",
            "format": "jats",
            "status": "parsed",
            "source": {"sha256": src_sha, "byte_size": len(JATS_BYTES)},
            "passages": [
                {
                    "passage_id": "g0001",
                    "text": secret,
                    "raw_text": secret,
                    "text_sha256": hashlib.sha256(secret.encode("utf-8")).hexdigest(),
                }
            ],
            "references": [],
            "citation_links": [
                {
                    "passage_id": "g0001",
                    "marker": "SECRET-marker",
                    "targets": ["SECRET-target"],
                    "state": "unresolved",
                    "resolved_ref_ids": [],
                }
            ],
            "citations": {"resolved": "SECRET-citation", "unresolved": "SECRET-count"},
            "assets": [],
            "non_text_assets_require_review": "SECRET-asset-flag",
            "human_action_required": False,
            "excludes_candidate": False,
            "reasons": ["SECRET-reason"],
            "authorizes_scientific_processing": False,
            "limitations": "SECRET-limitation",
        }
        envelope = {
            "schema_version": 1,
            "document_type": "parsed_passages",
            "payload": payload,
        }
        repo = ArtifactRepository(self.store)
        repo.create_artifact("art-generic", kind="report", created_by="import")
        repo.import_revision(
            artifact_id="art-generic",
            revision_id="rev-generic",
            content=envelope,
            origin_locator="generic:untrusted",
            rights=_rights(self.project_id, "art-generic", "rev-generic"),
            dependencies=[
                DependencyRef(self.project_id, "art-src", "rev-src", "derived_from")
            ],
            acquired_by="import",
        )

    def test_unverified_generic_report_is_static_and_content_is_opt_in(self):
        self._add_generic_report()

        # show: verifiable false is visible and the summary fails closed to a
        # human action, never an exclusion, with no arbitrary payload field.
        show = self.cli(
            "parsing", "show", "--project", self.project_id,
            "--report-artifact-id", "art-generic", "--report-revision-id", "rev-generic",
        )
        # no arbitrary payload field (reasons/citations/counts) leaks by default
        self.assertNotIn("SECRET", json.dumps(show))
        # verifiable false is visible and the summary fails closed to a human
        # action, never an exclusion
        self.assertFalse(show["verifiable"])
        self.assertFalse(show["supported"])
        self.assertTrue(show["human_action_required"])
        self.assertFalse(show["excludes_candidate"])
        self.assertIn("parser_version_unverified", show["reasons"])

        # only the explicit private opt-in exposes the full payload
        content = self.cli(
            "parsing", "show", "--project", self.project_id,
            "--report-artifact-id", "art-generic", "--report-revision-id", "rev-generic",
            "--content",
        )
        self.assertTrue(content["private_content_included"])
        self.assertIn("SECRET", json.dumps(content))

        # locator: the default summary must redact the unverified version strings
        # (arbitrary suffix / normalization label) just like show/resolve
        loc_default = self.cli(
            "parsing", "locator", "--project", self.project_id,
            "--report-artifact-id", "art-generic", "--report-revision-id", "rev-generic",
            "--passage-id", "g0001",
        )
        self.assertNotIn("SECRET", json.dumps(loc_default))
        self.assertFalse(loc_default["verifiable"])
        self.assertFalse(loc_default["supported"])
        self.assertEqual(loc_default["parser_version"], "step07-jats-tei/")
        self.assertIsNone(loc_default["normalization_version"])

        # ...but --content still returns the full original locator (usable by
        # resolve) with the true, unredacted version strings
        loc_full = self.cli(
            "parsing", "locator", "--project", self.project_id,
            "--report-artifact-id", "art-generic", "--report-revision-id", "rev-generic",
            "--passage-id", "g0001", "--content",
        )
        self.assertIn("SECRET", json.dumps(loc_full["locator"]))
        self.assertEqual(loc_full["locator"]["parser_version"], "step07-jats-tei/SECRET")
        self.assertEqual(loc_full["locator"]["normalization_version"], "SECRET")
        locator_path = self.write_json("generic-locator.json", loc_full["locator"])
        self.store.close()
        self.store = ProjectStore.open_at(self.app_dir)

        resolved = self.cli(
            "parsing", "resolve", "--project", self.project_id,
            "--locator-file", locator_path,
        )
        self.assertEqual(resolved["status"], "unverified")
        self.assertFalse(resolved["verifiable"])
        self.assertTrue(resolved["human_action_required"])
        self.assertFalse(resolved["excludes_candidate"])
        # the citation check is a static unknown, never arbitrary marker counts
        self.assertIsNone(resolved["citation_check"]["ok"])
        self.assertIsNone(resolved["citation_check"]["unresolved_count"])
        self.assertNotIn("SECRET", json.dumps(resolved))

        # the raw marker structures remain behind the explicit --content opt-in
        resolved_content = self.cli(
            "parsing", "resolve", "--project", self.project_id,
            "--locator-file", locator_path, "--content",
        )
        self.assertIn("SECRET", json.dumps(resolved_content["citation_check"]))


if __name__ == "__main__":
    unittest.main()
