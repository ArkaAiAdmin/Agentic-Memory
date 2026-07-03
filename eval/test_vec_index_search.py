#!/usr/bin/env python3
"""Sprint 4 / 8.3 tests: indexed search path in embedding_search.py.

Verifies that:
  * When no index exists, the search falls back to the full-scan path
    (the pre-Sprint-4 behavior).
  * When a real usearch index exists, the search uses it AND the
    rerank produces results in the same order as a full scan would
    (high overlap / high recall on tiny DBs).
  * The LEFT-JOIN safety net surfaces unindexed memories added
    after the last rebuild.
  * Corrupt or dimension-mismatched indexes fall back to full scan
    without raising.
  * The in-process index cache self-invalidates when the singleton
    row's built_at / blob_len change (i.e. a rebuild happened).

The recall@200 ≥ 0.95 on 10K synthetic test is 8.5's job, not this
file's. These are correctness tests.

Run with:
    ~/.config/agentic-memory/venv/bin/python -m unittest eval.test_vec_index_search
"""

import json
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
import unittest
from pathlib import Path


INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

import infra.memory_common as memory_common
from _fixtures import bootstrap_temp_db_clean  # noqa: E402
import rebuild_vec_index  # noqa: E402


def _init_db(db_path: Path) -> None:
    """H21: bootstrap with full prod schema (no custom schema)."""
    bootstrap_temp_db_clean(db_path)


def _insert_memories(db_path: Path, items) -> None:
    """items: iterable of (id, content)."""
    now = datetime.now(timezone.utc).isoformat()
    with memory_common.open_db(db_path, timeout=5.0) as conn:
        with conn:
            conn.executemany(
                "INSERT INTO memories "
                "(id, content, source_file, tags, created_at, updated_at, observed_at, pinned) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "content=excluded.content, source_file=excluded.source_file, "
                "tags=excluded.tags, created_at=excluded.created_at, "
                "updated_at=excluded.updated_at, observed_at=excluded.observed_at, "
                "pinned=excluded.pinned",
                [
                    (
                        mid,
                        content,
                        f"memory/{mid.split('/')[-1]}.md",
                        json.dumps(["test"]),
                        now,
                        now,
                        now,
                        0,
                    )
                    for mid, content in items
                ],
            )


def _corrupt_index(db_path: Path) -> None:
    """Replace the index BLOB with random bytes; search must fall back gracefully."""
    with memory_common.open_db(db_path, timeout=5.0) as conn:
        with conn:
            conn.execute(
                "UPDATE memory_vec_idx SET index_blob = ? WHERE id=1",
                (b"this-is-not-a-usearch-blob-" * 100,),
            )


def _mismatched_dim_index(db_path: Path) -> None:
    """Set dim to 128 (not 256) but keep the BLOB. Should be rejected
    by _load_vec_index (dim mismatch) and trigger fallback."""
    with memory_common.open_db(db_path, timeout=5.0) as conn:
        with conn:
            conn.execute("UPDATE memory_vec_idx SET dim = 128 WHERE id=1")


class _TestBase(unittest.TestCase):
    """Per-test fresh DB + per-test fresh EmbeddingSearch instance.

    We don't use the process-wide singleton because tests need to
    control when the in-process index cache resets. We patch
    memory_mcp to redirect to a tmpdir so a stray call cannot reach
    the prod DB (see pinned lesson `lessons/bench-against-test-db-not-prod`).
    """

    @classmethod
    def setUpClass(cls):
        from infra.embedding_search import EmbeddingSearch
        _es = EmbeddingSearch()
        if _es.model is None:
            raise unittest.SkipTest("Embedding model unavailable")

    def setUp(self):
        super().setUp()
        import memory_mcp
        from infra.embedding_search import EmbeddingSearch

        self._memory_mcp = memory_mcp
        self._orig_global = memory_mcp.GLOBAL_MEM_DIR
        self._orig_resolve = memory_mcp.resolve_active_memory_dir
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vec_search_"))
        self.test_mem = self.tmpdir / "memory"
        self.test_mem.mkdir(parents=True)
        self.db_path = self.test_mem / "memory.db"
        memory_mcp.GLOBAL_MEM_DIR = self.test_mem
        memory_mcp.resolve_active_memory_dir = lambda **_: self.test_mem
        _init_db(self.db_path)
        # Fresh EmbeddingSearch instance with its own cache for this test.
        self.es = EmbeddingSearch()

    def tearDown(self):
        self._memory_mcp.GLOBAL_MEM_DIR = self._orig_global
        self._memory_mcp.resolve_active_memory_dir = self._orig_resolve
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        super().tearDown()


