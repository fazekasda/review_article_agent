"""Tests for the bounded local folder-batch reader (Step 06 F1).

Synthetic data only. Run with::

    PYTHONPATH=src python3 -m unittest discover -s tests -p test_acquisition_folders.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from review_article_agent.acquisition import folders  # noqa: E402
from review_article_agent.acquisition.folders import (  # noqa: E402
    FolderInputError,
    read_folder_batch,
)


def _snapshot(root: Path) -> list[tuple[str, int, int]]:
    out = []
    for p in sorted(root.rglob("*")):
        st = p.lstat()
        out.append((str(p.relative_to(root)), st.st_size, st.st_mtime_ns))
    return out


class FolderBatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "root"
        self.root.mkdir()
        self.outside = Path(self._tmp.name) / "outside"
        self.outside.mkdir()
        (self.outside / "secret.txt").write_bytes(b"secret")

    def write(self, rel: str, data: bytes = b"data") -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def code(self, entries, **kw) -> str:
        with self.assertRaises(FolderInputError) as cm:
            read_folder_batch(self.root, entries, **kw)
        return cm.exception.code

    def test_reads_nested_explicit_files_in_order_with_metadata(self) -> None:
        self.write("a.pdf", b"AAA")
        self.write("sub/dir/b.xml", b"BBBB")
        self.write("unlisted.txt", b"nope")
        meta = {"note": ["x"], "n": 1}
        entries = [
            {"relative_path": "sub/dir/b.xml", "meta": meta},
            {"relative_path": "a.pdf"},
        ]
        before = _snapshot(self.root)
        result = read_folder_batch(self.root, entries)
        self.assertEqual(
            result,
            [
                {"relative_path": "sub/dir/b.xml", "meta": meta, "content": b"BBBB"},
                {"relative_path": "a.pdf", "content": b"AAA"},
            ],
        )
        self.assertEqual(before, _snapshot(self.root))
        self.assertNotIn("content", entries[0])
        self.assertEqual(set(result[1]), {"relative_path", "content"})

    def test_result_is_independent_copy(self) -> None:
        self.write("a.txt", b"A")
        meta = {"tags": ["x"]}
        entries = [{"relative_path": "a.txt", "meta": meta}]
        result = read_folder_batch(self.root, entries)
        result[0]["meta"]["tags"].append("y")
        self.assertEqual(meta, {"tags": ["x"]})
        self.assertIsInstance(result[0]["content"], bytes)

    def test_no_inferred_identity_keys(self) -> None:
        self.write("10.1000_xyz.pdf", b"A")
        result = read_folder_batch(self.root, [{"relative_path": "10.1000_xyz.pdf"}])
        self.assertEqual(set(result[0]), {"relative_path", "content"})

    def test_empty_manifest(self) -> None:
        self.assertEqual(read_folder_batch(self.root, []), [])

    def test_bad_paths_rejected(self) -> None:
        self.write("a.txt")
        bad = [
            "../outside/secret.txt",
            "sub/../a.txt",
            "/etc/passwd",
            "",
            "./a.txt",
            "a.txt/",
            "a//b",
            "a\x00b",
            "a\nb",
            "a\\b",
            "bad\udcffname",
            ".git/config",
            "x/.GIT/config",
            "a" * 300,
            None,
            5,
        ]
        for path in bad:
            with self.subTest(path=path):
                self.assertEqual(
                    self.code([{"relative_path": path}]), "invalid_path"
                )

    def test_manifest_shape_rejected(self) -> None:
        for entries in ("a.txt", {"relative_path": "a"}, [{"x": 1}], ["a.txt"], [{"relative_path": "a", "content": b""}]):
            with self.subTest(entries=entries):
                self.assertEqual(self.code(entries), "invalid_manifest")

    def test_duplicate_path(self) -> None:
        self.write("a.txt")
        e = [{"relative_path": "a.txt"}, {"relative_path": "a.txt", "m": 2}]
        self.assertEqual(self.code(e), "duplicate_path")

    def test_validation_precedes_reading(self) -> None:
        self.write("a.txt")
        entries = [{"relative_path": "a.txt"}, {"relative_path": "../x"}]
        with mock.patch.object(folders.os, "open") as m:
            with self.assertRaises(FolderInputError):
                read_folder_batch(self.root, entries)
        m.assert_not_called()

    def test_missing_file_no_partial_results(self) -> None:
        self.write("a.txt")
        e = [{"relative_path": "a.txt"}, {"relative_path": "missing.txt"}]
        self.assertEqual(self.code(e), "not_found")

    def test_symlink_final_and_directory_escape(self) -> None:
        os.symlink(self.outside / "secret.txt", self.root / "link.txt")
        os.symlink(self.outside, self.root / "linkdir")
        self.assertEqual(self.code([{"relative_path": "link.txt"}]), "unsafe_path")
        self.assertEqual(
            self.code([{"relative_path": "linkdir/secret.txt"}]), "unsafe_path"
        )

    def test_symlink_root_rejected(self) -> None:
        link = Path(self._tmp.name) / "rootlink"
        os.symlink(self.root, link)
        self.write("a.txt")
        with self.assertRaises(FolderInputError) as cm:
            read_folder_batch(link, [{"relative_path": "a.txt"}])
        self.assertEqual(cm.exception.code, "root_unavailable")

    def root_code(self, root) -> str:
        with self.assertRaises(FolderInputError) as cm:
            read_folder_batch(root, [{"relative_path": "a.txt"}])
        return cm.exception.code

    def test_ancestor_symlink_in_root_chain_rejected(self) -> None:
        (self.outside / "sub").mkdir()
        (self.outside / "sub" / "a.txt").write_bytes(b"leak")
        link = Path(self._tmp.name) / "ancestor"
        os.symlink(self.outside, link)
        self.assertEqual(self.root_code(link / "sub"), "root_unavailable")
        self.assertEqual(self.root_code(str(link) + "/./sub/"), "root_unavailable")

    def test_dotdot_and_bad_root_values_static_errors(self) -> None:
        self.assertEqual(self.root_code(str(self.root) + "/../root"), "root_unavailable")
        for bad in ("", "a\x00b", "bad\udcffroot"):
            with self.subTest(bad=bad):
                self.assertIn(
                    self.root_code(bad), ("invalid_arguments", "root_unavailable")
                )
        with self.assertRaises(FolderInputError) as cm:
            read_folder_batch(123, [])
        self.assertEqual(cm.exception.code, "invalid_arguments")

    def test_absolute_relative_and_bytes_roots_work(self) -> None:
        self.write("nest/a.txt", b"A")
        e = [{"relative_path": "a.txt"}]
        real = os.path.realpath(self.root / "nest")
        self.assertEqual(read_folder_batch(real, e)[0]["content"], b"A")
        self.assertEqual(read_folder_batch(real + "/", e)[0]["content"], b"A")
        self.assertEqual(read_folder_batch(os.fsencode(real), e)[0]["content"], b"A")
        old = os.getcwd()
        os.chdir(os.path.realpath(self.root))
        try:
            self.assertEqual(read_folder_batch("nest", e)[0]["content"], b"A")
            self.assertEqual(read_folder_batch("./nest", e)[0]["content"], b"A")
        finally:
            os.chdir(old)

    def test_root_failures_leak_no_descriptors(self) -> None:
        link = Path(self._tmp.name) / "ancestor"
        os.symlink(self.outside, link)
        before = set(os.listdir("/proc/self/fd"))
        for bad in (link / "x", self.root / "missing" / "deeper", str(self.root) + "/.."):
            self.root_code(bad)
        read_folder_batch(os.path.realpath(self.root), [])
        self.assertEqual(before, set(os.listdir("/proc/self/fd")))

    def test_missing_root(self) -> None:
        with self.assertRaises(FolderInputError) as cm:
            read_folder_batch(self.root / "nope", [])
        self.assertEqual(cm.exception.code, "root_unavailable")

    def test_directory_as_file_rejected(self) -> None:
        (self.root / "d").mkdir()
        self.assertEqual(self.code([{"relative_path": "d"}]), "not_regular")

    def test_fifo_rejected_without_hanging(self) -> None:
        os.mkfifo(self.root / "pipe")
        outcome = []

        def run() -> None:
            try:
                read_folder_batch(self.root, [{"relative_path": "pipe"}])
            except FolderInputError as exc:
                outcome.append(exc.code)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(5)
        self.assertFalse(t.is_alive(), "FIFO open hung")
        self.assertEqual(outcome, ["not_regular"])

    def test_file_count_limit(self) -> None:
        for n in range(3):
            self.write(f"f{n}.txt")
        e = [{"relative_path": f"f{n}.txt"} for n in range(3)]
        self.assertEqual(self.code(e, max_files=2), "too_many_files")
        self.assertEqual(len(read_folder_batch(self.root, e, max_files=3)), 3)

    def test_total_cap_not_truncated(self) -> None:
        self.write("a.bin", b"x" * 600)
        self.write("b.bin", b"y" * 600)
        e = [{"relative_path": "a.bin"}, {"relative_path": "b.bin"}]
        self.assertEqual(self.code(e, max_total_bytes=1000), "total_too_large")
        out = read_folder_batch(self.root, e, max_total_bytes=1200)
        self.assertEqual([len(r["content"]) for r in out], [600, 600])

    def test_per_file_cap(self) -> None:
        self.write("a.bin", b"x" * 20)
        with mock.patch.object(folders, "MAX_FILE_BYTES", 10):
            self.assertEqual(self.code([{"relative_path": "a.bin"}]), "file_too_large")

    def test_invalid_limits(self) -> None:
        for kw in (
            {"max_files": 0},
            {"max_files": True},
            {"max_total_bytes": 0},
            {"max_total_bytes": 256 * 1024 * 1024 + 1},
            {"max_files": "5"},
        ):
            with self.subTest(kw=kw):
                self.assertEqual(self.code([], **kw), "invalid_arguments")

    def test_changed_during_read_detected(self) -> None:
        self.write("a.txt", b"abcdef")
        real_read = os.read

        def growing(fd, n):
            data = real_read(fd, n)
            with open(self.root / "a.txt", "ab") as fh:
                fh.write(b"more")
            return data

        with mock.patch.object(folders.os, "read", growing):
            self.assertEqual(
                self.code([{"relative_path": "a.txt"}]), "changed_during_read"
            )

    def test_descriptors_closed_on_failure(self) -> None:
        self.write("sub/a.txt")
        self.write("sub/b.txt", b"x" * 20)
        before = set(os.listdir("/proc/self/fd"))
        e = [{"relative_path": "sub/a.txt"}, {"relative_path": "sub/b.txt"}]
        with mock.patch.object(folders, "MAX_FILE_BYTES", 10):
            self.assertEqual(self.code(e), "file_too_large")
        self.assertEqual(self.code([{"relative_path": "sub/zzz"}]), "not_found")
        self.assertEqual(before, set(os.listdir("/proc/self/fd")))

    def test_error_text_has_no_caller_paths(self) -> None:
        with self.assertRaises(FolderInputError) as cm:
            read_folder_batch(self.root, [{"relative_path": "secretname/x.txt"}])
        self.assertNotIn("secretname", str(cm.exception))
        self.assertNotIn(str(self.root), str(cm.exception))


if __name__ == "__main__":
    unittest.main()
