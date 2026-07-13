#!/usr/bin/env python3
"""Sprint 4 / 8.2 tests: rebuild_vec_index.py end-to-end.

Mirrors test_audit_log.py: per-test setUp patches memory_mcp.GLOBAL_MEM_DIR
+ resolve_active_memory_dir so a stray call can't reach the prod DB.

Run with:
    ~/.config/agentic-memory/venv/bin/python -m unittest eval.test_vec_index
or:
    ~/.config/agentic-memory/venv/bin/python eval/test_vec_index.py
"""
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
import unittest
from pathlib import Path

import numpy as np

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
                "INSERT OR REPLACE INTO memories "
                "(id, content, source_file, tags, created_at, updated_at, observed_at, pinned) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (mid, content, f"memory/{mid.split('/')[-1]}.md",
                     json.dumps(["test"]), now, now, now, 0)
                    for mid, content in items
                ],
            )


def _read_singleton(db_path: Path):
    with memory_common.open_db(db_path, timeout=5.0) as conn:
        fetched = conn.execute(
            "SELECT n_vectors, dim, metric, quantization, connectivity, "
            "       expansion_add, expansion_search, length(index_blob), key_count "
            "FROM memory_vec_idx WHERE id=1"
        ).fetchone()
        keys = conn.execute(
            "SELECT key, memory_id FROM memory_vec_keys"
        ).fetchall()
    return fetched, keys


def _load_index_from_db(db_path: Path):
    from usearch.index import Index
    with memory_common.open_db(db_path, timeout=5.0) as conn:
        fetched = conn.execute(
            "SELECT dim, metric, quantization, connectivity, expansion_add, "
            "       expansion_search, index_blob FROM memory_vec_idx WHERE id=1"
        ).fetchone()
    assert fetched is not None
    dim, metric, dtype, conn_, exp_a, exp_s, blob = fetched
    idx = Index(
        ndim=dim, metric=metric, dtype=dtype,
        connectivity=conn_, expansion_add=exp_a, expansion_search=exp_s,
    )
    idx.load(blob)
    return idx, dim, metric, dtype


class _VecIndexTestBase(unittest.TestCase):
    """Per-test fresh DB + prod-isolation patch.

    The prod-isolation patch mirrors test_audit_log.py. Without it, a
    stray call inside rebuild_vec_index would write to the real prod
    DB. See pinned lesson `lessons/bench-against-test-db-not-prod`.

    We use a base class (not a mixin) so unittest's MRO is unambiguous.
    super().setUp()/tearDown() are no-ops on TestCase, so they're safe
    to call.
    """

    def setUp(self):
        super().setUp()
        import memory_mcp
        self._memory_mcp = memory_mcp
        self._orig_global = memory_mcp.GLOBAL_MEM_DIR
        self._orig_resolve = memory_mcp.resolve_active_memory_dir
        self.tmpdir = Path(tempfile.mkdtemp(prefix="vec_rebuild_"))
        self.test_mem = self.tmpdir / "memory"
        self.test_mem.mkdir(parents=True)
        self.db_path = self.test_mem / "memory.db"
        memory_mcp.GLOBAL_MEM_DIR = self.test_mem
        memory_mcp.resolve_active_memory_dir = lambda **_: self.test_mem
        _init_db(self.db_path)

    def tearDown(self):
        self._memory_mcp.GLOBAL_MEM_DIR = self._orig_global
        self._memory_mcp.resolve_active_memory_dir = self._orig_resolve
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        super().tearDown()


class TestBuildIndex(_VecIndexTestBase):
    def test_builds_index_for_populated_db(self):
        _insert_memories(self.db_path, [
            ("lessons/one",   "Vector indexing for memory recall."),
            ("lessons/two",   "Cosine similarity on unit-norm embeddings."),
            ("lessons/three", "SQLite is the substrate of this memory system."),
            ("lessons/four",  "usearch HNSW runs in a few milliseconds."),
            ("lessons/five",  "f16 quantization preserves recall near fp32."),
        ])
        from infra._lazy_imports import get_embedding_search
        es = get_embedding_search()
        es.wait_for_model()
        expected_dim = int(es.model.dim)

        stats = rebuild_vec_index.rebuild_vec_index(self.db_path)
        self.assertEqual(stats["n_memories"], 5)
        self.assertEqual(stats["n_indexed"], 5)
        self.assertEqual(stats["n_skipped"], 0)
        self.assertEqual(stats["dim"], expected_dim)            # potion-base-8M
        self.assertEqual(stats["quantization"], "f16")
        self.assertEqual(stats["metric"], "cos")
        self.assertGreater(stats["serialized_bytes"], 0)
        self.assertGreater(stats["elapsed_s"], 0.0)
        self.assertEqual(stats["collisions_resolved"], 0)

        row, keys = _read_singleton(self.db_path)
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 5)                    # n_vectors
        self.assertEqual(row[1], expected_dim)                  # dim
        self.assertEqual(row[2], "cos")
        self.assertEqual(row[3], "f16")
        self.assertEqual(row[4], 16)                   # connectivity
        self.assertEqual(row[5], 128)                  # expansion_add
        self.assertEqual(row[6], 64)                   # expansion_search
        self.assertEqual(row[7], stats["serialized_bytes"])
        self.assertEqual(row[8], 5)                    # key_count
        self.assertEqual(len(keys), 5)

    def test_persisted_index_is_searchable(self):
        """Load the BLOB and confirm the index answers queries."""
        _insert_memories(self.db_path, [
            ("lessons/a", "first memory about indexing"),
            ("lessons/b", "second memory about quantization"),
            ("lessons/c", "third memory about cosine similarity"),
        ])
        rebuild_vec_index.rebuild_vec_index(self.db_path)
        idx, dim, metric, dtype = _load_index_from_db(self.db_path)
        self.assertEqual(len(idx), 3)
        from infra.embedding_search import get_embedding_search
        es = get_embedding_search()
        q = es.encode(["cosine similarity"])[0]
        matches = idx.search(q.astype(np.float32), 3)
        self.assertEqual(len(matches.keys), 3)


