#!/usr/bin/env python3
"""Unit tests for arc_cache.py.

Run with:
    ~/.config/agentic-memory/venv/bin/python eval/test_arc_cache.py
"""

import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path

# Make arc_cache importable
INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

from infra.arc_cache import ARCCache  # noqa: E402


class TestARCCache(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="arc_cache_test_")
        self.db_path = Path(self.tmpdir) / "test.db"

    def tearDown(self):
        try:
            for p in Path(self.tmpdir).glob("*"):
                p.unlink()
            Path(self.tmpdir).rmdir()
        except Exception:
            pass

    def test_init_creates_tables(self):
        with ARCCache(self.db_path) as cache:
            tables = {
                row[0]
                for row in cache.db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            self.assertIn("arc_ghosts", tables)
            self.assertIn("arc_stats", tables)

    def test_record_eviction_is_idempotent(self):
        with ARCCache(self.db_path) as cache:
            cache.record_eviction("lessons/foo", "working")
            cache.record_eviction("lessons/foo", "working")
            n = cache.db.execute("SELECT COUNT(*) FROM arc_ghosts").fetchone()[0]
            self.assertEqual(n, 1, "duplicate eviction should upsert, not append")

    def test_record_hit_marks_ghost(self):
        with ARCCache(self.db_path) as cache:
            cache.record_eviction("lessons/foo", "working")
            self.assertFalse(cache.check_ghost("lessons/foo"))
            cache.record_hit("lessons/foo")
            self.assertTrue(cache.check_ghost("lessons/foo"))

    def test_record_hit_unknown_id_is_noop(self):
        with ARCCache(self.db_path) as cache:
            cache.record_hit("lessons/never-evicted")
            self.assertFalse(cache.check_ghost("lessons/never-evicted"))

    def test_compute_eviction_pressure_empty(self):
        with ARCCache(self.db_path) as cache:
            pressure = cache.compute_eviction_pressure()
            self.assertEqual(pressure, 0.5, "empty ghost list -> neutral 0.5")
            stats = cache.get_stats()
            self.assertEqual(stats["ghost_count"], 0)
            self.assertEqual(stats["eviction_pressure"], 0.5)

    def test_compute_eviction_pressure_all_hits(self):
        with ARCCache(self.db_path) as cache:
            for i in range(10):
                cache.record_eviction(f"id-{i}", "working")
            for i in range(10):
                cache.record_hit(f"id-{i}")
            pressure = cache.compute_eviction_pressure()
            self.assertAlmostEqual(pressure, 0.0, places=6)

    def test_compute_eviction_pressure_no_hits(self):
        with ARCCache(self.db_path) as cache:
            for i in range(10):
                cache.record_eviction(f"id-{i}", "working")
            pressure = cache.compute_eviction_pressure()
            self.assertAlmostEqual(pressure, 1.0, places=6)

    def test_cleanup_old_ghosts(self):
        with ARCCache(self.db_path) as cache:
            cache.record_eviction("recent", "working")
            # Backdate one row directly
            cache.db.execute(
                """UPDATE arc_ghosts
                      SET evicted_at = '2000-01-01T00:00:00'
                    WHERE memory_id = 'recent'"""
            )
            cache.db.commit()
            deleted = cache.cleanup_old_ghosts(max_age_days=30)
            self.assertEqual(deleted, 1)
            n = cache.db.execute("SELECT COUNT(*) FROM arc_ghosts").fetchone()[0]
            self.assertEqual(n, 0)

    def test_context_manager_closes_connection(self):
        cache = ARCCache(self.db_path)
        cache.record_eviction("a", "working")
        cache.__exit__(None, None, None)
        # sqlite3.Connection raises ProgrammingError on use after close.
        with self.assertRaises(sqlite3.ProgrammingError):
            cache.db.execute("SELECT 1")

    def test_get_stats_returns_expected_keys(self):
        with ARCCache(self.db_path) as cache:
            cache.compute_eviction_pressure()
            stats = cache.get_stats()
            for key in (
                "eviction_pressure",
                "ghost_hit_rate",
                "total_ghosts",
                "ghost_count",
            ):
                self.assertIn(key, stats)


class TestARCCacheEvictionIntegration(unittest.TestCase):
    """E8 fix (2026-06-22): integration test for the production eviction path.

    The other tests in this file exercise ``ARCCache`` directly.  In
    production, ``ARCCache.record_eviction`` is called from
    ``tier_migration.archive_cold_files`` (tier_migration.py:236) when
    a memory note gets archived to the cold tier.  This test wires the
    two together: build a temp memory directory with a note that has a
    "created" date > 90 days old, run the migration, and verify that
    the ``arc_ghosts`` table ends up with a row that references the
    archived note's stem-based memory_id.
    """

    def setUp(self):

        self.tmpdir = Path(tempfile.mkdtemp(prefix="arc_integration_"))
        self.memory_dir = self.tmpdir / "memory"
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir = self.memory_dir / "archive"
        self.db_path = self.memory_dir / "memory.db"

        # Bring the DB up to current schema so the migration can call
        # ARCCache against it without crashing.
        from infra.db_migrations import run_schema_setup
        import sqlite3

        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA foreign_keys=ON")
        run_schema_setup(conn)
        conn.commit()
        conn.close()

    def tearDown(self):
        import shutil as _shutil

        _shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_archive_cold_file_records_arc_eviction(self):
        """A note older than 90 days should produce a row in arc_ghosts."""
        # Create a memory note with a created date > 90 days in the
        # past.  We use a fixed year so the test is deterministic
        # regardless of when it runs.
        old_date = "2024-01-01"  # ~ 1.5 years old
        old_note = self.memory_dir / "lessons"
        old_note.mkdir(parents=True, exist_ok=True)
        note_path = old_note / "stale-lesson.md"
        note_path.write_text(
            f"---\n"
            f"created: {old_date}\n"
            f"tags: [test, stale]\n"
            f"---\n\n"
            f"This lesson is very old and should be archived.\n",
            encoding="utf-8",
        )

        # Run the production archive function.  We import lazily so a
        # missing tier_migration import doesn't break the rest of the
        # test class.
        from tier_migration import archive_cold_files

        result = archive_cold_files(self.memory_dir, dry_run=False)

        # The migration should have archived the note.
        self.assertGreaterEqual(
            result.get("archived", 0),
            1,
            f"expected at least 1 archived note, got {result}",
        )
        self.assertGreaterEqual(
            result.get("arc_evictions_recorded", 0),
            1,
            f"expected at least 1 arc eviction recorded, got {result}",
        )

        # And the arc_ghosts table should now contain a row for it.
        import sqlite3

        conn = sqlite3.connect(str(self.db_path))
        n = conn.execute("SELECT COUNT(*) FROM arc_ghosts").fetchone()[0]
        conn.close()
        self.assertGreaterEqual(
            n,
            1,
            f"expected arc_ghosts to be populated after tier_migration, got {n} rows",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
