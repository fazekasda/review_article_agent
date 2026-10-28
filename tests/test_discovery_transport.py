"""Deterministic tests for the bounded literature GET transport (Step 05 slice 1).

Covers ``src/review_article_agent/discovery/transport.py`` against the task T1
acceptance criteria:

* only the fixed allowlisted HTTPS origins/paths are accepted;
* redirects are refused and no proxy is ever used; no credential is sent;
* malformed numeric options (``bool``/``NaN``/``inf``/out-of-range) are refused;
* safe transient statuses (429/5xx) and network failures retry with explicit
  attempts, while permanent statuses never retry;
* a ``Retry-After`` beyond the bound fails instead of retrying early;
* an oversize body is an explicit failure (no hidden truncation);
* diagnostics are static (never peer text) and a failure exposes no body;
* attempt provenance is immutable and records the exact URL/status/digest/bytes.

No network, no credential, no paid call: the low-level transport, clock, and
sleep are always local fixtures. Run:

    PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_discovery_*.py'
"""

from __future__ import annotations

import dataclasses
import hashlib
import http.client
import io
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.discovery import transport  # noqa: E402

ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


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
        self.calls: list[tuple[str, float, int]] = []

    def __call__(self, url, *, timeout, max_response_bytes):
        self.calls.append((url, timeout, max_response_bytes))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def response(status: int, body: bytes = b"", headers=None, truncated: bool = False):
    return transport.TransportResponse(
        status=status, body=body, headers=headers or {}, truncated=truncated
    )


def make_client(script, **overrides):
    clock = FakeClock()
    sleep = FakeSleep(clock)
    fake = FakeTransport(script)
    kwargs = dict(
        sleep=sleep,
        clock=clock,
        utcnow=lambda: "2026-10-28T00:00:00+00:00",
        transport=fake,
        rate_spacing=0.0,
    )
    kwargs.update(overrides)
    client = transport.DiscoveryTransport(**kwargs)
    return client, fake, sleep, clock


# --------------------------------------------------------------------------- #
# Allowlist and URL building
# --------------------------------------------------------------------------- #
class AllowlistTests(unittest.TestCase):
    def test_every_allowlisted_endpoint_is_accepted(self):
        self.assertEqual(set(transport.ALLOWED_ENDPOINTS), {ESEARCH, EFETCH, EPMC})
        for endpoint in transport.ALLOWED_ENDPOINTS:
            with self.subTest(endpoint=endpoint):
                built = transport.build_request_url(endpoint, {"term": "x"})
                self.assertTrue(built.startswith(endpoint + "?"))

    def test_params_are_encoded_sorted_and_exact(self):
        built = transport.build_request_url(
            ESEARCH, {"db": "pubmed", "term": "cancer therapy", "retmax": 20}
        )
        self.assertEqual(
            built,
            ESEARCH + "?db=pubmed&retmax=20&term=cancer+therapy",
        )

    def test_url_query_used_verbatim_when_params_absent(self):
        url = EPMC + "?query=aspirin&format=json"
        self.assertEqual(transport.build_request_url(url), url)

    def test_non_allowlisted_urls_are_refused(self):
        bad = [
            "http://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
            "https://evil.example.com/entrez/eutils/esearch.fcgi",
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/other.fcgi",
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi:8443",
            "https://user:pass@eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi#frag",
            "https://www.ebi.ac.uk/europepmc/webservices/rest/search/extra",
        ]
        for url in bad:
            with self.subTest(url=url):
                with self.assertRaises(transport.DiscoveryEndpointError):
                    transport.build_request_url(url)

    def test_credentials_in_params_are_refused(self):
        for key in ("api_key", "apikey", "token", "secret", "password", "auth"):
            with self.subTest(key=key):
                with self.assertRaises(transport.DiscoveryValidationError):
                    transport.build_request_url(ESEARCH, {key: "leak"})

    def test_credentials_in_url_query_are_refused(self):
        with self.assertRaises(transport.DiscoveryValidationError):
            transport.build_request_url(ESEARCH + "?api_key=leak")

    def test_url_query_with_params_is_ambiguous(self):
        with self.assertRaises(transport.DiscoveryValidationError):
            transport.build_request_url(ESEARCH + "?term=x", {"term": "y"})

    def test_non_scalar_param_values_are_refused(self):
        with self.assertRaises(transport.DiscoveryValidationError):
            transport.build_request_url(ESEARCH, {"term": ["a", "b"]})
        with self.assertRaises(transport.DiscoveryValidationError):
            transport.build_request_url(ESEARCH, {"term": True})


