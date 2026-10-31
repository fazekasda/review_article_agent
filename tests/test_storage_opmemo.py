"""Tests for the operation-scoped validation memo (S16-P2a).

Covers ``src/review_article_agent/storage/opmemo.py`` and its transaction
lifetime wiring: the memo caches only inside a transaction, keys on the
connection's ``total_changes`` so the operation's own writes invalidate it, is
cleared on savepoint rollback and at every outermost-transaction boundary, and
never caches a raised exception.  No result may outlive the operation that
produced it.

Fixtures are synthetic and self-authored; no network, provider, or model call
is made.

Run::

    PYTHONPATH=src:tests python3 -m unittest tests.test_storage_opmemo
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from review_article_agent.parsing.repository import _NestedStore  # noqa: E402
from review_article_agent.storage import ProjectStore, opmemo, paths  # noqa: E402


class OpMemoTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="raa-opmemo-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def new_store(self, name: str = "p") -> ProjectStore:
        store = ProjectStore.create_at(
            paths.app_dir_for(str(self.root / name)), paths.new_project_id()
        )
        self.addCleanup(store.close)
        return store


class OutsideTransactionTests(OpMemoTestBase):
    def test_compute_runs_every_call_outside_transaction(self):
        store = self.new_store()
        conn = store.connection
        opmemo.invalidate(conn)
        calls: list[int] = []

        def compute():
            calls.append(1)
            return {"n": len(calls)}

        results = [opmemo.memoize(conn, ("k",), compute) for _ in range(3)]
        self.assertEqual(len(calls), 3)
        # Nothing is cached (or accounted) outside a transaction.
        self.assertEqual(opmemo.stats(conn), (0, 0))
        self.assertEqual([r["n"] for r in results], [1, 2, 3])

    def test_unhashable_key_is_rejected(self):
        store = self.new_store()
        conn = store.connection
        with store.transaction():
            with self.assertRaises(TypeError):
                opmemo.memoize(conn, ["not", "hashable"], lambda: 1)


class InsideTransactionTests(OpMemoTestBase):
    def test_same_key_hits_and_different_key_misses(self):
        store = self.new_store()
        conn = store.connection
        calls: list[int] = []

        def compute():
            calls.append(1)
            return {"v": len(calls)}

        with store.transaction():
            first = opmemo.memoize(conn, ("k",), compute)
            second = opmemo.memoize(conn, ("k",), compute)
            other = opmemo.memoize(conn, ("other",), compute)
            self.assertEqual(opmemo.stats(conn), (1, 2))
        self.assertEqual(len(calls), 2)
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)

    def test_write_makes_next_call_miss(self):
        store = self.new_store()
        conn = store.connection
        calls: list[int] = []

        def compute():
            calls.append(1)
            return len(calls)

        with store.transaction():
            opmemo.memoize(conn, ("k",), compute)  # miss
            opmemo.memoize(conn, ("k",), compute)  # hit
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)", ("opmemo_t", "1")
            )
            opmemo.memoize(conn, ("k",), compute)  # miss: total_changes changed
            self.assertEqual(opmemo.stats(conn), (1, 2))
        self.assertEqual(len(calls), 2)


class SavepointRollbackTests(OpMemoTestBase):
    def test_rollback_to_savepoint_invalidates_memo(self):
        store = self.new_store()
        conn = store.connection
        calls: list[int] = []

        def count_rows():
            calls.append(1)
            return conn.execute(
                "SELECT COUNT(*) FROM meta WHERE key = 'opmemo_sp'"
            ).fetchone()[0]

        with store.transaction():
            with self.assertRaises(RuntimeError):
                with _NestedStore(store).transaction():
                    conn.execute(
                        "INSERT INTO meta (key, value) VALUES (?, ?)",
                        ("opmemo_sp", "1"),
                    )
                    # Cache an entry under the post-write total_changes.
                    self.assertEqual(opmemo.memoize(conn, ("k",), count_rows), 1)
                    raise RuntimeError("boom")
            # ``ROLLBACK TO`` reverted the row but did not decrease
            # ``total_changes``; without invalidation this would be a stale hit.
            self.assertEqual(opmemo.memoize(conn, ("k",), count_rows), 0)
        self.assertEqual(len(calls), 2)


class CrossOperationTests(OpMemoTestBase):
    def test_new_transaction_after_commit_or_rollback_has_empty_memo(self):
        with self.subTest(case="test_commit_starts_new_transaction_with_empty_memo"):
            store = self.new_store("commit")
            conn = store.connection
            calls: list[int] = []

            def compute():
                calls.append(1)
                return len(calls)

            with store.transaction():
                opmemo.memoize(conn, ("k",), compute)
            with store.transaction():
                opmemo.memoize(conn, ("k",), compute)
            self.assertEqual(len(calls), 2)

        with self.subTest(case="test_rollback_starts_new_transaction_with_empty_memo"):
            store = self.new_store("rollback")
            conn = store.connection
            calls = []

            def compute():
                calls.append(1)
                return len(calls)

            with self.assertRaises(RuntimeError):
                with store.transaction():
                    opmemo.memoize(conn, ("k",), compute)
                    raise RuntimeError("rollback")
            with store.transaction():
                opmemo.memoize(conn, ("k",), compute)
            self.assertEqual(len(calls), 2)


class ExceptionAndIsolationTests(OpMemoTestBase):
    def test_exceptions_are_not_cached(self):
        store = self.new_store()
        conn = store.connection
        calls: list[str] = []

        def failing():
            calls.append("fail")
            raise ValueError("nope")

        def succeeding():
            calls.append("ok")
            return {"ok": True}

        with store.transaction():
            with self.assertRaises(ValueError):
                opmemo.memoize(conn, ("k",), failing)
            with self.assertRaises(ValueError):
                opmemo.memoize(conn, ("k",), failing)
            first = opmemo.memoize(conn, ("k",), succeeding)
            second = opmemo.memoize(conn, ("k",), succeeding)
        self.assertEqual(calls, ["fail", "fail", "ok"])
        self.assertEqual(first, second)

    def test_mutating_returned_value_does_not_affect_later_hits(self):
        store = self.new_store()
        conn = store.connection

        def compute():
            return {"x": []}

        with store.transaction():
            first = opmemo.memoize(conn, ("k",), compute)
            first["x"].append("mutated")
            second = opmemo.memoize(conn, ("k",), compute)
            self.assertEqual(second, {"x": []})
            self.assertEqual(opmemo.stats(conn), (1, 1))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
