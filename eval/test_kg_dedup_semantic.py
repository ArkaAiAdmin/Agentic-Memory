"""Tests for kg_dedup.py semantic entity resolution.

Covers: merge_entities (shared exact+semantic), compute_semantic_merge_candidates
(mocked embedding), dedup_entities_semantic (mocked embedding).
"""
import os, sys, sqlite3, tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

os.environ["MEMORY_KNOWLEDGE_GRAPH"] = "1"
sys.path.insert(0, os.path.expanduser("~/.config/agentic-memory"))

from kg_dedup import (
    merge_entities, dedup_entities, compute_semantic_merge_candidates,
    dedup_entities_semantic,
)


def _make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("""
        CREATE TABLE kg_entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            observations TEXT DEFAULT '[]',
            mentions INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE kg_edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            relation TEXT NOT NULL,
            weight REAL DEFAULT 1.0,
            observations TEXT DEFAULT '[]',
            valid_at TEXT,
            invalid_at TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    return conn, path


def _insert_entity(conn, name, etype, mentions=1):
    conn.execute(
        "INSERT INTO kg_entities (name, entity_type, mentions) VALUES (?, ?, ?)",
        (name, etype, mentions),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _insert_edge(conn, src, tgt, relation, weight=1.0):
    conn.execute(
        "INSERT INTO kg_edges (source_id, target_id, relation, weight) VALUES (?, ?, ?, ?)",
        (src, tgt, relation, weight),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _make_mock_embedding_search(vectors_map):
    """Create a mock get_embedding_search that returns predetermined vectors."""
    import numpy as np
    mock_es = MagicMock()
    mock_es.model = True  # Not None
    mock_es.np = np

    def mock_encode(names):
        result = np.array([vectors_map.get(n, np.zeros(128)) for n in names], dtype=np.float32)
        norms = np.linalg.norm(result, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return result / norms

    mock_es.encode = mock_encode
    return mock_es


class TestMergeEntities:
    """Direct tests for merge_entities (shared by exact and semantic dedup)."""

    def test_merge_transfers_edges(self):
        conn, _ = _make_db()
        try:
            id_keep = _insert_entity(conn, "A", "t")
            id_merge = _insert_entity(conn, "B", "t")
            id_c = _insert_entity(conn, "C", "t")
            # Edge from merge -> C
            _insert_edge(conn, id_merge, id_c, "knows")
            result = merge_entities(conn, id_keep, id_merge)
            assert result["edges_redirected"] == 1
            edges = conn.execute("SELECT source_id, target_id, relation FROM kg_edges").fetchall()
            assert len(edges) == 1
            assert edges[0][0] == id_keep  # source redirected
            assert edges[0][1] == id_c     # target unchanged
        finally:
            conn.close()

    def test_merge_self_loop_becomes_self_loop_on_keep(self):
        """Edge from merge->keep becomes keep->keep self-loop (not cleaned up)."""
        conn, _ = _make_db()
        try:
            id_keep = _insert_entity(conn, "A", "t")
            id_merge = _insert_entity(conn, "B", "t")
            # Edge from merge -> keep
            _insert_edge(conn, id_merge, id_keep, "self_ref")
            result = merge_entities(conn, id_keep, id_merge)
            assert result["edges_redirected"] == 1
            # Self-loop persists (keep->keep), orphan cleanup only deletes edges referencing merge_id
            edges = conn.execute("SELECT source_id, target_id FROM kg_edges").fetchall()
            assert len(edges) == 1
            assert edges[0][0] == id_keep
            assert edges[0][1] == id_keep
        finally:
            conn.close()

    def test_merge_adds_mentions(self):
        conn, _ = _make_db()
        try:
            id_keep = _insert_entity(conn, "A", "t", mentions=3)
            id_merge = _insert_entity(conn, "B", "t", mentions=5)
            merge_entities(conn, id_keep, id_merge)
            row = conn.execute("SELECT mentions FROM kg_entities WHERE id = ?", (id_keep,)).fetchone()
            assert row[0] == 8
        finally:
            conn.close()

    def test_merge_deletes_old_entity(self):
        conn, _ = _make_db()
        try:
            id_keep = _insert_entity(conn, "A", "t")
            id_merge = _insert_entity(conn, "B", "t")
            merge_entities(conn, id_keep, id_merge)
            count = conn.execute("SELECT COUNT(*) FROM kg_entities").fetchone()[0]
            assert count == 1
        finally:
            conn.close()

    def test_merge_dry_run_no_changes(self):
        conn, _ = _make_db()
        try:
            id_keep = _insert_entity(conn, "A", "t", mentions=3)
            id_merge = _insert_entity(conn, "B", "t", mentions=5)
            id_c = _insert_entity(conn, "C", "t")
            _insert_edge(conn, id_merge, id_c, "knows")
            result = merge_entities(conn, id_keep, id_merge, dry_run=True)
            # dry_run skips all SQL, returns 0
            assert result["edges_redirected"] == 0
            # Nothing actually changed
            assert conn.execute("SELECT COUNT(*) FROM kg_entities").fetchone()[0] == 3
            assert conn.execute("SELECT COUNT(*) FROM kg_edges").fetchone()[0] == 1
            mentions_keep = conn.execute("SELECT mentions FROM kg_entities WHERE id = ?", (id_keep,)).fetchone()[0]
            mentions_merge = conn.execute("SELECT mentions FROM kg_entities WHERE id = ?", (id_merge,)).fetchone()[0]
            assert mentions_keep == 3 and mentions_merge == 5
        finally:
            conn.close()

    def test_merge_deduplicates_edges(self):
        """When redirect creates a duplicate edge, existing edge weight is bumped."""
        conn, _ = _make_db()
        try:
            id_keep = _insert_entity(conn, "A", "t")
            id_merge = _insert_entity(conn, "B", "t")
            id_c = _insert_entity(conn, "C", "t")
            # Edge from merge -> C
            _insert_edge(conn, id_merge, id_c, "works_with", weight=2.0)
            # Edge from keep -> C
            _insert_edge(conn, id_keep, id_c, "works_with", weight=1.0)
            merge_entities(conn, id_keep, id_merge)
            edges = conn.execute("SELECT weight FROM kg_edges").fetchall()
            assert len(edges) == 1
            # Existing keep->C edge kept, weight bumped by 0.1 → 1.1
            # (merged edge's weight 2.0 is discarded)
            assert abs(edges[0][0] - 1.1) < 0.01
        finally:
            conn.close()

    def test_merge_multiple_edges(self):
        conn, _ = _make_db()
        try:
            id_keep = _insert_entity(conn, "A", "t")
            id_merge = _insert_entity(conn, "B", "t")
            id_c = _insert_entity(conn, "C", "t")
            id_d = _insert_entity(conn, "D", "t")
            _insert_edge(conn, id_merge, id_c, "r1")
            _insert_edge(conn, id_d, id_merge, "r2")
            result = merge_entities(conn, id_keep, id_merge)
            assert result["edges_redirected"] == 2
            edges = conn.execute("SELECT source_id, target_id, relation FROM kg_edges ORDER BY id").fetchall()
            assert len(edges) == 2
            # Both should point to keep_id
            assert all(e[0] == id_keep or e[1] == id_keep for e in edges)
        finally:
            conn.close()


class TestComputeSemanticMergeCandidates:
    """Tests for compute_semantic_merge_candidates with mocked embedding."""

    def test_no_entities_returns_empty(self):
        conn, _ = _make_db()
        try:
            result = compute_semantic_merge_candidates(conn)
            # No embedding model available in test env → returns []
            assert result == []
        finally:
            conn.close()

    def test_single_entity_returns_empty(self):
        conn, _ = _make_db()
        try:
            _insert_entity(conn, "Alice", "person")
            result = compute_semantic_merge_candidates(conn)
            assert result == []
        finally:
            conn.close()

    def test_similar_names_detected(self):
        import numpy as np
        conn, _ = _make_db()
        try:
            _insert_entity(conn, "OpenAI", "org")
            _insert_entity(conn, "Open AI", "org")
            v1 = np.zeros(128, dtype=np.float32); v1[0] = 1.0
            v2 = np.zeros(128, dtype=np.float32); v2[0] = 0.99; v2[1] = 0.14
            v1 = v1 / np.linalg.norm(v1); v2 = v2 / np.linalg.norm(v2)
            mock_es = _make_mock_embedding_search({"OpenAI": v1, "Open AI": v2})
            with patch("embedding_search.get_embedding_search", return_value=mock_es):
                candidates = compute_semantic_merge_candidates(conn, threshold=0.90)
                assert len(candidates) == 1
                assert candidates[0]["similarity"] >= 0.90
                assert candidates[0]["entity_type"] == "org"
        finally:
            conn.close()

    def test_different_types_not_compared(self):
        import numpy as np
        conn, _ = _make_db()
        try:
            _insert_entity(conn, "Python", "tech")
            _insert_entity(conn, "Python", "language")
            v = np.zeros(128, dtype=np.float32); v[0] = 1.0
            mock_es = _make_mock_embedding_search({"Python": v})
            with patch("embedding_search.get_embedding_search", return_value=mock_es):
                candidates = compute_semantic_merge_candidates(conn, threshold=0.90)
                assert len(candidates) == 0
        finally:
            conn.close()

    def test_low_similarity_not_returned(self):
        import numpy as np
        conn, _ = _make_db()
        try:
            _insert_entity(conn, "Apple", "org")
            _insert_entity(conn, "Banana", "org")
            v1 = np.zeros(128, dtype=np.float32); v1[0] = 1.0
            v2 = np.zeros(128, dtype=np.float32); v2[1] = 1.0  # Orthogonal
            mock_es = _make_mock_embedding_search({"Apple": v1, "Banana": v2})
            with patch("embedding_search.get_embedding_search", return_value=mock_es):
                candidates = compute_semantic_merge_candidates(conn, threshold=0.90)
                assert len(candidates) == 0
        finally:
            conn.close()

    def test_max_pairs_limit(self):
        import numpy as np
        conn, _ = _make_db()
        try:
            for i in range(5):
                _insert_entity(conn, f"Similar{i}", "org")
            v = np.zeros(128, dtype=np.float32); v[0] = 1.0
            vectors = {f"Similar{i}": v for i in range(5)}
            mock_es = _make_mock_embedding_search(vectors)
            with patch("embedding_search.get_embedding_search", return_value=mock_es):
                candidates = compute_semantic_merge_candidates(conn, threshold=0.90, max_pairs=2)
                assert len(candidates) == 2
        finally:
            conn.close()

    def test_keeps_higher_mentions(self):
        import numpy as np
        conn, _ = _make_db()
        try:
            _insert_entity(conn, "Low", "org", mentions=1)
            _insert_entity(conn, "Low Inc", "org", mentions=10)
            v1 = np.zeros(128, dtype=np.float32); v1[0] = 1.0
            v2 = np.zeros(128, dtype=np.float32); v2[0] = 0.99; v2[1] = 0.14
            v1 = v1 / np.linalg.norm(v1); v2 = v2 / np.linalg.norm(v2)
            mock_es = _make_mock_embedding_search({"Low": v1, "Low Inc": v2})
            with patch("embedding_search.get_embedding_search", return_value=mock_es):
                candidates = compute_semantic_merge_candidates(conn, threshold=0.90)
                assert len(candidates) == 1
                assert candidates[0]["keep_name"] == "Low Inc"
        finally:
            conn.close()


class TestDedupEntitiesSemantic:
    """Tests for dedup_entities_semantic (integration of candidates + merge)."""

    def test_no_candidates_returns_zeros(self):
        conn, _ = _make_db()
        try:
            stats = dedup_entities_semantic(conn)
            assert stats["semantic_groups_found"] == 0
            assert stats["semantic_entities_merged"] == 0
        finally:
            conn.close()

    def test_merges_similar_entities(self):
        import numpy as np
        conn, _ = _make_db()
        try:
            id1 = _insert_entity(conn, "Acme Corp", "org", mentions=2)
            id2 = _insert_entity(conn, "Acme Corporation", "org", mentions=3)
            v1 = np.zeros(128, dtype=np.float32); v1[0] = 1.0
            v2 = np.zeros(128, dtype=np.float32); v2[0] = 0.99; v2[1] = 0.14
            v1 = v1 / np.linalg.norm(v1); v2 = v2 / np.linalg.norm(v2)
            mock_es = _make_mock_embedding_search({"Acme Corp": v1, "Acme Corporation": v2})
            with patch("embedding_search.get_embedding_search", return_value=mock_es):
                stats = dedup_entities_semantic(conn, threshold=0.90)
                assert stats["semantic_entities_merged"] == 1
                assert conn.execute("SELECT COUNT(*) FROM kg_entities").fetchone()[0] == 1
        finally:
            conn.close()

    def test_dry_run_no_merge(self):
        import numpy as np
        conn, _ = _make_db()
        try:
            _insert_entity(conn, "Acme Corp", "org")
            _insert_entity(conn, "Acme Corporation", "org")
            v1 = np.zeros(128, dtype=np.float32); v1[0] = 1.0
            v2 = np.zeros(128, dtype=np.float32); v2[0] = 0.99; v2[1] = 0.14
            v1 = v1 / np.linalg.norm(v1); v2 = v2 / np.linalg.norm(v2)
            mock_es = _make_mock_embedding_search({"Acme Corp": v1, "Acme Corporation": v2})
            with patch("embedding_search.get_embedding_search", return_value=mock_es):
                stats = dedup_entities_semantic(conn, threshold=0.90, dry_run=True)
                assert stats["dry_run"] is True
                assert conn.execute("SELECT COUNT(*) FROM kg_entities").fetchone()[0] == 2
        finally:
            conn.close()

    def test_prevents_double_merge(self):
        import numpy as np
        conn, _ = _make_db()
        try:
            _insert_entity(conn, "Alpha", "org", mentions=1)
            _insert_entity(conn, "Alph", "org", mentions=2)
            _insert_entity(conn, "Alphi", "org", mentions=3)
            v = np.zeros(128, dtype=np.float32); v[0] = 1.0
            vectors = {"Alpha": v, "Alph": v, "Alphi": v}
            mock_es = _make_mock_embedding_search(vectors)
            with patch("embedding_search.get_embedding_search", return_value=mock_es):
                stats = dedup_entities_semantic(conn, threshold=0.90)
                remaining = conn.execute("SELECT COUNT(*) FROM kg_entities").fetchone()[0]
                assert remaining >= 1
                assert stats["semantic_entities_merged"] <= 2
        finally:
            conn.close()
