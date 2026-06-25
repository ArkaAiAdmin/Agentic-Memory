#!/usr/bin/env python3
"""Unit tests for tier_migration.py.

L6 fix: the tier-migration lifecycle was previously untested. The
functions are pure with respect to the memory directory, so we run
each one against a fresh tempdir populated with fixtures of known ages.

Run with:
    ~/.config/agentic-memory/venv/bin/python eval/test_tier_migration.py
"""

import datetime
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

import tier_migration  # noqa: E402
from memory_common import atomic_write  # noqa: E402


def _write_note(
    md_path: Path,
    *,
    age_days: int,
    pinned: bool = False,
    content_body: str = "Body content.",
) -> None:
    """Write a markdown note with a created_at date ``age_days`` ago."""
    md_path.parent.mkdir(parents=True, exist_ok=True)
    created = (datetime.date.today() - datetime.timedelta(days=age_days)).isoformat()
    fm_pinned = "true" if pinned else "false"
    body = (
        f"---\n"
        f"created: {created}\n"
        f"updated: {created}\n"
        f"observed_at: {created}\n"
        f"tags: [test]\n"
        f"pinned: {fm_pinned}\n"
        f"---\n\n"
        f"# Note\n\n{content_body}\n"
    )
    atomic_write(md_path, body)


