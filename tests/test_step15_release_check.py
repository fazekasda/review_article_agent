#!/usr/bin/env python3
"""Unit regressions for ``scripts/release_check.py`` (Step 15, AC8).

These test the pure secret-scan functions on small *synthetic* inputs: a
synthetic canary is detected, clean text is ignored, each high-confidence
pattern is detected, obvious placeholders and ``.env.example`` are exempt, and a
candidate secret is never echoed verbatim. They make no network call and read no
real credential.

The full clean-environment venv install + console-script recovery run is
expensive and may need the network (setuptools), so it is gated behind the
environment variable ``RAA_RELEASE_CHECK=1`` and skipped otherwise.

Synthetic secrets are assembled by concatenation so this very file, which the
tracker scans under ``tests/``, contains no contiguous high-confidence pattern.

Run::

    PYTHONPATH=src python3 -m unittest tests.test_step15_release_check -v
    RAA_RELEASE_CHECK=1 PYTHONPATH=src python3 -m unittest tests.test_step15_release_check -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import release_check as rc  # noqa: E402


def _canary() -> str:
    return "sk-canary-" + "z" * 24


def _openai_key() -> str:
    return "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2"


def _github_pat() -> str:
    return "ghp_" + "a" * 36


def _aws_key() -> str:
    return "AKIA" + "1234567890ABCDEF"


def _private_key_block() -> str:
    return "-----BEGIN RSA " + "PRIVATE KEY-----"


class FindCanariesTests(unittest.TestCase):
    def test_detects_canary(self) -> None:
        canary = _canary()
        text = f"log line\nAPI key = {canary}\nmore"
        self.assertEqual(rc.find_canaries(text, [canary]), [canary])

    def test_ignores_clean_text(self) -> None:
        self.assertEqual(rc.find_canaries("no secrets here", [_canary()]), [])

    def test_multiple_canaries(self) -> None:
        a, b = _canary(), _canary() + "2"
        found = rc.find_canaries(f"{a} and {b}", [a, b, "absent"])
        self.assertEqual(found, [a, b])


class FindSecretPatternsTests(unittest.TestCase):
    def test_detects_each_pattern(self) -> None:
        text = "\n".join([_openai_key(), _github_pat(), _aws_key(), _private_key_block()])
        kinds = {f["kind"] for f in rc.find_secret_patterns(text, path="f.txt")}
        self.assertEqual(
            kinds,
            {"openai_style_key", "github_pat", "aws_access_key_id", "private_key_block"},
        )

    def test_ignores_clean_text(self) -> None:
        self.assertEqual(rc.find_secret_patterns("planted tree, clean prose", path="f.txt"), [])

    def test_redacts_match(self) -> None:
        findings = rc.find_secret_patterns(_openai_key(), path="f.txt")
        self.assertEqual(len(findings), 1)
        self.assertNotIn(_openai_key(), findings[0]["match"])
        self.assertIn("redacted", findings[0]["match"])

    def test_line_number(self) -> None:
        text = "first\nsecond " + _aws_key()
        findings = rc.find_secret_patterns(text, path="f.txt")
        self.assertEqual(findings[0]["line"], 2)

    def test_placeholder_ignored(self) -> None:
        self.assertEqual(rc.find_secret_patterns("sk-your-example-xxxxxxxxxxxxxxxxxx", path="f.txt"), [])

    def test_env_example_exempt(self) -> None:
        self.assertEqual(rc.find_secret_patterns(_openai_key(), path=".env.example"), [])


class ScanTextTests(unittest.TestCase):
    def test_combined_canary_and_pattern(self) -> None:
        canary = _canary()
        text = f"{canary}\n{_github_pat()}"
        findings = rc.scan_text(text, canaries=[canary], patterns=True, path="x")
        kinds = sorted(f["kind"] for f in findings)
        self.assertEqual(kinds, ["canary", "github_pat"])

    def test_canary_only_when_patterns_disabled(self) -> None:
        canary = _canary()
        findings = rc.scan_text(f"{canary}\n{_github_pat()}", canaries=[canary], path="x")
        self.assertEqual([f["kind"] for f in findings], ["canary"])


class ScanDirectoryTests(unittest.TestCase):
    def test_scan_directory_finds_canary(self) -> None:
        import tempfile

        canary = _canary()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "store.sqlite3").write_bytes(b"\x00\x01" + canary.encode() + b"\xff")
            (root / "clean.txt").write_text("nothing", encoding="utf-8")
            findings, files = rc.scan_directory(root, canaries=[canary])
        self.assertEqual(files, 2)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["kind"], "canary")
        self.assertEqual(findings[0]["canary"], canary)


@unittest.skipUnless(
    os.environ.get("RAA_RELEASE_CHECK") == "1",
    "set RAA_RELEASE_CHECK=1 to run the full clean-environment release check",
)
class FullReleaseCheckTests(unittest.TestCase):
    def test_release_check_passes(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "release_check.py")],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=1200,
        )
        self.assertEqual(proc.returncode, 0, f"stdout={proc.stdout}\nstderr={proc.stderr}")
        summary = json.loads(proc.stdout)
        self.assertTrue(summary["ok"])
        self.assertTrue(summary["install"]["ok"])
        self.assertTrue(summary["secret_scan"]["ok"])


if __name__ == "__main__":
    unittest.main()
