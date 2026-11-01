"""Windowed screening: deterministic packing and conservative aggregation.

Covers the S19-SCRWIN-WF addition to ``screening/workflow.py``: the pure
``case_windows``/``window_bodies`` packing (every window fits the Jev request
bound, no text is lost, deterministic) and the ``aggregate_window_decisions``
maximum/retain rule, plus the ``_case`` response forms for multiple windows
(``window_responses`` imports and per-window ``decision_result_refs``).

Run:

    PYTHONPATH=src:tests python3 -m unittest tests.test_screening_windows
"""

from __future__ import annotations

import unittest
from unittest import mock

from test_acquisition_manifest import ManifestBase
import test_acquisition_validation as av

from review_article_agent.domain.records import canonical_json
from review_article_agent.parsing import ParsingRepository
from review_article_agent.parsing.core import NORMALIZATION_VERSION, PARSER_VERSION
from review_article_agent.runtime.jev import MODEL, SAFE_MAX_REQUEST_BYTES, build_request, JevValidationError
from review_article_agent.screening import ScreeningError, ScreeningRepository
from review_article_agent.screening import workflow
from review_article_agent.screening.workflow import (
    _classify_and_observe,
    aggregate_window_decisions,
    case_context,
    case_windows,
    questions,
    window_bodies,
)
from review_article_agent.storage.artifacts import sha256_digest


def config():
    return {"questions": [{"id": "eligible", "instructions": "Does the original text meet the protocol eligibility criteria?",
                           "purpose": "eligibility", "exclude_below": "0.2", "retain_above": "0.8"}],
            "pilot_size": 1, "random_size": 1, "risk_size": 1,
            "acceptance_rule": "No important errors; inspect all pilot sources.",
            "model": MODEL, "parser": PARSER_VERSION, "normalization": NORMALIZATION_VERSION}


def definitions():
    return questions(config())


def response(probability, *, model=MODEL):
    return {"model": model, "answers": {"eligible": {"type": "noul", "noul": probability}},
            "usage": {"input_tokens": 10, "output_tokens": 0}}


def refusal():
    return {"model": MODEL, "answers": {"eligible": {"type": "error", "reason": "refused"}},
            "usage": {"input_tokens": 10, "output_tokens": 0}}


def fake_context(article_text, *, target_passage=None, non_text=False):
    """A minimal ``case_context``-shaped mapping for pure packing tests."""
    return {"article_text": article_text, "target_passage": target_passage,
            "report": {"payload": {"non_text_assets_require_review": non_text}}}


def _request_bytes(text):
    return len(canonical_json(build_request(definitions(), text)))


class WindowPackingTests(unittest.TestCase):
    def test_short_article_is_one_window(self):
        context = fake_context("Hello scientific world.")
        windows = case_windows(definitions(), context)
        self.assertEqual(len(windows), 1)
        self.assertTrue(windows[0].startswith("Article window 1 of 1."))
        self.assertEqual(window_bodies(definitions(), context), ["Hello scientific world."])

    def test_large_article_splits_into_bounded_lossless_windows(self):
        body = ("R\u00e9sum\u00e9 na\u00efve caf\u00e9 \u2014 motivation. " * 40 + "\n\n") * 40
        context = fake_context(body, non_text=True)
        windows = case_windows(definitions(), context)
        self.assertGreater(len(windows), 1)
        for index, window in enumerate(windows, start=1):
            self.assertTrue(window.startswith("Article window %d of %d." % (index, len(windows))))
            self.assertLessEqual(_request_bytes(window), SAFE_MAX_REQUEST_BYTES)
            self.assertIn(workflow.NON_TEXT_MATERIAL_LIMITATION, window)
        # No text is dropped or reordered: the bodies concatenate exactly.
        self.assertEqual("".join(window_bodies(definitions(), context)), body)
        # Deterministic across calls.
        self.assertEqual(case_windows(definitions(), context), windows)

    def test_oversize_single_passage_split_losslessly(self):
        passage = "word " * 20_000  # one passage, no paragraph breaks, over the bound
        context = fake_context(passage)
        bodies = window_bodies(definitions(), context)
        self.assertGreater(len(bodies), 1)
        self.assertEqual("".join(bodies), passage)
        for window in case_windows(definitions(), context):
            self.assertLessEqual(_request_bytes(window), SAFE_MAX_REQUEST_BYTES)

    def test_passage_case_puts_target_in_every_window(self):
        body = ("context sentence about the study. " * 40 + "\n\n") * 40
        context = fake_context(body, target_passage="TARGET PASSAGE BODY")
        windows = case_windows(definitions(), context)
        self.assertGreater(len(windows), 1)
        for window in windows:
            self.assertIn("Target passage: TARGET PASSAGE BODY", window)
        self.assertEqual("".join(window_bodies(definitions(), context)), body)

    def test_no_limitation_line_when_not_flagged(self):
        context = fake_context("short text")
        self.assertNotIn(workflow.NON_TEXT_MATERIAL_LIMITATION, case_windows(definitions(), context)[0])

    def test_oversize_header_region_raises_not_character_windows(self):
        # A target passage so large its "Target passage" header region alone
        # cannot fit one request: raise immediately instead of emitting thousands
        # of one-character windows.
        huge_target = "T" * (SAFE_MAX_REQUEST_BYTES + 1)
        context = fake_context("short body", target_passage=huge_target)
        with self.assertRaises(JevValidationError):
            case_windows(definitions(), context)
        with self.assertRaises(JevValidationError):
            window_bodies(definitions(), context)