class TestGetNoteDate(unittest.TestCase):
    def test_iso_date_string(self):
        d = tier_migration.get_note_date(
            {"created": "2024-01-15"}, Path("/nonexistent")
        )
        self.assertEqual(d, datetime.date(2024, 1, 15))

    def test_iso_datetime_string(self):
        d = tier_migration.get_note_date(
            {"created": "2024-01-15T10:30:00"}, Path("/nonexistent")
        )
        self.assertEqual(d, datetime.date(2024, 1, 15))

    def test_missing_field_falls_back_to_mtime(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "note.md"
            p.write_text("no frontmatter")
            os.utime(p, (1000000000, 1000000000))
            d = tier_migration.get_note_date({}, p)
            self.assertEqual(d, datetime.date(2001, 9, 9))

    def test_unparseable_field_falls_back_to_mtime(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "note.md"
            p.write_text("x")
            os.utime(p, (1000000000, 1000000000))
            d = tier_migration.get_note_date({"created": "garbage"}, p)
            self.assertEqual(d, datetime.date(2001, 9, 9))


class TestIsPinned(unittest.TestCase):
    def test_true(self):
        self.assertTrue(tier_migration.is_pinned({"pinned": True}))

    def test_false(self):
        self.assertFalse(tier_migration.is_pinned({"pinned": False}))

    def test_missing_field(self):
        self.assertFalse(tier_migration.is_pinned({}))


class TestArchiveColdFiles(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tier_test_"))
        self.memdir = self.tmpdir / "memory"
        self.memdir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_cold_file_archived(self):
        _write_note(self.memdir / "lessons" / "old.md", age_days=120)
        stats = tier_migration.archive_cold_files(self.memdir, dry_run=False)
        self.assertEqual(stats["archived"], 1)
        self.assertEqual(stats["pinned_protected"], 0)
        # New contract: source file is REPLACED with a stub, not
        # unlinked. The full body is preserved in a gzip bundle.
        stub_path = self.memdir / "lessons" / "old.md"
        self.assertTrue(stub_path.exists(), "stub should replace the original")
        stub_text = stub_path.read_text()
        self.assertIn("archived: true", stub_text, "stub frontmatter marks it archived")
        # Bundle directory and gzip file exist
        archive = self.memdir / "archive"
        self.assertTrue(archive.exists())
        bundles = list(archive.glob("lessons_*.md.gz"))
        self.assertEqual(len(bundles), 1, "one gzip bundle per category per run")
        # Bundle round-trips the original body
        import gzip

        with gzip.open(bundles[0], "rt", encoding="utf-8") as gz:
            bundle_text = gz.read()
        self.assertIn("Body content.", bundle_text, "bundle must contain full body")

    def test_warm_file_not_archived(self):
        _write_note(self.memdir / "lessons" / "warm.md", age_days=30)
        stats = tier_migration.archive_cold_files(self.memdir, dry_run=False)
        self.assertEqual(stats["archived"], 0)
        self.assertGreater(stats["skip_reasons"]["not_cold"], 0)
        # Source file should still be there
        self.assertTrue((self.memdir / "lessons" / "warm.md").exists())

    def test_pinned_file_protected(self):
        _write_note(self.memdir / "lessons" / "pinned.md", age_days=365, pinned=True)
        stats = tier_migration.archive_cold_files(self.memdir, dry_run=False)
        self.assertEqual(stats["pinned_protected"], 1)
        self.assertEqual(stats["archived"], 0)
        self.assertTrue((self.memdir / "lessons" / "pinned.md").exists())

    def test_dry_run_does_not_delete(self):
        _write_note(self.memdir / "lessons" / "old.md", age_days=120)
        stats = tier_migration.archive_cold_files(self.memdir, dry_run=True)
        self.assertEqual(
            stats["archived"], 1, "dry run should report what would happen"
        )
        # Source file should NOT be touched in dry run — neither
        # unlinked nor replaced with a stub.
        original = self.memdir / "lessons" / "old.md"
        self.assertTrue(original.exists())
        self.assertNotIn("archived: true", original.read_text())
        # No bundle written in dry run
        bundles = list((self.memdir / "archive").glob("lessons_*.md.gz"))
        self.assertEqual(len(bundles), 0, "dry run must not write gzip bundles")

    def test_idempotent_on_second_run(self):
        # First run: replace cold file with a stub and write a bundle.
        _write_note(self.memdir / "lessons" / "old.md", age_days=120)
        first = tier_migration.archive_cold_files(self.memdir, dry_run=False)
        self.assertEqual(first["archived"], 1)
        # Second run: stub is skipped (already_archived) and bundle is
        # preserved (not overwritten because no new content).
        second = tier_migration.archive_cold_files(self.memdir, dry_run=False)
        self.assertEqual(
            second["archived"], 0, "second run should find no new cold files"
        )
        self.assertGreater(
            second["skip_reasons"]["already_archived"],
            0,
            "second run must skip the stub from the first run",
        )
        # Exactly one bundle file (from the first run)
        bundles = list((self.memdir / "archive").glob("lessons_*.md.gz"))
        self.assertEqual(len(bundles), 1)

    def test_binary_file_skipped(self):
        path = self.memdir / "lessons" / "binary.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00\x01\x02binary content")
        # Backdate the mtime so the size/binary check fires first
        old = (datetime.date.today() - datetime.timedelta(days=200)).isoformat()
        ts = datetime.datetime.fromisoformat(old).timestamp()
        os.utime(path, (ts, ts))
        stats = tier_migration.archive_cold_files(self.memdir, dry_run=False)
        self.assertEqual(stats["archived"], 0)
        self.assertGreater(stats["skip_reasons"]["binary"], 0)

    def test_archive_subdir_is_skipped(self):
        # Files inside archive/ should not be re-archived.
        archive = self.memdir / "archive"
        archive.mkdir()
        _write_note(archive / "already.md", age_days=365)
        stats = tier_migration.archive_cold_files(self.memdir, dry_run=False)
        self.assertEqual(stats["archived"], 0)
        self.assertGreater(stats["skip_reasons"]["archive_or_global"], 0)


class TestConsolidateWarmSessions(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tier_test_"))
        self.memdir = self.tmpdir / "memory"
        self.memdir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_warm_session_consolidated(self):
        _write_note(
            self.memdir / "sessions" / "old.md",
            age_days=30,
            content_body="**user:** hello\n**assistant:** hi",
        )
        tier_migration.consolidate_warm_sessions(self.memdir, dry_run=False)
        # Should have produced a lessons/ summary
        lessons = list((self.memdir / "lessons").glob("*.md"))
        self.assertGreaterEqual(len(lessons), 1, "expected at least one summary lesson")

    def test_hot_session_untouched(self):
        # 3-day-old session is "hot" (< 7 days) and should not be consolidated
        _write_note(
            self.memdir / "sessions" / "hot.md",
            age_days=3,
            content_body="recent content",
        )
        tier_migration.consolidate_warm_sessions(self.memdir, dry_run=False)
        # The original hot session should still exist
        self.assertTrue((self.memdir / "sessions" / "hot.md").exists())


class TestRunTierMigration(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="tier_test_"))
        self.memdir = self.tmpdir / "memory"
        self.memdir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_runs_end_to_end(self):
        # Mixed corpus: one cold pinned, one cold unpinned, one warm.
        _write_note(self.memdir / "lessons" / "cold.md", age_days=120)
        _write_note(self.memdir / "lessons" / "kept.md", age_days=120, pinned=True)
        _write_note(self.memdir / "lessons" / "warm.md", age_days=30)
        # Should not raise; pinned should remain untouched, cold
        # should be replaced by a stub, warm should be skipped.
        tier_migration.run_tier_migration(self.memdir, dry_run=False)
        # Pinned: untouched
        self.assertTrue((self.memdir / "lessons" / "kept.md").exists())
        kept_text = (self.memdir / "lessons" / "kept.md").read_text()
        self.assertNotIn("archived: true", kept_text)
        # Cold: now a stub, not unlinked
        cold_stub = self.memdir / "lessons" / "cold.md"
        self.assertTrue(cold_stub.exists(), "cold file should be replaced by a stub")
        cold_text = cold_stub.read_text()
        self.assertIn("archived: true", cold_text)
        # Warm: untouched
        self.assertTrue((self.memdir / "lessons" / "warm.md").exists())


def _write_superseded_note(
    md_path: Path,
    *,
    superseded_age_days: int,
    superseded_by: str = "lessons/newer",
    pinned: bool = False,
    body_lines: int = 5,
) -> None:
    """Write a markdown note with a valid_to date ``superseded_age_days``
    ago AND a non-empty superseded_by. Both fields are required to
    match prune_superseded's filter.
    """
    md_path.parent.mkdir(parents=True, exist_ok=True)
    valid_to = (
        datetime.date.today() - datetime.timedelta(days=superseded_age_days)
    ).isoformat()
    created = (
        datetime.date.today() - datetime.timedelta(days=superseded_age_days + 365)
    ).isoformat()
    fm_pinned = "true" if pinned else "false"
    body = (
        f"---\n"
        f"created: {created}\n"
        f"updated: {valid_to}\n"
        f"observed_at: {valid_to}\n"
        f"valid_to: {valid_to}\n"
        f"superseded_by: {superseded_by}\n"
        f"tags: [test]\n"
        f"pinned: {fm_pinned}\n"
        f"---\n\n"
        f"# Superseded Note\n\n"
        + "Body line that should end up in the bundle.\n"
        * body_lines
    )
    atomic_write(md_path, body)


class TestPruneSuperseded(unittest.TestCase):
    """Sprint 2 Task 3: prune_superseded archives old superseded notes
    to gzip bundles, replacing them with 20-line preview stubs carrying
    ``pruned: true`` in frontmatter. Idempotent on re-run.
    """

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="prune-test-"))
        self.memdir = self.tmpdir / "memory"
        self.memdir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_prunes_old_superseded(self):
        """A note superseded 60 days ago (threshold=30) gets pruned."""
        src = self.memdir / "lessons" / "old.md"
        _write_superseded_note(src, superseded_age_days=60, body_lines=50)
        original_text = src.read_text()

        stats = tier_migration.prune_superseded(
            self.memdir, older_than_days=30, dry_run=False
        )

        # Stats reflect one pruned, zero skipped.
        self.assertEqual(stats["pruned"], 1)
        self.assertEqual(stats["skipped"], 0)
        # Stub exists in place of the original.
        self.assertTrue(src.exists(), "stub should replace the file, not unlink it")
        stub_text = src.read_text()
        self.assertIn("pruned: true", stub_text)
        self.assertIn("pruned_to:", stub_text)
        self.assertIn("pruned_at:", stub_text)
        # And the body must be replaced — first 20 lines of the new stub.
        self.assertNotEqual(stub_text, original_text)
        self.assertIn("(pruned stub)", stub_text)
        # Bundle exists and contains the full original content.
        bundles = list((self.memdir / "archive").glob("lessons_pruned_*.md.gz"))
        self.assertEqual(len(bundles), 1, f"expected 1 prune bundle, got {bundles}")
        import gzip

        with gzip.open(bundles[0], "rb") as gz:
            bundle_text = gz.read().decode("utf-8")
        self.assertIn("Body line that should end up in the bundle.", bundle_text)

    def test_keeps_recent_superseded(self):
        """A note superseded 5 days ago (threshold=30) is NOT pruned."""
        src = self.memdir / "lessons" / "recent.md"
        _write_superseded_note(src, superseded_age_days=5, body_lines=10)
        original_text = src.read_text()

        stats = tier_migration.prune_superseded(
            self.memdir, older_than_days=30, dry_run=False
        )

        # Stats reflect one skipped (not_old_enough), zero pruned.
        self.assertEqual(stats["pruned"], 0)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["skip_reasons"]["not_old_enough"], 1)
        # File is byte-identical to the original.
        self.assertEqual(src.read_text(), original_text)
        # And no bundle was created. (The archive/ dir may exist from
        # unconditional mkdir, but it must be empty of .md.gz files.)
        archive = self.memdir / "archive"
        if archive.exists():
            self.assertEqual(
                list(archive.glob("*.md.gz")),
                [],
                "no bundle should be created for skipped notes",
            )

    def test_keeps_old_non_superseded(self):
        """An old note WITHOUT `superseded_by` is NOT pruned."""
        src = self.memdir / "lessons" / "old.md"
        # Use the standard _write_note (no superseded_by, no valid_to).
        _write_note(src, age_days=200)
        original_text = src.read_text()

        stats = tier_migration.prune_superseded(
            self.memdir, older_than_days=30, dry_run=False
        )

        # Skipped with not_superseded reason.
        self.assertEqual(stats["pruned"], 0)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["skip_reasons"]["not_superseded"], 1)
        # File is unchanged.
        self.assertEqual(src.read_text(), original_text)

    def test_idempotent_on_second_run(self):
        """Second run is a no-op: stub already carries `pruned: true`."""
        src = self.memdir / "lessons" / "old.md"
        _write_superseded_note(src, superseded_age_days=60, body_lines=30)

        # First run: prune it.
        stats1 = tier_migration.prune_superseded(
            self.memdir, older_than_days=30, dry_run=False
        )
        self.assertEqual(stats1["pruned"], 1)
        first_stub = src.read_text()
        bundles_after_first = list(
            (self.memdir / "archive").glob("lessons_pruned_*.md.gz")
        )
        self.assertEqual(len(bundles_after_first), 1)

        # Second run: must be a no-op.
        stats2 = tier_migration.prune_superseded(
            self.memdir, older_than_days=30, dry_run=False
        )
        self.assertEqual(stats2["pruned"], 0)
        self.assertEqual(stats2["skipped"], 1)
        self.assertEqual(stats2["skip_reasons"]["already_pruned"], 1)
        # Stub text is unchanged.
        self.assertEqual(src.read_text(), first_stub)
        # And no new bundle was created.
        bundles_after_second = list(
            (self.memdir / "archive").glob("lessons_pruned_*.md.gz")
        )
        self.assertEqual(len(bundles_after_second), 1)

    def test_dry_run_does_not_write(self):
        """dry_run=True computes the plan but writes nothing."""
        src = self.memdir / "lessons" / "old.md"
        _write_superseded_note(src, superseded_age_days=60, body_lines=10)
        original_text = src.read_text()

        stats = tier_migration.prune_superseded(
            self.memdir, older_than_days=30, dry_run=True
        )

        # Stats say it would prune, but nothing happened on disk.
        self.assertEqual(stats["pruned"], 1)
        self.assertEqual(src.read_text(), original_text)
        # No archive/ dir created either (we only mkdir in non-dry-run paths).
        # Actually archive_dir.mkdir is unconditional in the function,
        # so the dir exists but is empty.
        archive = self.memdir / "archive"
        if archive.exists():
            self.assertEqual(
                list(archive.glob("*.md.gz")), [], "dry_run must not write bundles"
            )

    def test_stats_shape_matches_archive_cold_files(self):
        """The stats dict from prune_superseded has the same SHAPE as
        archive_cold_files — parallel top-level keys, same number of
        skip_reasons, same value type per key. The actual skip_reasons
        KEYS differ by intent (not_cold vs not_superseded, etc.) but
        the structure must match.
        """
        # Mixed corpus: one of each skip case.
        _write_superseded_note(
            self.memdir / "lessons" / "old_super.md", superseded_age_days=60
        )
        _write_superseded_note(
            self.memdir / "lessons" / "recent_super.md", superseded_age_days=5
        )
        _write_note(self.memdir / "lessons" / "old_plain.md", age_days=200)
        _write_note(self.memdir / "lessons" / "warm_plain.md", age_days=10)

        cold_stats = tier_migration.archive_cold_files(self.memdir, dry_run=True)
        prune_stats = tier_migration.prune_superseded(
            self.memdir, older_than_days=30, dry_run=True
        )

        # Top-level shape: 4 required keys (primary counter, skipped,
        # pinned_protected, skip_reasons) plus the optional
        # 'arc_evictions_recorded' counter added in P0 fix #4 (the
        # ARC ghost list is dry_run-inert, so it must be 0 here).
        # Strict equality on the keyset keeps the test honest: a
        # future refactor that drops a key fails loudly, instead of
        # silently passing via `issubset`.
        self.assertEqual(
            set(cold_stats.keys()),
            {
                "archived",
                "skipped",
                "pinned_protected",
                "skip_reasons",
                "arc_evictions_recorded",
            },
        )
        self.assertEqual(cold_stats["arc_evictions_recorded"], 0)
        self.assertEqual(
            set(prune_stats.keys()),
            {"pruned", "skipped", "pinned_protected", "skip_reasons"},
        )
        # skip_reasons is a dict[str, int] in both cases. The actual
        # key set differs by intent (cold has 8 reasons, prune has 9
        # because prune has TWO independent filters — superseded_by
        # and age — whereas cold has only one). The structural shape
        # is identical: dict of int counters, all non-negative.
        self.assertIsInstance(cold_stats["skip_reasons"], dict)
        self.assertIsInstance(prune_stats["skip_reasons"], dict)
        for v in cold_stats["skip_reasons"].values():
            self.assertIsInstance(v, int)
            self.assertGreaterEqual(v, 0)
        for v in prune_stats["skip_reasons"].values():
            self.assertIsInstance(v, int)
            self.assertGreaterEqual(v, 0)
        # And the prune-specific keys must exist.
        self.assertIn("not_superseded", prune_stats["skip_reasons"])
        self.assertIn("already_pruned", prune_stats["skip_reasons"])
        self.assertIn("not_old_enough", prune_stats["skip_reasons"])
        # Pruned total = pruned key (which we asserted above). The
        # other 3 top-level ints are skipped / pinned_protected /
        # sum-of-skip-reasons. Use a looser check: just that every
        # non-dict value is an int.
        for k, v in prune_stats.items():
            if k != "skip_reasons":
                self.assertIsInstance(v, int)


if __name__ == "__main__":
    unittest.main(verbosity=2)
