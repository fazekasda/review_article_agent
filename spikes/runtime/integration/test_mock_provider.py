"""Offline unit tests for the deterministic fixture provider helpers.

These do not start a server or touch the network; they pin the small pure
functions the integration run depends on (prompt extraction and the bounded
delay), so a change that would silently break the fixture's determinism fails
fast, without the real-service run.
"""

from __future__ import annotations

import unittest

import mock_provider


class ExtractTextTests(unittest.TestCase):
    def test_last_user_string_message_wins(self) -> None:
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "second"},
        ]
        self.assertEqual(mock_provider._extract_text(messages), "second")

    def test_list_content_parts_are_joined(self) -> None:
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hello"},
                    {"type": "image", "data": "..."},
                    {"type": "text", "text": "world"},
                ],
            }
        ]
        self.assertEqual(mock_provider._extract_text(messages), "hello\nworld")

    def test_non_list_and_missing_user_yield_empty(self) -> None:
        self.assertEqual(mock_provider._extract_text(None), "")
        self.assertEqual(mock_provider._extract_text([{"role": "system", "content": "x"}]), "")


class DelayTests(unittest.TestCase):
    def test_explicit_delay_is_read(self) -> None:
        self.assertEqual(mock_provider._delay_ms("please DELAY_MS=1234 now"), 1234)

    def test_default_zero_when_absent(self) -> None:
        self.assertEqual(mock_provider._delay_ms("no directive here"), 0)

    def test_delay_is_capped(self) -> None:
        cap = mock_provider.MAX_DELAY_MS
        self.assertEqual(mock_provider._delay_ms(f"DELAY_MS={cap + 5_000}"), cap)

    def test_earliest_directive_wins(self) -> None:
        self.assertEqual(mock_provider._delay_ms("DELAY_MS=100 and DELAY_MS=900"), 100)


if __name__ == "__main__":
    unittest.main()
