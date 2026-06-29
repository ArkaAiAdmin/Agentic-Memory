"""Tests for skill_extractor.py — push coverage from 94% to 100%."""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

os.chdir(os.path.dirname(os.path.abspath(__file__)))
os.chdir("..")
sys.path.insert(0, os.getcwd())


class TestSkillWorthy(unittest.TestCase):
    def test_empty_content_not_worthy(self):
        from skill_extractor import is_skill_worthy

        self.assertFalse(is_skill_worthy(""))
        self.assertFalse(is_skill_worthy("too short"))

    def test_procedural_content_is_worthy(self):
        from skill_extractor import is_skill_worthy

        c = "## Step 1: Install\n$ sudo apt install nginx\n## Step 2: Configure\nEdit the config file."
        self.assertTrue(is_skill_worthy(c))

    def test_fact_content_not_worthy(self):
        from skill_extractor import is_skill_worthy

        self.assertFalse(is_skill_worthy("## What is nginx\nNginx is a web server."))


class TestExtractSkill(unittest.TestCase):
    def test_extract_returns_none_for_non_skill(self):
        from skill_extractor import extract_skill_from_memory

        self.assertIsNone(
            extract_skill_from_memory("test/a", "## Summary\nJust a note.")
        )

    def test_extract_returns_dict_for_skill(self):
        from skill_extractor import extract_skill_from_memory

        r = extract_skill_from_memory(
            "test/skill",
            "## Install Nginx\n$ sudo apt install nginx\n## Configure\n$ sudo vi /etc/nginx/conf",
        )
        self.assertIsNotNone(r)
        self.assertIn("name", r)
        self.assertIn("triggers", r)


class TestSaveSkill(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="sk_"))
        self.db_path = self.tmpdir / "memory.db"
        c = sqlite3.connect(str(self.db_path))
        c.execute("PRAGMA journal_mode=WAL")
        from skill_extractor import ensure_skill_schema

        ensure_skill_schema(c)
        c.commit()
        c.close()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_save_skill_inserts(self):
        from skill_extractor import save_skill

        c = sqlite3.connect(str(self.db_path))
        sid = save_skill(
            c,
            {
                "name": "install-nginx",
                "topic": "Install Nginx",
                "triggers": ["nginx"],
                "steps": ["step1"],
            },
        )
        self.assertGreater(sid, 0)
        n = c.execute(
            "SELECT COUNT(*) FROM memory_skills WHERE name='install-nginx'"
        ).fetchone()[0]
        c.close()
        self.assertEqual(n, 1)

    def test_save_skill_idempotent(self):
        from skill_extractor import save_skill

        c = sqlite3.connect(str(self.db_path))
        s = {
            "name": "test-skill",
            "topic": "Test",
            "triggers": ["test"],
            "content_hash": "abc123",
        }
        sid1 = save_skill(c, s.copy())
        sid2 = save_skill(c, s.copy())
        c.close()
        self.assertEqual(sid1, sid2)


class TestSearchSkills(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="sk_"))
        self.db_path = self.tmpdir / "memory.db"
        c = sqlite3.connect(str(self.db_path))
        from skill_extractor import ensure_skill_schema, save_skill

        ensure_skill_schema(c)
        save_skill(
            c,
            {
                "name": "nginx-install",
                "topic": "Install Nginx",
                "triggers": ["nginx", "install", "apt"],
                "steps": ["step1"],
            },
        )
        c.commit()
        c.close()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_search_skills_finds_match(self):
        from skill_extractor import search_skills

        c = sqlite3.connect(str(self.db_path))
        r = search_skills(c, "install nginx server", limit=5)
        c.close()
        self.assertGreater(len(r), 0)

    def test_search_skills_empty_query(self):
        from skill_extractor import search_skills

        c = sqlite3.connect(str(self.db_path))
        r = search_skills(c, "")
        c.close()
        self.assertEqual(r, [])


class TestRecordHit(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="sk_"))
        self.db_path = self.tmpdir / "memory.db"
        c = sqlite3.connect(str(self.db_path))
        from skill_extractor import ensure_skill_schema, save_skill

        ensure_skill_schema(c)
        save_skill(c, {"name": "test", "topic": "T", "triggers": ["t"]})
        c.commit()
        c.close()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_record_skill_hit_increments(self):
        from skill_extractor import record_skill_hit

        c = sqlite3.connect(str(self.db_path))
        h0 = c.execute(
            "SELECT hit_count FROM memory_skills WHERE name='test'"
        ).fetchone()[0]
        record_skill_hit(c, 1)
        h1 = c.execute(
            "SELECT hit_count FROM memory_skills WHERE name='test'"
        ).fetchone()[0]
        c.close()
        self.assertGreater(h1, h0)


if __name__ == "__main__":
    unittest.main()
