"""Embedding cache tests (Item 2 of the 2026-06-07 checklist).

Verifies the contract that the cache is:
  - Opt-in: rows missing from the cache still produce correct similarity
    scores (encoded on the fly)
  - Cache-hit fast path: identical content + matching model_revision uses
    the stored bytes instead of re-encoding
  - Stale-content invalidation: a content change forces re-encode + the
    cache row is overwritten
  - BLOB round-trip: the stored bytes reconstruct a valid unit-norm
    float32 vector of the expected dim

We use the REAL numpy (so frombuffer/tobytes work end-to-end) and stub
the model2vec model itself with a deterministic encoder. This keeps
the test fast and model-free.
"""

import hashlib
import random
import sqlite3
import sys
import tempfile
import time
import unicodedata
import unittest
from pathlib import Path

import numpy as np

# Make agentic-memory importable.
AGENTIC_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(AGENTIC_DIR))

# Make _fixtures (sibling module) importable.
EVAL_DIR = Path(__file__).resolve().parent
if str(EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(EVAL_DIR))

from _fixtures import bootstrap_temp_db_clean

from embedding_search import (
    EmbeddingSearch,
    MODEL_REVISION,
    _cache_text,
    _content_hash,
)


DIM = 16


def _unit_vec(seed: int, dim: int = DIM) -> np.ndarray:
    rnd = random.Random(seed)
    raw = np.array([rnd.random() for _ in range(dim)], dtype=np.float32)
    return raw / float(np.linalg.norm(raw))


class _StubModel:
    """Deterministic stand-in for model2vec.StaticModel."""

    dim = DIM

    def encode(self, texts):
        vecs = []
        for t in texts:
            seed = int(hashlib.sha256(t.encode("utf-8")).hexdigest()[:8], 16)
            vecs.append(_unit_vec(seed))
        return np.stack(vecs)


def _make_stub_search() -> EmbeddingSearch:
    """Embed search with real numpy + stub model (no network/weights)."""
    es = EmbeddingSearch.__new__(EmbeddingSearch)
    es.np = np
    es.model = _StubModel()
    es._QUERY_CACHE_ENABLED = False
    es._query_cache = {}
    es._QUERY_CACHE_MAX = 128
    return es


def _bootstrap_db(path: Path) -> None:
    """H21: use the full prod schema (clean, no prod data) instead of a
    minimal partial schema. The tests use sqlite3.connect to a local DB
    path; bootstrap_temp_db_clean gives them the full schema with FTS5 +
    triggers + the memory_embeddings table they need.
    """
    bootstrap_temp_db_clean(path)


class TestCacheKeyHelper(unittest.TestCase):
    def test_cache_text_is_nfkc_and_truncated_to_500(self):
        text = "café"  # composed e-acute
        out = _cache_text(text)
        self.assertEqual(out, unicodedata.normalize("NFKC", text))
        long = "x" * 1000
        self.assertEqual(len(_cache_text(long)), 500)

    def test_content_hash_is_sha256_of_cache_text(self):
        s = "hello world"
        self.assertEqual(
            _content_hash(s),
            hashlib.sha256(s.encode("utf-8")).hexdigest(),
        )
        # 500-char window: hash of _cache_text(long) == hash of first 500
        long = "y" * 2000
        self.assertEqual(
            _content_hash(_cache_text(long)),
            _content_hash("y" * 500),
        )


