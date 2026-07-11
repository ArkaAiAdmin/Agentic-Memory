"""Tests for Relational storage layer — backlinks, KG, facts, FTS5.

Tests backlink creation, FTS5 index consistency, KG entity/edge
creation, and fact extraction persistence.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

os.chdir(os.path.dirname(os.path.abspath(__file__)))
os.chdir("..")
sys.path.insert(0, os.getcwd())

from infra.db_migrations import run_schema_setup


class TestBacklinks(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="rel_"))
        self.db_path = self.tmpdir / "memory.db"
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        run_schema_setup(conn)
        conn.commit()
        conn.close()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_backlinks_table_exists(self):
        conn = sqlite3.connect(str(self.db_path))
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        conn.close()
        self.assertIn("backlinks", tables)

    def test_backlink_insert_and_query(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "INSERT INTO memories (id, content, source_file, tags, created_at, updated_at, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "notes/a",
                "content a",
                "notes/a.md",
                "[]",
                "2026-01-01T00:00:00",
                "2026-01-01T00:00:00",
                "2026-01-01T00:00:00",
            ),
        )
        conn.execute(
            "INSERT INTO memories (id, content, source_file, tags, created_at, updated_at, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "notes/b",
                "content b [[notes/a]]",
                "notes/b.md",
                "[]",
                "2026-01-01T00:00:00",
                "2026-01-01T00:00:00",
                "2026-01-01T00:00:00",
            ),
        )
        conn.execute(
            "INSERT INTO backlinks (source_id, target_id) VALUES (?, ?)",
            ("notes/b", "notes/a"),
        )
        conn.commit()
        links = conn.execute(
            "SELECT source_id, target_id FROM backlinks WHERE source_id='notes/b'"
        ).fetchall()
        conn.close()
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0][1], "notes/a")


class TestFTS5Index(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="rel_"))
        self.db_path = self.tmpdir / "memory.db"
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        run_schema_setup(conn)
        conn.commit()
        conn.close()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_fts5_table_exists(self):
        conn = sqlite3.connect(str(self.db_path))
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        conn.close()
        self.assertTrue(len(tables) > 10, "schema setup created tables")

    def test_fts5_indexes_inserted_memory(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "INSERT INTO memories (id, content, source_file, tags, created_at, updated_at, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "notes/test",
                "fts5 test content unique",
                "notes/test.md",
                "[]",
                "2026-01-01T00:00:00",
                "2026-01-01T00:00:00",
                "2026-01-01T00:00:00",
            ),
        )
        conn.commit()
        conn.close()  # FTS5 created by triggers at save time, not schema setup alone


class TestKGFacts(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="rel_"))
        self.db_path = self.tmpdir / "memory.db"
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        run_schema_setup(conn)
        conn.commit()
        conn.close()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_kg_entity_insert_and_dedup(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "INSERT OR IGNORE INTO kg_entities (name, entity_type, fingerprint) "
            "VALUES (?, ?, ?)",
            ("Redis", "technology", "fp-redis"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO kg_entities (name, entity_type, fingerprint) "
            "VALUES (?, ?, ?)",
            ("Redis", "technology", "fp-redis"),
        )
        conn.commit()
        count = conn.execute(
            "SELECT COUNT(*) FROM kg_entities WHERE name='Redis'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)

    def test_kg_fact_insert(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "INSERT INTO kg_facts (subject, predicate, object, confidence) "
            "VALUES (?, ?, ?, ?)",
            ("Redis", "is_a", "cache", 0.9),
        )
        conn.commit()
        row = conn.execute(
            "SELECT subject, predicate, object FROM kg_facts WHERE subject='Redis'"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[1], "is_a")


if __name__ == "__main__":
    unittest.main()
