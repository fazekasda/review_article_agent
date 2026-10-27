"""Unit tests for the strict CLI configuration loader and data-dir resolution.

Synthetic only: the "secret" values here are obviously fake test strings and no
real credential or provider setting is involved.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.cli.config import (  # noqa: E402
    CONFIG_SCHEMA_VERSION,
    ConfigError,
    load_config_file,
    resolve_data_dir,
    validate_config,
)
from review_article_agent.domain.errors import ReviewAgentError  # noqa: E402
from review_article_agent.storage import paths  # noqa: E402


class ValidateConfigTest(unittest.TestCase):
    def test_minimal_valid(self) -> None:
        cfg = validate_config({"schema_version": 1})
        self.assertEqual(cfg, {"schema_version": 1, "data_dir": None})

    def test_valid_with_data_dir(self) -> None:
        cfg = validate_config({"schema_version": 1, "data_dir": "/tmp/x"})
        self.assertEqual(cfg["data_dir"], "/tmp/x")

    def test_config_error_is_review_agent_error(self) -> None:
        self.assertTrue(issubclass(ConfigError, ReviewAgentError))

    def test_non_object_root_rejected(self) -> None:
        for bad in ([], "x", 3, None):
            with self.assertRaises(ConfigError):
                validate_config(bad)

    def test_unknown_key_rejected_without_echoing_value_or_name(self) -> None:
        secret = "SUPER_SECRET_VALUE_DO_NOT_ECHO"
        with self.assertRaises(ConfigError) as cm:
            validate_config({"schema_version": 1, "api_key": secret})
        message = str(cm.exception)
        # Neither the value nor the (possibly secret-bearing) field name leaks.
        self.assertNotIn(secret, message)
        self.assertNotIn("api_key", message)
        self.assertIn("data_dir", message)  # the permitted structural fields are named

    def test_credential_style_keys_rejected(self) -> None:
        for key in ("token", "password", "provider", "model", "budget"):
            with self.assertRaises(ConfigError):
                validate_config({"schema_version": 1, key: "x"})

    def test_newer_schema_version_rejected(self) -> None:
        with self.assertRaises(ConfigError) as cm:
            validate_config({"schema_version": CONFIG_SCHEMA_VERSION + 1})
        self.assertIn("newer", str(cm.exception))

    def test_version_zero_or_negative_rejected(self) -> None:
        for version in (0, -1):
            with self.assertRaises(ConfigError):
                validate_config({"schema_version": version})

    def test_missing_schema_version_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            validate_config({"data_dir": "/tmp/x"})

    def test_non_integer_version_rejected(self) -> None:
        for bad in ("1", 1.0, True):
            with self.assertRaises(ConfigError):
                validate_config({"schema_version": bad})

    def test_bad_data_dir_rejected(self) -> None:
        for bad in ("", "  spaced  ", 5, None):
            with self.assertRaises(ConfigError):
                validate_config({"schema_version": 1, "data_dir": bad})


class LoadConfigFileTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-config-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def write(self, name: str, content: str) -> Path:
        path = self.tmp / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_loads_valid_file(self) -> None:
        path = self.write("c.json", '{"schema_version": 1, "data_dir": "/tmp/d"}')
        self.assertEqual(load_config_file(path)["data_dir"], "/tmp/d")

    def test_malformed_json_is_rejected_without_echo(self) -> None:
        path = self.write("c.json", '{"schema_version": 1, "data_dir": ')
        with self.assertRaises(ConfigError):
            load_config_file(path)

    def test_missing_file_is_actionable(self) -> None:
        with self.assertRaises(ConfigError):
            load_config_file(self.tmp / "nope.json")

    def test_non_utf8_is_rejected(self) -> None:
        path = self.tmp / "c.json"
        path.write_bytes(b'{"schema_version": 1, "data_dir": "\xff\xfe"}')
        with self.assertRaises(ConfigError):
            load_config_file(path)

    def test_oversize_is_rejected(self) -> None:
        from review_article_agent.cli.config import MAX_CONFIG_BYTES

        path = self.tmp / "big.json"
        path.write_text(" " * (MAX_CONFIG_BYTES + 10), encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config_file(path)


class ResolveDataDirTest(unittest.TestCase):
    def test_explicit_wins(self) -> None:
        got = resolve_data_dir("/tmp/explicit", {"data_dir": "/tmp/from-config"})
        self.assertEqual(got, paths.resolve_root("/tmp/explicit"))

    def test_config_beats_default(self) -> None:
        got = resolve_data_dir(None, {"data_dir": "/tmp/from-config"})
        self.assertEqual(got, paths.resolve_root("/tmp/from-config"))

    def test_default_xdg_used_last(self) -> None:
        env = {"XDG_DATA_HOME": "/tmp/xdg-home"}
        got = resolve_data_dir(None, {"data_dir": None}, env)
        self.assertEqual(got, paths.default_data_dir(env))
        self.assertTrue(got.startswith(paths.resolve_root("/tmp/xdg-home")))

    def test_no_env_no_config_uses_home_default(self) -> None:
        got = resolve_data_dir(None, None, {})
        self.assertEqual(got, paths.default_data_dir({}))


class StrictJsonTest(unittest.TestCase):
    """Strict JSON at all levels: duplicates, non-finite, over-deep."""

    def test_duplicate_keys_rejected(self) -> None:
        from review_article_agent.cli.config import StrictJsonError, load_strict_json

        for text in (
            '{"a": 1, "a": 2}',
            '{"outer": {"x": 1, "x": 2}}',
            '[{"k": 1, "k": 1}]',
        ):
            with self.assertRaises(StrictJsonError) as cm:
                load_strict_json(text)
            self.assertEqual(cm.exception.code, "duplicate")

    def test_nonfinite_rejected(self) -> None:
        from review_article_agent.cli.config import StrictJsonError, load_strict_json

        for text in ('{"a": NaN}', '{"a": Infinity}', '{"a": -Infinity}', '{"a": 1e9999}'):
            with self.assertRaises(StrictJsonError) as cm:
                load_strict_json(text)
            self.assertEqual(cm.exception.code, "nonfinite")

    def test_overdeep_rejected(self) -> None:
        from review_article_agent.cli.config import StrictJsonError, load_strict_json

        with self.assertRaises(StrictJsonError) as cm:
            load_strict_json("[" * 5000 + "]" * 5000)
        self.assertEqual(cm.exception.code, "too_deep")

    def test_malformed_rejected(self) -> None:
        from review_article_agent.cli.config import StrictJsonError, load_strict_json

        with self.assertRaises(StrictJsonError) as cm:
            load_strict_json("{not json")
        self.assertEqual(cm.exception.code, "malformed")

    def test_valid_preserves_values(self) -> None:
        from review_article_agent.cli.config import load_strict_json

        self.assertEqual(load_strict_json('{"a": [1, 2.5, "x", true, null]}'),
                         {"a": [1, 2.5, "x", True, None]})

    def test_config_file_duplicate_keys_rejected(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="raa-strict-")
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "c.json"
        path.write_text('{"schema_version": 1, "schema_version": 1}', encoding="utf-8")
        with self.assertRaises(ConfigError) as cm:
            load_config_file(path)
        self.assertIn("duplicate", str(cm.exception))

    def test_config_file_nonfinite_rejected(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="raa-strict-")
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "c.json"
        path.write_text('{"schema_version": 1, "data_dir": Infinity}', encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config_file(path)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
