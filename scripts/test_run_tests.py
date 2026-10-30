#!/usr/bin/env python3
"""Offline unit regressions for ``scripts/run_tests.py``.

Stdlib-only, offline, no network. The tests build a throwaway *fake* repo (a
``tests/`` directory with tiny passing/failing/skipping/erroring modules) and
drive the runner against it via ``--repo`` / ``--tests-dir`` so the real suite
is never executed. The parser and module-name normalizer are also exercised
directly.

Run (from anywhere):

    python3 -m unittest discover -s scripts -p 'test_*.py'
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import run_tests as rt  # noqa: E402

RUNNER = os.path.join(HERE, "run_tests.py")

FAKE_MODULES = {
    "test_pass": (
        "import unittest\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_ok(self):\n"
        "        self.assertTrue(True)\n"
    ),
    "test_two": (
        "import unittest\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_a(self):\n"
        "        self.assertEqual(1, 1)\n\n"
        "    def test_b(self):\n"
        "        self.assertEqual(2, 2)\n"
    ),
    "test_fail": (
        "import unittest\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_bad(self):\n"
        "        self.assertEqual(1, 2)\n"
    ),
    "test_error": (
        "import unittest\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_err(self):\n"
        "        raise RuntimeError('boom')\n"
    ),
    "test_skip": (
        "import unittest\n\n"
        "class T(unittest.TestCase):\n"
        "    @unittest.skip('skip')\n"
        "    def test_skip(self):\n"
        "        pass\n"
    ),
    "test_slow": (
        "import os\n"
        "import unittest\n\n"
        "class T(unittest.TestCase):\n"
        "    @unittest.skipUnless(os.environ.get('RAA_SLOW_TESTS') == '1',\n"
        "                         'slow tier')\n"
        "    def test_slow(self):\n"
        "        pass\n"
    ),
    "test_sqlite_env": (
        "import os\n"
        "import unittest\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_off_by_default(self):\n"
        "        self.assertEqual(\n"
        "            os.environ.get('RAA_TEST_SQLITE_SYNCHRONOUS'), 'OFF')\n"
    ),
    "test_sqlite_durable": (
        "import os\n"
        "import unittest\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_absent_when_durable(self):\n"
        "        self.assertNotIn('RAA_TEST_SQLITE_SYNCHRONOUS', os.environ)\n"
    ),
    "test_sleep": (
        "import time\n"
        "import unittest\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_sleep(self):\n"
        "        time.sleep(1.5)\n"
    ),
    "test_hang": (
        "import time\n"
        "import unittest\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_hang(self):\n"
        "        time.sleep(30)\n"
    ),
}


class FakeRepoMixin:
    def make_repo(self, modules=("test_pass", "test_two", "test_fail",
                                 "test_error", "test_skip", "test_slow")):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tests_dir = os.path.join(tmp.name, "tests")
        os.makedirs(tests_dir)
        for name in modules:
            with open(os.path.join(tests_dir, name + ".py"), "w",
                      encoding="utf-8") as fh:
                fh.write(FAKE_MODULES[name])
        return tmp.name, tests_dir

    def run_cli(self, repo, *args):
        cmd = [sys.executable, RUNNER, "--repo", repo, *args]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    def summary_skipped(self, stdout):
        for line in stdout.splitlines():
            if "skipped:" in line and "modules:" in line:
                return int(line.split("skipped:", 1)[1].split()[0])
        raise AssertionError(f"no summary skipped line in:\n{stdout}")


class TestParsing(unittest.TestCase):
    def test_ok(self):
        p = rt.parse_output("....\n----\nRan 3 tests in 0.01s\n\nOK\n")
        self.assertEqual(p["tests"], 3)
        self.assertTrue(p["ok"])
        self.assertFalse(p["failed"])
        self.assertEqual(p["skipped"], 0)

    def test_ok_skipped(self):
        p = rt.parse_output("Ran 4 tests in 0.02s\n\nOK (skipped=2)\n")
        self.assertTrue(p["ok"])
        self.assertEqual(p["tests"], 4)
        self.assertEqual(p["skipped"], 2)

    def test_failed_counts(self):
        p = rt.parse_output(
            "Ran 5 tests in 0.03s\n\n"
            "FAILED (failures=1, errors=2, skipped=3)\n"
        )
        self.assertFalse(p["ok"])
        self.assertTrue(p["failed"])
        self.assertEqual(p["tests"], 5)
        self.assertEqual(p["failures"], 1)
        self.assertEqual(p["errors"], 2)
        self.assertEqual(p["skipped"], 3)

    def test_no_summary_is_not_ok(self):
        p = rt.parse_output("Traceback (most recent call last):\nImportError\n")
        self.assertFalse(p["ok"])
        self.assertFalse(p["failed"])


class TestNormalizeModule(unittest.TestCase):
    def test_forms(self):
        for raw in ("tests.test_x", "test_x", "tests/test_x.py", "test_x.py"):
            self.assertEqual(rt.normalize_module(raw), "test_x")


class TestBuildEnv(unittest.TestCase):
    def test_default_sets_sqlite_off(self):
        env = rt.build_env({}, slow=False)
        self.assertEqual(env["RAA_TEST_SQLITE_SYNCHRONOUS"], "OFF")
        self.assertNotIn("RAA_SLOW_TESTS", env)

    def test_slow_keeps_sqlite_off(self):
        env = rt.build_env({}, slow=True)
        self.assertEqual(env["RAA_SLOW_TESTS"], "1")
        self.assertEqual(env["RAA_TEST_SQLITE_SYNCHRONOUS"], "OFF")

    def test_durable_sqlite_removes_variable(self):
        env = rt.build_env(
            {"RAA_TEST_SQLITE_SYNCHRONOUS": "OFF"}, slow=False,
            durable_sqlite=True,
        )
        self.assertNotIn("RAA_TEST_SQLITE_SYNCHRONOUS", env)

    def test_durable_sqlite_overrides_parent_normal(self):
        env = rt.build_env(
            {"RAA_TEST_SQLITE_SYNCHRONOUS": "NORMAL"}, slow=False,
            durable_sqlite=True,
        )
        self.assertNotIn("RAA_TEST_SQLITE_SYNCHRONOUS", env)


class TestFakeRepo(FakeRepoMixin, unittest.TestCase):
    def test_passing_module_exit_zero(self):
        repo, _ = self.make_repo()
        proc = self.run_cli(repo, "test_pass")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PASS", proc.stdout)
        self.assertIn("test_pass", proc.stdout)
        self.assertIn("OK: all modules passed", proc.stdout)

    def test_failing_module_exit_one_and_full_output(self):
        repo, _ = self.make_repo()
        proc = self.run_cli(repo, "test_fail")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("FAIL", proc.stdout)
        self.assertIn("===== FAIL test_fail =====", proc.stdout)
        self.assertIn("FAILED (failures=1)", proc.stdout)

    def test_erroring_module_exit_one(self):
        repo, _ = self.make_repo()
        proc = self.run_cli(repo, "test_error")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("errors: 1", proc.stdout)
        self.assertIn("boom", proc.stdout)

    def test_skipping_module_ok_with_skipped(self):
        repo, _ = self.make_repo()
        proc = self.run_cli(repo, "test_skip")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.summary_skipped(proc.stdout), 1)

    def test_default_discovers_all_modules(self):
        repo, _ = self.make_repo()
        proc = self.run_cli(repo)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)  # has failures
        self.assertIn("modules: 6", proc.stdout)

    def test_unknown_module_exit_two(self):
        repo, _ = self.make_repo()
        proc = self.run_cli(repo, "test_does_not_exist")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("unknown module", proc.stderr)

    def test_module_name_normalization(self):
        repo, _ = self.make_repo()
        proc = self.run_cli(repo, "tests.test_pass", "test_two",
                            "tests/test_skip.py")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("modules: 3", proc.stdout)

    def test_tests_dir_option(self):
        repo, tests_dir = self.make_repo()
        # Point --tests-dir explicitly at the fake tests directory.
        proc = subprocess.run(
            [sys.executable, RUNNER, "--repo", repo,
             "--tests-dir", tests_dir, "test_pass"],
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_durations_json_format(self):
        repo, _ = self.make_repo()
        out = os.path.join(repo, "durations.json")
        proc = self.run_cli(repo, "test_pass", "test_two",
                            "--durations-out", out)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        with open(out, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["schema"], 1)
        self.assertEqual(data["unit"], "seconds")
        self.assertEqual(sorted(data["modules"]), ["test_pass", "test_two"])
        for value in data["modules"].values():
            self.assertIsInstance(value, float)
            self.assertEqual(value, round(value, 1))

    def test_slow_env_propagation(self):
        repo, _ = self.make_repo()
        without = self.run_cli(repo, "test_slow")
        self.assertEqual(without.returncode, 0, without.stdout + without.stderr)
        self.assertEqual(self.summary_skipped(without.stdout), 1)

        with_slow = self.run_cli(repo, "test_slow", "--slow")
        self.assertEqual(with_slow.returncode, 0,
                         with_slow.stdout + with_slow.stderr)
        self.assertEqual(self.summary_skipped(with_slow.stdout), 0)

    def test_sqlite_env_off_by_default(self):
        repo, _ = self.make_repo(("test_sqlite_env",))
        proc = self.run_cli(repo, "test_sqlite_env")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_durable_sqlite_flag_keeps_full(self):
        repo, _ = self.make_repo(("test_sqlite_durable",))
        # Without the flag the child gets OFF, so "absent" must fail.
        default = self.run_cli(repo, "test_sqlite_durable")
        self.assertEqual(default.returncode, 1,
                         default.stdout + default.stderr)
        # With --durable-sqlite the variable is removed -> production FULL.
        durable = self.run_cli(repo, "test_sqlite_durable", "--durable-sqlite")
        self.assertEqual(durable.returncode, 0,
                         durable.stdout + durable.stderr)

    def test_budget_overrun_exit_one(self):
        repo, _ = self.make_repo(("test_sleep",))
        proc = self.run_cli(repo, "test_sleep", "--budget", "0.05")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("budget exceeded", proc.stdout)

    def test_budget_within_passes(self):
        repo, _ = self.make_repo()
        proc = self.run_cli(repo, "test_pass", "--budget", "60")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_timeout_marks_fail(self):
        repo, _ = self.make_repo(("test_hang",))
        proc = self.run_cli(repo, "test_hang", "--timeout", "0.5")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("TIMEOUT", proc.stdout)

    def test_top_lists_slowest(self):
        repo, _ = self.make_repo(("test_sleep", "test_pass"))
        proc = self.run_cli(repo, "test_sleep", "test_pass", "--top", "1")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("slowest 1:", proc.stdout)
        self.assertIn("test_sleep", proc.stdout)

    def test_top_zero_disables(self):
        repo, _ = self.make_repo()
        proc = self.run_cli(repo, "test_pass", "--top", "0")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("slowest", proc.stdout)

    def test_jobs_zero_is_usage_error(self):
        repo, _ = self.make_repo()
        proc = self.run_cli(repo, "test_pass", "-j", "0")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
