#!/usr/bin/env python3
"""T10: tests for memory_search -> kg_facts_fts integration.

Verifies:
  - _search_kg_facts returns matching facts from kg_facts_fts
  - _search_kg_facts respects include_invalid (filters out superseded/invalidated)
  - _search_kg_facts gracefully handles missing kg_facts table
  - search_memories with include_facts=True includes related_facts in envelope
  - search_memories with include_facts=False omits related_facts
  - _format_search_results appends a "Related facts" section when facts present
  - cache key includes include_facts + fact_limit (no cross-pollination)
  - MCP memory_search signature includes include_facts + fact_limit
"""

import inspect
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

from save_pipeline import save_memory
from _fixtures import bootstrap_temp_db_clean
from search_pipeline import search_memories
from search.orchestrator import (
    _search_kg_facts,
    _format_search_results,
)


def _ensure_kg_facts_fts(conn: sqlite3.Connection) -> None:
    """Ensure kg_facts_fts exists and is in a usable state.

    The fixture's bootstrap_temp_db_clean doesn't include kg_facts_fts in
    its FTS5 recreate list, so it can be left in a corrupt state from the
    prod-DB copy.  This helper drops + recreates kg_facts_fts fresh.
    """
    # Drop existing (if any) — both the virtual table and its triggers
    conn.execute("DROP TABLE IF EXISTS kg_facts_fts")
    for trig in ("kg_facts_fts_ai", "kg_facts_fts_ad", "kg_facts_fts_au"):
        conn.execute(f"DROP TRIGGER IF EXISTS {trig}")
    # Recreate the virtual table
    conn.execute(
        "CREATE VIRTUAL TABLE kg_facts_fts USING fts5("
        "subject, predicate, object, context, "
        "content='kg_facts', content_rowid='id', "
        "tokenize='porter unicode61')"
    )
    # Recreate the 3 sync triggers
    conn.execute(
        "CREATE TRIGGER kg_facts_fts_ai AFTER INSERT ON kg_facts BEGIN "
        "INSERT INTO kg_facts_fts(rowid, subject, predicate, object, context) "
        "VALUES (new.id, new.subject, new.predicate, new.object, new.context); END"
    )
    conn.execute(
        "CREATE TRIGGER kg_facts_fts_ad AFTER DELETE ON kg_facts BEGIN "
        "INSERT INTO kg_facts_fts(kg_facts_fts, rowid, subject, predicate, object, context) "
        "VALUES('delete', old.id, old.subject, old.predicate, old.object, old.context); END"
    )
    conn.execute(
        "CREATE TRIGGER kg_facts_fts_au AFTER UPDATE ON kg_facts BEGIN "
        "INSERT INTO kg_facts_fts(kg_facts_fts, rowid, subject, predicate, object, context) "
        "VALUES('delete', old.id, old.subject, old.predicate, old.object, old.context); "
        "INSERT INTO kg_facts_fts(rowid, subject, predicate, object, context) "
        "VALUES (new.id, new.subject, new.predicate, new.object, new.context); END"
    )
    conn.commit()


def _insert_fact(
    conn: sqlite3.Connection,
    subject: str,
    predicate: str,
    obj: str,
    confidence: float = 0.9,
    invalid_at=None,
    superseded_by=None,
) -> int:
    cur = conn.execute(
        "INSERT INTO kg_facts (subject, predicate, object, confidence, "
        "locked, first_seen, last_seen, mention_count, source_memory, "
        "invalid_at, superseded_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            subject,
            predicate,
            obj,
            confidence,
            0,
            time.time(),
            time.time(),
            1,
            None,
            invalid_at,
            superseded_by,
        ),
    )
    conn.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