class AggregationTests(unittest.TestCase):
    def _machine(self, probability):
        return _classify_and_observe(config(), response(probability))[0]

    def test_maximum_probability_across_windows(self):
        self.assertEqual(
            aggregate_window_decisions(config(), [self._machine(0.1), self._machine(0.5)])["label"],
            "uncertain")
        self.assertEqual(
            aggregate_window_decisions(config(), [self._machine(0.1), self._machine(0.85)])["label"],
            "retain")
        self.assertEqual(
            aggregate_window_decisions(config(), [self._machine(0.1), self._machine(0.1)])["label"],
            "proposed_exclusion")

    def test_one_high_window_retains(self):
        # 0.1 alone would propose an exclusion; the 0.9 window forces a retain.
        self.assertEqual(
            aggregate_window_decisions(config(), [self._machine(0.1), self._machine(0.9)])["label"],
            "retain")

    def test_refusal_in_any_window_never_excludes(self):
        refused = _classify_and_observe(config(), refusal())[0]
        aggregated = aggregate_window_decisions(config(), [self._machine(0.1), refused])
        self.assertEqual(aggregated["label"], "uncertain")
        self.assertIsNone(aggregated["decisions"][0]["probability"])

    def test_empty_window_list_is_uncertain(self):
        self.assertEqual(aggregate_window_decisions(config(), [])["label"], "uncertain")

    def test_not_ready_is_uncertain(self):
        self.assertEqual(
            aggregate_window_decisions(config(), [self._machine(0.1)], context_ready=False)["label"],
            "uncertain")


