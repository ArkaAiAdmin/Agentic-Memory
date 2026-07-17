"""Tests for the 4 advanced features:
A. Enhanced semantic chunking with parent metadata
B. Late interaction reranking
C. Configurable temporal decay
D. Async pipeline
"""

import asyncio
import os
import sqlite3
import sys
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

# Preload the embedding model once at import time so suite-level resource
# contention doesn't cause timeout during per-test setUp rebuild_index() calls.
try:
    from infra.embedding_search import get_embedding_search
    _es = get_embedding_search()
    _es.wait_for_model(timeout_s=60.0)
except Exception:
    pass


class _TempDirMixin:
    """Mixin that sets up a temporary directory with env vars and
    cleans up both after all tests in the class run."""

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.mkdtemp()
        cls._environ_patch = {
            "MEMORY_GLOBAL_DIR": cls._tmpdir,
            "MEMORY_LOCAL_DIR": cls._tmpdir,
            "MEMORY_DB_PATH": str(Path(cls._tmpdir) / "memory.db"),
            "MEMORY_INDEXING_OFF": "1",
        }
        cls._environ_orig = {k: os.environ.get(k) for k in cls._environ_patch}
        os.environ.update(cls._environ_patch)

    @classmethod
    def tearDownClass(cls):
        for k, v in cls._environ_orig.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        try:
            shutil.rmtree(cls._tmpdir)
        except Exception:
            pass


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rebuild_index import rebuild_index
from infra.memory_common import reset_rate_limiter


