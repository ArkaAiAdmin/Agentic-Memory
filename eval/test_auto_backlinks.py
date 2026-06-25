"""Comprehensive tests for auto-backlinks: bidirectional wiki-links and
semantic similarity edges."""
import os
import sqlite3
import tempfile
import unittest
import unittest.mock
from pathlib import Path


def _make_test_db() -> sqlite3.Connection:
    """Create a temp DB with all tables needed by auto-backlinks."""
    conn = sqlite3.connect(':memory:')
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute("""
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            note_id TEXT UNIQUE NOT NULL,
            content TEXT,
            tags TEXT DEFAULT '[]',
            category TEXT DEFAULT '',
            source_file TEXT DEFAULT '',
            pinned INTEGER DEFAULT 0,
            is_global INTEGER DEFAULT 0,
            metadata TEXT DEFAULT '{}',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            accessed_at TEXT DEFAULT (datetime('now')),
            access_count INTEGER DEFAULT 0,
            success_score REAL DEFAULT 1.0,
            adaptive_halflife_days REAL DEFAULT 30.0,
            tier TEXT DEFAULT 'warm',
            psi REAL DEFAULT 0.0,
            deleted_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE memory_embeddings (
            memory_id TEXT PRIMARY KEY,
            embedding BLOB NOT NULL,
            dim INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE backlinks (
            source_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            PRIMARY KEY (source_id, target_id)
        )
    """)
    conn.execute("""
        CREATE TABLE kg_entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            entity_type TEXT NOT NULL DEFAULT 'concept',
            mention_count INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(name, entity_type)
        )
    """)
    conn.execute("""
        CREATE TABLE kg_edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            relation TEXT NOT NULL,
            weight REAL DEFAULT 1.0,
            context TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            valid_to TEXT,
            FOREIGN KEY (source_id) REFERENCES kg_entities(id) ON DELETE CASCADE,
            FOREIGN KEY (target_id) REFERENCES kg_entities(id) ON DELETE CASCADE,
            UNIQUE(source_id, target_id, relation)
        )
    """)
    conn.execute("""
        CREATE TABLE memory_vec_keys (
            note_id TEXT PRIMARY KEY,
            vec_key TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TEXT DEFAULT (datetime('now'))
        )
    """)
    return conn


def _insert_memory(conn, note_id, content='test content', tags='[]'):
    """Insert a memory row."""
    conn.execute(
        'INSERT INTO memories (note_id, content, tags) VALUES (?, ?, ?)',
        (note_id, content, tags)
    )


def _insert_embedding(conn, note_id, vec):
    """Insert a float32 embedding blob for a memory."""
    import numpy as np
    blob = np.array(vec, dtype=np.float32).tobytes()
    conn.execute(
        'INSERT OR REPLACE INTO memory_embeddings (memory_id, embedding, dim) VALUES (?, ?, ?)',
        (note_id, blob, len(vec))
    )


# ---------------------------------------------------------------------------
# _index_backlinks tests
# ---------------------------------------------------------------------------

