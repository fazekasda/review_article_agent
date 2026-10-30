"""Test-tier helper: the ``slow`` gate (Step 16, kickoff §4).

The suite has two tiers. The **fast** tier is the default local command and CI
``fast``; the **slow** tier holds tests that spawn real subprocess CLI runs, a
real detached worker, the mock OpenCode service, SIGKILL/recovery matrices,
scale runs, and clean-venv installs, plus otherwise expensive tests.

Mirrors the existing ``RAA_RELEASE_CHECK`` pattern in
``tests/test_step15_release_check.py``: set ``RAA_SLOW_TESTS=1`` to run them.
``scripts/run_tests.py --slow`` sets that variable for its children; without it
the variable is removed, so the default run excludes the slow tier. CI ``full``
passes ``--slow``; CI ``fast`` does not.

Use on a class or a method::

    from _tiers import slow

    @slow
    class RealServiceTests(unittest.TestCase): ...

    class MixedTests(unittest.TestCase):
        @slow
        def test_spawns_a_cli_subprocess(self) -> None: ...
"""

from __future__ import annotations

import os
import unittest

SLOW_TESTS_ENABLED = os.environ.get("RAA_SLOW_TESTS") == "1"

slow = unittest.skipUnless(
    SLOW_TESTS_ENABLED,
    "slow tier: run with RAA_SLOW_TESTS=1 (python3 scripts/run_tests.py --slow)",
)