class TestNoIndexFallsBack(_TestBase):
    """When memory_vec_idx has no row, search must take the full-scan path."""

    def test_no_index_row_uses_full_scan(self):
        _insert_memories(
            self.db_path,
            [
                ("lessons/a", "first memory about indexing"),
                ("lessons/b", "second memory about quantization"),
            ],
        )
        # No rebuild_vec_index call → no memory_vec_idx row.
        with memory_common.open_db(self.db_path, timeout=5.0) as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM memory_vec_idx").fetchone()[0], 0
            )
        results = self.es.search("indexing", self.db_path, limit=5)
        self.assertIsInstance(results, list)
        self.assertEqual(len(results), 2)

    def test_missing_memory_vec_keys_table_falls_back(self):
        """Even if the migration ran but no row was written, search still works."""
        _insert_memories(self.db_path, [("lessons/x", "x memory")])
        results = self.es.search("x", self.db_path, limit=5)
        self.assertIsInstance(results, list)
        self.assertEqual(len(results), 1)


class TestIndexedPathFindsResults(_TestBase):
    """The indexed path produces results in roughly the same order as a full scan."""

    def test_indexed_topk_matches_full_scan_on_small_db(self):
        items = [
            ("lessons/a", "Cosine similarity on unit-norm embeddings."),
            ("lessons/b", "WAL mode keeps writes non-blocking."),
            ("lessons/c", "Vector indexing for memory recall."),
            ("lessons/d", "f16 quantization preserves recall near fp32."),
            ("lessons/e", "SQLite is the substrate of this memory system."),
            ("lessons/f", "usearch HNSW runs in a few milliseconds."),
        ]
        _insert_memories(self.db_path, items)
        rebuild_vec_index.rebuild_vec_index(self.db_path)

        # Self-es: indexed path.
        # A fresh es instance to use the full-scan path independently.
        from infra.embedding_search import EmbeddingSearch

        es_indexed = self.es
        es_full = EmbeddingSearch()
        es_full.clear_vec_index_cache()

        results_indexed = es_indexed.search("cosine similarity", self.db_path, limit=3)
        results_full = es_full.search("cosine similarity", self.db_path, limit=3)

        self.assertIsInstance(results_indexed, list)
        self.assertEqual(len(results_indexed), 3)
        # On a 6-row DB every result is in both — top-K overlap should be 100%.
        ids_indexed = {r["id"] for r in results_indexed}
        ids_full = {r["id"] for r in results_full}
        self.assertEqual(ids_indexed, ids_full)
        # The top-1 should be the most cosine-relevant doc, which is "a".
        self.assertEqual(results_indexed[0]["id"], "lessons/a")

    def test_indexed_results_have_score_field(self):
        _insert_memories(
            self.db_path,
            [
                ("lessons/x", "alpha content"),
                ("lessons/y", "beta content"),
            ],
        )
        rebuild_vec_index.rebuild_vec_index(self.db_path)
        results = self.es.search("alpha", self.db_path, limit=2)
        self.assertEqual(len(results), 2)
        for r in results:
            self.assertIn("score", r)
            self.assertIn("id", r)
            self.assertIn("preview", r)
            self.assertIsInstance(r["score"], float)


class TestUnindexedMemorySafetyNet(_TestBase):
    """Memories added after the last rebuild must still be discoverable."""

    def test_unindexed_memory_shows_up_in_results(self):
        _insert_memories(
            self.db_path,
            [
                ("lessons/old1", "first indexed memory"),
                ("lessons/old2", "second indexed memory"),
            ],
        )
        rebuild_vec_index.rebuild_vec_index(self.db_path)

        # Now add a new memory WITHOUT rebuilding the index.
        _insert_memories(
            self.db_path,
            [
                ("lessons/old1", "first indexed memory"),
                ("lessons/old2", "second indexed memory"),
                ("lessons/new", "third new memory about a unique topic on rebases"),
            ],
        )
        # Sanity: vec_keys does NOT include the new memory.
        with memory_common.open_db(self.db_path, timeout=5.0) as conn:
            indexed_ids = {
                r[0]
                for r in conn.execute(
                    "SELECT memory_id FROM memory_vec_keys"
                ).fetchall()
            }
        self.assertIn("lessons/old1", indexed_ids)
        self.assertIn("lessons/old2", indexed_ids)
        self.assertNotIn("lessons/new", indexed_ids)

        # Search for the new memory's unique term — it should still appear
        # in the top results because of the LEFT-JOIN safety net.
        results = self.es.search("rebases", self.db_path, limit=3)
        self.assertIsInstance(results, list)
        ids = {r["id"] for r in results}
        self.assertIn("lessons/new", ids)