class TestIdempotent(_VecIndexTestBase):
    def test_rerun_replaces_in_place(self):
        _insert_memories(self.db_path, [
            ("lessons/x", "x memory"),
            ("lessons/y", "y memory"),
        ])
        rebuild_vec_index.rebuild_vec_index(self.db_path)
        _insert_memories(self.db_path, [
            ("lessons/x", "x memory"),
            ("lessons/y", "y memory"),
            ("lessons/z", "z memory"),
        ])
        stats = rebuild_vec_index.rebuild_vec_index(self.db_path)
        self.assertEqual(stats["n_indexed"], 3)
        with memory_common.open_db(self.db_path, timeout=5.0) as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM memory_vec_idx").fetchone()[0], 1
            )
            self.assertEqual(
                conn.execute("SELECT count(*) FROM memory_vec_keys").fetchone()[0], 3
            )

    def test_deletes_old_keys_on_rebuild(self):
        """After rebuild, vec_keys should reflect the current memory set, not stale rows."""
        _insert_memories(self.db_path, [
            ("lessons/one", "alpha"),
            ("lessons/two", "beta"),
        ])
        rebuild_vec_index.rebuild_vec_index(self.db_path)
        with memory_common.open_db(self.db_path, timeout=5.0) as conn:
            with conn:
                conn.execute("DELETE FROM memories WHERE id='lessons/one'")
        rebuild_vec_index.rebuild_vec_index(self.db_path)
        with memory_common.open_db(self.db_path, timeout=5.0) as conn:
            ids = {
                r[0] for r in conn.execute("SELECT memory_id FROM memory_vec_keys").fetchall()
            }
            self.assertEqual(ids, {"lessons/two"})


class TestEdgeCases(_VecIndexTestBase):
    def test_empty_db_no_memories(self):
        """memories table exists but has 0 rows. Should early-return cleanly."""
        stats = rebuild_vec_index.rebuild_vec_index(self.db_path)
        self.assertEqual(stats["n_memories"], 0)
        self.assertEqual(stats["n_indexed"], 0)
        with memory_common.open_db(self.db_path, timeout=5.0) as conn:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM memory_vec_idx").fetchone()[0], 0
            )
            self.assertEqual(
                conn.execute("SELECT count(*) FROM memory_vec_keys").fetchone()[0], 0
            )

    def test_missing_db_raises(self):
        with self.assertRaises(FileNotFoundError):
            rebuild_vec_index.rebuild_vec_index(self.tmpdir / "no_such.db")

    def test_missing_memories_table_raises(self):
        with memory_common.open_db(self.db_path, timeout=5.0) as conn:
            with conn:
                conn.execute("DROP TABLE memories")
        with self.assertRaises(RuntimeError) as cm:
            rebuild_vec_index.rebuild_vec_index(self.db_path)
        self.assertIn("memories", str(cm.exception))
        self.assertIn("rebuild_index.py", str(cm.exception))


class TestKeyDerivation(_VecIndexTestBase):
    def test_md5_keys_unique_for_distinct_ids(self):
        items = [(f"lessons/k_{i:02d}", f"content {i}") for i in range(20)]
        _insert_memories(self.db_path, items)
        rebuild_vec_index.rebuild_vec_index(self.db_path)
        with memory_common.open_db(self.db_path, timeout=5.0) as conn:
            keys = [r[0] for r in conn.execute("SELECT key FROM memory_vec_keys").fetchall()]
        self.assertEqual(len(keys), 20)
        self.assertEqual(len(set(keys)), 20)
        for k in keys:
            self.assertGreaterEqual(k, 0)
            self.assertLess(k, 1 << 63)

    def test_key_lookup_round_trip(self):
        items = [
            ("lessons/aaa", "memory A"),
            ("lessons/bbb", "memory B"),
            ("lessons/ccc", "memory C"),
        ]
        _insert_memories(self.db_path, items)
        rebuild_vec_index.rebuild_vec_index(self.db_path)
        with memory_common.open_db(self.db_path, timeout=5.0) as conn:
            rows = conn.execute(
                "SELECT k.key, k.memory_id FROM memory_vec_keys k "
                "JOIN memories m ON m.id = k.memory_id"
            ).fetchall()
        self.assertEqual(len(rows), 3)
        mid_set = {r[1] for r in rows}
        self.assertEqual(mid_set, {"lessons/aaa", "lessons/bbb", "lessons/ccc"})


class TestConcurrency(_VecIndexTestBase):
    def test_lock_file_blocks_concurrent_rebuilds(self):
        import fcntl
        _insert_memories(self.db_path, [("lessons/x", "x")])
        lock_path = self.db_path.parent / ".vec_rebuild.lock"
        held = open(lock_path, "w")
        try:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                rebuild_vec_index.rebuild_vec_index(self.db_path)
        finally:
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
            held.close()
            try:
                lock_path.unlink()
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