class TestIndexBacklinks(unittest.TestCase):
    """Bidirectional wiki-link extraction tests."""

    def setUp(self):
        self.conn = _make_test_db()
        self.db = self.conn

    def tearDown(self):
        self.conn.close()

    def _call(self, note_id, content):
        from save_pipeline import _index_backlinks
        _index_backlinks(self.db, note_id, content)

    def test_single_link_creates_bidirectional(self):
        """[[target]] from A creates A->target AND target->A."""
        _insert_memory(self.db, 'a')
        _insert_memory(self.db, 'b')
        self._call('a', 'See [[b]] for details')
        rows = self.db.execute('SELECT * FROM backlinks ORDER BY source_id').fetchall()
        self.assertEqual(len(rows), 2)
        pairs = {(r[0], r[1]) for r in rows}
        self.assertIn(('a', 'b'), pairs)
        self.assertIn(('b', 'a'), pairs)

    def test_multiple_links(self):
        """[[x]] and [[y]] in one document create 4 backlink rows."""
        _insert_memory(self.db, 'a')
        _insert_memory(self.db, 'x')
        _insert_memory(self.db, 'y')
        self._call('a', 'See [[x]] and also [[y]]')
        rows = self.db.execute('SELECT * FROM backlinks').fetchall()
        pairs = {(r[0], r[1]) for r in rows}
        # a->x, x->a, a->y, y->a
        self.assertEqual(len(pairs), 4)
        self.assertIn(('a', 'x'), pairs)
        self.assertIn(('x', 'a'), pairs)
        self.assertIn(('a', 'y'), pairs)
        self.assertIn(('y', 'a'), pairs)

    def test_no_links_no_rows(self):
        """No [[wiki-links]] means no backlink rows."""
        _insert_memory(self.db, 'a')
        self._call('a', 'This is plain text with no links.')
        rows = self.db.execute('SELECT * FROM backlinks').fetchall()
        self.assertEqual(len(rows), 0)

    def test_self_link_suppressed(self):
        """[[self]] should be ignored (no self-referencing backlink)."""
        _insert_memory(self.db, 'a')
        self._call('a', 'See [[a]] for more.')
        rows = self.db.execute('SELECT * FROM backlinks').fetchall()
        self.assertEqual(len(rows), 0)

    def test_pipe_syntax(self):
        """[[target|label]] extracts 'target', ignores 'label'."""
        _insert_memory(self.db, 'a')
        _insert_memory(self.db, 'b')
        self._call('a', 'See [[b|the B note]] please')
        rows = self.db.execute('SELECT * FROM backlinks ORDER BY source_id').fetchall()
        pairs = {(r[0], r[1]) for r in rows}
        self.assertIn(('a', 'b'), pairs)
        self.assertIn(('b', 'a'), pairs)

    def test_md_stripping(self):
        """[[other.md]] normalizes to 'other'."""
        _insert_memory(self.db, 'a')
        _insert_memory(self.db, 'other')
        self._call('a', 'Link to [[other.md]]')
        rows = self.db.execute('SELECT * FROM backlinks').fetchall()
        pairs = {(r[0], r[1]) for r in rows}
        self.assertIn(('a', 'other'), pairs)

    def test_case_normalization(self):
        """[[Target-Note]] normalizes to 'target-note'."""
        _insert_memory(self.db, 'a')
        _insert_memory(self.db, 'target-note')
        self._call('a', 'See [[Target-Note]]')
        rows = self.db.execute('SELECT * FROM backlinks').fetchall()
        pairs = {(r[0], r[1]) for r in rows}
        self.assertIn(('a', 'target-note'), pairs)

    def test_backslash_normalization(self):
        """[[a\\b]] normalizes to 'a/b'."""
        _insert_memory(self.db, 'a')
        _insert_memory(self.db, 'a/b')
        self._call('a', 'Link to [[a\\b]]')
        rows = self.db.execute('SELECT * FROM backlinks').fetchall()
        pairs = {(r[0], r[1]) for r in rows}
        self.assertIn(('a', 'a/b'), pairs)

    def test_duplicate_links_idempotent(self):
        """Calling twice with same link doesn't create duplicate rows."""
        _insert_memory(self.db, 'a')
        _insert_memory(self.db, 'b')
        self._call('a', '[[b]] and [[b]]')
        rows = self.db.execute('SELECT * FROM backlinks').fetchall()
        pairs = {(r[0], r[1]) for r in rows}
        # INSERT OR IGNORE ensures no duplicates
        self.assertEqual(len(pairs), 2)  # a->b, b->a

    def test_empty_target_ignored(self):
        """[[  ]] (whitespace-only) produces no backlinks."""
        _insert_memory(self.db, 'a')
        self._call('a', 'See [[]] and [[  ]]')
        rows = self.db.execute('SELECT * FROM backlinks').fetchall()
        self.assertEqual(len(rows), 0)

    def test_realistic_content(self):
        """Realistic multi-paragraph content with mixed wiki-links."""
        _insert_memory(self.db, 'project-alpha')
        _insert_memory(self.db, 'meeting-notes-2026-06-09')
        _insert_memory(self.db, 'todo-list')
        content = '''# Project Alpha

This project builds on [[meeting-notes-2026-06-09]] decisions.

## TODO
- Fix the [[todo-list]] items
- Review [[meeting-notes-2026-06-09]] again
'''
        self._call('project-alpha', content)
        rows = self.db.execute('SELECT * FROM backlinks').fetchall()
        pairs = {(r[0], r[1]) for r in rows}
        self.assertIn(('project-alpha', 'meeting-notes-2026-06-09'), pairs)
        self.assertIn(('meeting-notes-2026-06-09', 'project-alpha'), pairs)
        self.assertIn(('project-alpha', 'todo-list'), pairs)
        self.assertIn(('todo-list', 'project-alpha'), pairs)


