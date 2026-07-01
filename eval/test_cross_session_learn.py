#!/usr/bin/env python3
"""Unit tests for cross_session_learn.py."""

import sys
import sqlite3
import tempfile
import unittest
import shutil
from pathlib import Path

# Make cross_session_learn importable
INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

import cross_session_learn  # noqa: E402
from infra.db_migrations import run_schema_setup  # noqa: E402


class TestCrossSessionLearn(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="cross_session_test_")
        self.db_path = Path(self.tmpdir) / "test.db"
        self.session_dir = Path(self.tmpdir) / "sessions"
        self.session_dir.mkdir(parents=True, exist_ok=True)

        # Patch resolve_active_memory_dir to point to our temp directory
        from unittest.mock import patch

        self._patcher = patch(
            "cross_session_learn.resolve_active_memory_dir",
            return_value=Path(self.tmpdir),
        )
        self._patcher.start()

        # P0-5 fix: use the full prod schema (via run_schema_setup)
        # so the cross_session_learn row write — which now routes
        # through save_pipeline.upsert_row — has every column
        # _upsert_memory_row expects (metadata, fitness_score,
        # importance, repo_id, valid_from, valid_to, superseded_by).
        self.conn = sqlite3.connect(self.db_path)
        run_schema_setup(self.conn)
        self.conn.commit()

    def tearDown(self):
        self._patcher.stop()
        self.conn.close()
        try:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
        except Exception:
            pass

    def test_learn_reusable_pattern(self):
        # Create a session file containing reusable patterns, padded to be >200 chars
        session_content = """# Solving the Bug
We resolved the import error in the API call by using the workaround.
Here are the steps to set up the configuration pipeline.
This is a long line to pad the text content of the session note.
Adding more details about our debugging session to make sure it is recognized
and scanned properly by the cross-session pattern learning subsystem.
We want to verify that lessons are extracted from sessions automatically.
"""
        session_file = self.session_dir / "session_abc.md"
        session_file.write_text(session_content, encoding="utf-8")

        stats = cross_session_learn.scan_sessions_and_learn(
            self.conn, days=7, dry_run=False
        )
        self.assertEqual(stats["sessions_scanned"], 1)
        self.assertEqual(stats["lessons_created"], 1)

        # Verify insertion did not fail and columns are correct
        row = self.conn.execute(
            "SELECT id, category, content, source_file, tier FROM memories"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertTrue(row[0].startswith("lessons/cross-session-"))
        self.assertEqual(row[1], "lessons")
        self.assertIn("workaround", row[2])
        self.assertTrue(row[3].startswith("lessons/cross-session-"))
        self.assertEqual(row[4], "warm")

    def test_skip_duplicates(self):
        # Padded to >200 chars
        session_content = """# Setup config
This contains steps to setup configuration env var.
This is a helpful workflow.
We are padding this file with extra characters to ensure it exceeds the minimum length requirement.
It has a clear pattern keyword workflow and setup configuration.
"""
        session_file = self.session_dir / "session_xyz.md"
        session_file.write_text(session_content, encoding="utf-8")

        # Insert a pre-existing lesson with a similar title
        self.conn.execute(
            "INSERT INTO memories (id, content, category, source_file, created_at, updated_at, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "lessons/existing",
                "# Lesson: Setup config\nExisting lesson here.",
                "lessons",
                "lessons/existing.md",
                "2026-06-01T00:00:00",
                "2026-06-01T00:00:00",
                "2026-06-01T00:00:00",
            ),
        )
        self.conn.commit()

        stats = cross_session_learn.scan_sessions_and_learn(
            self.conn, days=7, dry_run=False
        )
        self.assertEqual(stats["sessions_scanned"], 1)
        self.assertEqual(stats["skipped_duplicates"], 1)
        self.assertEqual(stats["lessons_created"], 0)


if __name__ == "__main__":
    unittest.main()
