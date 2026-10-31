"""Deterministic tests for the official full-text resolution waterfall (S6-R1).

Covers ``src/review_article_agent/acquisition/resolution.py`` and its bounded
transport against the task acceptance criteria:

* the two-step official XML waterfall (Europe PMC ``fullTextXML`` then NCBI PMC
  ``EFetch``) succeeds on the first source, falls back on a first-source 404, and
  falls back past a metadata-only first source;
* HTML/login, metadata-only, malformed XML, oversize, and network failures are
  classified, retained, and never silently accepted as scientific full text;
* an unofficial/publisher locator is never fetched and becomes a manual item;
* a missing/unknown authorization makes **zero** network calls;
* the transport allowlist refuses non-allowlisted hosts/paths/schemes, refuses
  credentials, and refuses redirects;
* results are JSON-safe and carry ``content`` **only** on ``retrieved``.

No network, no credential, no paid call: the low-level transport, clock, and
sleep are always local fixtures. Run:

    PYTHONPATH=src python3 -m unittest discover -s tests -p test_acquisition_resolution.py
"""

from __future__ import annotations

import json
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.acquisition import resolution, transport  # noqa: E402

EPMC_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC3257301/fullTextXML"
EFETCH_URL = (
    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
    "?db=pmc&id=PMC3257301&retmode=xml"
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeSleep:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock.advance(seconds)


class FakeTransport:
    """A scripted low-level transport: each item is a response or an exception."""

    def __init__(self, script: list[object]) -> None:
        self.script = list(script)
        self.calls: list[str] = []

    def __call__(self, url, *, timeout, max_response_bytes):
        self.calls.append(url)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def response(status, body=b"", headers=None, truncated=False):
    return transport.TransportResponse(
        status=status, body=body, headers=headers or {}, truncated=truncated
    )


def xml_response(body, status=200, media="application/xml"):
    return response(status, body, headers={"Content-Type": media})


def jats_fulltext(body=b"<p>Full text body.</p>"):
    return (
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b"<article><front><article-meta><title-group>"
        b"<article-title>A study</article-title></title-group>"
        b"</article-meta></front><body>" + body + b"</body></article>"
    )


def pmc_efetch_fulltext(body=b"<p>Full text body.</p>"):
    return (
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b"<pmc-articleset><article><front><article-meta>"
        b"<article-title>A study</article-title></article-meta></front>"
        b"<body>" + body + b"</body></article></pmc-articleset>"
    )


def jats_metadata_only():
    return (
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b"<article><front><article-meta><title-group>"
        b"<article-title>A study</article-title></title-group>"
        b"</article-meta></front></article>"
    )


HTML_LOGIN = b"<!DOCTYPE html><html><body>Sign in to read</body></html>"


def make_transport(script, **overrides):
    clock = FakeClock()
    sleep = FakeSleep(clock)
    fake = FakeTransport(script)
    kwargs = dict(
        sleep=sleep,
        clock=clock,
        utcnow=lambda: "2026-10-29T00:00:00+00:00",
        transport=fake,
        rate_spacing=0.0,
    )
    kwargs.update(overrides)
    return transport.FullTextTransport(**kwargs), fake


def authorization(**overrides):
    base = {
        "acquisition": {
            "allowed": True,
            "terms_ref": "rights/acq-1",
            "checked_at": "2026-10-29T00:00:00Z",
        },
        "storage": {
            "allowed": True,
            "terms_ref": "rights/store-1",
            "checked_at": "2026-10-29T00:00:00Z",
        },
        "privacy_denied": False,
    }
    base.update(overrides)
    return base


_UNSET = object()


def resolve(script, identifiers=None, auth=_UNSET, **overrides):
    tx, fake = make_transport(script, **overrides)
    result = resolution.resolve_full_text(
        identifiers if identifiers is not None else {"pmcid": "PMC3257301"},
        authorization=authorization() if auth is _UNSET else auth,
        transport=tx,
    )
    return result, fake


# --------------------------------------------------------------------------- #
# URL builders
# --------------------------------------------------------------------------- #
class UrlBuilderTests(unittest.TestCase):
    def test_epmc_fulltext_url_is_exact(self):
        self.assertEqual(
            transport.build_epmc_fulltext_url("PMC3257301"), EPMC_URL
        )

    def test_pmc_efetch_url_is_exact_and_fixed(self):
        self.assertEqual(
            transport.build_pmc_efetch_url("PMC3257301"), EFETCH_URL
        )

    def test_pmcid_validation_refuses_unsafe_values(self):
        for bad in ("", "PMC", "pmc123", "PMC12a", "PMC/1", "PMC 1", "PMC1/../2"):
            with self.subTest(bad=bad):
                with self.assertRaises(transport.FullTextValidationError):
                    transport.validate_pmcid(bad)


# --------------------------------------------------------------------------- #
# Transport allowlist / redirect / credentials
# --------------------------------------------------------------------------- #
class TransportSafetyTests(unittest.TestCase):
    def test_allowlisted_endpoints_are_accepted(self):
        self.assertEqual(set(transport.ALLOWED_ENDPOINTS), {transport.PMC_EFETCH_URL})
        for url in (EPMC_URL, EFETCH_URL):
            with self.subTest(url=url):
                self.assertTrue(transport.build_request_url(url))

    def test_non_allowlisted_urls_are_refused(self):
        bad = [
            "http://www.ebi.ac.uk/europepmc/webservices/rest/PMC1/fullTextXML",
            "https://evil.example.com/europepmc/webservices/rest/PMC1/fullTextXML",
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
            "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC1/fullTextXML:8443",
            "https://user:pass@www.ebi.ac.uk/europepmc/webservices/rest/PMC1/fullTextXML",
            "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC1/fullTextXML#frag",
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi/extra",
        ]
        for url in bad:
            with self.subTest(url=url):
                with self.assertRaises(transport.FullTextEndpointError):
                    transport.build_request_url(url)

    def test_credentials_in_params_are_refused(self):
        for key in ("api_key", "apikey", "token", "secret", "password", "auth"):
            with self.subTest(key=key):
                with self.assertRaises(transport.FullTextValidationError):
                    transport.build_request_url(transport.PMC_EFETCH_URL, {key: "x"})

    def test_redirect_handler_refuses_every_3xx(self):
        handler = transport._NoRedirect()
        request = urllib.request.Request(EPMC_URL)
        for code in (301, 302, 303, 307, 308):
            with self.subTest(code=code):
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    handler.http_error_302(request, None, code, "moved", {})
                self.assertEqual(ctx.exception.code, code)
                ctx.exception.close()

    def test_http_get_refuses_redirect_without_following(self):
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError(
            EPMC_URL, 302, "redirect refused", {}, None
        )
        with mock.patch.object(
            transport.urllib.request, "build_opener", return_value=opener
        ):
            result = transport.http_get(
                EPMC_URL, timeout=5.0, max_response_bytes=4096
            )
        self.assertEqual(result.status, 302)
        opener.open.assert_called_once()

    def test_malformed_numeric_options_are_refused(self):
        for kwargs in (
            {"timeout": float("nan")},
            {"timeout": float("inf")},
            {"timeout": True},
            {"max_response_bytes": 0},
            {"max_attempts": 0},
            {"max_attempts": 99},
            {"rate_spacing": -1.0},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(transport.FullTextValidationError):
                    transport.FullTextTransport(**kwargs)

    def test_constructor_performs_no_io(self):
        # No low-level transport is injected: constructing must not call one.
        tx = transport.FullTextTransport()
        self.assertIsNotNone(tx)
        self.assertEqual(tx.max_attempts, transport.DEFAULT_MAX_ATTEMPTS)


# --------------------------------------------------------------------------- #
# Body classification
# --------------------------------------------------------------------------- #
class ClassifyTests(unittest.TestCase):
    def test_fulltext_metadata_html_malformed_empty(self):
        cases = [
            (jats_fulltext(), "application/xml", "fulltext"),
            (pmc_efetch_fulltext(), "application/xml", "fulltext"),
            (jats_metadata_only(), "application/xml", "metadata_only"),
            (HTML_LOGIN, "text/html", "html"),
            (HTML_LOGIN, "application/xml", "html"),
            (b"<article><body><p>x</p></body>", "application/xml", "malformed"),
            (b"", "application/xml", "empty"),
            (b"   \n  ", "application/xml", "empty"),
            (b"<article><body><p>&x;</p></body></article>", "application/xml", "malformed"),
        ]
        for body, media, expected in cases:
            with self.subTest(media=media, expected=expected):
                self.assertEqual(resolution.classify_body(body, media), expected)


# --------------------------------------------------------------------------- #
# Waterfall
# --------------------------------------------------------------------------- #
class WaterfallTests(unittest.TestCase):
    def test_first_source_success(self):
        result, fake = resolve([xml_response(jats_fulltext())])
        self.assertEqual(result["status"], "retrieved")
        self.assertEqual(result["source"], resolution.EPMC_SOURCE)
        self.assertEqual(result["source_version"], resolution.EPMC_SOURCE_VERSION)
        self.assertEqual(result["origin_locator"], EPMC_URL)
        self.assertEqual(result["media_type"], "application/xml")
        self.assertEqual(result["content_bytes"], len(jats_fulltext()))
        self.assertIsNotNone(result["content"])
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0], EPMC_URL)
        # content decodes back to the exact bytes
        import base64

        self.assertEqual(
            base64.b64decode(result["content"]), jats_fulltext()
        )

    def test_first_404_falls_back_to_efetch(self):
        result, fake = resolve(
            [response(404, b"not in OA"), xml_response(pmc_efetch_fulltext())]
        )
        self.assertEqual(result["status"], "retrieved")
        self.assertEqual(result["source"], resolution.PMC_EFETCH_SOURCE)
        self.assertEqual(result["origin_locator"], EFETCH_URL)
        self.assertEqual(fake.calls, [EPMC_URL, EFETCH_URL])
        self.assertIn(
            {"source": resolution.EPMC_SOURCE, "code": "http_status", "status": 404},
            result["failures"],
        )
        # both attempts are retained (404 permanent + 200 ok)
        statuses = [a["status"] for a in result["attempts"]]
        self.assertEqual(statuses, [404, 200])

    def test_metadata_only_first_source_falls_back(self):
        result, fake = resolve(
            [xml_response(jats_metadata_only()), xml_response(pmc_efetch_fulltext())]
        )
        self.assertEqual(result["status"], "retrieved")
        self.assertEqual(result["source"], resolution.PMC_EFETCH_SOURCE)
        self.assertIn(
            {
                "source": resolution.EPMC_SOURCE,
                "code": "metadata_only",
                "status": 200,
                "media_type": "application/xml",
            },
            result["failures"],
        )

    def test_explicit_failure_matrix(self):
        # One parameterized test: each failing body/transport condition is an
        # explicit ``manual_required`` failure (never silently accepted).  Each
        # former method is kept as a subTest case label.
        def metadata_only_both_sources_is_manual():
            result, _ = resolve(
                [xml_response(jats_metadata_only()), xml_response(jats_metadata_only())]
            )
            self.assertEqual(result["status"], "manual_required")
            self.assertIsNone(result["content"])
            codes = [f["code"] for f in result["failures"]]
            self.assertEqual(codes.count("metadata_only"), 2)

        def html_login_both_sources_is_manual():
            result, _ = resolve(
                [xml_response(HTML_LOGIN, media="text/html"), xml_response(HTML_LOGIN, media="text/html")]
            )
            self.assertEqual(result["status"], "manual_required")
            self.assertIsNone(result["content"])
            codes = [f["code"] for f in result["failures"]]
            self.assertEqual(codes.count("html_login"), 2)

        def oversize_cap_is_explicit_failure():
            result, _ = resolve(
                [response(200, b"x" * 64, truncated=True), response(200, b"x" * 64, truncated=True)]
            )
            self.assertEqual(result["status"], "manual_required")
            codes = [f["code"] for f in result["failures"]]
            self.assertEqual(codes.count("response_too_large"), 2)

        def network_timeout_is_explicit_failure():
            result, _ = resolve(
                [transport.TransportNetworkError("x"), transport.TransportNetworkError("x")],
                max_attempts=1,
            )
            self.assertEqual(result["status"], "manual_required")
            codes = [f["code"] for f in result["failures"]]
            self.assertEqual(codes.count("network_error"), 2)
            outcomes = [a["outcome"] for a in result["attempts"]]
            self.assertEqual(outcomes, ["network_error", "network_error"])

        def malformed_xml_is_explicit_failure():
            result, _ = resolve(
                [
                    xml_response(b"<article><body>oops"),
                    xml_response(b"<article><body>oops"),
                ]
            )
            self.assertEqual(result["status"], "manual_required")
            codes = [f["code"] for f in result["failures"]]
            self.assertEqual(codes.count("malformed_xml"), 2)

        cases = {
            "test_metadata_only_both_sources_is_manual": metadata_only_both_sources_is_manual,
            "test_html_login_both_sources_is_manual": html_login_both_sources_is_manual,
            "test_oversize_cap_is_explicit_failure": oversize_cap_is_explicit_failure,
            "test_network_timeout_is_explicit_failure": network_timeout_is_explicit_failure,
            "test_malformed_xml_is_explicit_failure": malformed_xml_is_explicit_failure,
        }
        for label, check in cases.items():
            with self.subTest(case=label):
                check()

    def test_redirect_status_is_permanent_and_falls_back(self):
        result, fake = resolve(
            [response(302, b"moved", headers={"Location": "https://elsewhere.example/paper"}), xml_response(jats_fulltext())]
        )
        self.assertEqual(result["status"], "retrieved")
        self.assertEqual(result["source"], resolution.PMC_EFETCH_SOURCE)
        # the 302 was never followed to its Location; only the two official URLs
        self.assertEqual(fake.calls, [EPMC_URL, EFETCH_URL])
        for url in fake.calls:
            self.assertNotIn("elsewhere.example", url)
        self.assertEqual(result["attempts"][0]["outcome"], "permanent")
        self.assertEqual(result["attempts"][0]["status"], 302)

    def test_retryable_status_retries_then_falls_back(self):
        result, fake = resolve(
            [response(503), response(503), xml_response(pmc_efetch_fulltext())],
            max_attempts=2,
        )
        self.assertEqual(result["status"], "retrieved")
        self.assertEqual(result["source"], resolution.PMC_EFETCH_SOURCE)
        # two 503 attempts for step 1 (attempts exhausted), then one 200 for step 2
        self.assertEqual(len(fake.calls), 3)
        outcomes = [a["outcome"] for a in result["attempts"]]
        self.assertEqual(outcomes, ["retryable", "retryable", "ok"])


