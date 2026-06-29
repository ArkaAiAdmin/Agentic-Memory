"""Test the skill operations — extract, search, list — from skill_extractor and cron_skill_extraction."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

INSTALL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL))

from skill_extractor import (
    ensure_skill_schema,
    save_skill,
    search_skills,
    list_skills,
    extract_skill_from_memory,
)
import cron_skill_extraction as cron_sk


_PROC = """\
# Install nginx
## Step 1: Install
$ sudo apt install -y nginx
## Step 2: Configure
$ sudo vi /etc/nginx/sites-available/default
"""


def _extract_single(conn, memory_id, dry_run=False):
    """Helper: read a memory from DB, extract skill, optionally save. Returns dict."""
    row = conn.execute(
        "SELECT id, content FROM memories WHERE id = ? AND deleted_at IS NULL",
        (memory_id,),
    ).fetchone()
    if row is None:
        return {"error": "NOT_FOUND", "message": f"Memory '{memory_id}' not found"}
    skill = extract_skill_from_memory(row["id"], row["content"])
    if skill is None:
        return {"skill": None, "message": "Content not skill-worthy"}
    if not dry_run:
        save_skill(conn, skill)
        conn.commit()
    return {"skill": skill, "saved": not dry_run}


class TestMemoryMaintenanceSkillOps(unittest.TestCase):
    """Verify skill_extract/skill_search/skill_list admin operations."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="mcp_admin_test_"))
        self.db_path = self.tmpdir / "memory.db"
        conn = sqlite3.connect(str(self.db_path))
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
        conn.execute(
            "INSERT INTO memories VALUES (?,?,?,'[]',"
            "datetime('now'),datetime('now'),datetime('now'),NULL)",
            ("lessons/nginx", _PROC, "lessons/nginx.md"),
        )
        conn.commit()
        conn.close()
        self._old_env = os.environ.get("MEMORY_DB_PATH")
        os.environ["MEMORY_DB_PATH"] = str(self.db_path)

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("MEMORY_DB_PATH", None)
        else:
            os.environ["MEMORY_DB_PATH"] = self._old_env
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _conn(self):
        c = sqlite3.connect(str(self.db_path))
        c.row_factory = sqlite3.Row
        ensure_skill_schema(c)
        return c

    def test_skill_extract_bulk(self):
        """skill_extract (no memory_id) scans all memories."""
        conn = self._conn()
        try:
            data = cron_sk.run_extraction(conn, dry_run=False)
            self.assertEqual(data["scanned"], 1)
            self.assertEqual(data["extracted"], 1)
            skills = list_skills(conn)
            self.assertEqual(len(skills), 1)
        finally:
            conn.close()

    def test_skill_extract_single_memory(self):
        """skill_extract(memory_id=...) extracts one skill."""
        conn = self._conn()
        try:
            data = _extract_single(conn, "lessons/nginx")
            self.assertIn("skill", data)
            self.assertIsNotNone(data["skill"])
            self.assertEqual(data["skill"]["name"], "install-nginx")
            self.assertTrue(data["saved"])
        finally:
            conn.close()

    def test_skill_extract_dry_run(self):
        """skill_extract(dry_run=True) doesn't persist."""
        conn = self._conn()
        try:
            data = _extract_single(conn, "lessons/nginx", dry_run=True)
            self.assertFalse(data["saved"])
            count = conn.execute("SELECT COUNT(*) FROM memory_skills").fetchone()[0]
            self.assertEqual(count, 0)
        finally:
            conn.close()

    def test_skill_extract_not_skill_worthy(self):
        """skill_extract returns null skill for non-procedural memory."""
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO memories VALUES (?,?,?,'[]',"
                "datetime('now'),datetime('now'),datetime('now'),NULL)",
                (
                    "lessons/fact",
                    "## Note\nProxmox uses Linux bridges.",
                    "lessons/fact.md",
                ),
            )
            conn.commit()
            data = _extract_single(conn, "lessons/fact")
            self.assertIsNone(data["skill"])
            self.assertIn("not skill-worthy", data["message"].lower())
        finally:
            conn.close()

    def test_skill_extract_missing_memory(self):
        """skill_extract on missing memory_id returns NOT_FOUND error."""
        conn = self._conn()
        try:
            data = _extract_single(conn, "lessons/missing")
            self.assertEqual(data["error"], "NOT_FOUND")
        finally:
            conn.close()

    def test_skill_search_finds_extracted_skill(self):
        """After extraction, skill_search finds the skill."""
        conn = self._conn()
        try:
            cron_sk.run_extraction(conn)
            results = search_skills(conn, "install nginx", limit=5)
            self.assertGreater(len(results), 0)
            self.assertEqual(results[0]["topic"], "install-nginx")
        finally:
            conn.close()

    def test_skill_search_empty_query(self):
        """skill_search with empty query returns no results."""
        conn = self._conn()
        try:
            results = search_skills(conn, "", limit=5)
            self.assertEqual(len(results), 0)
        finally:
            conn.close()

    def test_skill_list_returns_all(self):
        """skill_list returns the extracted skill."""
        conn = self._conn()
        try:
            cron_sk.run_extraction(conn)
            skills = list_skills(conn, limit=10)
            self.assertEqual(len(skills), 1)
            self.assertEqual(skills[0]["name"], "install-nginx")
        finally:
            conn.close()

    def test_skill_list_empty(self):
        """skill_list on empty DB returns 0."""
        conn = self._conn()
        try:
            skills = list_skills(conn, limit=10)
            self.assertEqual(len(skills), 0)
        finally:
            conn.close()


class TestMemoryMaintenanceOpMap(unittest.TestCase):
    """Verify the skill operations work through the actual code paths."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="mcp_opmap_test_"))
        self.db_path = self.tmpdir / "memory.db"
        conn = sqlite3.connect(str(self.db_path))
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
        conn.commit()
        ensure_skill_schema(conn)
        self.conn = conn

    def tearDown(self):
        self.conn.close()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_skill_ops_accessible(self):
        """Direct skill operations execute correctly (no Unknown operation)."""
        self.assertEqual(list_skills(self.conn, limit=10), [])
        self.assertEqual(search_skills(self.conn, "", limit=5), [])
        result = cron_sk.run_extraction(self.conn, dry_run=True)
        self.assertEqual(result["scanned"], 0)


if __name__ == "__main__":
    unittest.main()
