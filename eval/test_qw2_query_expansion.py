#!/usr/bin/env python3
"""QW2: Query expansion via synonym/abbreviation dictionary.

Verifies that:
  1. The _QUERY_EXPANSIONS dict is non-empty and well-formed
  2. The reverse map is built correctly (alias → canonical)
  3. _expand_query produces OR-groups for known terms
  4. _expand_query preserves quoted phrases verbatim
  5. _expand_query is a no-op for unknown terms
  6. _expand_query handles mixed known/unknown terms
  7. _expand_query dedupes aliases when the same canonical appears twice
  8. End-to-end: search_memories on a test DB finds "database" when user types "db"
  9. Backward compat: unknown-query search still works
 10. Original tokens are always kept in the expansion (no information loss)
"""

from __future__ import annotations


import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

# Make memory_mcp importable
INSTALL = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL))

import memory_mcp
from _fixtures import bootstrap_temp_db_clean


class TestQueryExpansionDict(unittest.TestCase):
    def test_01_expansions_non_empty(self):
        self.assertGreater(
            len(memory_mcp._QUERY_EXPANSIONS),
            30,
            "should have at least 30 expansion entries",
        )

    def test_02_all_values_are_lists_of_strings(self):
        for k, v in memory_mcp._QUERY_EXPANSIONS.items():
            self.assertIsInstance(k, str)
            self.assertIsInstance(v, list)
            for item in v:
                self.assertIsInstance(item, str)
                self.assertGreater(len(item), 0)

    def test_03_reverse_map_canonical_to_canonical(self):
        for canon in memory_mcp._QUERY_EXPANSIONS:
            self.assertEqual(
                memory_mcp._QUERY_EXPANSION_REVERSE.get(canon),
                canon,
                f"reverse map should map {canon!r} to itself",
            )

    def test_04_reverse_map_aliases_resolve(self):
        # "ml" should map to "ml" (its own canonical)
        self.assertEqual(memory_mcp._QUERY_EXPANSION_REVERSE.get("ml"), "ml")
        # "machine learning" should map to "ml"
        self.assertEqual(
            memory_mcp._QUERY_EXPANSION_REVERSE.get("machine learning"), "ml"
        )
        # "k8s" should map to "k8s"
        self.assertEqual(memory_mcp._QUERY_EXPANSION_REVERSE.get("k8s"), "k8s")
        # "kubernetes" should map to "k8s"
        self.assertEqual(memory_mcp._QUERY_EXPANSION_REVERSE.get("kubernetes"), "k8s")


class TestExpandQueryFunction(unittest.TestCase):
    def test_05_known_term_produces_or_group(self):
        out = memory_mcp._expand_query("db speed")
        # Should contain both "db" and "database" in an OR group
        self.assertIn('"db"', out)
        self.assertIn('"database"', out)
        # Should also preserve the unknown term "speed"
        self.assertIn('"speed"', out)
        # Should contain OR
        self.assertIn("OR", out)

    def test_06_quoted_phrases_preserved(self):
        out = memory_mcp._expand_query('"exact phrase" db')
        self.assertIn('"exact phrase"', out)
        # db should still be expanded
        self.assertIn('"database"', out)

    def test_07_unknown_term_unchanged(self):
        out = memory_mcp._expand_query("zzzqqq unknown")
        # Both should be quoted; always-OR joins them
        self.assertIn('"zzzqqq"', out)
        self.assertIn('"unknown"', out)

    def test_08_mixed_known_and_unknown(self):
        out = memory_mcp._expand_query("how does db handle auth")
        # db expanded
        self.assertIn('"db"', out)
        self.assertIn('"database"', out)
        # auth expanded
        self.assertIn('"auth"', out)
        self.assertIn('"authentication"', out)
        # "handle" is not a stop word — should appear
        self.assertIn('"handle"', out)

    def test_09_dedupes_same_canonical_twice(self):
        # "db" and "database" both canonicalize to "db"
        out = memory_mcp._expand_query("db database")
        # Count occurrences of "db" canonical form marker
        # The "(...OR...)" should appear only once for the "db" canonical
        canonical_mentions = out.count('"db"')
        # Both 'db' and 'database' input tokens should map to the same canonical,
        # so the expansion should only have one OR group for the db canonical.
        # The token "db" appears once in the OR group.
        self.assertLessEqual(
            canonical_mentions,
            1,
            f"db canonical should appear at most once in expansion, got: {out}",
        )

    def test_10_empty_query_returns_empty(self):
        self.assertEqual(memory_mcp._expand_query(""), "")
        self.assertEqual(memory_mcp._expand_query("   "), "   ")

    def test_11_acronym_resolves_full_form(self):
        # "ml" expands to "machine learning" too
        out = memory_mcp._expand_query("ml")
        self.assertIn('"ml"', out)
        self.assertIn('"machine learning"', out)