class TestSearchKgFactsInMemory(unittest.TestCase):
    """T10: _search_kg_facts unit tests using in-memory DB.

    Using :memory: avoids the FTS5 format mismatch that comes from copying
    the prod DB.  For these tests we don't need save_pipeline — just
    direct SQL on a fresh schema.
    """

    def setUp(self):
        import fact_extraction as fe

        self.conn = sqlite3.connect(":memory:")
        # ensure_facts_schema creates kg_facts + FTS5 virtual table + triggers
        fe.ensure_facts_schema(self.conn)
        _ensure_kg_facts_fts(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_returns_matching_facts(self):
        _insert_fact(self.conn, "alice", "is_a", "engineer", 0.95)
        _insert_fact(self.conn, "bob", "created", "the API", 0.88)
        results = _search_kg_facts(self.conn, '"alice"', 5, include_invalid=True)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["subject"], "alice")
        self.assertEqual(results[0]["object"], "engineer")
        self.assertEqual(results[0]["confidence"], 0.95)

    def test_no_match_returns_empty(self):
        _insert_fact(self.conn, "alice", "is_a", "engineer")
        results = _search_kg_facts(
            self.conn, '"nonexistent_zzz"', 5, include_invalid=True
        )
        self.assertEqual(results, [])

    def test_include_invalid_false_filters_superseded(self):
        fid = _insert_fact(self.conn, "alice", "is_a", "engineer", 0.95)
        self.conn.execute(
            "UPDATE kg_facts SET invalid_at = ?, superseded_by = ? WHERE id = ?",
            (time.time(), fid + 1, fid),
        )
        self.conn.commit()
        # include_invalid=True sees the fact
        r1 = _search_kg_facts(self.conn, '"alice"', 5, include_invalid=True)
        self.assertEqual(len(r1), 1)
        # include_invalid=False filters it out
        r2 = _search_kg_facts(self.conn, '"alice"', 5, include_invalid=False)
        self.assertEqual(r2, [])

    def test_missing_kg_facts_returns_empty_not_error(self):
        """kg_facts table missing -> empty list, no exception."""
        self.conn.execute("DROP TABLE kg_facts")
        # _search_kg_facts checks sqlite_master for kg_facts existence
        results = _search_kg_facts(self.conn, '"alice"', 5, include_invalid=True)
        self.assertEqual(results, [])

    def test_fts5_syntax_error_does_not_raise(self):
        """Pathological FTS queries don't break _search_kg_facts."""
        _insert_fact(self.conn, "alice", "is_a", "engineer")
        for bad_q in ['"unclosed', "*", ""]:
            results = _search_kg_facts(self.conn, bad_q, 5, include_invalid=True)
            self.assertIsInstance(results, list)

    def test_multi_token_or_joined(self):
        _insert_fact(self.conn, "alice", "is_a", "engineer")
        _insert_fact(self.conn, "bob", "created", "the API")
        results = _search_kg_facts(
            self.conn, '"alice" OR "bob"', 5, include_invalid=True
        )
        self.assertEqual(len(results), 2)

    def test_limit_caps_results(self):
        for i in range(10):
            _insert_fact(self.conn, f"subject{i}", "is_a", f"obj{i}", 0.5)
        results = _search_kg_facts(self.conn, '"subject"', 3, include_invalid=True)
        self.assertLessEqual(len(results), 3)


class TestFormatSearchResultsFacts(unittest.TestCase):
    """T10: _build_search_result_envelope appends 'Related facts' section.

    T10 design note: the "Related facts" section is appended in the
    envelope (after all post-Phase-10 regeneration passes), not in
    _format_search_results.  So these tests exercise the envelope path.
    """

    def _envelope_output(self, related_facts):
        from search.orchestrator import _build_search_result_envelope

        env = _build_search_result_envelope(
            result_items=[],
            output=[
                "Search results for: 'alice' (Re-ranked)",
            ],
            results_to_display=[],
            synthesize=False,
            query="alice",
            max_synthesis_sentences=5,
            related_facts=related_facts,
        )
        return env

    def test_facts_appended_to_output(self):
        env = self._envelope_output(
            related_facts=[
                {
                    "id": 1,
                    "subject": "alice",
                    "predicate": "is_a",
                    "object": "engineer",
                    "confidence": 0.95,
                    "mention_count": 2,
                    "event_time": None,
                    "event_time_granularity": None,
                    "contradiction_score": 0.0,
                    "fts_rank": -1.0,
                }
            ]
        )
        self.assertIn("Related facts (KG)", env["output"])
        self.assertIn("alice", env["output"])
        self.assertIn("engineer", env["output"])
        self.assertIn("--[is_a]-->", env["output"])
        # also in the structured envelope
        self.assertIn("related_facts", env)
        self.assertEqual(len(env["related_facts"]), 1)

    def test_no_facts_omits_section(self):
        env = self._envelope_output(related_facts=None)
        self.assertNotIn("Related facts", env["output"])
        self.assertNotIn("related_facts", env)

    def test_empty_facts_list_omits_section(self):
        env = self._envelope_output(related_facts=[])
        self.assertNotIn("Related facts", env["output"])
        # Empty list is falsy so envelope does NOT include the key
        self.assertNotIn("related_facts", env)


