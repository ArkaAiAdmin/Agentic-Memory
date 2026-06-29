"""Tests for the zero-result UX payloads.

When `search_memories` returns 0 hits it should still give the caller
useful next-step suggestions: did-you-mean spellings, recent tags,
recent notes, and recent source files. These tests verify the four
helper functions that assemble those payloads.
"""
import unittest
import sqlite3
import tempfile
from pathlib import Path

from memory_mcp import (
    _did_you_mean,
    _top_recent_notes,
    _top_recent_tags,
    _top_recent_source_files,
    _build_zero_result_suggestions,
)


class TestZeroResultUX(unittest.TestCase):
    def setUp(self):
        """Create a temp DB with the minimal schema the helpers need."""
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "memory.db"
        with sqlite3.connect(str(self.db)) as conn:
            conn.executescript("""
                CREATE TABLE memories (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    source_file TEXT,
                    tags TEXT DEFAULT '[]',
                    created_at TEXT,
                    updated_at TEXT,
                    observed_at TEXT,
                    deleted_at TEXT
                );
                INSERT INTO memories VALUES
                    ('n1', 'first note about python', 'a.md', '[]', '2026-01-01', '2026-01-01', '2026-01-01', NULL),
                    ('n2', 'second note about java',   'b.md', '["tag1"]', '2026-01-02', '2026-01-02', '2026-01-02', NULL);
            """)

    def test_top_recent_notes(self):
        result = _top_recent_notes(self.db, limit=5)
        self.assertEqual(len(result), 2)
        # Newer row (n2) should come first.
        self.assertEqual(result[0]["id"], "n2")
        self.assertEqual(result[0]["observed_at"], "2026-01-02")
        # Previews are truncated to 80 chars.
        self.assertIn("preview", result[0])
        self.assertTrue(len(result[0]["preview"]) <= 80)

    def test_top_recent_tags(self):
        result = _top_recent_tags(self.db, limit=5)
        # Only one row has a non-empty tag list.
        self.assertEqual(len(result), 1)
        self.assertIn("tag", result[0])
        self.assertEqual(result[0]["tag"], '["tag1"]')
        self.assertIn("latest_observed_at", result[0])

    def test_top_recent_source_files(self):
        result = _top_recent_source_files(self.db, limit=5)
        self.assertEqual(len(result), 2)
        for row in result:
            self.assertIn("source_file", row)
            self.assertIn("count", row)
            self.assertIn("latest_observed_at", row)
        # Both source files should appear (order is by latest observed_at).
        sources = {row["source_file"] for row in result}
        self.assertEqual(sources, {"a.md", "b.md"})

    def test_did_you_mean_uses_synonyms(self):
        syn_map = {"python": ["py", "python3"]}
        result = _did_you_mean("python tutorial", syn_map)
        self.assertGreater(len(result), 0)
        # The first expansion should replace "python" with the first synonym
        # while keeping the trailing word intact.
        self.assertIn("py", result[0])
        self.assertIn("tutorial", result[0])

    def test_did_you_mean_no_match_returns_empty(self):
        syn_map = {"foo": ["bar"]}
        result = _did_you_mean("hello world", syn_map)
        self.assertEqual(result, [])

    def test_build_zero_result_suggestions_full_shape(self):
        """Top-level helper returns a dict with the 4 documented channels."""
        result = _build_zero_result_suggestions(self.db, "python tutorial")
        self.assertIn("did_you_mean", result)
        self.assertIn("by_tag", result)
        self.assertIn("by_recency", result)
        self.assertIn("by_source_file", result)
        # Each channel must be a list (possibly empty).
        for key in ("did_you_mean", "by_tag", "by_recency", "by_source_file"):
            self.assertIsInstance(result[key], list)

    def test_did_you_mean_handles_empty_query(self):
        """Empty / None query should short-circuit to an empty list, not raise."""
        self.assertEqual(_did_you_mean("", {"a": ["b"]}), [])
        self.assertEqual(_did_you_mean("anything", {}), [])

    def test_did_you_mean_caps_at_three(self):
        """At most 3 expansions are returned even when more match."""
        syn_map = {
            "a": ["a1", "a2"],
            "b": ["b1", "b2"],
            "c": ["c1", "c2"],
            "d": ["d1", "d2"],
        }
        result = _did_you_mean("a b c d", syn_map)
        self.assertLessEqual(len(result), 3)

    def test_top_recent_tags_skips_empty_tag_lists(self):
        """Rows with `tags = '[]'` must not appear in the tag channel."""
        result = _top_recent_tags(self.db, limit=10)
        for row in result:
            self.assertNotEqual(row["tag"], "[]")

    def test_helpers_tolerate_missing_db(self):
        """A non-existent DB must degrade to [], never raise."""
        bogus = self.tmp / "does_not_exist.db"
        self.assertEqual(_top_recent_tags(bogus, limit=5), [])
        self.assertEqual(_top_recent_notes(bogus, limit=5), [])
        self.assertEqual(_top_recent_source_files(bogus, limit=5), [])


if __name__ == "__main__":
    unittest.main()
