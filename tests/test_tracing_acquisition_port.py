"""Focused tests: the Step 13 acquisition port over real acquired sources.

Covers ``src/review_article_agent/tracing/acquisition_port.py`` against
``docs/implementation/13-origin-tracing.md`` (new upstream candidates follow the
existing acquisition/disposition rules; no automatic unofficial acquisition) and
the kickoff 16 §8 wiring: ``acquire(request)`` resolves a requested upstream
reference against the project's **already acquired** sources only --
``acquired`` when the reference maps to an acquired candidate whose source has a
valid parsed report, ``pending_disposition`` when the reference is known but not
acquired/parsed, and ``unavailable`` otherwise.  No network is ever used.

The acquired manifest + parsed JATS report are the real Step 06/07 modules over a
real disposable ``ProjectStore`` (fixtures inherited from
:mod:`test_acquisition_manifest`).  No model, network, provider, or OpenCode
session call is made.

Run::

    PYTHONPATH=src:tests python3 -m unittest tests.test_tracing_acquisition_port -v
"""

from __future__ import annotations

import socket
import sys
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

import test_acquisition_manifest as acq  # noqa: E402

from review_article_agent.parsing.repository import ParsingRepository  # noqa: E402
from review_article_agent.tracing import acquisition_port as port_mod  # noqa: E402
from review_article_agent.tracing.acquisition_port import make_acquire_port  # noqa: E402


class AcquirePortBase(acq.ManifestBase):
    """A real frozen roster + JATS source, with an optional gated manifest/report."""

    def _gated_manifest_with_report(self, *, parse=True):
        _a, _b, source, entries = self.standard()
        manifest = self.make(entries)
        self.gate(manifest)
        report = None
        if parse:
            report = ParsingRepository(self.store).parse_stored_document(
                source,
                report_artifact_id="src-rep",
                report_revision_id="src-rep-r1",
                rights=self.rights("src-rep", "src-rep-r1"),
            )
        return source, manifest, report

    @staticmethod
    def _request(*, pmid=None, doi=None, ref_id="r1"):
        identifiers = {}
        if pmid is not None:
            identifiers["pmid"] = pmid
        if doi is not None:
            identifiers["doi"] = doi
        return {"ref_id": ref_id, "identifiers": identifiers}


class AcquiredTests(AcquirePortBase):
    def test_acquired_source_is_returned_from_the_parsed_report(self):
        source, _manifest, report = self._gated_manifest_with_report()
        acquire = make_acquire_port(self.store)

        response = acquire(self._request(pmid="111"))
        self.assertEqual(response["status"], "acquired")
        source_dict = response["source"]
        # Exact tracing-source shape.
        self.assertEqual(
            set(source_dict), {"source_key", "source", "passages", "references", "citation_links"}
        )
        self.assertEqual(source_dict["source_key"], source["artifact_id"])
        # The acquired asset sha is the parsed report's source digest.
        read = ParsingRepository(self.store).read_report(report["report_ref"])
        self.assertEqual(source_dict["source"]["sha256"], read["payload"]["source"]["sha256"])
        self.assertTrue(source_dict["passages"])

    def test_unknown_reference_is_unavailable(self):
        self._gated_manifest_with_report()
        acquire = make_acquire_port(self.store)
        self.assertEqual(acquire(self._request(pmid="999"))["status"], "unavailable")
        self.assertEqual(acquire(self._request(doi="10.9999/nope"))["status"], "unavailable")

    def test_missing_identifiers_is_unavailable(self):
        self._gated_manifest_with_report()
        acquire = make_acquire_port(self.store)
        self.assertEqual(acquire({"ref_id": "r1", "identifiers": {}})["status"], "unavailable")


class PendingDispositionTests(AcquirePortBase):
    def test_known_but_not_gated_is_pending(self):
        # A manifest entry exists for pmid 111 but the acquisition gate is not
        # satisfied, so the reference is known-but-not-acquired.
        _a, _b, _source, entries = self.standard()
        self.make(entries)  # proposed, never gated
        acquire = make_acquire_port(self.store)
        self.assertEqual(acquire(self._request(pmid="111"))["status"], "pending_disposition")

    def test_gated_but_not_parsed_is_pending(self):
        self._gated_manifest_with_report(parse=False)
        acquire = make_acquire_port(self.store)
        self.assertEqual(acquire(self._request(pmid="111"))["status"], "pending_disposition")

    def test_no_manifest_at_all_is_unavailable(self):
        self.setup_frozen()
        acquire = make_acquire_port(self.store)
        self.assertEqual(acquire(self._request(pmid="111"))["status"], "unavailable")


class NoNetworkTests(AcquirePortBase):
    def test_port_never_uses_the_network(self):
        self._gated_manifest_with_report()
        acquire = make_acquire_port(self.store)
        with mock.patch.object(socket, "socket", side_effect=AssertionError("network")):
            with mock.patch.object(socket, "create_connection", side_effect=AssertionError("network")):
                self.assertEqual(acquire(self._request(pmid="111"))["status"], "acquired")
                self.assertEqual(acquire(self._request(pmid="999"))["status"], "unavailable")

    def test_module_has_no_network_imports(self):
        names = set(port_mod.__dict__)
        for forbidden in ("socket", "urllib", "http", "ssl", "transport"):
            self.assertNotIn(forbidden, names)


if __name__ == "__main__":
    unittest.main()
