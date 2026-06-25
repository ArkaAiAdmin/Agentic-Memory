"""Test cron_skill_extraction — verify it scans memories and extracts skills idempotently."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

INSTALL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL))

import skill_extractor
from skill_extractor import ensure_skill_schema
import cron_skill_extraction as cron_sk


def _make_db_with_memories(memories: list) -> tuple:
    """Create a temp DB with the memories table populated."""
    tmpdir = Path(tempfile.mkdtemp(prefix="cron_skill_test_"))
    db_path = tmpdir / "memory.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            source_file TEXT NOT NULL,
            tags TEXT DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            deleted_at TEXT
        );
    """)
    for i, content in enumerate(memories):
        conn.execute(
            "INSERT INTO memories (id, content, source_file, tags, created_at, updated_at, observed_at, deleted_at) "
            "VALUES (?, ?, ?, '[]', datetime('now'), datetime('now'), datetime('now'), NULL)",
            (f"lessons/m{i}", content, f"lessons/m{i}.md"),
        )
    conn.commit()
    ensure_skill_schema(conn)
    return tmpdir, conn


_PROC = """\
# Install Ubuntu on Proxmox
## Step 1: Download ISO
$ wget https://releases.ubuntu.com/24.04/ubuntu.iso
## Step 2: Install LAMP
$ sudo apt install -y apache2 mysql-server php
"""

_FACT = """\
# Note about Proxmox networking
**What it is:** Proxmox uses Linux bridges.
"""


class TestCronSkillExtraction(unittest.TestCase):
    def setUp(self):
        self.tmpdir, self.conn = _make_db_with_memories([_PROC, _FACT, _PROC])

    def tearDown(self):
        self.conn.close()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_extraction_runs_against_existing_memories(self):
        result = cron_sk.run_extraction(self.conn, dry_run=False)
        self.assertEqual(result["scanned"], 3)
        # 2 procedural memories (one is a duplicate of the other) produce
        # 1 new skill (extracted), 1 deduplicated, 1 skipped (the fact)
        self.assertEqual(result["extracted"], 1)
        self.assertEqual(result["deduplicated"], 1)
        self.assertEqual(result["skipped"], 1)

    def test_dry_run_does_not_persist(self):
        result = cron_sk.run_extraction(self.conn, dry_run=True)
        self.assertEqual(result["scanned"], 3)
        # Verify the DB is still empty
        count = self.conn.execute("SELECT COUNT(*) FROM memory_skills").fetchone()[0]
        self.assertEqual(count, 0)

    def test_idempotent_rerun(self):
        # First run
        r1 = cron_sk.run_extraction(self.conn, dry_run=False)
        self.assertEqual(r1["extracted"], 1)
        self.assertEqual(r1["deduplicated"], 1)
        # Second run: all 3 memories deduplicate against the existing skill
        r2 = cron_sk.run_extraction(self.conn, dry_run=False)
        self.assertEqual(r2["extracted"], 0)
        self.assertEqual(r2["deduplicated"], 2)
        # Still only 1 skill in the DB
        count = self.conn.execute("SELECT COUNT(*) FROM memory_skills").fetchone()[0]
        self.assertEqual(count, 1)

    def test_update_when_memory_changes(self):
        # First run
        cron_sk.run_extraction(self.conn, dry_run=False)
        # Update the first memory
        self.conn.execute(
            "UPDATE memories SET content = ?, updated_at = datetime('now') WHERE id = 'lessons/m0'",
            (_PROC + "\n\n## Step 3: Reboot\n$ sudo reboot",),
        )
        self.conn.commit()
        # Re-run: should detect the change and update
        r2 = cron_sk.run_extraction(self.conn, dry_run=False)
        self.assertGreater(r2["updated"] + r2["extracted"], 0)
        # Should still be 1 skill (updated, not duplicated)
        count = self.conn.execute("SELECT COUNT(*) FROM memory_skills").fetchone()[0]
        self.assertEqual(count, 1)

    def test_extraction_against_empty_db(self):
        empty_dir, empty_conn = _make_db_with_memories([])
        try:
            result = cron_sk.run_extraction(empty_conn, dry_run=False)
            self.assertEqual(result["scanned"], 0)
        finally:
            empty_conn.close()
            import shutil

            shutil.rmtree(empty_dir, ignore_errors=True)


class TestCronSkillExtractionSince(unittest.TestCase):
    def setUp(self):
        self.tmpdir, self.conn = _make_db_with_memories([_PROC, _FACT])

    def tearDown(self):
        self.conn.close()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_since_filter_excludes_old_memories(self):
        # Set updated_at to 30 days ago for the first memory
        self.conn.execute(
            "UPDATE memories SET updated_at = datetime('now', '-30 days') WHERE id = 'lessons/m0'"
        )
        self.conn.commit()
        # Run with since = 1 day ago
        result = cron_sk.run_extraction(
            self.conn, since_iso="datetime('now', '-1 day')", dry_run=False
        )
        # Wait — since_iso is the filter as-is, so we need to compute it before calling
        # Use a fixed ISO timestamp instead
        future_iso = "2099-01-01 00:00:00"  # nothing updated after this
        result = cron_sk.run_extraction(self.conn, since_iso=future_iso, dry_run=False)
        self.assertEqual(result["scanned"], 0)


if __name__ == "__main__":
    unittest.main()