# --------------------------------------------------------------------------- #
# Malformed bounds
# --------------------------------------------------------------------------- #
class BoundTests(unittest.TestCase):
    def test_timeout_bounds(self):
        for value in (True, False, "10", float("nan"), float("inf"), 0.0, 1e9):
            with self.subTest(value=value):
                with self.assertRaises(transport.DiscoveryValidationError):
                    transport.DiscoveryTransport(timeout=value)

    def test_response_byte_bounds(self):
        for value in (True, 0, 10, transport.MAX_RESPONSE_BYTES + 1, 1.5):
            with self.subTest(value=value):
                with self.assertRaises(transport.DiscoveryValidationError):
                    transport.DiscoveryTransport(max_response_bytes=value)

    def test_attempt_bounds(self):
        for value in (True, 0, transport.MAX_ATTEMPTS + 1, 2.0):
            with self.subTest(value=value):
                with self.assertRaises(transport.DiscoveryValidationError):
                    transport.DiscoveryTransport(max_attempts=value)

    def test_retry_delay_bounds(self):
        for value in (True, float("nan"), -1.0, transport.MAX_RETRY_DELAY_SECONDS + 1):
            with self.subTest(value=value):
                with self.assertRaises(transport.DiscoveryValidationError):
                    transport.DiscoveryTransport(retry_delay=value)

    def test_max_retry_delay_must_cover_retry_delay(self):
        with self.assertRaises(transport.DiscoveryValidationError):
            transport.DiscoveryTransport(retry_delay=5.0, max_retry_delay=1.0)

    def test_rate_spacing_bounds(self):
        for value in (True, -0.1, transport.MAX_RATE_SPACING_SECONDS + 1):
            with self.subTest(value=value):
                with self.assertRaises(transport.DiscoveryValidationError):
                    transport.DiscoveryTransport(rate_spacing=value)

    def test_injectables_must_be_callable(self):
        with self.assertRaises(transport.DiscoveryValidationError):
            transport.DiscoveryTransport(sleep=object())
        with self.assertRaises(transport.DiscoveryValidationError):
            transport.DiscoveryTransport(transport=object())


# --------------------------------------------------------------------------- #
# Retries, retry-after, permanent errors
# --------------------------------------------------------------------------- #
class RetryTests(unittest.TestCase):
    def test_transient_status_retries_then_succeeds(self):
        client, fake, sleep, _ = make_client(
            [response(503), response(200, b'{"ok":true}')]
        )
        result = client.get(ESEARCH, {"term": "x"})
        self.assertTrue(result.ok)
        self.assertEqual(result.attempt_count, 2)
        self.assertEqual(result.status, 200)
        self.assertEqual(result.body, b'{"ok":true}')
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual([a.outcome for a in result.attempts], ["retryable", "ok"])
        self.assertEqual(sleep.calls, [1.0])  # default retry delay

    def test_network_error_retries_then_fails(self):
        client, fake, sleep, _ = make_client(
            [
                transport.TransportNetworkError("boom"),
                transport.TransportNetworkError("boom"),
                transport.TransportNetworkError("boom"),
            ]
        )
        result = client.get(ESEARCH, {"term": "x"})
        self.assertFalse(result.ok)
        self.assertEqual(result.failure, "network_error")
        self.assertEqual(result.attempt_count, 3)
        self.assertEqual(len(fake.calls), 3)
        self.assertEqual(sleep.calls, [1.0, 1.0])

    def test_attempts_exhausted_on_persistent_transient(self):
        client, _, _, _ = make_client([response(500), response(500), response(500)])
        result = client.get(ESEARCH, {"term": "x"})
        self.assertFalse(result.ok)
        self.assertEqual(result.failure, "attempts_exhausted")
        self.assertEqual(result.attempt_count, 3)

    def test_permanent_status_never_retries(self):
        client, fake, sleep, _ = make_client([response(404, b"not found")])
        result = client.get(ESEARCH, {"term": "x"})
        self.assertFalse(result.ok)
        self.assertEqual(result.failure, "http_status")
        self.assertEqual(result.status, 404)
        self.assertEqual(result.attempt_count, 1)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(sleep.calls, [])

    def test_retry_after_within_bound_waits_at_least_that_long(self):
        client, _, sleep, _ = make_client(
            [response(429, headers={"Retry-After": "2"}), response(200, b"ok")],
            retry_delay=1.0,
            max_retry_delay=5.0,
        )
        result = client.get(ESEARCH, {"term": "x"})
        self.assertTrue(result.ok)
        self.assertEqual(sleep.calls, [2.0])
        self.assertEqual(result.attempts[0].retry_after_seconds, 2.0)

    def test_retry_after_exceeding_bound_fails_without_retry(self):
        client, fake, sleep, _ = make_client(
            [response(429, headers={"Retry-After": "999"})],
            retry_delay=1.0,
            max_retry_delay=5.0,
        )
        result = client.get(ESEARCH, {"term": "x"})
        self.assertFalse(result.ok)
        self.assertEqual(result.failure, "retry_after_exceeds_bound")
        self.assertEqual(result.attempt_count, 1)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(sleep.calls, [])

    def test_retry_after_http_date_is_parsed(self):
        client, _, sleep, _ = make_client(
            [
                response(429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}),
                response(200, b"ok"),
            ],
            retry_delay=1.0,
            max_retry_delay=5.0,
        )
        result = client.get(ESEARCH, {"term": "x"})
        self.assertTrue(result.ok)
        # A past HTTP-date clamps to 0, so the fallback retry delay is used.
        self.assertEqual(sleep.calls, [1.0])