# --------------------------------------------------------------------------- #
# Identifiers / manual queue
# --------------------------------------------------------------------------- #
class IdentifierTests(unittest.TestCase):
    def test_missing_or_invalid_pmcid_is_manual_with_zero_calls(self):
        # One parameterized test: a missing or malformed PMCID becomes a manual
        # item with zero network calls.  Each former method is a subTest label.
        def missing_pmcid_is_manual_with_zero_calls():
            result, fake = resolve([], identifiers={"pmid": "31452104", "doi": "10.1/x"})
            self.assertEqual(result["status"], "manual_required")
            self.assertEqual(fake.calls, [])
            self.assertIn({"code": "missing_pmcid"}, result["failures"])

        def invalid_pmcid_is_manual_with_zero_calls():
            result, fake = resolve([], identifiers={"pmcid": "pmc-not-valid"})
            self.assertEqual(result["status"], "manual_required")
            self.assertEqual(fake.calls, [])
            self.assertIn({"code": "invalid_pmcid"}, result["failures"])

        cases = {
            "test_missing_pmcid_is_manual_with_zero_calls": missing_pmcid_is_manual_with_zero_calls,
            "test_invalid_pmcid_is_manual_with_zero_calls": invalid_pmcid_is_manual_with_zero_calls,
        }
        for label, check in cases.items():
            with self.subTest(case=label):
                check()

    def test_unofficial_locator_is_never_fetched(self):
        result, fake = resolve([], identifiers={"url": "https://sci-hub.example/paper.pdf"})
        self.assertEqual(result["status"], "manual_required")
        self.assertEqual(fake.calls, [])
        self.assertIn({"code": "unofficial_locator"}, result["failures"])
        self.assertTrue(result["unofficial_locator_present"])

    def test_unofficial_locator_alongside_pmcid_is_not_contacted(self):
        result, fake = resolve(
            [xml_response(jats_fulltext())],
            identifiers={"pmcid": "PMC3257301", "publisher_url": "https://paywall.example/x"},
        )
        self.assertEqual(result["status"], "retrieved")
        for url in fake.calls:
            self.assertNotIn("paywall.example", url)

    def test_non_mapping_identifier_is_rejected(self):
        tx, _ = make_transport([])
        with self.assertRaises(ValueError):
            resolution.resolve_full_text(
                "PMC1", authorization=authorization(), transport=tx
            )

    def test_non_string_identifier_value_is_rejected(self):
        tx, _ = make_transport([])
        with self.assertRaises(ValueError):
            resolution.resolve_full_text(
                {"pmcid": 123}, authorization=authorization(), transport=tx
            )


