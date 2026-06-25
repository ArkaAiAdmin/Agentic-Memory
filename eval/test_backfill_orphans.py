#!/usr/bin/env python3
"""Unit tests for backfill_orphans.py.

Run with:
    ~/.config/agentic-memory/venv/bin/python eval/test_backfill_orphans.py
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

from backfill_orphans import cleanup


def _make_db(path: Path) -> sqlite3.Connection:
    from _fixtures import bootstrap_temp_db_clean

    bootstrap_temp_db_clean(path)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys=OFF")
    return conn


def _seed_memory(conn: sqlite3.Connection, mem_id: str = "test/mem-1"):
    conn.execute(
        "INSERT INTO memories (id, content, source_file, created_at, updated_at, observed_at) "
        "VALUES (?, 'content', ?, datetime('now'), datetime('now'), datetime('now'))",
        (mem_id, f"{mem_id}.md"),
    )


class TestBackfillOrphansCleanup(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "test.db"

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_cleanup_empty_db(self):
        conn = _make_db(self.db_path)
        counts = cleanup(conn)
        self.assertIsInstance(counts, dict)
        for v in counts.values():
            self.assertIsInstance(v, int)
        conn.close()

    def test_orphan_facts_deleted(self):
        conn = _make_db(self.db_path)
        _seed_memory(conn, "test/mem-1")
        conn.execute(
            "INSERT INTO kg_facts (subject, predicate, object, confidence, source_memory, first_seen, last_seen) "
            "VALUES ('Subj', 'is_a', 'Obj', 0.9, 'nonexistent', datetime('now'), datetime('now'))"
        )
        conn.commit()
        counts = cleanup(conn)
        self.assertEqual(counts["kg_facts_deleted"], 1)
        conn.close()

    def test_orphan_edges_deleted(self):
        conn = _make_db(self.db_path)
        conn.execute(
            "INSERT INTO kg_entities (name, entity_type, mentions, created_at, updated_at) "
            "VALUES ('Entity-A', 'concept', 1, datetime('now'), datetime('now'))"
        )
        conn.execute(
            "INSERT INTO kg_edges (source_id, target_id, relation, weight, created_at, valid_at, invalid_at) "
            "VALUES (999, 1, 'related_to', 1.0, datetime('now'), datetime('now'), '9999-12-31T23:59:59')"
        )
        conn.commit()
        counts = cleanup(conn)
        self.assertEqual(counts["kg_edges_deleted"], 1)
        conn.close()

    def test_orphan_entities_deleted(self):
        conn = _make_db(self.db_path)
        conn.execute(
            "INSERT INTO kg_entities (name, entity_type, mentions, created_at, updated_at) "
            "VALUES ('Lonely', 'concept', 1, datetime('now'), datetime('now'))"
        )
        conn.commit()
        counts = cleanup(conn)
        self.assertEqual(counts["kg_entities_deleted"], 1)
        conn.close()

    def test_orphan_chunks_deleted(self):
        conn = _make_db(self.db_path)
        conn.execute(
            "INSERT INTO memory_chunks (parent_id, chunk_idx, start_offset, end_offset, content, created_at) "
            "VALUES ('nonexistent', 0, 0, 5, 'chunk', datetime('now'))"
        )
        conn.commit()
        counts = cleanup(conn)
        self.assertEqual(counts["chunks_deleted"], 1)
        conn.close()

    def test_orphan_embeddings_deleted(self):
        conn = _make_db(self.db_path)
        conn.execute(
            "INSERT INTO memory_embeddings (memory_id, content_hash, embedding, model_revision, dim, updated_at) "
            "VALUES ('nonexistent', 'abc', X'00', 'v1', 256, 1.0)"
        )
        conn.commit()
        counts = cleanup(conn)
        self.assertEqual(counts["embeddings_deleted"], 1)
        conn.close()

    def test_orphan_vec_keys_deleted(self):
        conn = _make_db(self.db_path)
        conn.execute(
            "INSERT INTO memory_vec_keys (memory_id, key) VALUES ('nonexistent', 42)"
        )
        conn.commit()
        counts = cleanup(conn)
        self.assertEqual(counts["vec_keys_deleted"], 1)
        conn.close()

    def test_cleanup_is_idempotent(self):
        conn = _make_db(self.db_path)
        conn.execute(
            "INSERT INTO kg_facts (subject, predicate, object, confidence, source_memory, first_seen, last_seen) "
            "VALUES ('S', 'is_a', 'O', 0.9, 'nonexistent', datetime('now'), datetime('now'))"
        )
        conn.commit()
        counts1 = cleanup(conn)
        self.assertEqual(counts1["kg_facts_deleted"], 1)
        counts2 = cleanup(conn)
        self.assertEqual(counts2["kg_facts_deleted"], 0)
        conn.close()

    def test_healthy_data_left_alone(self):
        conn = _make_db(self.db_path)
        _seed_memory(conn, "test/mem-1")
        conn.execute(
            "INSERT INTO memory_chunks (parent_id, chunk_idx, start_offset, end_offset, content, created_at) "
            "VALUES ('test/mem-1', 0, 0, 5, 'ok', datetime('now'))"
        )
        conn.execute(
            "INSERT INTO memory_embeddings (memory_id, content_hash, embedding, model_revision, dim, updated_at) "
            "VALUES ('test/mem-1', 'abc', X'00', 'v1', 256, 1.0)"
        )
        conn.execute(
            "INSERT INTO memory_vec_keys (memory_id, key) VALUES ('test/mem-1', 0)"
        )
        conn.commit()
        counts = cleanup(conn)
        self.assertEqual(counts["chunks_deleted"], 0)
        self.assertEqual(counts["embeddings_deleted"], 0)
        self.assertEqual(counts["vec_keys_deleted"], 0)
        conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
