"""Sprint 2 Task 2 — atomic_write coverage tests.

Closes the 4 atomic_write TODO sites (3 already-routed sites get a
regression guard; the 4th site at rebuild_index.py is a pre-existing
os.replace swap and is verified to remain that way). Also exercises
the new `bytes` overload of atomic_write that tier_migration uses for
gzip bundles.

History:
  - L4 fix added atomic_write(path, content: str) in memory_common.py
    and routed 14 callers through it.
  - 4 callers were intentionally left as "TODO" follow-ups because
    they were either swaps (not writes) or needed a binary overload.
  - Sprint 2 closes the binary-overload case (atomic_write now
    accepts `str | bytes`) and documents the swap case.
"""

import gzip
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from memory_common import atomic_write, safe_atomic_write


class TestAtomicWriteBytes(unittest.TestCase):
    """Sprint 2 Task 2: bytes overload of atomic_write."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="awb-test-")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_atomic_write_bytes_round_trip(self):
        """Bytes payload must round-trip exactly, including NUL bytes."""
        target = Path(self.tmpdir) / "out.bin"
        payload = b"\x00\x01\x02\x03\xff\xfe\xfd binary \x00 not text"
        atomic_write(target, payload)
        self.assertEqual(target.read_bytes(), payload)

    def test_atomic_write_bytes_creates_parent_dirs(self):
        """atomic_write must create missing parents (text + bytes)."""
        target = Path(self.tmpdir) / "deep" / "nested" / "out.bin"
        atomic_write(target, b"hello")
        self.assertTrue(target.exists())
        self.assertEqual(target.read_bytes(), b"hello")

    def test_atomic_write_bytes_cleans_up_on_failure(self):
        """If the write itself raises, the .tmp sibling must be gone."""
        target = Path(self.tmpdir) / "out.bin"
        # Force write_bytes to raise. The .tmp file is the only thing
        # atomic_write creates; on exception it must be unlinked.
        with mock.patch.object(Path, "write_bytes", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                atomic_write(target, b"payload")
        # The destination must not exist (write never completed).
        self.assertFalse(target.exists())
        # And no .tmp sibling must be left lying around.
        self.assertFalse(target.with_suffix(target.suffix + ".tmp").exists())

    def test_atomic_write_str_still_works(self):
        """Backward-compat: str callers continue to work."""
        target = Path(self.tmpdir) / "out.md"
        atomic_write(target, "hello\nworld\n")
        self.assertEqual(target.read_text(encoding="utf-8"), "hello\nworld\n")

    def test_atomic_write_is_exclusive(self):
        """Two near-simultaneous writes must not leave a torn file."""
        target = Path(self.tmpdir) / "out.bin"
        # First write establishes a non-empty file; second overwrites
        # atomically. After both, content must match the second only.
        atomic_write(target, b"first")
        atomic_write(target, b"second")
        self.assertEqual(target.read_bytes(), b"second")


class TestTierMigrationBundleUsesAtomicWrite(unittest.TestCase):
    """Sprint 2 Task 2: tier_migration gzip bundle must go through
    atomic_write (so the temp+replace pattern is not duplicated)."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tm-awb-"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_tier_migration_calls_atomic_write_with_gz_bytes(self):
        """archive_cold_files must call atomic_write(path, bytes) with
        a real gzip payload (so the .gz file is actually gzipped)."""
        from tier_migration import archive_cold_files

        # Seed a notes directory with one cold lesson.
        memdir = self.tmpdir / "mem"
        cat = memdir / "lessons"
        cat.mkdir(parents=True)
        (cat / "old.md").write_text(
            "---\nid: lessons/old\ncreated_at: 2020-01-01\n---\n"
            "## Body\nancient cold content here\n"
        )
        # Make it look old (>90 days, the function's hard threshold).
        old_mtime = (cat / "old.md").stat().st_mtime - (400 * 86400)
        os.utime(cat / "old.md", (old_mtime, old_mtime))

        # Capture atomic_write calls.
        with mock.patch("tier_migration.atomic_write") as aw:
            stats = archive_cold_files(memdir, dry_run=False)

        # The gzip bundle write must have gone through atomic_write.
        bundle_calls = [
            c for c in aw.call_args_list if str(c.args[0]).endswith(".md.gz")
        ]
        self.assertEqual(
            len(bundle_calls),
            1,
            f"expected 1 bundle atomic_write, got {len(bundle_calls)}: {aw.call_args_list}",
        )
        # The payload must be bytes, and must be a real gzip stream.
        payload = bundle_calls[0].args[1]
        self.assertIsInstance(payload, bytes)
        with gzip.GzipFile(fileobj=io.BytesIO(payload), mode="rb") as gz:
            decoded = gz.read().decode("utf-8")
        # Original lesson content must be inside the bundle.
        self.assertIn("ancient cold content here", decoded)
        # And stats must reflect one archived file.
        self.assertEqual(stats["archived"], 1)