# --------------------------------------------------------------------------- #
# Authorization
# --------------------------------------------------------------------------- #
class AuthorizationTests(unittest.TestCase):
    def test_missing_authorization_blocks_with_zero_calls(self):
        tx, fake = make_transport([])
        result = resolution.resolve_full_text(
            {"pmcid": "PMC3257301"}, authorization=None, transport=tx
        )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(fake.calls, [])
        self.assertIn({"code": "authorization_missing"}, result["failures"])
        self.assertFalse(result["authorization_checked"])

    def test_authorization_block_matrix(self):
        # One parameterized test: each authorization denial blocks with zero
        # network calls.  Each former method is kept as a subTest case label.
        def privacy_denied_blocks_with_zero_calls():
            result, fake = resolve(
                [], auth=authorization(privacy_denied=True)
            )
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(fake.calls, [])
            self.assertIn({"code": "privacy_denied"}, result["failures"])

        def acquisition_not_allowed_blocks():
            auth = authorization()
            auth["acquisition"] = {
                "allowed": False,
                "terms_ref": "r",
                "checked_at": "t",
            }
            result, fake = resolve([], auth=auth)
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(fake.calls, [])
            self.assertIn({"code": "acquisition_not_authorized"}, result["failures"])

        def storage_not_allowed_blocks():
            auth = authorization()
            auth["storage"] = {"allowed": False, "terms_ref": "r", "checked_at": "t"}
            result, fake = resolve([], auth=auth)
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(fake.calls, [])
            self.assertIn({"code": "storage_not_authorized"}, result["failures"])

        def missing_terms_evidence_blocks():
            auth = authorization()
            auth["acquisition"] = {"allowed": True, "terms_ref": "", "checked_at": "t"}
            result, fake = resolve([], auth=auth)
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(fake.calls, [])
            self.assertIn({"code": "terms_evidence_missing"}, result["failures"])

        cases = {
            "test_privacy_denied_blocks_with_zero_calls": privacy_denied_blocks_with_zero_calls,
            "test_acquisition_not_allowed_blocks": acquisition_not_allowed_blocks,
            "test_storage_not_allowed_blocks": storage_not_allowed_blocks,
            "test_missing_terms_evidence_blocks": missing_terms_evidence_blocks,
        }
        for label, check in cases.items():
            with self.subTest(case=label):
                check()

    def test_malformed_authorization_blocks(self):
        for auth in ("nope", {}, {"privacy_denied": "no"}):
            with self.subTest(auth=auth):
                tx, fake = make_transport([])
                result = resolution.resolve_full_text(
                    {"pmcid": "PMC3257301"}, authorization=auth, transport=tx
                )
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(fake.calls, [])

    def test_flat_authorization_form_is_accepted(self):
        auth = {
            "acquisition_allowed": True,
            "storage_allowed": True,
            "privacy_denied": False,
            "terms_ref": "rights/1",
            "checked_at": "2026-10-29T00:00:00Z",
        }
        result, fake = resolve([xml_response(jats_fulltext())], auth=auth)
        self.assertEqual(result["status"], "retrieved")
        self.assertEqual(len(fake.calls), 1)


