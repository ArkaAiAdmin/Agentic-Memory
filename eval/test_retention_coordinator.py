#!/usr/bin/env python3
"""Unit tests for the unified retention coordinator pipeline."""

import sys
import unittest
import json
from pathlib import Path

# Add repo root to sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from background.retention_coordinator import run_retention_pipeline
from infra.db import open_db


class TestRetentionCoordinator(unittest.TestCase):
    def setUp(self):
        self.db_path = Path("/tmp/test_retention_coord.db")
        if self.db_path.exists():
            self.db_path.unlink()
            
        # Initialize a clean DB via open_db to automatically bootstrap full migrations
        with open_db(self.db_path) as conn:
            pass

    def tearDown(self):
        if self.db_path.exists():
            try:
                self.db_path.unlink()
            except Exception:
                pass

    def test_retention_pipeline_integration(self):
        # 1. Insert dummy memories in the bootstrapped database
        with open_db(self.db_path) as conn:
            conn.execute(
                "INSERT INTO memories (id, content, source_file, created_at, updated_at, observed_at, fitness_score, importance, access_count, last_accessed, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("note-1", "This is an important memory about project alpha.", "/tmp/test.md", "2026-07-01T12:00:00", "2026-07-01T12:00:00", "2026-07-01T12:00:00", 0.8, 4, 5, "2026-07-01T12:00:00", "{}")
            )
            # Add access logs to trigger adaptive retention half-life increase
            conn.execute(
                "INSERT INTO user_access_log (note_id, access_ts, source) VALUES (?, ?, ?)",
                ("note-1", 123456789.0, "test")
            )
            conn.commit()

        # Mock AR enabling to guarantee execution
        import sys
        import adaptive_retention as ar
        ar.ADAPTIVE_RETENTION_ENABLED = True
        sys.modules["adaptive_retention"] = ar
        sys.modules["background.adaptive_retention"] = ar

        # Run pipeline
        results = run_retention_pipeline(self.db_path)
        self.assertIn("adaptive_retention", results)
        self.assertIn("neural_forget", results)

        # Check that metadata has half-life stored and score computed
        with open_db(self.db_path) as conn:
            row = conn.execute("SELECT score, metadata FROM memories WHERE id='note-1'").fetchone()
            self.assertIsNotNone(row)
            score, meta_str = row
            meta = json.loads(meta_str or "{}")
            self.assertIn("adaptive_halflife_days", meta)
            self.assertIsNotNone(score)


if __name__ == "__main__":
    unittest.main()