# ---------------------------------------------------------------------------
# _auto_semantic_backlinks tests
# ---------------------------------------------------------------------------

class TestAutoSemanticBacklinks(unittest.TestCase):
    """Semantic similarity edge creation tests."""

    def setUp(self):
        self.conn = _make_test_db()
        self.db = self.conn

    def tearDown(self):
        self.conn.close()

    def _call(self, note_id, content='', top_k=5):
        from save_pipeline import _auto_semantic_backlinks
        _auto_semantic_backlinks(self.db, note_id, content, top_k=top_k)

    def _seed_memories(self, n=5, dim=8):
        """Insert N memories with random-ish embeddings."""
        import numpy as np
        rng = np.random.RandomState(42)
        for i in range(n):
            mid = f'mem-{i}'
            _insert_memory(self.db, mid)
            vec = rng.randn(dim).astype(np.float32)
            vec /= max(np.linalg.norm(vec), 1e-8)
            _insert_embedding(self.db, mid, vec)

    def _seed_identical_memories(self, n=5, dim=8):
        """Insert N memories with identical embeddings."""
        import numpy as np
        vec = np.ones(dim, dtype=np.float32)
        vec /= np.linalg.norm(vec)
        for i in range(n):
            mid = f'mem-{i}'
            _insert_memory(self.db, mid)
            _insert_embedding(self.db, mid, vec)

    def test_no_embedding_skips(self):
        """Memory without embedding produces no edges."""
        _insert_memory(self.db, 'no-emb')
        self._call('no-emb')
        edges = self.db.execute('SELECT COUNT(*) FROM kg_edges').fetchone()[0]
        self.assertEqual(edges, 0)

    def test_single_candidate_creates_edges(self):
        """With 2 memories, creates bidirectional edge between them."""
        import numpy as np
        _insert_memory(self.db, 'a')
        _insert_memory(self.db, 'b')
        vec_a = np.array([1, 0, 0, 0, 0, 0, 0, 0], dtype=np.float32)
        vec_b = np.array([0.99, 0.1, 0, 0, 0, 0, 0, 0], dtype=np.float32)
        _insert_embedding(self.db, 'a', vec_a)
        _insert_embedding(self.db, 'b', vec_b)
        self._call('a')
        edges = self.db.execute('SELECT COUNT(*) FROM kg_edges').fetchone()[0]
        # Bidirectional: a->b and b->a
        self.assertEqual(edges, 2)
        # Check weight is cosine similarity
        edge = self.db.execute('SELECT weight FROM kg_edges LIMIT 1').fetchone()
        self.assertGreater(edge[0], 0.9)  # very similar vectors

    def test_top_k_limits_results(self):
        """With top_k=2, only 2 closest memories get edges."""
        import numpy as np
        rng = np.random.RandomState(99)
        _insert_memory(self.db, 'query')
        qvec = rng.randn(8).astype(np.float32)
        qvec /= np.linalg.norm(qvec)
        _insert_embedding(self.db, 'query', qvec)
        for i in range(5):
            mid = f'cand-{i}'
            _insert_memory(self.db, mid)
            cvec = rng.randn(8).astype(np.float32)
            cvec /= np.linalg.norm(cvec)
            _insert_embedding(self.db, mid, cvec)
        self._call('query', top_k=2)
        # top_k=2 means at most 2 forward edges + 2 reverse = 4 edges
        edges = self.db.execute('SELECT COUNT(*) FROM kg_edges').fetchone()[0]
        self.assertLessEqual(edges, 4)

    def test_low_similarity_filtered(self):
        """Candidates below 0.30 threshold produce no edges."""
        import numpy as np
        _insert_memory(self.db, 'a')
        _insert_memory(self.db, 'b')
        # Orthogonal vectors -> cosine ~0
        vec_a = np.array([1, 0, 0, 0, 0, 0, 0, 0], dtype=np.float32)
        vec_b = np.array([0, 1, 0, 0, 0, 0, 0, 0], dtype=np.float32)
        _insert_embedding(self.db, 'a', vec_a)
        _insert_embedding(self.db, 'b', vec_b)
        self._call('a')
        edges = self.db.execute('SELECT COUNT(*) FROM kg_edges').fetchone()[0]
        self.assertEqual(edges, 0)

    def test_identical_memories_max_similarity(self):
        """Identical embeddings produce edges with weight ~1.0."""
        self._seed_identical_memories(3)
        self._call('mem-0')
        edges = self.db.execute('SELECT weight FROM kg_edges').fetchall()
        for (w,) in edges:
            self.assertAlmostEqual(w, 1.0, places=3)

    def test_idempotent(self):
        """Calling twice doesn't duplicate edges (INSERT OR IGNORE)."""
        import numpy as np
        _insert_memory(self.db, 'a')
        _insert_memory(self.db, 'b')
        vec = np.ones(8, dtype=np.float32) / np.sqrt(8)
        _insert_embedding(self.db, 'a', vec)
        _insert_embedding(self.db, 'b', vec)
        self._call('a')
        self._call('a')
        edges = self.db.execute('SELECT COUNT(*) FROM kg_edges').fetchone()[0]
        # Still exactly 2 (bidirectional), not 4
        self.assertEqual(edges, 2)

    def test_zero_norm_embedding_skipped(self):
        """Zero-norm embedding produces no edges (division safety)."""
        _insert_memory(self.db, 'a')
        _insert_memory(self.db, 'b')
        _insert_embedding(self.db, 'a', [0, 0, 0, 0])
        _insert_embedding(self.db, 'b', [1, 0, 0, 0])
        self._call('a')
        edges = self.db.execute('SELECT COUNT(*) FROM kg_edges').fetchone()[0]
        self.assertEqual(edges, 0)

    def test_self_excluded(self):
        """Query memory is excluded from candidates (no self-loop)."""
        import numpy as np
        _insert_memory(self.db, 'a')
        vec = np.ones(8, dtype=np.float32) / np.sqrt(8)
        _insert_embedding(self.db, 'a', vec)
        self._call('a')
        edges = self.db.execute('SELECT COUNT(*) FROM kg_edges').fetchone()[0]
        self.assertEqual(edges, 0)

    def test_relation_entity_created(self):
        """'semantically_related' relation entity is auto-created."""
        import numpy as np
        _insert_memory(self.db, 'a')
        _insert_memory(self.db, 'b')
        vec = np.ones(8, dtype=np.float32) / np.sqrt(8)
        _insert_embedding(self.db, 'a', vec)
        _insert_embedding(self.db, 'b', vec)
        self._call('a')
        rel = self.db.execute(
            "SELECT name FROM kg_entities WHERE entity_type = 'relation'"
        ).fetchone()
        self.assertIsNotNone(rel)
        self.assertEqual(rel[0], 'semantically_related')

    def test_memory_entities_created(self):
        """Memory entities in kg_entities are auto-created for both sides."""
        import numpy as np
        _insert_memory(self.db, 'a')
        _insert_memory(self.db, 'b')
        vec = np.ones(8, dtype=np.float32) / np.sqrt(8)
        _insert_embedding(self.db, 'a', vec)
        _insert_embedding(self.db, 'b', vec)
        self._call('a')
        entities = {
            r[0] for r in self.db.execute(
                "SELECT name FROM kg_entities WHERE entity_type = 'memory'"
            ).fetchall()
        }
        self.assertIn('a', entities)
        self.assertIn('b', entities)

    def test_bidirectional_edges_both_exist(self):
        """Both a->b and b->a edges exist after calling _auto_semantic_backlinks."""
        import numpy as np
        _insert_memory(self.db, 'a')
        _insert_memory(self.db, 'b')
        vec = np.ones(8, dtype=np.float32) / np.sqrt(8)
        _insert_embedding(self.db, 'a', vec)
        _insert_embedding(self.db, 'b', vec)
        self._call('a')
        # Get entity IDs
        a_id = self.db.execute("SELECT id FROM kg_entities WHERE name = 'a' AND entity_type = 'memory'").fetchone()[0]
        b_id = self.db.execute("SELECT id FROM kg_entities WHERE name = 'b' AND entity_type = 'memory'").fetchone()[0]
        ab = self.db.execute('SELECT COUNT(*) FROM kg_edges WHERE source_id = ? AND target_id = ?', (a_id, b_id)).fetchone()[0]
        ba = self.db.execute('SELECT COUNT(*) FROM kg_edges WHERE source_id = ? AND target_id = ?', (b_id, a_id)).fetchone()[0]
        self.assertEqual(ab, 1)
        self.assertEqual(ba, 1)

    def test_graceful_on_corrupt_blob(self):
        """Corrupt embedding blob is silently skipped."""
        _insert_memory(self.db, 'a')
        _insert_memory(self.db, 'b')
        # Insert corrupt blob (not valid float32)
        self.db.execute(
            'INSERT OR REPLACE INTO memory_embeddings (memory_id, embedding, dim) VALUES (?, ?, ?)',
            ('a', b'not-a-valid-vector', 4)
        )
        _insert_embedding(self.db, 'b', [1, 0, 0, 0])
        self._call('a')  # should not raise
        edges = self.db.execute('SELECT COUNT(*) FROM kg_edges').fetchone()[0]
        self.assertEqual(edges, 0)

    def test_many_candidates_respects_top_k(self):
        """With 20 candidates and top_k=3, only 3 forward edges created."""
        import numpy as np
        rng = np.random.RandomState(123)
        dim = 16
        _insert_memory(self.db, 'query')
        qvec = rng.randn(dim).astype(np.float32)
        qvec /= np.linalg.norm(qvec)
        _insert_embedding(self.db, 'query', qvec)
        for i in range(20):
            mid = f'cand-{i:02d}'
            _insert_memory(self.db, mid)
            cvec = rng.randn(dim).astype(np.float32)
            cvec /= np.linalg.norm(cvec)
            _insert_embedding(self.db, mid, cvec)
        self._call('query', top_k=3)
        # 3 forward + 3 reverse = 6 edges max
        edges = self.db.execute('SELECT COUNT(*) FROM kg_edges').fetchone()[0]
        self.assertLessEqual(edges, 6)

    def test_no_candidates_at_all(self):
        """Only the query memory has an embedding -> no edges."""
        _insert_memory(self.db, 'only-one')
        _insert_embedding(self.db, 'only-one', [1, 0, 0, 0])
        self._call('only-one')
        edges = self.db.execute('SELECT COUNT(*) FROM kg_edges').fetchone()[0]
        self.assertEqual(edges, 0)


