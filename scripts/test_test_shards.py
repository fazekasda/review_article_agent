#!/usr/bin/env python3
"""Offline unit regressions for ``scripts/test_shards.py``.

Stdlib-only, offline, no network. Each test builds a throwaway tests directory
with fake ``test_*.py`` modules and, where needed, a ``durations.json``, then
exercises the planner directly and through its CLI.

Run (from anywhere):

    python3 -m unittest discover -s scripts -p 'test_*.py'
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import test_shards as ts  # noqa: E402


def write_modules(tests_dir, names):
    os.makedirs(tests_dir, exist_ok=True)
    for name in names:
        with open(os.path.join(tests_dir, name + ".py"), "w", encoding="utf-8") as fh:
            fh.write("# fake module\n")


def write_durations(path, modules):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"schema": 1, "unit": "seconds", "modules": modules}, fh)


def run_cli(argv):
    """Return (exit_code, stdout, stderr) for a CLI invocation."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = ts.main(argv)
    return code, out.getvalue(), err.getvalue()


class TempCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.tests_dir = os.path.join(self.dir, "tests")

    def durations_path(self):
        return os.path.join(self.tests_dir, "durations.json")


class TestDiscovery(unittest.TestCase):
    def test_only_test_modules_sorted(self):
        with tempfile.TemporaryDirectory() as d:
            write_modules(d, ["test_b", "test_a", "helper", "test_c"])
            with open(os.path.join(d, "not_a_test.py"), "w") as fh:
                fh.write("")
            self.assertEqual(ts.discover_modules(d), ["test_a", "test_b", "test_c"])

    def test_missing_dir_raises(self):
        with self.assertRaises(ts.ShardError):
            ts.discover_modules("/nonexistent/definitely/not/here")


class TestEffectiveDurations(TempCase):
    def test_unknown_uses_median_of_known(self):
        modules = ["test_a", "test_b", "test_c"]
        durations = {"test_a": 10.0, "test_b": 4.0}  # median of known = 7.0
        eff = ts.effective_durations(modules, durations)
        self.assertEqual(eff["test_a"], 10.0)
        self.assertEqual(eff["test_b"], 4.0)
        self.assertEqual(eff["test_c"], 7.0)

    def test_no_known_uses_one(self):
        eff = ts.effective_durations(["test_a", "test_b"], {})
        self.assertEqual(eff, {"test_a": 1.0, "test_b": 1.0})

    def test_stale_entries_ignored(self):
        modules = ["test_a"]
        durations = {"test_a": 3.0, "test_gone": 999.0}
        eff = ts.effective_durations(modules, durations)
        self.assertEqual(eff, {"test_a": 3.0})


class TestAssignment(TempCase):
    def test_lpt_known_example(self):
        # durations 5,4,3,2,1 over 2 shards: LPT yields {a,d,e}=8 and {b,c}=7.
        modules = ["test_e", "test_d", "test_c", "test_b", "test_a"]
        durations = {"test_a": 5.0, "test_b": 4.0, "test_c": 3.0,
                     "test_d": 2.0, "test_e": 1.0}
        shards = ts.assign_shards(modules, durations, 2)
        self.assertEqual(shards[0]["modules"], ["test_a", "test_d", "test_e"])
        self.assertEqual(shards[0]["estimated_seconds"], 8.0)
        self.assertEqual(shards[1]["modules"], ["test_b", "test_c"])
        self.assertEqual(shards[1]["estimated_seconds"], 7.0)

    def test_deterministic(self):
        modules = [f"test_{c}" for c in "abcdefghij"]
        durations = {m: float(i + 1) for i, m in enumerate(modules)}
        first = ts.assign_shards(modules, durations, 3)
        second = ts.assign_shards(list(reversed(modules)), durations, 3)
        self.assertEqual(first, second)

    def test_complete_and_disjoint(self):
        modules = [f"test_{c}" for c in "abcdefghijklm"]
        durations = {m: float(i) for i, m in enumerate(modules)}
        shards = ts.assign_shards(modules, durations, 4)
        seen = [m for s in shards for m in s["modules"]]
        self.assertEqual(sorted(seen), sorted(modules))
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual([s["index"] for s in shards], [0, 1, 2, 3])

    def test_empty_shards_when_more_shards_than_modules(self):
        shards = ts.assign_shards(["test_a", "test_b"], {}, 5)
        self.assertEqual(len(shards), 5)
        self.assertEqual(sum(len(s["modules"]) for s in shards), 2)
        self.assertEqual([len(s["modules"]) for s in shards].count(0), 3)

    def test_nonpositive_shards_rejected(self):
        with self.assertRaises(ts.ShardError):
            ts.assign_shards(["test_a"], {}, 0)
        with self.assertRaises(ts.ShardError):
            ts.assign_shards(["test_a"], {}, -2)