# --------------------------------------------------------------------------- #
# Byte cap and sanitized diagnostics
# --------------------------------------------------------------------------- #
class SizeAndDiagnosticsTests(unittest.TestCase):
    def test_oversize_body_is_an_explicit_failure(self):
        client, _, _, _ = make_client(
            [response(200, b"x" * 10, truncated=True)]
        )
        result = client.get(ESEARCH, {"term": "x"})
        self.assertFalse(result.ok)
        self.assertEqual(result.failure, "response_too_large")
        self.assertEqual(result.body, b"")
        self.assertEqual(result.attempts[0].outcome, "too_large")

    def test_failure_does_not_leak_error_body(self):
        secret = b"<error>internal secret token=abc</error>"
        client, _, _, _ = make_client([response(403, secret)])
        result = client.get(ESEARCH, {"term": "x"})
        self.assertFalse(result.ok)
        self.assertEqual(result.body, b"")
        self.assertNotIn("secret", result.diagnostic or "")
        self.assertNotIn("abc", result.diagnostic or "")
        # The digest/bytes are retained as provenance, but not the body.
        self.assertEqual(result.attempts[0].body_bytes, len(secret))
        self.assertEqual(
            result.attempts[0].body_sha256, hashlib.sha256(secret).hexdigest()
        )


# --------------------------------------------------------------------------- #
# Provenance and immutability
# --------------------------------------------------------------------------- #
class ProvenanceTests(unittest.TestCase):
    def test_success_provenance_is_exact_and_immutable(self):
        body = b'{"esearchresult":{"count":1}}'
        client, _, _, _ = make_client([response(200, body)])
        result = client.get(ESEARCH, {"term": "cancer", "db": "pubmed"})
        self.assertTrue(result.ok)
        self.assertEqual(
            result.requested_url,
            ESEARCH + "?db=pubmed&term=cancer",
        )
        self.assertEqual(result.body_sha256, hashlib.sha256(body).hexdigest())
        self.assertEqual(result.body_bytes, len(body))
        self.assertIsInstance(result.attempts, tuple)
        attempt = result.attempts[0]
        self.assertEqual(attempt.requested_url, result.requested_url)
        self.assertEqual(attempt.status, 200)
        self.assertEqual(attempt.outcome, "ok")
        self.assertEqual(attempt.requested_at, "2026-10-28T00:00:00+00:00")
        self.assertEqual(attempt.completed_at, "2026-10-28T00:00:00+00:00")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            attempt.status = 500  # type: ignore[misc]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.ok = False  # type: ignore[misc]

    def test_result_text_decodes_safely(self):
        client, _, _, _ = make_client([response(200, b"caf\xc3\xa9")])
        result = client.get(ESEARCH, {"term": "x"})
        self.assertEqual(result.text(), "café")

    def test_rate_spacing_applies_between_calls(self):
        client, fake, sleep, clock = make_client(
            [response(200, b"a"), response(200, b"b")], rate_spacing=0.5
        )
        client.get(ESEARCH, {"term": "one"})
        client.get(ESEARCH, {"term": "two"})
        self.assertEqual(sleep.calls, [0.5])
        self.assertEqual(len(fake.calls), 2)


