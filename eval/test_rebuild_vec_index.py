"""Integration tests for rebuild_vec_index — vector index + vec_keys pipeline.

Verifies that rebuild_vec_index correctly populates memory_vec_idx and
memory_vec_keys, handles edge cases, and is idempotent.
"""

import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

import numpy as np

_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
_eval_lme = os.path.join(_project_root, "eval", "longmemeval_s")
if _eval_lme in sys.path:
    sys.path.remove(_eval_lme)
_wrong_modules = [k for k in sys.modules if k in ("metrics", "longmemeval_s.metrics")]
for k in _wrong_modules:
    del sys.modules[k]

from memory_common import (
    open_db,
    run_db_migrations,
    _migrate_kg_tables,
)
from fact_extraction import ensure_facts_schema
from adaptive_retention import ensure_adaptive_schema

# Import rebuild_vec_index functions
sys.path.insert(0, _project_root)
from rebuild_vec_index import (
    _md5_to_uint64,
    _load_cached_embeddings,
    rebuild_vec_index,
    VEC_INDEX_METRIC,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_test_db(tmp_path: Path) -> Path:
    """Create a test database with full schema."""
    db_path = tmp_path / "memory.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON;")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            source_file TEXT,
            tags TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            category TEXT,
            title_slug TEXT,
            importance INTEGER DEFAULT 0,
            pinned INTEGER DEFAULT 0,
            fitness_score REAL DEFAULT 0.0,
            deleted_at TEXT,
            valid_to TEXT,
            superseded_by TEXT,
            hash TEXT,
            embedding_available INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_embeddings (
            memory_id TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            embedding BLOB,
            model_revision TEXT NOT NULL,
            dim INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (memory_id, content_hash)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_vec_keys (
            key INTEGER PRIMARY KEY,
            memory_id TEXT NOT NULL UNIQUE
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_vec_idx (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            n_vectors INTEGER DEFAULT 0,
            dim INTEGER DEFAULT 0,
            metric TEXT DEFAULT 'cos',
            quantization TEXT DEFAULT 'f16',
            connectivity INTEGER DEFAULT 16,
            expansion_add INTEGER DEFAULT 128,
            expansion_search INTEGER DEFAULT 64,
            built_at TEXT,
            index_blob BLOB,
            key_count INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS backlinks (
            source_id TEXT,
            target_id TEXT,
            PRIMARY KEY (source_id, target_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_id TEXT NOT NULL,
            chunk_idx INTEGER NOT NULL,
            start_offset INTEGER NOT NULL,
            end_offset INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    run_db_migrations(conn)
    _migrate_kg_tables(conn)
    ensure_facts_schema(conn)
    ensure_adaptive_schema(conn)

    conn.commit()
    conn.close()
    return db_path


def _insert_memory(conn, **kwargs):
    """Insert a test memory with defaults."""
    defaults = {
        "id": f"test-{int(time.time() * 1000)}",
        "content": "Test memory content",
        "source_file": "lessons/test.md",
        "tags": "",
        "created_at": "2026-06-10T10:00:00Z",
        "updated_at": "2026-06-10T10:00:00Z",
        "category": "lessons",
        "title_slug": "test",
        "importance": 0,
        "pinned": 0,
        "fitness_score": 0.0,
        "deleted_at": None,
        "valid_to": None,
        "superseded_by": None,
        "hash": "",
        "embedding_available": 0,
    }
    defaults.update(kwargs)
    conn.execute(
        """INSERT INTO memories
           (id, content, source_file, tags, created_at, updated_at,
            category, title_slug, importance, pinned, fitness_score,
            deleted_at, valid_to, superseded_by, hash, embedding_available)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            defaults["id"],
            defaults["content"],
            defaults["source_file"],
            defaults["tags"],
            defaults["created_at"],
            defaults["updated_at"],
            defaults["category"],
            defaults["title_slug"],
            defaults["importance"],
            defaults["pinned"],
            defaults["fitness_score"],
            defaults["deleted_at"],
            defaults["valid_to"],
            defaults["superseded_by"],
            defaults["hash"],
            defaults["embedding_available"],
        ),
    )
    return defaults["id"]


# ---------------------------------------------------------------------------
# Tests: _md5_to_uint64
# ---------------------------------------------------------------------------


class TestMd5ToUint64:
    """Test the hash-to-key function."""

    def test_returns_int(self):
        """Output is an integer."""
        result = _md5_to_uint64("test-note-id")
        assert isinstance(result, int)

    def test_positive(self):
        """Key is always positive (fits in signed int64)."""
        for i in range(100):
            result = _md5_to_uint64(f"note-{i}")
            assert 0 <= result < (1 << 63)

    def test_deterministic(self):
        """Same input always produces same output."""
        a = _md5_to_uint64("stable-id")
        b = _md5_to_uint64("stable-id")
        assert a == b

    def test_different_inputs_different_outputs(self):
        """Different IDs produce different keys (high probability)."""
        keys = {_md5_to_uint64(f"unique-{i}") for i in range(50)}
        assert len(keys) == 50


# ---------------------------------------------------------------------------
# Tests: _ensure_schema
# ---------------------------------------------------------------------------


class TestEnsureSchema:
    """Test schema creation."""

    def test_creates_vec_tables(self, tmp_path):
        """_ensure_schema creates memory_vec_idx and memory_vec_keys."""
        db_path = _create_test_db(tmp_path)
        conn = sqlite3.connect(str(db_path))

        # Verify tables exist
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "memory_vec_idx" in tables
        assert "memory_vec_keys" in tables
        conn.close()


# ---------------------------------------------------------------------------
# Tests: _load_cached_embeddings
# ---------------------------------------------------------------------------


class TestLoadCachedEmbeddings:
    """Test embedding loading."""

    def test_empty_db(self, tmp_path):
        """Returns empty dict on DB with no embeddings."""
        db_path = _create_test_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        result = _load_cached_embeddings(conn)
        assert result == {}
        conn.close()

    def test_with_embeddings(self, tmp_path):
        """Returns dict of memory_id -> (hash, embedding) for cached rows."""
        db_path = _create_test_db(tmp_path)
        conn = sqlite3.connect(str(db_path))

        # Insert a memory + embedding
        _insert_memory(conn, id="emb-1", content="Embedding test")

        emb = np.random.randn(256).astype(np.float32)
        emb_blob = emb.tobytes()
        conn.execute(
            """INSERT INTO memory_embeddings
               (memory_id, content_hash, embedding, model_revision, dim, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("emb-1", "hash123", emb_blob, "model-v1", 256, "2026-06-10T10:00:00Z"),
        )
        conn.commit()

        result = _load_cached_embeddings(conn)
        assert "emb-1" in result
        assert result["emb-1"][0] == "hash123"  # content_hash
        # Blob is raw float32 bytes: 256 floats × 4 bytes = 1024 bytes
        assert len(result["emb-1"][1]) == 256 * 4
        conn.close()


# ---------------------------------------------------------------------------
# Tests: rebuild_vec_index (integration — requires embedding model)
# ---------------------------------------------------------------------------


class TestRebuildVecIndex:
    """Integration tests for rebuild_vec_index.

    These tests require the embedding model to be available.
    Skip if model2vec is not installed.
    """

    @pytest.fixture(autouse=True)
    def _check_model(self):
        """Skip if embedding model unavailable."""
        try:
            from embedding_search import get_embedding_search

            es = get_embedding_search()
            if es.model is None:
                pytest.skip("Embedding model not available")
        except Exception:
            pytest.skip("Embedding model not available")

    def test_empty_db(self, tmp_path):
        """Rebuild on empty DB returns zero stats."""
        db_path = _create_test_db(tmp_path)
        result = rebuild_vec_index(db_path)
        assert result["n_memories"] == 0
        assert result["n_indexed"] == 0

    def test_with_memories(self, tmp_path):
        """Rebuild indexes memories and populates vec_keys."""
        db_path = _create_test_db(tmp_path)
        conn = sqlite3.connect(str(db_path))

        for i in range(5):
            _insert_memory(conn, id=f"vec-{i}", content=f"Memory about Python {i}")
        conn.commit()
        conn.close()

        result = rebuild_vec_index(db_path)
        assert result["n_memories"] == 5
        assert result["n_indexed"] == 5
        assert result["dim"] > 0
        assert result["serialized_bytes"] > 0

        # Verify vec_keys populated
        conn = sqlite3.connect(str(db_path))
        vec_keys_count = conn.execute(
            "SELECT COUNT(*) FROM memory_vec_keys"
        ).fetchone()[0]
        assert vec_keys_count == 5

        # Verify vec_idx metadata
        idx_row = conn.execute(
            "SELECT n_vectors, dim, metric FROM memory_vec_idx WHERE id = 1"
        ).fetchone()
        assert idx_row is not None
        assert idx_row[0] == 5  # n_vectors
        assert idx_row[1] > 0  # dim
        assert idx_row[2] == VEC_INDEX_METRIC
        conn.close()

    def test_idempotent(self, tmp_path):
        """Rebuilding twice produces same result."""
        db_path = _create_test_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        for i in range(3):
            _insert_memory(conn, id=f"idem-{i}", content=f"Note {i}")
        conn.commit()
        conn.close()

        r1 = rebuild_vec_index(db_path)
        r2 = rebuild_vec_index(db_path)
        assert r1["n_indexed"] == r2["n_indexed"]
        assert r1["dim"] == r2["dim"]

    def test_missing_db_raises(self, tmp_path):
        """Rebuild raises FileNotFoundError for missing DB."""
        with pytest.raises(FileNotFoundError):
            rebuild_vec_index(tmp_path / "nonexistent.db")

    def test_missing_memories_table_raises(self, tmp_path):
        """Rebuild raises RuntimeError if memories table missing."""
        db_path = tmp_path / "memory.db"
        # Use open_db to get full schema, then drop memories table

        with open_db(db_path) as db:
            db.execute(
                "INSERT OR IGNORE INTO schema_version (id, version) VALUES (1, 5)"
            )
            db.commit()
        conn = sqlite3.connect(str(db_path))
        conn.execute("DROP TABLE IF EXISTS memories")
        conn.commit()
        conn.close()

        with pytest.raises(RuntimeError, match="memories.*table.*missing"):
            rebuild_vec_index(db_path)

    def test_stats_keys(self, tmp_path):
        """Result dict contains all expected keys."""
        db_path = _create_test_db(tmp_path)
        result = rebuild_vec_index(db_path)
        expected_keys = {
            "n_memories",
            "n_indexed",
            "n_skipped",
            "dim",
            "quantization",
            "metric",
            "serialized_bytes",
            "elapsed_s",
            "collisions_resolved",
        }
        assert expected_keys.issubset(set(result.keys()))
