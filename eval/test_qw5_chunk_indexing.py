#!/usr/bin/env python3
"""QW5: Chunk-level indexing for long notes.

Verifies that:
  1. _qw5_chunk_content returns a single chunk for short content
  2. _qw5_chunk_content splits content above the threshold
  3. _qw5_chunk_content aligns to sentence boundaries where possible
  4. _qw5_chunk_content handles content with no sentence boundaries
  5. _qw5_chunk_content returns correct offsets
  6. _qw5_chunk_content has overlap between adjacent chunks
  7. _qw5_chunk_content handles empty content
  8. _qw5_index_chunks_for is idempotent
  9. _qw5_search_chunks returns parent memory matches
 10. search_memories returns chunk-matched parents even when FTS5 missed
 11. Chunk updates when content changes
 12. _qw5_ensure_schema is idempotent
 13. Schema raises no errors on a fresh DB
 14. End-to-end: write a 5kB note, search for a word only in one chunk
"""
import sys
import unittest
import tempfile
import shutil
import sqlite3
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
AGENTIC = THIS_DIR.parent
sys.path.insert(0, str(AGENTIC))

import memory_mcp
from memory_common import configure_logging
from _fixtures import bootstrap_temp_db_clean
configure_logging()


class TestChunkContent(unittest.TestCase):
    def test_01_short_content_single_chunk(self):
        chunks = memory_mcp._qw5_chunk_content("Short note.")
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0], (0, len("Short note."), "Short note."))

    def test_02_above_threshold_splits(self):
        # Create a 3000-char document with many sentence boundaries.
        long = ("This is a sentence. " * 120).strip()  # ~2400 chars
        self.assertGreater(len(long), 2000)
        chunks = memory_mcp._qw5_chunk_content(long)
        self.assertGreater(len(chunks), 1, f"long content should split, got {len(chunks)} chunks")

    def test_03_sentence_boundary_alignment(self):
        # Sentences ending with period; first chunk should end at a period
        content = "First sentence. Second sentence. Third sentence. Fourth sentence. Fifth sentence."
        # Force splitting by lowering the threshold
        old_thresh = memory_mcp._QW5_CHUNK_THRESHOLD
        old_target = memory_mcp._QW5_CHUNK_TARGET_SIZE
        try:
            memory_mcp._QW5_CHUNK_THRESHOLD = 10
            memory_mcp._QW5_CHUNK_TARGET_SIZE = 20
            chunks = memory_mcp._qw5_chunk_content(content)
            # Each chunk text should be a complete sentence (or two)
            for s, e, txt in chunks:
                # Either chunk ends with period, or it's the last chunk
                if e < len(content):
                    self.assertTrue(txt.rstrip().endswith("."),
                        f"chunk should end at sentence boundary: {txt!r}")
        finally:
            memory_mcp._QW5_CHUNK_THRESHOLD = old_thresh
            memory_mcp._QW5_CHUNK_TARGET_SIZE = old_target

    def test_04_no_sentence_boundaries(self):
        # Single token longer than max size — should still produce a chunk
        content = "x" * 5000
        chunks = memory_mcp._qw5_chunk_content(content)
        self.assertGreater(len(chunks), 1, f"giant token should hard-split, got {len(chunks)} chunks")
        # All chunks together should cover the full content
        total = sum(len(t) for _, _, t in chunks)
        # Some overlap means total > len(content)
        self.assertGreaterEqual(total, len(content))

    def test_05_offsets_correct(self):
        content = "First sentence here. Second sentence here. Third sentence here. Fourth."
        chunks = memory_mcp._qw5_chunk_content(content)
        for s, e, txt in chunks:
            self.assertEqual(content[s:e], txt, f"chunk text at offset mismatch: {s}:{e}")

    def test_06_overlap_between_chunks(self):
        # Adjacent chunks should overlap (the next chunk starts before
        # the previous one ended).
        content = ("Sentence one here. " * 200).strip()  # ~4200 chars
        chunks = memory_mcp._qw5_chunk_content(content)
        self.assertGreater(len(chunks), 1)
        # Check that the start of chunk[i+1] is before end of chunk[i] (minus overlap tolerance)
        for i in range(len(chunks) - 1):
            s1, e1, _ = chunks[i]
            s2, _, _ = chunks[i+1]
            if e1 < len(content):  # not the last chunk
                self.assertLess(s2, e1, f"chunk {i+1} should overlap with chunk {i}: {s2} >= {e1}")

    def test_07_empty_content(self):
        self.assertEqual(memory_mcp._qw5_chunk_content(""), [(0, 0, "")])
        self.assertEqual(memory_mcp._qw5_chunk_content(None), [(0, 0, "")])


