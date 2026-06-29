#!/usr/bin/env python3
"""Unit tests for cron_concept_drift.py (without numpy — tests the no-embedding path).

Run with:
    ~/.config/agentic-memory/venv/bin/python eval/test_cron_concept_drift.py
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

from cron_concept_drift import _compute_centroid, _get_threshold


def _make_db(path: Path) -> sqlite3.Connection:
    from _fixtures import bootstrap_temp_db_clean

    bootstrap_temp_db_clean(path)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class TestGetThreshold(unittest.TestCase):
    def test_default_threshold(self):
        t = _get_threshold()
        self.assertAlmostEqual(t, 0.15)

    def test_env_override(self):
        os.environ["MEMORY_CONCEPT_DRIFT_THRESHOLD"] = "0.42"
        try:
            t = _get_threshold()
            self.assertAlmostEqual(t, 0.42)
        finally:
            del os.environ["MEMORY_CONCEPT_DRIFT_THRESHOLD"]


class TestComputeCentroid(unittest.TestCase):
    def test_no_embeddings_returns_none(self):
        tmpdir = Path(tempfile.mkdtemp())
        db_path = tmpdir / "test.db"
        conn = _make_db(db_path)
        centroid = _compute_centroid(conn)
        self.assertIsNone(centroid)
        conn.close()

    def test_no_embeddings_table(self):
        import importlib
        import cron_concept_drift

        importlib.reload(cron_concept_drift)
        conn = sqlite3.connect(":memory:")
        conn.executescript("""
            CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT, source_file TEXT, created_at TEXT, updated_at TEXT, observed_at TEXT);
            CREATE TABLE memory_embeddings (memory_id TEXT, content_hash TEXT, embedding BLOB, model_revision TEXT, dim INTEGER, updated_at REAL);
        """)
        centroid = cron_concept_drift._compute_centroid(conn)
        self.assertIsNone(centroid)
        conn.close()


class TestConceptDriftTable(unittest.TestCase):
    def test_concept_drift_table_exists(self):
        tmpdir = Path(tempfile.mkdtemp())
        db_path = tmpdir / "test.db"
        from _fixtures import bootstrap_temp_db_clean

        bootstrap_temp_db_clean(db_path)
        conn = sqlite3.connect(str(db_path))
        tables = set(
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        )
        conn.close()
        self.assertIn("concept_drift", tables)


if __name__ == "__main__":
    unittest.main(verbosity=2)