# ---------------------------------------------------------------------------
# Integration: both features together
# ---------------------------------------------------------------------------

class TestBacklinksIntegration(unittest.TestCase):
    """Integration tests: wiki-links + semantic backlinks together."""

    def setUp(self):
        self.conn = _make_test_db()
        self.db = self.conn

    def tearDown(self):
        self.conn.close()

    def test_both_features_coexist(self):
        """Wiki-links and semantic edges don't interfere."""
        import numpy as np
        from save_pipeline import _index_backlinks, _auto_semantic_backlinks

        # Create 3 memories
        for i in range(3):
            _insert_memory(self.db, f'mem-{i}')

        # Semantic: mem-0 and mem-1 are similar (identical)
        vec = np.ones(8, dtype=np.float32) / np.sqrt(8)
        _insert_embedding(self.db, 'mem-0', vec)
        _insert_embedding(self.db, 'mem-1', vec)
        # mem-2 is truly orthogonal (negative dot product with query)
        diff_vec = np.array([-1, 0, 0, 0, 0, 0, 0, 0], dtype=np.float32)
        _insert_embedding(self.db, 'mem-2', diff_vec)

        # Wiki-link: mem-0 references mem-2
        _index_backlinks(self.db, 'mem-0', 'See [[mem-2]] for details')
        # Semantic: mem-0 auto-links to mem-1
        _auto_semantic_backlinks(self.db, 'mem-0', '')

        # Backlinks: mem-0<->mem-2
        bl = self.db.execute('SELECT * FROM backlinks ORDER BY source_id').fetchall()
        bl_pairs = {(r[0], r[1]) for r in bl}
        self.assertIn(('mem-0', 'mem-2'), bl_pairs)
        self.assertIn(('mem-2', 'mem-0'), bl_pairs)

        # KG edges: mem-0<->mem-1 (semantic)
        edges = self.db.execute(
            "SELECT e1.name, e2.name FROM kg_edges edge "
            "JOIN kg_entities e1 ON edge.source_id = e1.id "
            "JOIN kg_entities e2 ON edge.target_id = e2.id "
            "WHERE edge.relation = 'semantically_related'"
        ).fetchall()
        edge_pairs = {(r[0], r[1]) for r in edges}
        self.assertIn(('mem-0', 'mem-1'), edge_pairs)
        self.assertIn(('mem-1', 'mem-0'), edge_pairs)
        # mem-0 should NOT have semantic edge to mem-2 (too different)
        self.assertNotIn(('mem-0', 'mem-2'), edge_pairs)