class CaseWindowFormsTest(ManifestBase):
    """``_case`` handling of multi-window imports and per-window result refs."""

    def setUp(self):
        super().setUp()
        self.screening = ScreeningRepository(self.store)
        _, _, self.source, entries = self.standard()
        self.manifest = self.make(entries)
        self.gate(self.manifest)
        # A large synthetic article: many paragraphs -> several windows.
        paragraph = b"<sec><p>" + b"Alpha beta gamma delta epsilon zeta eta theta. " * 8 + b"</p></sec>"
        jats = av.jats(body=paragraph * 400)
        source = self.put_source(jats)
        self.report = ParsingRepository(self.store).parse_stored_document(
            source, report_artifact_id="big-report", report_revision_id="big-report-r1",
            rights=self.rights("big-report", "big-report-r1"))["report_ref"]

    def _windows(self):
        context = case_context(
            ParsingRepository(self.store), self.report, None, questions=definitions())
        return case_windows(definitions(), context)

    def _digests(self):
        return [sha256_digest(canonical_json(build_request(definitions(), window)))
                for window in self._windows()]

    def _item(self, response_value):
        return {"id": "p1", "report_ref": self.report, "passage_id": None,
                "response": response_value}

    def _ref(self, dispatch_id):
        return {"project_id": self.project_id, "dispatch_id": dispatch_id}

    def test_multi_window_import_needs_window_responses(self):
        count = len(self._windows())
        self.assertGreater(count, 1)
        # A single imported envelope cannot cover a multi-window article.
        with self.assertRaises(ScreeningError):
            self.screening._case(config(), self._item(response(0.1)))

    def test_window_responses_aggregate_conservatively(self):
        count = len(self._windows())
        responses = [response(0.1) for _ in range(count)]
        responses[-1] = response(0.9)
        case = self.screening._case(config(), self._item({"window_responses": responses}))
        self.assertEqual(case["machine"]["label"], "retain")
        self.assertEqual(case["windowing_version"], workflow.WINDOWING_VERSION)
        self.assertEqual(len(case["windows"]), count)
        self.assertEqual(len(case["window_decisions"]), count)
        self.assertEqual(case["response_provenance"], "imported_unverified_dispatch")

    def test_window_responses_wrong_count_refused(self):
        with self.assertRaises(ScreeningError):
            self.screening._case(config(), self._item({"window_responses": [response(0.1)]}))

    def test_mixed_observed_model_refused(self):
        count = len(self._windows())
        responses = [response(0.1) for _ in range(count)]
        responses[-1] = response(0.1, model=MODEL + "-20261014")
        with self.assertRaises(ScreeningError):
            self.screening._case(config(), self._item({"window_responses": responses}))

    def test_decision_result_refs_wrong_count_refused(self):
        count = len(self._windows())
        refs = [self._ref("disp-%d" % index) for index in range(count - 1)]
        with self.assertRaises(ScreeningError):
            self.screening._case(config(), self._item({"decision_result_refs": refs}))

    def test_decision_result_refs_bind_each_window_digest(self):
        digests = self._digests()
        count = len(digests)
        expected = {"disp-%d" % index: digest for index, digest in enumerate(digests)}

        def fake_verify(_config, ref, request_sha256, ready):
            if expected.get(ref["dispatch_id"]) != request_sha256:
                raise ScreeningError("window request digest mismatch")
            return (aggregate_window_decisions(
                _config, [_classify_and_observe(_config, response(0.1))[0]],
                context_ready=ready), MODEL, "verified_dispatch")

        refs = [self._ref("disp-%d" % index) for index in range(count)]
        with mock.patch.object(ScreeningRepository, "_verified_response",
                               staticmethod(fake_verify)):
            case = self.screening._case(config(), self._item({"decision_result_refs": refs}))
        self.assertEqual(case["response_provenance"], "verified_dispatch")
        self.assertEqual(case["machine"]["label"], "proposed_exclusion")

        # A ref whose committed digest is not this window's is refused: shift the
        # expected digests so every passed (correct) digest mismatches its ref.
        shifted = {"disp-%d" % index: digests[(index + 1) % count] for index in range(count)}

        def shifted_verify(_config, ref, request_sha256, ready):
            if shifted.get(ref["dispatch_id"]) != request_sha256:
                raise ScreeningError("window request digest mismatch")
            raise AssertionError("unreachable")  # pragma: no cover

        with mock.patch.object(ScreeningRepository, "_verified_response",
                               staticmethod(shifted_verify)):
            with self.assertRaises(ScreeningError):
                self.screening._case(config(), self._item({"decision_result_refs": refs}))

    def test_decision_result_refs_mixed_model_refused(self):
        count = len(self._windows())
        digests = self._digests()
        models = [MODEL] * (count - 1) + [MODEL + "-20261014"]

        def fake_verify(_config, ref, request_sha256, ready):
            index = int(ref["dispatch_id"].rsplit("-", 1)[1])
            assert digests[index] == request_sha256
            return (aggregate_window_decisions(
                _config, [_classify_and_observe(_config, response(0.1))[0]],
                context_ready=ready), models[index], "verified_dispatch")

        refs = [self._ref("disp-%d" % index) for index in range(count)]
        with mock.patch.object(ScreeningRepository, "_verified_response",
                               staticmethod(fake_verify)):
            with self.assertRaises(ScreeningError):
                self.screening._case(config(), self._item({"decision_result_refs": refs}))

    def test_oversize_header_region_is_not_ready_and_retained(self):
        # A single passage so large the "Target passage" header cannot fit one
        # request: ``case_context`` is not ready and ``_case`` retains the case
        # (a static ``ScreeningError``), never a crash or a silent pass.
        big = b"<sec><p>" + b"word " * 8000 + b"</p></sec>"
        source = self.put_source(av.jats(body=big))
        report = ParsingRepository(self.store).parse_stored_document(
            source, report_artifact_id="huge-report", report_revision_id="huge-report-r1",
            rights=self.rights("huge-report", "huge-report-r1"))["report_ref"]
        rep = ParsingRepository(self.store).read_report(report)
        passage_id = rep["payload"]["passages"][0]["passage_id"]
        context = case_context(
            ParsingRepository(self.store), report, passage_id, questions=definitions())
        self.assertFalse(context["ready"])
        item = {"id": "p1", "report_ref": report, "passage_id": passage_id,
                "response": response(0.1)}
        with self.assertRaises(ScreeningError):
            self.screening._case(config(), item)


if __name__ == "__main__":
    unittest.main()