class TestFeatureAChunkMerging(_TempDirMixin, unittest.TestCase):
    """Feature A: Enhanced semantic chunking with parent metadata."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "memory.db"
        rebuild_index(Path(self.tmpdir), self.db_path)
        # Patch paths
        import memory_mcp

        self._orig_resolve = memory_mcp.resolve_active_memory_dir
        memory_mcp.resolve_active_memory_dir = lambda **_: Path(self.tmpdir)
        self._orig_paths = memory_mcp.get_memory_paths
        memory_mcp.get_memory_paths = lambda: (
            Path(self.tmpdir),
            Path(self.tmpdir),
            Path(self.tmpdir),
        )
        memory_mcp._search_cache.clear()
        reset_rate_limiter()

    def tearDown(self):
        import memory_mcp

        memory_mcp.resolve_active_memory_dir = self._orig_resolve
        memory_mcp.get_memory_paths = self._orig_paths
        memory_mcp._search_cache.clear()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_merge_consecutive_chunks(self):
        """Consecutive chunks from same parent are merged."""
        from search_pipeline import _merge_chunk_hits

        hits = [
            ("parent/1", 0, "chunk zero text", 0, 100, -2.0),
            ("parent/1", 1, "chunk one text", 100, 200, -1.5),
            ("parent/1", 2, "chunk two text", 200, 300, -1.0),
            ("other/2", 0, "other chunk", 0, 100, -3.0),
        ]
        merged = _merge_chunk_hits(hits)
        self.assertEqual(len(merged), 2)
        # Parent 1 should have all 3 chunks merged
        p1 = [m for m in merged if m[0] == "parent/1"][0]
        self.assertEqual(p1[4], 3)  # chunk_count = 3
        self.assertIn("chunk zero text", p1[2])
        self.assertIn("chunk one text", p1[2])
        # Best rank should be the minimum
        self.assertEqual(p1[3], -2.0)

    def test_merge_non_consecutive_chunks(self):
        """Non-consecutive chunks from same parent keep best score."""
        from search_pipeline import _merge_chunk_hits

        hits = [
            ("parent/1", 0, "chunk zero", 0, 100, -2.0),
            ("parent/1", 3, "chunk three", 300, 400, -1.0),
        ]
        merged = _merge_chunk_hits(hits)
        # Non-consecutive: two separate groups
        p1 = [m for m in merged if m[0] == "parent/1"]
        self.assertEqual(len(p1), 2)

    def test_merge_empty_hits(self):
        """Empty chunk hits list returns empty."""
        from search_pipeline import _merge_chunk_hits

        self.assertEqual(_merge_chunk_hits([]), [])

    def test_chunks_table_exists(self):
        """The chunks table is created by migrations."""
        db = sqlite3.connect(str(self.db_path))
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        db.close()
        self.assertIn("memory_chunks", tables)


class TestFeatureBLateInteraction(unittest.TestCase):
    """Feature B: Late interaction reranking."""

    def test_late_interaction_score_basic(self):
        """Same tokens -> high score."""
        from search_pipeline import _late_interaction_score

        score, _ = _late_interaction_score("hello world test", "hello world test")
        self.assertGreater(score, 0.5)

    def test_late_interaction_score_no_overlap(self):
        """No shared tokens -> zero score."""
        from search_pipeline import _late_interaction_score

        score, _ = _late_interaction_score("alpha beta gamma", "xyz xyz xyz")
        self.assertEqual(score, 0.0)

    def test_late_interaction_score_partial(self):
        """Partial overlap -> score between 0 and 1."""
        from search_pipeline import _late_interaction_score

        score, _ = _late_interaction_score("the quick brown fox", "the quick red fox")
        self.assertGreater(score, 0.0)
        self.assertLess(score, 1.0)

    def test_late_interaction_empty_input(self):
        """Empty input returns 0."""
        from search_pipeline import _late_interaction_score

        score, _ = _late_interaction_score("", "test")
        self.assertEqual(score, 0.0)
        score, _ = _late_interaction_score("test", "")
        self.assertEqual(score, 0.0)

    def test_late_interaction_enabled_by_default(self):
        """Late interaction is on by default (memory.toml)."""
        from search_pipeline import _LATE_INTERACTION_ENABLED

        self.assertTrue(_LATE_INTERACTION_ENABLED)

    def test_apply_late_interaction_produces_output(self):
        """When enabled, results are reranked with late interaction scores."""
        from search_pipeline import _apply_late_interaction_rerank

        results = [
            ("id1", "content1", "src", "[]", "2026-01-01", -1.0, 0.8, 1.0, 3, False),
            ("id2", "content2", "src", "[]", "2026-01-02", -2.0, 0.5, 1.0, 3, False),
        ]
        out = _apply_late_interaction_rerank("query", results, top_k=2)
        self.assertEqual(len(out), 2)  # results returned
        self.assertTrue(all(r[6] is not None for r in out))  # scores assigned


class TestFeatureCTemporalDecay(unittest.TestCase):
    """Feature C: Configurable temporal decay."""

    def test_decay_recent_note(self):
        """Recent note has high decay factor."""
        from search_pipeline import _temporal_decay_factor

        now = datetime.now().isoformat()
        factor = _temporal_decay_factor(now)
        self.assertGreater(factor, 0.9)

    def test_decay_old_note(self):
        """Old note has low decay factor."""
        from search_pipeline import _temporal_decay_factor

        old = (datetime.now() - timedelta(days=365)).isoformat()
        factor = _temporal_decay_factor(old)
        self.assertLess(factor, 0.5)

    def test_decay_empty_created(self):
        """Empty created string returns 1.0."""
        from search_pipeline import _temporal_decay_factor

        self.assertEqual(_temporal_decay_factor(""), 1.0)

    def test_decay_invalid_created(self):
        """Invalid created string returns 1.0."""
        from search_pipeline import _temporal_decay_factor

        self.assertEqual(_temporal_decay_factor("not-a-date"), 1.0)

    def test_apply_temporal_decay(self):
        """Temporal decay modifies scores."""
        from search_pipeline import _apply_temporal_decay

        now = datetime.now().isoformat()
        old = (datetime.now() - timedelta(days=365)).isoformat()
        results = [
            ("id1", "c1", "src", "[]", now, -1.0, 1.0, 1.0, 3, False),
            ("id2", "c2", "src", "[]", old, -1.0, 1.0, 1.0, 3, False),
        ]
        out = _apply_temporal_decay(results)
        # Recent note should score higher than old note
        self.assertGreater(out[0][6], out[1][6])

    def test_decay_mode_off(self):
        """When decay mode is off, scores are unchanged."""
        import search_pipeline

        orig = search_pipeline._TEMPORAL_DECAY_MODE
        search_pipeline._TEMPORAL_DECAY_MODE = "off"
        try:
            from search_pipeline import _apply_temporal_decay

            results = [
                ("id1", "c1", "src", "[]", "2026-01-01", -1.0, 1.0, 1.0, 3, False),
            ]
            out = _apply_temporal_decay(results)
            self.assertEqual(out[0][6], 1.0)
        finally:
            search_pipeline._TEMPORAL_DECAY_MODE = orig


class TestFeatureDAsyncPipeline(unittest.TestCase):
    """Feature D: Async pipeline for concurrent operations."""

    def test_async_save_returns_string(self):
        """async_memory_save returns a string result."""
        import memory_mcp
        import save_pipeline

        orig_resolve = memory_mcp.resolve_active_memory_dir
        orig_paths = memory_mcp.get_memory_paths
        orig_sp_resolve = save_pipeline.resolve_active_memory_dir
        orig_sp_paths = save_pipeline.get_memory_paths
        tmpdir = tempfile.mkdtemp()
        memory_mcp.resolve_active_memory_dir = lambda **_: Path(tmpdir)
        memory_mcp.get_memory_paths = lambda: (Path(tmpdir), Path(tmpdir), Path(tmpdir))
        save_pipeline.resolve_active_memory_dir = lambda **_: Path(tmpdir)
        save_pipeline.get_memory_paths = lambda: (
            Path(tmpdir),
            Path(tmpdir),
            Path(tmpdir),
        )
        memory_mcp._search_cache.clear()
        from config import get_config
        print(f"\nDEBUG: MEMORY_LLM_EXTRACTION={os.environ.get('MEMORY_LLM_EXTRACTION')}")
        print(f"DEBUG: get_config().llm_extraction={get_config().llm_extraction}")
        try:
            result = asyncio.run(
                memory_mcp.async_memory_save(
                    content="async test note",
                    category="lessons",
                    title_slug="async-test",
                    tags=["test"],
                )
            )
            self.assertIn("Successfully saved memory", result)
        finally:
            memory_mcp.resolve_active_memory_dir = orig_resolve
            memory_mcp.get_memory_paths = orig_paths
            save_pipeline.resolve_active_memory_dir = orig_sp_resolve
            save_pipeline.get_memory_paths = orig_sp_paths
            memory_mcp._search_cache.clear()

    def test_async_search_returns_string(self):
        """async_memory_search returns a string result."""
        import memory_mcp

        orig_resolve = memory_mcp.resolve_active_memory_dir
        orig_paths = memory_mcp.get_memory_paths
        tmpdir = tempfile.mkdtemp()
        memory_mcp.resolve_active_memory_dir = lambda **_: Path(tmpdir)
        memory_mcp.get_memory_paths = lambda: (Path(tmpdir), Path(tmpdir), Path(tmpdir))
        memory_mcp._search_cache.clear()
        try:
            result = asyncio.run(memory_mcp.async_memory_search(query="nonexistent"))
            self.assertIsInstance(result, str)
        finally:
            memory_mcp.resolve_active_memory_dir = orig_resolve
            memory_mcp.get_memory_paths = orig_paths
            memory_mcp._search_cache.clear()

    def test_async_save_batch(self):
        """async_memory_save_batch saves multiple items concurrently."""
        import memory_mcp
        import save_pipeline

        orig_resolve = memory_mcp.resolve_active_memory_dir
        orig_paths = memory_mcp.get_memory_paths
        orig_sp_resolve = save_pipeline.resolve_active_memory_dir
        orig_sp_paths = save_pipeline.get_memory_paths
        tmpdir = tempfile.mkdtemp()
        memory_mcp.resolve_active_memory_dir = lambda **_: Path(tmpdir)
        memory_mcp.get_memory_paths = lambda: (Path(tmpdir), Path(tmpdir), Path(tmpdir))
        save_pipeline.resolve_active_memory_dir = lambda **_: Path(tmpdir)
        save_pipeline.get_memory_paths = lambda: (
            Path(tmpdir),
            Path(tmpdir),
            Path(tmpdir),
        )
        memory_mcp._search_cache.clear()
        try:
            items = [
                {
                    "content": f"batch item {i}",
                    "category": "lessons",
                    "title_slug": f"batch-{i}",
                }
                for i in range(3)
            ]
            results = asyncio.run(memory_mcp.async_memory_save_batch(items))
            self.assertEqual(len(results), 3)
            for result, elapsed in results:
                self.assertIn("Successfully saved memory", result)
                self.assertGreater(elapsed, 0)
        finally:
            memory_mcp.resolve_active_memory_dir = orig_resolve
            memory_mcp.get_memory_paths = orig_paths
            save_pipeline.resolve_active_memory_dir = orig_sp_resolve
            save_pipeline.get_memory_paths = orig_sp_paths
            memory_mcp._search_cache.clear()

    def test_async_search_batch(self):
        """async_memory_search_batch searches multiple queries concurrently."""
        import memory_mcp

        orig_resolve = memory_mcp.resolve_active_memory_dir
        orig_paths = memory_mcp.get_memory_paths
        tmpdir = tempfile.mkdtemp()
        memory_mcp.resolve_active_memory_dir = lambda **_: Path(tmpdir)
        memory_mcp.get_memory_paths = lambda: (Path(tmpdir), Path(tmpdir), Path(tmpdir))
        prev_db_path = os.environ.get("MEMORY_DB_PATH")
        os.environ["MEMORY_DB_PATH"] = str(Path(tmpdir) / "memory.db")
        memory_mcp._search_cache.clear()
        try:
            queries = [
                {"query": "test query 1"},
                {"query": "test query 2"},
            ]
            results = asyncio.run(memory_mcp.async_memory_search_batch(queries))
            self.assertEqual(len(results), 2)
            for result, elapsed in results:
                self.assertIsInstance(result, str)
                self.assertGreaterEqual(elapsed, 0)
        finally:
            memory_mcp.resolve_active_memory_dir = orig_resolve
            memory_mcp.get_memory_paths = orig_paths
            memory_mcp._search_cache.clear()
            if prev_db_path is not None:
                os.environ["MEMORY_DB_PATH"] = prev_db_path
            else:
                os.environ.pop("MEMORY_DB_PATH", None)


if __name__ == "__main__":
    unittest.main()