class TestIndexedPathFallbacks(_TestBase):
    """Corrupt / dimension-mismatched indexes must fall back, not crash."""

    def test_corrupt_blob_falls_back_to_full_scan(self):
        _insert_memories(
            self.db_path,
            [
                ("lessons/a", "alpha"),
                ("lessons/b", "beta"),
            ],
        )
        rebuild_vec_index.rebuild_vec_index(self.db_path)
        _corrupt_index(self.db_path)
        # Clear the cache so the next search re-loads from DB.
        self.es.clear_vec_index_cache()
        results = self.es.search("alpha", self.db_path, limit=2)
        self.assertIsInstance(results, list)
        self.assertEqual(len(results), 2)

    def test_dim_mismatch_falls_back_to_full_scan(self):
        _insert_memories(
            self.db_path,
            [
                ("lessons/a", "alpha"),
                ("lessons/b", "beta"),
            ],
        )
        rebuild_vec_index.rebuild_vec_index(self.db_path)
        _mismatched_dim_index(self.db_path)
        self.es.clear_vec_index_cache()
        results = self.es.search("alpha", self.db_path, limit=2)
        self.assertIsInstance(results, list)
        self.assertEqual(len(results), 2)


class TestIndexCacheInvalidation(_TestBase):
    """The in-process index cache must reload after a rebuild."""

    def test_cache_invalidates_on_rebuild(self):
        _insert_memories(self.db_path, [("lessons/x", "x memory")])
        rebuild_vec_index.rebuild_vec_index(self.db_path)
        # First search loads the index into the cache.
        self.es.search("x", self.db_path, limit=1)
        cache_before = self.es._vec_index_cache.get(str(self.db_path))
        self.assertIsNotNone(cache_before)

        # Add another memory and rebuild. The new index has a different
        # built_at and a different blob length, so the cache entry is stale.
        _insert_memories(
            self.db_path,
            [
                ("lessons/x", "x memory"),
                ("lessons/y", "y memory"),
            ],
        )
        # Bump time by 1.1s so built_at advances past the previous one.
        # This is a deterministic time-bump (not a wait for an async event),
        # so time.sleep is correct here: the rebuild indexes by built_at and
        # we need strictly newer timestamp. Using wait_until would just
        # poll monotonic() and add noise.
        time.sleep(1.1)
        rebuild_vec_index.rebuild_vec_index(self.db_path)
        # Verify the on-disk row changed.
        with memory_common.open_db(self.db_path, timeout=5.0) as conn:
            new_blob_len = conn.execute(
                "SELECT length(index_blob) FROM memory_vec_idx WHERE id=1"
            ).fetchone()[0]
        self.assertNotEqual(new_blob_len, cache_before[1]["blob_len"])

        # Second search MUST reload — the cached entry is stale.
        results = self.es.search("y", self.db_path, limit=2)
        ids = {r["id"] for r in results}
        self.assertIn("lessons/y", ids)

        cache_after = self.es._vec_index_cache.get(str(self.db_path))
        self.assertIsNotNone(cache_after)
        self.assertEqual(cache_after[1]["blob_len"], new_blob_len)


class TestIndexedPathIsConsistent(_TestBase):
    """Repeated indexed searches return consistent results."""

    def test_repeated_search_returns_same_ids(self):
        _insert_memories(
            self.db_path,
            [
                ("lessons/a", "alpha content about cosine"),
                ("lessons/b", "beta content about wal mode"),
                ("lessons/c", "gamma content about hnsw"),
            ],
        )
        rebuild_vec_index.rebuild_vec_index(self.db_path)
        r1 = self.es.search("cosine", self.db_path, limit=2)
        r2 = self.es.search("cosine", self.db_path, limit=2)
        self.assertEqual([r["id"] for r in r1], [r["id"] for r in r2])
        self.assertEqual(r1[0]["id"], "lessons/a")


if __name__ == "__main__":
    unittest.main()