class TestEndToEndExpansion(unittest.TestCase):
    """Integration: search finds documents via expanded forms."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="qw2_test_")
        self.db_path = Path(self.tmpdir) / "memory.db"
        # H21: use full prod schema instead of inline minimal schema
        bootstrap_temp_db_clean(self.db_path)
        self._insert_test_notes()
        # Patch semantic expansion to isolate FTS+expansion testing
        self._semantic_patcher = unittest.mock.patch(
            "search.query_parser._semantic_expand", return_value=[]
        )
        self._semantic_patcher.start()

    def tearDown(self):
        self._semantic_patcher.stop()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _setup_db(self):
        # H21: this method is no-op (bootstrap_temp_db_clean does the work).
        # Kept for backward compat — the test inserts are done inline below.
        pass

    def _insert_test_notes(self):
        db = sqlite3.connect(str(self.db_path))
        # Insert: a note that says "database" but query says "db"
        import json

        db.execute(
            """INSERT INTO memories
               (id, content, source_file, tags, created_at, updated_at, observed_at, fitness_score)
               VALUES (?, ?, ?, ?, datetime('now'), datetime('now'), datetime('now'), 1.0)""",
            (
                "lessons/db-note-1",
                "We use a database for storage.",
                "lessons/db-note-1.md",
                json.dumps(["lessons"]),
            ),
        )
        # Insert: a note that says "db" (the abbreviation)
        db.execute(
            """INSERT INTO memories
               (id, content, source_file, tags, created_at, updated_at, observed_at, fitness_score)
               VALUES (?, ?, ?, ?, datetime('now'), datetime('now'), datetime('now'), 1.0)""",
            (
                "lessons/db-abbr-1",
                "Connection to the db failed.",
                "lessons/db-abbr-1.md",
                json.dumps(["lessons"]),
            ),
        )
        # Insert: an unrelated note
        db.execute(
            """INSERT INTO memories
               (id, content, source_file, tags, created_at, updated_at, observed_at, fitness_score)
               VALUES (?, ?, ?, ?, datetime('now'), datetime('now'), datetime('now'), 1.0)""",
            (
                "lessons/other-1",
                "The weather is sunny.",
                "lessons/other-1.md",
                json.dumps(["lessons"]),
            ),
        )
        db.commit()
        db.close()

    def test_12_db_query_finds_database_note(self):
        # Type "db" — should find BOTH "db" and "database" notes
        result = memory_mcp.search_memories(self.db_path, "db", limit=5)
        ids = [r["id"] for r in result.get("results", [])]
        self.assertIn(
            "lessons/db-abbr-1", ids, f"db query should find db-abbr-1, got {ids}"
        )
        self.assertIn(
            "lessons/db-note-1",
            ids,
            f"db query should ALSO find db-note-1 (via expansion), got {ids}",
        )

    def test_13_database_query_finds_db_note(self):
        # Type "database" — should find BOTH
        result = memory_mcp.search_memories(self.db_path, "database", limit=5)
        ids = [r["id"] for r in result.get("results", [])]
        self.assertIn("lessons/db-note-1", ids)
        self.assertIn(
            "lessons/db-abbr-1",
            ids,
            f"database query should find db-abbr-1 via expansion, got {ids}",
        )

    def test_14_unrelated_query_unaffected(self):
        # Type "weather" — should ONLY find weather note.
        # hybrid=False to isolate FTS5+expansion from semantic RRF.
        result = memory_mcp.search_memories(
            self.db_path, "weather", limit=5, hybrid=False
        )
        ids = [r["id"] for r in result.get("results", [])]
        self.assertIn("lessons/other-1", ids)
        self.assertNotIn("lessons/db-note-1", ids)
        self.assertNotIn("lessons/db-abbr-1", ids)

    def test_15_unknown_query_backward_compat(self):
        # "sunny" only matches "weather is sunny" — backward compat.
        # Use hybrid=False to test the FTS5+expansion path in isolation
        # (hybrid=True would also pull in semantically-similar but
        # unrelated docs, which is a separate test).
        result = memory_mcp.search_memories(
            self.db_path, "sunny", limit=5, hybrid=False
        )
        ids = [r["id"] for r in result.get("results", [])]
        self.assertIn("lessons/other-1", ids)
        self.assertEqual(len(ids), 1, f"sunny should match only one note, got {ids}")


if __name__ == "__main__":
    unittest.main()
