"""Tests for cron_skill_decay — especially _delete_skills disk cleanup."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

INSTALL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL))

from cron_skill_decay import _delete_skills


class TestDeleteSkillsDiskCleanup(unittest.TestCase):
    """_delete_skills must remove disk dirs in addition to DB rows."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="skill_decay_test_"))
        self.skills_dir = self.tmpdir / ".agents" / "skills"
        self.skills_dir.mkdir(parents=True)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE memory_skills (
                id INTEGER PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                topic TEXT,
                description TEXT,
                triggers TEXT DEFAULT '[]',
                steps TEXT DEFAULT '[]',
                content_hash TEXT,
                source_memory_id TEXT,
                hit_count INTEGER DEFAULT 0,
                hit_vector TEXT DEFAULT '{}',
                last_used_vector TEXT DEFAULT '{}',
                logical_clock INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)
        conn.commit()
        return conn

    def test_delete_skills_removes_db_row_and_disk_dir(self):
        """Deleting a skill must remove both the DB row and the disk dir."""
        conn = self._make_conn()
        conn.execute(
            "INSERT INTO memory_skills (name, topic, description, triggers, steps, content_hash, source_memory_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
            ("junk-test-skill", "test", "A test skill", json.dumps(["test"]), json.dumps(["$ do something"]), "abc123", "m1"),
        )
        conn.commit()

        # Create the corresponding disk dir
        skill_dir = self.skills_dir / "junk-test-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# Junk test skill")

        self.assertTrue(skill_dir.is_dir())

        with patch("pathlib.Path.home", return_value=self.tmpdir):
            count = _delete_skills(conn, ["junk-test-skill"])

        self.assertEqual(count, 1)

        # DB row must be gone
        row = conn.execute(
            "SELECT COUNT(*) FROM memory_skills WHERE name = ?", ("junk-test-skill",)
        ).fetchone()
        self.assertEqual(row[0], 0)

        # Disk dir must be gone
        self.assertFalse(skill_dir.exists())

        conn.close()

    def test_delete_skills_ignores_missing_disk_dir(self):
        """Deleting a skill that has no disk dir must not error."""
        conn = self._make_conn()
        conn.execute(
            "INSERT INTO memory_skills (name, topic, description, triggers, steps, content_hash, source_memory_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
            ("missing-dir-skill", "test", "A test skill", json.dumps(["test"]), json.dumps(["$ do something"]), "abc123", "m1"),
        )
        conn.commit()

        with patch("pathlib.Path.home", return_value=self.tmpdir):
            count = _delete_skills(conn, ["missing-dir-skill"])

        self.assertEqual(count, 1)
        row = conn.execute(
            "SELECT COUNT(*) FROM memory_skills WHERE name = ?", ("missing-dir-skill",)
        ).fetchone()
        self.assertEqual(row[0], 0)
        conn.close()

    def test_delete_skills_empty_list_returns_zero(self):
        """Deleting an empty list must return 0 without errors."""
        conn = self._make_conn()
        count = _delete_skills(conn, [])
        self.assertEqual(count, 0)
        conn.close()


if __name__ == "__main__":
    unittest.main()