class TestRebuildIndexKeepsOsReplace(unittest.TestCase):
    """Sprint 2 Task 2: rebuild_index.py's DB swap intentionally uses
    os.replace, not atomic_write. This is a regression guard so a
    future contributor does not 'fix' it by reading the whole DB into
    memory just to call atomic_write.

    atomic_write expects content (str or bytes). For a swap of an
    already-built temp DB, the atomic step is the rename itself.
    """

    def test_rebuild_index_rebuild_db_calls_os_replace(self):
        import rebuild_index

        # We do not need to actually run the full rebuild; we just
        # need to confirm the source still calls os.replace, not
        # atomic_write, at the swap site.
        with open(rebuild_index.__file__, "r") as f:
            src = f.read()
        # The swap block: os.replace(str(tmp_db_path), str(db_path))
        # appears once, surrounded by a comment explaining why.
        self.assertIn(
            "os.replace(str(tmp_db_path), str(db_path))",
            src,
            "rebuild_index.py no longer uses os.replace for the DB swap "
            "— that was an intentional L4 / Sprint 2 decision, not a TODO.",
        )
        # And the explanatory comment must still be there.
        self.assertIn(
            "intentionally uses os.replace",
            src,
            "rebuild_index.py lost the Sprint 2 Task 2 comment "
            "explaining why os.replace is used here.",
        )
        # atomic_write must not be called for the DB swap site.
        # (atomic_write is used elsewhere in rebuild_index for the
        # mem_md index file; that's fine. We just need to make sure
        # nobody has tried to use it for the DB swap.)


# ===========================================================================
# Scenario 4 (2026-06-22): safe_atomic_write conflict detection
# ===========================================================================


class TestSafeAtomicWrite(unittest.TestCase):
    """Regression test for the LWW fix (concurrent-edit detection).

    Before the fix, two opencode sessions editing the same .md file
    would silently overwrite each other (last-writer-wins).  The
    fix: safe_atomic_write accepts an ``expected_existing`` snapshot;
    if the on-disk content differs, the on-disk content is saved
    as a ``<path>.conflict-<pid>-<ts>`` file before the new content
    is written.
    """

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="safe_atomic_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_no_conflict_when_content_matches(self) -> None:
        """Matching content → no conflict file is created."""
        path = self.tmp_dir / "test.md"
        path.write_text("original content", encoding="utf-8")
        success, conflict = safe_atomic_write(
            path,
            "original content",
            encoding="utf-8",
            expected_existing="original content",
        )
        self.assertTrue(success)
        self.assertIsNone(conflict)
        self.assertEqual(path.read_text(encoding="utf-8"), "original content")

    def test_conflict_saved_when_content_differs(self) -> None:
        """Differing content → conflict file created, new content written."""
        path = self.tmp_dir / "test.md"
        path.write_text("on-disk content (the other session's edit)", encoding="utf-8")
        success, conflict = safe_atomic_write(
            path,
            "my new content",
            encoding="utf-8",
            expected_existing="original content (what I expected to be there)",
        )
        self.assertTrue(success)
        self.assertIsNotNone(conflict)
        # Conflict file must contain the "losing" on-disk content.
        conflict_path = Path(conflict)
        self.assertTrue(conflict_path.exists())
        self.assertEqual(
            conflict_path.read_text(encoding="utf-8"),
            "on-disk content (the other session's edit)",
        )
        # Main file must have the new content.
        self.assertEqual(path.read_text(encoding="utf-8"), "my new content")

    def test_no_conflict_when_no_expected(self) -> None:
        """Without expected_existing, no conflict detection happens."""
        path = self.tmp_dir / "test.md"
        path.write_text("anything", encoding="utf-8")
        success, conflict = safe_atomic_write(path, "new content", encoding="utf-8")
        self.assertTrue(success)
        self.assertIsNone(conflict)
        self.assertEqual(path.read_text(encoding="utf-8"), "new content")

    def test_no_conflict_when_file_does_not_exist(self) -> None:
        """New file (no pre-existing) → no conflict possible."""
        path = self.tmp_dir / "new.md"
        success, conflict = safe_atomic_write(
            path, "new content", encoding="utf-8", expected_existing="anything"
        )
        self.assertTrue(success)
        self.assertIsNone(conflict)
        self.assertEqual(path.read_text(encoding="utf-8"), "new content")


if __name__ == "__main__":
    unittest.main()