# --------------------------------------------------------------------------- #
# Result shape
# --------------------------------------------------------------------------- #
class ResultShapeTests(unittest.TestCase):
    def test_every_status_is_json_safe_and_content_only_on_retrieved(self):
        scenarios = [
            ("retrieved", [xml_response(jats_fulltext())], authorization()),
            ("manual_required", [response(404), response(404)], authorization()),
            ("blocked", [], None),
        ]
        for expected_status, script, auth in scenarios:
            with self.subTest(status=expected_status):
                result, _ = resolve(script, auth=auth)
                self.assertEqual(result["status"], expected_status)
                json.dumps(result)  # must not raise
                if expected_status == "retrieved":
                    self.assertIsNotNone(result["content"])
                    self.assertEqual(result["content_encoding"], "base64")
                else:
                    self.assertIsNone(result["content"])
                    self.assertIsNone(result["origin_locator"])
                    self.assertIsNone(result["source"])

    def test_caps_and_limits_are_reported(self):
        result, _ = resolve([xml_response(jats_fulltext())])
        caps = result["caps"]
        self.assertEqual(caps["max_requests"], resolution.MAX_REQUESTS)
        self.assertEqual(caps["max_attempts"], transport.DEFAULT_MAX_ATTEMPTS)
        self.assertEqual(
            caps["max_response_bytes"], transport.DEFAULT_MAX_RESPONSE_BYTES
        )
        self.assertEqual(result["resolver_version"], resolution.RESOLVER_VERSION)

    def test_attempts_retain_exact_url_status_and_digest(self):
        result, _ = resolve([xml_response(jats_fulltext())])
        attempt = result["attempts"][0]
        self.assertEqual(attempt["requested_url"], EPMC_URL)
        self.assertEqual(attempt["status"], 200)
        self.assertEqual(attempt["outcome"], "ok")
        self.assertEqual(attempt["media_type"], "application/xml")
        self.assertIsNotNone(attempt["body_sha256"])
        self.assertEqual(attempt["body_bytes"], len(jats_fulltext()))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