# --------------------------------------------------------------------------- #
# Redirect / proxy / credential avoidance
# --------------------------------------------------------------------------- #
class NetworkSafetyTests(unittest.TestCase):
    def test_redirect_handler_refuses_every_3xx(self):
        handler = transport._NoRedirect()
        request = urllib.request.Request(ESEARCH)
        for code in (301, 302, 303, 307, 308):
            with self.subTest(code=code):
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    handler.http_error_301(
                        request, io.BytesIO(b""), code, "moved", {}
                    )
                ctx.exception.close()

    def test_opener_never_consults_environment_proxies(self):
        with mock.patch.object(
            urllib.request,
            "getproxies",
            side_effect=AssertionError("ambient proxies must not be consulted"),
        ):
            opener = transport._http_opener()
        self.assertTrue(
            any(isinstance(h, transport._NoRedirect) for h in opener.handlers)
        )
        for handler in opener.handlers:
            if isinstance(handler, urllib.request.ProxyHandler):
                self.assertEqual(handler.proxies, {})

    def test_http_get_sends_no_credential_and_a_user_agent(self):
        captured = {}

        class FakeResponse:
            status = 200
            headers = {"Content-Type": "application/json"}

            def read(self, n=-1):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class FakeOpener:
            def open(self, request, timeout=None):
                captured["request"] = request
                captured["timeout"] = timeout
                return FakeResponse()

        with mock.patch.object(transport, "_http_opener", return_value=FakeOpener()):
            out = transport.http_get(ESEARCH, timeout=5.0, max_response_bytes=1024)
        self.assertEqual(out.status, 200)
        request = captured["request"]
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.get_header("Authorization"))
        self.assertIsNotNone(request.get_header("User-agent"))
        self.assertEqual(captured["timeout"], 5.0)


