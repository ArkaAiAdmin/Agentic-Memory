import os
import sys
import unittest
import tempfile
import sqlite3
from pathlib import Path

INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))

from search.query_parser import _parse_search_query
from save.session_end_extractor import extract_session_findings

class TestSearchPrecisionFixes(unittest.TestCase):
    def test_adjacent_bigrams_generation(self):
        # 1. Verify that bigrams are generated for bare words
        # Pass a mock Path that points to a temporary DB directory to avoid /tmp.flock locking issues
        tmpdir = tempfile.mkdtemp()
        db_path = Path(tmpdir) / "memory.db"
        norm, fts, bare, kg = _parse_search_query("double sigmoid bug", db_path)
        self.assertIn('"double sigmoid"', fts)
        self.assertIn('"sigmoid bug"', fts)
        self.assertIn('"double"', fts)
        self.assertIn('"sigmoid"', fts)
        self.assertIn('"bug"', fts)

    def test_inline_findings_extraction(self):
        # 2. Test findings extraction from session notes
        tmpdir = tempfile.mkdtemp()
        db_path = Path(tmpdir) / "memory.db"
        
        # Initialize basic test database schema
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                content TEXT,
                category TEXT,
                tags TEXT DEFAULT '[]',
                source_file TEXT DEFAULT '',
                pinned INTEGER DEFAULT 0,
                is_global INTEGER DEFAULT 0,
                metadata TEXT DEFAULT '{}',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                accessed_at TEXT DEFAULT (datetime('now')),
                importance INTEGER DEFAULT 2,
                deleted_at TEXT
            )
        """)
        
        # Write a memory note containing a clear resolved/finding pattern
        now_iso = "2026-07-13T12:00:00Z"
        conn.execute(
            "INSERT INTO memories (id, content, category, importance, created_at, updated_at) "
            "VALUES ('sessions/end-test', 'We investigated the issue. Fixed: resolved the double sigmoid bug by patching query_parser.', 'sessions', 2, ?, ?)",
            (now_iso, now_iso)
        )
        conn.commit()
        conn.close()
        
        # Patch active memory dir and database connection logic
        from unittest import mock
        with mock.patch("infra.infrastructure.resolve_active_memory_dir", return_value=Path(tmpdir)):
            # Mock save_memory_auto to just write directly into the test sqlite db (for simplicity in unit testing)
            def mock_save_memory_auto(content, category, title_slug, tags, pinned, importance, safety_wiring):
                conn_test = sqlite3.connect(str(db_path))
                conn_test.execute(
                    "INSERT INTO memories (id, content, category, importance, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))",
                    (f"lessons/{title_slug}", content, category, importance)
                )
                conn_test.commit()
                conn_test.close()
                return f"lessons/{title_slug}"

            with mock.patch("save.pipeline.save_memory_auto", side_effect=mock_save_memory_auto):
                marker = {
                    "first_tool_at": 1783936800, # 2026-07-13 12:00:00
                }
                res = extract_session_findings(marker)
                self.assertEqual(res.get("extracted"), 1)
                
                # Verify that a new lesson note was created in the DB
                conn = sqlite3.connect(str(db_path))
                rows = conn.execute(
                    "SELECT id, content, category, importance FROM memories WHERE category='lessons'"
                ).fetchall()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0][3], 3) # importance=3
                self.assertIn("Fixed: resolved the double sigmoid bug", rows[0][1])
                conn.close()

if __name__ == "__main__":
    unittest.main()