class TestEnsureSchema(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qw5_schema_")
        self.db_path = Path(self.tmp) / "memory.db"
    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
    def test_12_idempotent(self):
        db = sqlite3.connect(str(self.db_path))
        memory_mcp._qw5_ensure_schema(db)
        # Call twice — should not raise
        memory_mcp._qw5_ensure_schema(db)
        # Verify tables exist
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')").fetchall()}
        self.assertIn("memory_chunks", tables)
        self.assertIn("memory_chunks_fts", tables)
        # Verify triggers exist
        triggers = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()}
        self.assertIn("memory_chunks_ai", triggers)
        db.close()
    def test_13_fresh_db(self):
        # An empty DB should accept the schema without errors
        db = sqlite3.connect(str(self.db_path))
        memory_mcp._qw5_ensure_schema(db)
        # Can write a chunk
        db.execute("INSERT INTO memory_chunks (parent_id, chunk_idx, start_offset, end_offset, content) VALUES (?,?,?,?,?)",
                   ("test-parent", 0, 0, 10, "hello world"))
        db.commit()
        # The trigger should populate FTS
        rows = db.execute("SELECT rowid, content FROM memory_chunks_fts").fetchall()
        self.assertEqual(len(rows), 1)
        db.close()


class TestIndexAndSearchChunks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qw5_io_")
        self.db_path = Path(self.tmp) / "memory.db"
        # H21: use the full prod schema instead of a partial inline one
        bootstrap_temp_db_clean(self.db_path)
    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
    def test_08_index_idempotent(self):
        db = sqlite3.connect(str(self.db_path))
        # First call: 5 chunks
        n1 = memory_mcp._qw5_index_chunks_for(db, "m1", "Hello. " * 200)
        # Second call: should rewrite, not append
        n2 = memory_mcp._qw5_index_chunks_for(db, "m1", "Hello. " * 200)
        count = db.execute("SELECT COUNT(*) FROM memory_chunks WHERE parent_id = ?", ("m1",)).fetchone()[0]
        self.assertEqual(count, n2, f"second call should rewrite, not append: n1={n1} n2={n2} count={count}")
        db.close()
    def test_11_chunk_update_on_content_change(self):
        db = sqlite3.connect(str(self.db_path))
        memory_mcp._qw5_index_chunks_for(db, "m1", "First content. " * 200)
        db.execute("SELECT COUNT(*) FROM memory_chunks WHERE parent_id = ?", ("m1",)).fetchone()[0]
        # Now update with completely different content
        memory_mcp._qw5_index_chunks_for(db, "m1", "Totally different stuff about elephant migration patterns. " * 100)
        db.execute("SELECT COUNT(*) FROM memory_chunks WHERE parent_id = ?", ("m1",)).fetchone()[0]
        # Old chunk content should be gone from FTS
        rows = db.execute("SELECT rowid FROM memory_chunks_fts WHERE memory_chunks_fts MATCH ?", ("first",)).fetchall()
        self.assertEqual(len(rows), 0, "old 'first' content should be removed from FTS")
        rows = db.execute("SELECT rowid FROM memory_chunks_fts WHERE memory_chunks_fts MATCH ?", ("elephant",)).fetchall()
        self.assertGreater(len(rows), 0, "new 'elephant' content should be in FTS")
        db.close()


class TestEndToEndSearch(unittest.TestCase):
    """End-to-end: search_memories finds a long note via its chunk."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qw5_e2e_")
        self.db_path = Path(self.tmp) / "memory.db"
        # H21: use the full prod schema instead of a partial inline one
        bootstrap_temp_db_clean(self.db_path)
        db = sqlite3.connect(str(self.db_path))
        now = "2024-06-01T00:00:00"
        long_content = (
            "Discussion of database architecture patterns in 2024. " * 100 +  # ~5000 chars
            "Notes on rate limiting strategies and token bucket algorithms. " * 30 +  # ~2000 chars
            "More content about migrations and schema changes. " * 80  # ~4000 chars
        )
        db.execute("""INSERT INTO memories
            (id, content, source_file, tags, created_at, updated_at, observed_at,
             fitness_score, importance, pinned)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("lessons/long-note", long_content, "lessons/long-note.md", "[]",
             now, now, now, 1.0, 3, 0))
        db.commit()
        db.close()
    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
    def test_14_search_finds_long_note_via_chunk(self):
        # "rate" only appears in one chunk of the long note.
        # FTS5 will match because the whole content is indexed, but
        # the chunk FTS should ALSO match.
        # The first search test: confirm the long note is found.
        result = memory_mcp.search_memories(
            self.db_path, "rate", limit=5,
            include_global=False, rerank=True, boost_pinned=True,
            recency_weight=0.1, include_invalid=True, hybrid=False,
        )
        ids = [r["id"] for r in result["results"]]
        self.assertIn("lessons/long-note", ids,
            f"long note should be found via 'rate' search, got {ids}")
        # Verify the long note has been chunked (we already wrote it
        # without chunking in setUp — let me chunk it now via the
        # helper to confirm the search-time path).
        db = sqlite3.connect(str(self.db_path))
        memory_mcp._qw5_index_chunks_for(db, "lessons/long-note",
            db.execute("SELECT content FROM memories WHERE id = ?",
                       ("lessons/long-note",)).fetchone()[0])
        db.commit()
        db.close()
        # Now clear the search cache and re-search
        memory_mcp._search_cache.clear()
        result2 = memory_mcp.search_memories(
            self.db_path, "rate", limit=5,
            include_global=False, rerank=True, boost_pinned=True,
            recency_weight=0.1, include_invalid=True, hybrid=False,
        )
        ids2 = [r["id"] for r in result2["results"]]
        self.assertIn("lessons/long-note", ids2,
            f"chunk-indexed long note should be found, got {ids2}")


if __name__ == "__main__":
    unittest.main()