# --------------------------------------------------------------------------- #
# http_get boundary enforcement (repair 1)
# --------------------------------------------------------------------------- #
class HttpGetBoundaryTests(unittest.TestCase):
    def test_http_get_rejects_before_opening_a_socket(self):
        bad_cases = [
            (
                dict(url="https://evil.example.com/x", timeout=5.0, max_response_bytes=1024),
                transport.DiscoveryEndpointError,
            ),
            (
                dict(url="http://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                     timeout=5.0, max_response_bytes=1024),
                transport.DiscoveryEndpointError,
            ),
            (
                dict(url=ESEARCH + "?api_key=leak", timeout=5.0, max_response_bytes=1024),
                transport.DiscoveryValidationError,
            ),
            (
                dict(url=ESEARCH, timeout=True, max_response_bytes=1024),
                transport.DiscoveryValidationError,
            ),
            (
                dict(url=ESEARCH, timeout=float("nan"), max_response_bytes=1024),
                transport.DiscoveryValidationError,
            ),
            (
                dict(url=ESEARCH, timeout=1e9, max_response_bytes=1024),
                transport.DiscoveryValidationError,
            ),
            (
                dict(url=ESEARCH, timeout=5.0, max_response_bytes=True),
                transport.DiscoveryValidationError,
            ),
            (
                dict(url=ESEARCH, timeout=5.0, max_response_bytes=1),
                transport.DiscoveryValidationError,
            ),
        ]
        with mock.patch.object(transport, "_http_opener") as opener_mock:
            for kwargs, exc in bad_cases:
                with self.subTest(kwargs=kwargs):
                    with self.assertRaises(exc):
                        transport.http_get(**kwargs)
            opener_mock.assert_not_called()


# --------------------------------------------------------------------------- #
# Retry-After overflow (repair 2)
# --------------------------------------------------------------------------- #
class RetryAfterOverflowTests(unittest.TestCase):
    def test_parse_retry_after_huge_and_malformed(self):
        huge = transport._parse_retry_after({"retry-after": "9" * 400})
        self.assertIsNotNone(huge)
        self.assertGreater(huge, transport.MAX_RETRY_DELAY_SECONDS)
        self.assertEqual(transport._parse_retry_after({"retry-after": "3"}), 3.0)
        self.assertIsNone(transport._parse_retry_after({"retry-after": "not a date"}))
        self.assertIsNone(transport._parse_retry_after({"retry-after": "-5"}))
        self.assertIsNone(transport._parse_retry_after({}))

    def test_parse_retry_after_beyond_int_digit_limit_is_not_ignored(self):
        # Python's int() digit limit is ~4300; a longer decimal must not be
        # silently dropped (which would let a retry happen sooner than asked).
        for digits in (4301, 5000, 20000):
            with self.subTest(digits=digits):
                value = transport._parse_retry_after({"retry-after": "9" * digits})
                self.assertIsNotNone(value)
                self.assertGreater(value, transport.MAX_RETRY_DELAY_SECONDS)

    def test_parse_retry_after_leading_zeros_and_zero(self):
        self.assertEqual(
            transport._parse_retry_after(
                {"retry-after": "0" * 5000 + "5"}
            ),
            5.0,
        )
        self.assertEqual(
            transport._parse_retry_after({"retry-after": "0" * 5000}), 0.0
        )
        self.assertEqual(transport._parse_retry_after({"retry-after": "+7"}), 7.0)

    def test_parse_retry_after_negative_and_malformed_are_conservative(self):
        self.assertIsNone(transport._parse_retry_after({"retry-after": "-5"}))
        self.assertIsNone(transport._parse_retry_after({"retry-after": "12.5"}))
        self.assertIsNone(transport._parse_retry_after({"retry-after": "9" * 5000 + "x"}))
        self.assertIsNone(transport._parse_retry_after({"retry-after": "not a date"}))

    def test_huge_retry_after_is_a_bounded_failure(self):
        client, fake, sleep, _ = make_client(
            [response(429, headers={"Retry-After": "9" * 400})],
            retry_delay=1.0,
            max_retry_delay=5.0,
        )
        result = client.get(ESEARCH, {"term": "x"})
        self.assertFalse(result.ok)
        self.assertEqual(result.failure, "retry_after_exceeds_bound")
        self.assertEqual(result.attempt_count, 1)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(sleep.calls, [])
        # Provenance stays finite and valid (never an OverflowError/inf).
        self.assertGreater(
            result.attempts[0].retry_after_seconds, transport.MAX_RETRY_DELAY_SECONDS
        )

    def test_gigantic_retry_after_beyond_digit_limit_is_a_bounded_failure(self):
        client, fake, sleep, _ = make_client(
            [response(429, headers={"Retry-After": "9" * 5000})],
            retry_delay=1.0,
            max_retry_delay=5.0,
        )
        result = client.get(ESEARCH, {"term": "x"})
        self.assertFalse(result.ok)
        self.assertEqual(result.failure, "retry_after_exceeds_bound")
        self.assertEqual(result.attempt_count, 1)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(sleep.calls, [])


# --------------------------------------------------------------------------- #
# TransportResponse immutability / cap enforcement (repair 5)
# --------------------------------------------------------------------------- #
class TransportResponseTests(unittest.TestCase):
    def test_copies_body_and_freezes_headers(self):
        body = bytearray(b"abc")
        headers = {"Retry-After": "2"}
        resp = transport.TransportResponse(status=200, body=body, headers=headers)
        body.append(0x21)
        headers["Retry-After"] = "999"
        self.assertIsInstance(resp.body, bytes)
        self.assertEqual(resp.body, b"abc")
        self.assertEqual(resp.headers["Retry-After"], "2")
        with self.assertRaises(TypeError):
            resp.headers["x"] = "y"  # type: ignore[index]

    def test_rejects_bad_status_headers_and_body(self):
        for status in (True, 99, 600, "200"):
            with self.subTest(status=status):
                with self.assertRaises(transport.DiscoveryValidationError):
                    transport.TransportResponse(status=status)
        with self.assertRaises(transport.DiscoveryValidationError):
            transport.TransportResponse(status=200, headers={"a": 1})
        with self.assertRaises(transport.DiscoveryValidationError):
            transport.TransportResponse(status=200, headers=[("a", "b")])
        with self.assertRaises(transport.DiscoveryValidationError):
            transport.TransportResponse(status=200, body="not bytes")

    def test_get_enforces_cap_even_without_truncated_flag(self):
        big = b"x" * (transport.MIN_RESPONSE_BYTES + 10)
        client, _, _, _ = make_client(
            [response(200, big)], max_response_bytes=transport.MIN_RESPONSE_BYTES
        )
        result = client.get(ESEARCH, {"term": "x"})
        self.assertFalse(result.ok)
        self.assertEqual(result.failure, "response_too_large")
        self.assertEqual(result.body, b"")
        self.assertEqual(result.attempts[0].outcome, "too_large")


# --------------------------------------------------------------------------- #
# Broken HTTP connections (repair 2)
# --------------------------------------------------------------------------- #
class _BadStatusOpener:
    def open(self, request, timeout=None):
        raise http.client.BadStatusLine("")


class _BrokenReadResponse:
    status = 200
    headers = {}

    def read(self, n=-1):
        raise http.client.IncompleteRead(b"partial")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _BrokenReadOpener:
    def open(self, request, timeout=None):
        return _BrokenReadResponse()


class _BrokenHTTPError(urllib.error.HTTPError):
    def read(self, n=-1):
        raise http.client.IncompleteRead(b"partial")


class _BrokenErrorOpener:
    def open(self, request, timeout=None):
        raise _BrokenHTTPError(ESEARCH, 500, "boom", {}, None)


class BrokenConnectionTests(unittest.TestCase):
    def test_bad_status_line_becomes_network_error(self):
        with mock.patch.object(transport, "_http_opener", return_value=_BadStatusOpener()):
            with self.assertRaises(transport.TransportNetworkError):
                transport.http_get(ESEARCH, timeout=5.0, max_response_bytes=1024)

    def test_incomplete_read_becomes_network_error(self):
        with mock.patch.object(
            transport, "_http_opener", return_value=_BrokenReadOpener()
        ):
            with self.assertRaises(transport.TransportNetworkError):
                transport.http_get(ESEARCH, timeout=5.0, max_response_bytes=1024)

    def test_wrapper_retries_broken_http_connections(self):
        clock = FakeClock()
        sleep = FakeSleep(clock)
        with mock.patch.object(transport, "_http_opener", return_value=_BadStatusOpener()):
            client = transport.DiscoveryTransport(
                sleep=sleep,
                clock=clock,
                utcnow=lambda: "2026-10-28T00:00:00+00:00",
                rate_spacing=0.0,
            )
            result = client.get(ESEARCH, {"term": "x"})
        self.assertFalse(result.ok)
        self.assertEqual(result.failure, "network_error")
        self.assertEqual(result.attempt_count, 3)
        self.assertEqual([a.outcome for a in result.attempts], ["network_error"] * 3)
        self.assertEqual(sleep.calls, [1.0, 1.0])

    def test_wrapper_retries_incomplete_read(self):
        clock = FakeClock()
        sleep = FakeSleep(clock)
        with mock.patch.object(
            transport, "_http_opener", return_value=_BrokenReadOpener()
        ):
            client = transport.DiscoveryTransport(
                sleep=sleep,
                clock=clock,
                utcnow=lambda: "2026-10-28T00:00:00+00:00",
                rate_spacing=0.0,
            )
            result = client.get(ESEARCH, {"term": "x"})
        self.assertFalse(result.ok)
        self.assertEqual(result.failure, "network_error")
        self.assertEqual(result.attempt_count, 3)

    def test_broken_error_body_returns_bounded_static_status(self):
        with mock.patch.object(
            transport, "_http_opener", return_value=_BrokenErrorOpener()
        ):
            out = transport.http_get(ESEARCH, timeout=5.0, max_response_bytes=1024)
        self.assertEqual(out.status, 500)
        self.assertEqual(out.body, b"")
        self.assertFalse(out.truncated)


if __name__ == "__main__":
    unittest.main()