# ---------------------------------------------------------------------------
# _auto_fts_backlinks tests
# ---------------------------------------------------------------------------

def _make_fts_test_db() -> sqlite3.Connection:
    """Create a temp DB with TEXT id column, matching production schema."""
    conn = sqlite3.connect(':memory:')
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute("""
        CREATE TABLE memories (
            id TEXT PRIMARY KEY,
            note_id TEXT UNIQUE NOT NULL,
            content TEXT,
            tags TEXT DEFAULT '[]',
            category TEXT DEFAULT '',
            source_file TEXT DEFAULT '',
            pinned INTEGER DEFAULT 0,
            is_global INTEGER DEFAULT 0,
            metadata TEXT DEFAULT '{}',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            accessed_at TEXT DEFAULT (datetime('now')),
            access_count INTEGER DEFAULT 0,
            success_score REAL DEFAULT 1.0,
            adaptive_halflife_days REAL DEFAULT 30.0,
            tier TEXT DEFAULT 'warm',
            psi REAL DEFAULT 0.0,
            deleted_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE backlinks (
            source_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            PRIMARY KEY (source_id, target_id)
        )
    """)
    return conn


class TestAutoFtsBacklinks(unittest.TestCase):
    """FTS5-based bidirectional relationship edge creation tests."""

    def setUp(self):
        self.conn = _make_fts_test_db()
        self.db = self.conn
        # Setup FTS virtual table
        self.db.execute("""
            CREATE VIRTUAL TABLE memories_fts USING fts5(
                content,
                tags,
                tokenize='porter unicode61'
            );
        """)

    def tearDown(self):
        self.conn.close()

    def _call(self, note_id, content, max_links=3):
        from save_pipeline import _auto_fts_backlinks
        _auto_fts_backlinks(self.db, note_id, content, max_links=max_links)

    @unittest.mock.patch('save_pipeline.atomic_write')
    def test_fts_backlink_db_only(self, mock_write):
        """Verify _auto_fts_backlinks inserts backlinks in DB, but does not modify files or content columns."""
        # Need unittest.mock imported or patched dynamically
        # 1. Insert two memories that have overlapping words to trigger FTS match
        self.db.execute(
            "INSERT INTO memories (id, note_id, content, source_file) VALUES (?, ?, ?, ?)",
            ("lessons/neighbor-note", "lessons/neighbor-note", "This is a note about cryptography and encryption protocols.", "lessons/neighbor-note.md")
        )
        self.db.execute(
            "INSERT INTO memories_fts (rowid, content, tags) VALUES (1, ?, ?)",
            ("This is a note about cryptography and encryption protocols.", "[]")
        )

        self.db.execute(
            "INSERT INTO memories (id, note_id, content, source_file) VALUES (?, ?, ?, ?)",
            ("lessons/query-note", "lessons/query-note", "A new note discussing cryptography research.", "lessons/query-note.md")
        )
        self.db.execute(
            "INSERT INTO memories_fts (rowid, content, tags) VALUES (2, ?, ?)",
            ("A new note discussing cryptography research.", "[]")
        )

        # Call _auto_fts_backlinks for query-note
        self._call("lessons/query-note", "A new note discussing cryptography research.")

        # 2. Assertions
        # Check that backlinks table has bidirectional rows
        rows = self.db.execute('SELECT * FROM backlinks ORDER BY source_id').fetchall()
        self.assertEqual(len(rows), 2, f"Expected 2 backlinks, got {len(rows)}: {rows}")
        pairs = {(r[0], r[1]) for r in rows}
        self.assertIn(('lessons/query-note', 'lessons/neighbor-note'), pairs)
        self.assertIn(('lessons/neighbor-note', 'lessons/query-note'), pairs)

        # Check that content column of both notes remains completely unmodified
        c1 = self.db.execute("SELECT content FROM memories WHERE note_id = ?", ("lessons/neighbor-note",)).fetchone()[0]
        c2 = self.db.execute("SELECT content FROM memories WHERE note_id = ?", ("lessons/query-note",)).fetchone()[0]
        self.assertEqual(c1, "This is a note about cryptography and encryption protocols.")
        self.assertEqual(c2, "A new note discussing cryptography research.")

        # Check that atomic_write was NEVER called to write to disk
        mock_write.assert_not_called()


if __name__ == '__main__':
    # Ensure unittest.mock is available
    import unittest.mock
    unittest.main()