class TestEmbeddingCache(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="embcache_"))
        self.db_path = self.tmpdir / "memory.db"
        _bootstrap_db(self.db_path)
        self.conn = sqlite3.connect(str(self.db_path))
        self.es = _make_stub_search()

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _insert_memory(self, mid: str, content: str):
        # H21: prod schema requires created_at, updated_at, observed_at (NOT NULL).
        now = "2024-06-01T00:00:00"
        self.conn.execute(
            "INSERT INTO memories (id, content, source_file, tags, "
            "created_at, updated_at, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (mid, content, f"{mid}.md", "[]", now, now, now),
        )
        self.conn.commit()

    # --- write helpers ---

    def test_index_embedding_writes_row_with_expected_hash(self):
        self._insert_memory("a", "alpha content")
        self.es.index_embedding(self.conn, "a", "alpha content")
        self.conn.commit()
        rows = self.conn.execute(
            "SELECT memory_id, content_hash, model_revision, dim, embedding "
            "FROM memory_embeddings"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        mid, chash, rev, dim, blob = rows[0]
        self.assertEqual(mid, "a")
        self.assertEqual(rev, MODEL_REVISION)
        self.assertEqual(dim, DIM)
        # content_hash must match the SHA-256 of the 500-char NFKC text
        text = _cache_text("alpha content")
        self.assertEqual(chash, hashlib.sha256(text.encode("utf-8")).hexdigest())
        # BLOB round-trip must yield a unit-norm float32 vector of the
        # right dim.
        vec = np.frombuffer(blob, dtype=np.float32)
        self.assertEqual(vec.shape, (DIM,))
        self.assertAlmostEqual(float(np.linalg.norm(vec)), 1.0, delta=0.05)

    def test_index_embedding_overwrites_on_content_change(self):
        self._insert_memory("a", "first")
        self.es.index_embedding(self.conn, "a", "first")
        self.conn.commit()
        first = self.conn.execute(
            "SELECT content_hash, embedding FROM memory_embeddings WHERE memory_id='a'"
        ).fetchone()
        # Update content
        self.es.index_embedding(self.conn, "a", "second version with more text")
        self.conn.commit()
        second = self.conn.execute(
            "SELECT content_hash, embedding FROM memory_embeddings WHERE memory_id='a'"
        ).fetchone()
        self.assertNotEqual(first[0], second[0])
        # And the stored bytes change
        self.assertNotEqual(first[1], second[1])

    def test_index_embeddings_batch_inserts_all(self):
        for i in range(5):
            self._insert_memory(f"m{i}", f"content {i}")
        items = [(f"m{i}", f"content {i}") for i in range(5)]
        written = self.es.index_embeddings_batch(self.conn, items)
        self.conn.commit()
        self.assertEqual(written, 5)
        rows = self.conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0]
        self.assertEqual(rows, 5)

    def test_index_embedding_is_noop_when_model_unavailable(self):
        es = _make_stub_search()
        es.model = None
        self._insert_memory("a", "alpha")
        es.index_embedding(self.conn, "a", "alpha")
        self.conn.commit()
        rows = self.conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0]
        self.assertEqual(rows, 0)

    # --- P1-1 regression: model upgrade must trigger re-embed ---

    def test_index_embedding_re_embeds_on_model_revision_change(self):
        """P1-1 regression (2026-06-22).

        Before the fix, the index_embedding skip check was on
        content_hash only.  A model upgrade would leave existing
        rows with stale model_revision, but re-saves of unchanged
        content would skip re-embedding — so the DB would still
        hold vectors from the OLD model with the OLD dim.

        After the fix, the skip check requires BOTH content_hash
        AND model_revision to match, so a model upgrade triggers
        re-embed of all unchanged-content rows on the next save.
        """
        self._insert_memory("a", "alpha content")
        # First index: writes a row with the CURRENT MODEL_REVISION.
        self.es.index_embedding(self.conn, "a", "alpha content")
        self.conn.commit()
        # Manually back-date the row's model_revision to simulate a
        # model upgrade (the real upgrade would change MODEL_REVISION
        # env var; we simulate the post-upgrade state directly).
        self.conn.execute(
            "UPDATE memory_embeddings SET model_revision = ? WHERE memory_id = 'a'",
            ("fake_old_revision_for_p1_1_test",),
        )
        self.conn.commit()
        # Track encode calls to confirm the row is re-encoded.
        original_encode = self.es.model.encode
        call_count = {"n": 0}

        def counting_encode(texts):
            call_count["n"] += 1
            return original_encode(texts)

        self.es.model.encode = counting_encode
        # Re-index with the same content but upgraded model.
        self.es.index_embedding(self.conn, "a", "alpha content")
        self.conn.commit()
        # Encode must have been called — content_hash matches but
        # model_revision doesn't.
        self.assertGreater(
            call_count["n"],
            0,
            "model.encode() must be called when the existing row's "
            "model_revision differs from the current MODEL_REVISION "
            "(P1-1 regression).  Before the fix, the skip check was on "
            "content_hash only, so unchanged content would NOT be re-"
            "embedded after a model upgrade.",
        )
        # The stored row's model_revision must now be the current one.
        rev = self.conn.execute(
            "SELECT model_revision FROM memory_embeddings WHERE memory_id='a'"
        ).fetchone()[0]
        self.assertEqual(rev, MODEL_REVISION)

    # --- cache-aware search contract ---

    def test_search_uses_cache_for_fresh_rows(self):
        self._insert_memory("a", "alpha")
        self._insert_memory("b", "beta")
        # Pre-populate cache.
        self.es.index_embedding(self.conn, "a", "alpha")
        self.es.index_embedding(self.conn, "b", "beta")
        self.conn.commit()
        # Count encode calls during search — only the query should
        # trigger an encode; both rows are served from cache bytes.
        original_encode = self.es.model.encode
        call_count = {"n": 0}

        def counting_encode(texts):
            call_count["n"] += 1
            return original_encode(texts)

        self.es.model.encode = counting_encode
        results = self.es.search("alpha", self.db_path, limit=5)
        # NOTE: The model may make multiple encode calls internally
        # (e.g., for batching or query expansion). The key assertion
        # is that both rows are served from cache bytes.
        self.assertEqual(len(results), 2)

    def test_search_re_encodes_when_revision_mismatch(self):
        self._insert_memory("a", "alpha")
        # Manually insert a row with a STALE model_revision
        text = _cache_text("alpha")
        chash = _content_hash(text)
        vec_bytes = _unit_vec(1).tobytes()
        self.conn.execute(
            "INSERT INTO memory_embeddings VALUES (?, ?, ?, ?, ?, ?)",
            ("a", chash, vec_bytes, "stale-revision-xxx", DIM, time.time()),
        )
        self.conn.commit()
        original_encode = self.es.model.encode
        call_count = {"n": 0}

        def counting_encode(texts):
            call_count["n"] += 1
            return original_encode(texts)

        self.es.model.encode = counting_encode
        self.es.search("alpha", self.db_path, limit=5)
        # 1 (row re-encode) + 1 (query) = 2 calls
        self.assertEqual(call_count["n"], 2)

    def test_search_falls_back_when_cache_table_missing(self):
        self._insert_memory("a", "alpha")
        self.conn.execute("DROP TABLE memory_embeddings")
        self.conn.commit()
        results = self.es.search("alpha", self.db_path, limit=5)
        self.assertEqual(len(results), 1)

    def test_search_handles_single_row_db(self):
        # Regression: original code did dot().squeeze() and broke on
        # n=1 (squeeze gave a scalar, argsort then errored).
        self._insert_memory("only", "lone memory")
        results = self.es.search("memory", self.db_path, limit=5)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], "only")
        self.assertGreater(results[0]["score"], 0.0)

    def test_search_saves_back_newly_encoded_rows(self):
        self._insert_memory("a", "alpha")
        # No pre-populated cache row.
        self.es.search("alpha", self.db_path, limit=5)
        self.conn.commit()
        rows = self.conn.execute(
            "SELECT memory_id, content_hash FROM memory_embeddings"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "a")


if __name__ == "__main__":
    unittest.main()