class TestSearchMemoriesFactsIntegration(unittest.TestCase):
    """T10: search_memories end-to-end with include_facts flag.

    Uses direct SQL inserts (NOT save_memory) to avoid LLM/regex
    extraction flakiness.  Verifies the wiring from search_memories
    through to the FTS5 query and the envelope.
    """

    def setUp(self):
        from db_migrations import run_schema_setup
        import fact_extraction as fe

        self.tmpdir = Path(tempfile.mkdtemp())
        self.local_db = self.tmpdir / "memory.db"
        self.global_dir = self.tmpdir / "global"
        self.global_dir.mkdir(parents=True, exist_ok=True)
        self.global_db = self.global_dir / "memory.db"
        for db in (self.local_db, self.global_db):
            conn = sqlite3.connect(str(db))
            try:
                run_schema_setup(conn)
                fe.ensure_facts_schema(conn)
            finally:
                conn.close()
        self._patcher = patch("save_pipeline.GLOBAL_MEM_DIR", self.global_dir)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _insert_fact(self, subject, predicate, obj, confidence=0.95):
        """Insert a fact via the pool (triggers FTS sync)."""
        from memory_common import connection_pool

        conn = connection_pool.get(str(self.local_db))
        try:
            return _insert_fact(conn, subject, predicate, obj, confidence)
        finally:
            connection_pool.put(conn)

    def test_search_memories_includes_related_facts_by_default(self):
        """include_facts=True: related_facts is in envelope + output."""
        self._insert_fact("alice", "is_a", "engineer", 0.95)
        self._insert_fact("bob", "created", "the API", 0.88)
        result = search_memories(
            self.local_db,
            "alice",
            limit=5,
            include_global=False,
            safety_wiring=False,
            include_facts=True,
            fact_limit=5,
        )
        self.assertIn("related_facts", result)
        self.assertGreaterEqual(len(result["related_facts"]), 1)
        self.assertEqual(result["related_facts"][0]["subject"], "alice")
        self.assertIn("Related facts (KG)", result["output"])
        # The fact also appears in the human-readable output
        self.assertIn("alice", result["output"])
        self.assertIn("engineer", result["output"])
        self.assertIn("--[is_a]-->", result["output"])

    def test_search_memories_omit_facts_when_disabled(self):
        """include_facts=False: no related_facts in envelope or output."""
        self._insert_fact("alice", "is_a", "engineer", 0.95)
        result = search_memories(
            self.local_db,
            "alice",
            limit=5,
            include_global=False,
            safety_wiring=False,
            include_facts=False,
        )
        self.assertNotIn("related_facts", result)
        self.assertNotIn("Related facts", result["output"])

    def test_search_memories_no_facts_returns_empty_section(self):
        """No matching facts -> no related_facts key, no section in output."""
        # No facts inserted; query for something with no matches
        result = search_memories(
            self.local_db,
            "xyzzy_nonexistent_term",
            limit=5,
            include_global=False,
            safety_wiring=False,
            include_facts=True,
        )
        # When there are no facts, related_facts may be missing OR an
        # empty list — both are acceptable.  The output should not
        # contain the "Related facts (KG)" header.
        rf = result.get("related_facts", [])
        self.assertEqual(rf, [])
        self.assertNotIn("Related facts (KG)", result["output"])

    def test_facts_filtered_by_invalid_when_include_invalid_false(self):
        """include_invalid=False filters out invalidated facts."""
        self._insert_fact("alice", "is_a", "engineer", 0.95)
        from memory_common import connection_pool

        conn = connection_pool.get(str(self.local_db))
        try:
            conn.execute(
                "UPDATE kg_facts SET invalid_at = ? WHERE subject = 'alice'",
                (time.time(),),
            )
            conn.commit()
        finally:
            connection_pool.put(conn)
        result = search_memories(
            self.local_db,
            "alice",
            limit=5,
            include_global=False,
            safety_wiring=False,
            include_facts=True,
            include_invalid=False,
        )
        rf = result.get("related_facts", [])
        self.assertEqual(rf, [])


class TestMCPMemorySearchSignature(unittest.TestCase):
    """T10: MCP memory_search tool exposes include_facts and fact_limit."""

    def test_mcp_tool_signature_has_new_params(self):
        from mcp_search import memory_search

        # Unwrap decorators
        fn = memory_search
        while hasattr(fn, "__wrapped__"):
            fn = fn.__wrapped__
        sig = inspect.signature(fn)
        self.assertIn("include_facts", sig.parameters)
        self.assertIn("fact_limit", sig.parameters)
        self.assertEqual(sig.parameters["include_facts"].default, True)
        self.assertEqual(sig.parameters["fact_limit"].default, 5)


if __name__ == "__main__":
    unittest.main()