class TestDurationsFile(TempCase):
    def test_missing_file_is_empty(self):
        self.assertEqual(ts.load_durations(self.durations_path()), {})

    def test_invalid_json_rejected(self):
        os.makedirs(self.tests_dir, exist_ok=True)
        with open(self.durations_path(), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        with self.assertRaises(ts.ShardError):
            ts.load_durations(self.durations_path())

    def test_bad_schema_rejected(self):
        os.makedirs(self.tests_dir, exist_ok=True)
        with open(self.durations_path(), "w", encoding="utf-8") as fh:
            json.dump({"schema": 2, "modules": {}}, fh)
        with self.assertRaises(ts.ShardError):
            ts.load_durations(self.durations_path())

    def test_non_numeric_duration_rejected(self):
        os.makedirs(self.tests_dir, exist_ok=True)
        with open(self.durations_path(), "w", encoding="utf-8") as fh:
            json.dump({"schema": 1, "modules": {"test_a": "fast"}}, fh)
        with self.assertRaises(ts.ShardError):
            ts.load_durations(self.durations_path())

    def test_valid_roundtrip(self):
        os.makedirs(self.tests_dir, exist_ok=True)
        write_durations(self.durations_path(), {"test_a": 2.5})
        self.assertEqual(ts.load_durations(self.durations_path()), {"test_a": 2.5})


class TestMerge(TempCase):
    def test_later_files_win_and_sorted_rounded(self):
        a = os.path.join(self.dir, "a.json")
        b = os.path.join(self.dir, "b.json")
        write_durations(a, {"test_x": 1.24, "test_y": 5.0})
        write_durations(b, {"test_x": 9.96, "test_z": 0.04})
        merged = ts.merge_durations([a, b])
        self.assertEqual(merged["test_x"], 9.96)  # later file wins
        self.assertEqual(merged["test_y"], 5.0)
        self.assertEqual(merged["test_z"], 0.04)

        out = os.path.join(self.dir, "out.json")
        ts.write_durations(out, merged)
        with open(out, encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertEqual(payload["schema"], 1)
        self.assertEqual(payload["unit"], "seconds")
        self.assertEqual(payload["modules"], {"test_x": 10.0, "test_y": 5.0,
                                              "test_z": 0.0})
        self.assertEqual(list(payload["modules"]), ["test_x", "test_y", "test_z"])

    def test_missing_merge_input_rejected(self):
        with self.assertRaises(ts.ShardError):
            ts.merge_durations([os.path.join(self.dir, "nope.json")])


class TestCli(TempCase):
    def _fixture(self):
        write_modules(self.tests_dir, ["test_a", "test_b", "test_c", "test_d"])
        write_durations(self.durations_path(),
                        {"test_a": 5.0, "test_b": 4.0, "test_c": 3.0, "test_d": 2.0})

    def test_index_prints_dotted_sorted(self):
        self._fixture()
        code, out, _ = run_cli(["--tests-dir", self.tests_dir,
                                "--shards", "2", "--index", "1"])
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["tests.test_b", "tests.test_c"])

    def test_every_module_in_exactly_one_shard(self):
        self._fixture()
        collected = []
        for i in range(2):
            _, out, _ = run_cli(["--tests-dir", self.tests_dir,
                                 "--shards", "2", "--index", str(i)])
            collected.extend(out.split())
        self.assertEqual(sorted(collected),
                         ["tests.test_a", "tests.test_b",
                          "tests.test_c", "tests.test_d"])

    def test_plan_table(self):
        self._fixture()
        code, out, _ = run_cli(["--tests-dir", self.tests_dir,
                                "--shards", "2", "--plan"])
        self.assertEqual(code, 0)
        self.assertIn("shard", out)
        self.assertIn("seconds", out)
        self.assertIn("total", out)

    def test_plan_json(self):
        self._fixture()
        code, out, _ = run_cli(["--tests-dir", self.tests_dir, "--shards", "2",
                                "--plan", "--format", "json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(len(payload["shards"]), 2)
        self.assertEqual(payload["shards"][0]["index"], 0)
        self.assertIn("estimated_seconds", payload["shards"][0])
        self.assertEqual(payload["shards"][0]["modules"][0], "tests.test_a")

    def test_bad_index_rejected(self):
        self._fixture()
        code, _, err = run_cli(["--tests-dir", self.tests_dir,
                                "--shards", "2", "--index", "2"])
        self.assertEqual(code, 2)
        self.assertIn("--index", err)

    def test_negative_shards_rejected(self):
        self._fixture()
        code, _, err = run_cli(["--tests-dir", self.tests_dir,
                                "--shards", "0", "--index", "0"])
        self.assertEqual(code, 2)
        self.assertIn("--shards", err)

    def test_shards_without_index_rejected(self):
        self._fixture()
        code, _, err = run_cli(["--tests-dir", self.tests_dir, "--shards", "2"])
        self.assertEqual(code, 2)
        self.assertIn("together", err)

    def test_plan_without_shards_rejected(self):
        self._fixture()
        code, _, err = run_cli(["--tests-dir", self.tests_dir, "--plan"])
        self.assertEqual(code, 2)
        self.assertIn("--plan", err)

    def test_no_command_rejected(self):
        self._fixture()
        code, _, err = run_cli(["--tests-dir", self.tests_dir])
        self.assertEqual(code, 2)
        self.assertIn("choose", err)

    def test_merge_cli(self):
        a = os.path.join(self.dir, "a.json")
        b = os.path.join(self.dir, "b.json")
        out = os.path.join(self.dir, "merged.json")
        write_durations(a, {"test_a": 1.0})
        write_durations(b, {"test_b": 2.0})
        code, _, _ = run_cli(["--merge", out, a, b])
        self.assertEqual(code, 0)
        with open(out, encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertEqual(payload["modules"], {"test_a": 1.0, "test_b": 2.0})

    def test_merge_requires_two_paths(self):
        code, _, err = run_cli(["--merge", os.path.join(self.dir, "only.json")])
        self.assertEqual(code, 2)
        self.assertIn("--merge", err)


class TestRealRepo(unittest.TestCase):
    def test_real_plan_balances_without_loss(self):
        # The planner works against the real repo from any cwd.
        modules = ts.discover_modules(ts.default_tests_dir())
        self.assertTrue(modules)
        shards = ts.assign_shards(modules, ts.load_durations(
            os.path.join(ts.default_tests_dir(), "durations.json")), 6)
        seen = [m for s in shards for m in s["modules"]]
        self.assertEqual(sorted(seen), sorted(modules))


if __name__ == "__main__":
    unittest.main()
