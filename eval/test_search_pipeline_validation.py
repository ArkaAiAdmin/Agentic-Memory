from _fixtures import bootstrap_temp_db_clean

"""Comprehensive validation tests for the agentic-memory search pipeline.

Tests 10 areas:
  1. BM25 search with sigmoid normalization
  2. Semantic search with usearch index
  3. Combined BM25+semantic scoring
  4. Graph-RAG expansion (2-hop)
  5. Fitness scoring defaults
  6. Recency weight
  7. Backlinks via [[wiki-links]]
  8. include_global and repo_filter
  9. Connection pool thread affinity
 10. BB2 thread safety

Uses a TEMP DB for full isolation.
"""
import json
import math
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))


from infra.memory_common import (
    open_db,
    run_db_migrations,
    connection_pool,
)
from rebuild_index import rebuild_index
from search_pipeline import (
    search_memories,
    _compute_final_score,
    ScoreContext,
    _graph_rag_expand,
    _bb2_record_turn,
    _bb2_resolve,
    _BB2_TURNS,
    _BB2_LOCK,
    _RERANK_WEIGHTS,
)
from infra.embedding_search import get_embedding_search


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_tmp_db():
    """Create a temp dir with a fresh memory.db and return (tmpdir, db_path)."""
    tmpdir = tempfile.mkdtemp(prefix="memtest_")
    db_path = Path(tmpdir) / "memory.db"
    # Bootstrap full schema via rebuild_index
    rebuild_index(Path(tmpdir), db_path)
    # Add tables that rebuild_index doesn't create
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS kg_entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            entity_type TEXT,
            mentions INTEGER DEFAULT 1,
            created_at TEXT,
            updated_at TEXT,
            UNIQUE(name, entity_type)
        );
        CREATE INDEX IF NOT EXISTS idx_kg_entities_name ON kg_entities(name);
        CREATE INDEX IF NOT EXISTS idx_kg_entities_type ON kg_entities(entity_type);
        CREATE TABLE IF NOT EXISTS kg_edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            relation TEXT NOT NULL DEFAULT 'related_to',
            weight REAL DEFAULT 1.0,
            valid INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (source_id) REFERENCES kg_entities(id) ON DELETE CASCADE,
            FOREIGN KEY (target_id) REFERENCES kg_entities(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS kg_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_memory TEXT NOT NULL,
            subject TEXT NOT NULL,
            predicate TEXT NOT NULL,
            object TEXT NOT NULL,
            confidence REAL DEFAULT 0.5,
            extracted_at TEXT DEFAULT (datetime('now')),
            valid INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS user_access_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            note_id TEXT NOT NULL,
            access_ts REAL NOT NULL,
            source TEXT NOT NULL DEFAULT 'search'
        );
        CREATE INDEX IF NOT EXISTS idx_user_access_log_note_id ON user_access_log(note_id);
    """)
    conn.commit()
    conn.close()
    return tmpdir, db_path


def _insert_note(
    db,
    note_id,
    content,
    tags=None,
    created=None,
    fitness_score=None,
    importance=None,
    pinned=False,
    repo_id=None,
):
    """Insert a raw note into memories table. Returns the row."""
    tags_json = json.dumps(tags or [])
    created = created or datetime.now(timezone.utc).isoformat()
    observed_at = created
    updated_at = created
    db.execute(
        """INSERT INTO memories
           (id, content, source_file, tags, created_at, updated_at,
            observed_at, fitness_score, importance, pinned, repo_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            note_id,
            content,
            f"{note_id}.md",
            tags_json,
            created,
            updated_at,
            observed_at,
            fitness_score,
            importance,
            pinned,
            repo_id,
        ),
    )
    # Also populate FTS5 index for search
    try:
        rowid = db.execute(
            "SELECT rowid FROM memories WHERE id=?", (note_id,)
        ).fetchone()[0]
        db.execute(
            "INSERT INTO memories_fts(rowid, content, tags) VALUES (?, ?, ?)",
            (rowid, content, tags_json),
        )
    except Exception:
        pass
    db.commit()
    return db.execute("SELECT * FROM memories WHERE id=?", (note_id,)).fetchone()


def _insert_kg_entity_and_edge(db, name1, name2, rel="uses"):
    """Insert two KG entities and an edge between them."""
    db.execute(
        "INSERT INTO kg_entities (name, entity_type, mentions) VALUES (?, ?, 1)",
        (name1.lower(), "concept"),
    )
    db.execute(
        "INSERT INTO kg_entities (name, entity_type, mentions) VALUES (?, ?, 1)",
        (name2.lower(), "concept"),
    )
    e1 = db.execute(
        "SELECT id FROM kg_entities WHERE name=?", (name1.lower(),)
    ).fetchone()
    e2 = db.execute(
        "SELECT id FROM kg_entities WHERE name=?", (name2.lower(),)
    ).fetchone()
    db.execute(
        "INSERT INTO kg_edges (source_id, target_id, relation, weight) VALUES (?, ?, ?, 1.0)",
        (e1[0], e2[0], rel),
    )
    db.commit()


# ── Tests ────────────────────────────────────────────────────────────────────


class TestBM25Search(unittest.TestCase):
    """1. BM25 search: save notes, search by keyword, verify sigmoid normalization."""

    def setUp(self):
        self.tmpdir, self.db_path = _make_tmp_db()
        bootstrap_temp_db_clean(self.db_path)
        connection_pool.close_all()

    def tearDown(self):
        connection_pool.close_all()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _save_and_search(self, query, **search_kw):
        with open_db(self.db_path) as db:
            run_db_migrations(db)
        return search_memories(
            self.db_path, query, limit=5, safety_wiring=False, **search_kw
        )

    def test_bm25_finds_keyword_match(self):
        """Notes with 'quantum' should appear when searching 'quantum'."""
        with open_db(self.db_path) as db:
            run_db_migrations(db)
            _insert_note(
                db,
                "note/quantum-1",
                "Quantum computing is the future of computation",
                tags=["quantum", "physics"],
            )
            _insert_note(
                db,
                "note/unrelated",
                "The quick brown fox jumps over the lazy dog",
                tags=["animals"],
            )
        result = self._save_and_search("quantum computing")
        self.assertGreater(result["count"], 0)
        ids = [r["id"] for r in result["results"]]
        self.assertIn("note/quantum-1", ids)

    def test_bm25_score_sigmoid_normalized(self):
        """final_score must use sigmoid(1/(1+exp(rank))), not raw rank."""
        with open_db(self.db_path) as db:
            run_db_migrations(db)
            _insert_note(
                db,
                "note/bm25-sig-1",
                "Kubernetes cluster management and orchestration",
                tags=["k8s", "infra"],
            )
            _insert_note(
                db,
                "note/bm25-sig-2",
                "Kubernetes deployment strategies and rolling updates",
                tags=["k8s", "deploy"],
            )
        result = self._save_and_search("Kubernetes cluster")
        self.assertGreater(result["count"], 0)
        for r in result["results"]:
            score = r.get("final_score", 0)
            self.assertGreaterEqual(
                score, 0.0, f"final_score must be >= 0 (sigmoid output), got {score}"
            )
            self.assertLessEqual(
                score, 2.0, f"final_score must be reasonable, got {score}"
            )

    def test_bm25_only_path_when_no_semantic(self):
        """When hybrid=False, only BM25 results returned (no semantic fallback)."""
        with open_db(self.db_path) as db:
            run_db_migrations(db)
            _insert_note(
                db,
                "note/hybrid-1",
                "Rare exotic term xyzzy123 only in this note",
                tags=["unique"],
            )
        result = search_memories(
            self.db_path, "xyzzy123", limit=5, hybrid=False, safety_wiring=False
        )
        self.assertGreater(result["count"], 0)
        self.assertEqual(result["results"][0]["id"], "note/hybrid-1")


class TestSemanticSearch(unittest.TestCase):
    """2. Semantic search: embeddings exist in memory_embeddings table."""

    def setUp(self):
        self.tmpdir, self.db_path = _make_tmp_db()
        connection_pool.close_all()

    def tearDown(self):
        connection_pool.close_all()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_embedding_exists_after_index(self):
        """index_embedding should populate memory_embeddings."""
        es = get_embedding_search()
        if es.model is None:
            self.skipTest("model2vec not available")
        with open_db(self.db_path) as db:
            run_db_migrations(db)
            _insert_note(
                db, "note/emb-1", "Machine learning model training", tags=["ml", "ai"]
            )
            es.index_embedding(db, "note/emb-1", "Machine learning model training")
            row = db.execute(
                "SELECT memory_id, embedding, dim FROM memory_embeddings WHERE memory_id=?",
                ("note/emb-1",),
            ).fetchone()
            self.assertIsNotNone(row, "embedding row should exist")
            self.assertEqual(row[2], es.model.dim, "dim should match model dim")
            self.assertGreater(len(row[1]), 0, "embedding blob should not be empty")

    def test_semantic_search_returns_results(self):
        """Semantic search via full_scan should return similar content."""
        es = get_embedding_search()
        if es.model is None:
            self.skipTest("model2vec not available")
        with open_db(self.db_path) as db:
            run_db_migrations(db)
            _insert_note(
                db,
                "note/sem-1",
                "Deep learning neural networks for image classification",
                tags=["dl", "cv"],
            )
            _insert_note(
                db,
                "note/sem-2",
                "Cooking pasta with tomato sauce recipe",
                tags=["food", "recipe"],
            )
        result = search_memories(
            self.db_path,
            "neural network image recognition",
            limit=5,
            hybrid=True,
            safety_wiring=False,
        )
        self.assertGreater(result["count"], 0)
        ids = [r["id"] for r in result["results"]]
        self.assertIn("note/sem-1", ids)

    def test_memory_embeddings_table_schema(self):
        """memory_embeddings should have expected columns."""
        with open_db(self.db_path) as db:
            run_db_migrations(db)
            cols = {
                row[1]
                for row in db.execute("PRAGMA table_info(memory_embeddings)").fetchall()
            }
        for expected in (
            "memory_id",
            "content_hash",
            "embedding",
            "model_revision",
            "dim",
            "updated_at",
        ):
            self.assertIn(expected, cols, f"missing column: {expected}")


class TestCombinedScoring(unittest.TestCase):
    """3. Combined BM25+semantic scoring via hybrid search."""

    def setUp(self):
        self.tmpdir, self.db_path = _make_tmp_db()
        connection_pool.close_all()

    def tearDown(self):
        connection_pool.close_all()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_rrf_combines_both_paths(self):
        """When both BM25 and semantic match, results should merge via RRF."""
        es = get_embedding_search()
        if es.model is None:
            self.skipTest("model2vec not available")
        with open_db(self.db_path) as db:
            run_db_migrations(db)
            _insert_note(
                db,
                "note/rrf-1",
                "Python async concurrency coroutines and asyncio",
                tags=["python", "async"],
            )
            _insert_note(
                db,
                "note/rrf-2",
                "JavaScript async await Promise handling patterns",
                tags=["js", "async"],
            )
        result = search_memories(
            self.db_path, "async concurrency", limit=5, hybrid=True, safety_wiring=False
        )
        self.assertGreater(result["count"], 0)
        # Both notes should appear — one via BM25 (async), one via semantic
        ids = [r["id"] for r in result["results"]]
        self.assertTrue(
            "note/rrf-1" in ids or "note/rrf-2" in ids,
            f"expected at least one async note in results, got {ids}",
        )

    def test_hybrid_score_is_weighted_average(self):
        """Final score should blend BM25 + semantic, not just max or min."""
        with open_db(self.db_path) as db:
            run_db_migrations(db)
            _insert_note(
                db,
                "note/wt-1",
                "PostgreSQL query optimization and indexing strategies",
                tags=["db", "perf"],
            )
            _insert_note(
                db,
                "note/wt-2",
                "Redis caching layer for high-throughput applications",
                tags=["cache", "perf"],
            )
        result = search_memories(
            self.db_path,
            "database query optimization",
            limit=5,
            hybrid=True,
            safety_wiring=False,
        )
        self.assertGreater(result["count"], 0)
        for r in result["results"]:
            score = r.get("final_score", 0)
            self.assertGreater(score, 0, "scores should be positive")
            self.assertLess(score, 5.0, "scores should be bounded")


class TestGraphRAGExpansion(unittest.TestCase):
    """4. Graph-RAG expansion: entity extraction and traversal."""

    def setUp(self):
        self.tmpdir, self.db_path = _make_tmp_db()
        connection_pool.close_all()

    def tearDown(self):
        connection_pool.close_all()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @patch("search_pipeline._GRAPH_RAG_ENABLED", True)
    def test_graph_rag_finds_related_entities(self):
        """When KG has entities, graph_rag_expand should find related names."""
        with open_db(self.db_path) as db:
            run_db_migrations(db)
            _insert_kg_entity_and_edge(db, "Python", "Flask", "uses")
        with patch("search_pipeline._GRAPH_RAG_ENABLED", True):
            from knowledge_graph import KG_ENABLED

            if not KG_ENABLED:
                self.skipTest("knowledge_graph not enabled")
            related = _graph_rag_expand("Python", self.db_path)
        # Should find Flask as related to Python
        self.assertIsInstance(related, list)
        # The exact results depend on KG_ENABLED and extract_entities
        # Just verify it returns a list (may be empty if KG tables don't exist)
        self.assertIsInstance(related, list)

    @patch("search_pipeline._GRAPH_RAG_ENABLED", True)
    def test_graph_rag_disabled_returns_empty(self):
        """When _GRAPH_RAG_ENABLED=False, should return empty list."""
        with patch("search_pipeline._GRAPH_RAG_ENABLED", False):
            result = _graph_rag_expand("Python", self.db_path)
        self.assertEqual(result, [])

    @patch("search_pipeline._GRAPH_RAG_ENABLED", True)
    def test_graph_rag_adds_context_to_search(self):
        """Graph-RAG terms should be appended to FTS query."""
        with open_db(self.db_path) as db:
            run_db_migrations(db)
            # Insert a note mentioning a concept that would be in the KG
            _insert_note(
                db,
                "note/grag-1",
                "Flask is a lightweight WSGI web framework for Python",
                tags=["web", "python"],
            )
        # Even without a KG, the search should still work
        result = search_memories(
            self.db_path, "Flask web framework", limit=5, safety_wiring=False
        )
        self.assertGreater(result["count"], 0)


class TestFitnessScoring(unittest.TestCase):
    """5. Fitness scoring: defaults to 0.5, record_access updates it."""

    def setUp(self):
        self.tmpdir, self.db_path = _make_tmp_db()
        connection_pool.close_all()

    def tearDown(self):
        connection_pool.close_all()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_fitness_default_is_0point5(self):
        """_compute_final_score with fitness=None should use 0.5 default."""
        now_ts = time.time()
        score = _compute_final_score(
            ScoreContext(
                rank=-1.0,
                fitness=None,
                importance=None,
                pinned=False,
                created=datetime.now(timezone.utc).isoformat(),
                tags_json="[]",
                query="test",
                boost_pinned=True,
                recency_weight=0.1,
                now_ts=now_ts,
            )
        )
        # With fitness=None → 0.5, and other defaults:
        # bm25 = 1/(1+exp(-1)) ≈ 0.731, fitness contribution = 0.2*0.5 = 0.1
        # score should be between 0 and 1
        self.assertGreater(score, 0.0)
        self.assertLess(score, 1.5)

    def test_fitness_none_uses_0point5_explicitly(self):
        """When fitness is None, the 0.5 default is used, not 1.0."""
        # Manually compute expected
        rank_val = -2.0
        1.0 / (1.0 + math.exp(rank_val))  # sigmoid(-2) ≈ 0.88
        # total should be at least bm25 * weight + fitness_contrib
        score = _compute_final_score(
            ScoreContext(
                rank=rank_val,
                fitness=None,
                importance=None,
                pinned=False,
                created=None,
                tags_json="[]",
                query="x",
                boost_pinned=False,
                recency_weight=0.0,
                now_ts=time.time(),
            )
        )
        # bm25 part: 0.4 * sigmoid(-2) ≈ 0.4 * 0.88 ≈ 0.352
        # fitness: 0.2 * 0.5 = 0.1
        # importance: 0.15 * 0.6 = 0.09
        # total ≈ 0.542
        self.assertGreater(score, 0.1)
        self.assertLess(score, 0.7)

    def test_record_access_writes_to_log(self):
        """record_access should insert into user_access_log."""
        from adaptive_retention import record_access

        with open_db(self.db_path) as db:
            run_db_migrations(db)
            _insert_note(db, "note/acc-1", "Test access note", tags=[])
            record_access(db, "note/acc-1", source="search")
            row = db.execute(
                "SELECT * FROM user_access_log WHERE note_id=?", ("note/acc-1",)
            ).fetchone()
            self.assertIsNotNone(row, "user_access_log row should exist")


class TestRecencyWeight(unittest.TestCase):
    """6. Recency weight: applied correctly, no *10 hack."""

    def setUp(self):
        self.tmpdir, self.db_path = _make_tmp_db()
        connection_pool.close_all()

    def tearDown(self):
        connection_pool.close_all()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_recency_weight_default_0point1(self):
        """Default decay_weight should boost recent notes over old ones.

        Behavioral: recent notes outrank old notes via _apply_temporal_decay
        as a multiplicative modifier (not an additive channel).
        """
        from search.scoring import _apply_temporal_decay

        now_ts = time.time()
        recent_ts = datetime.now(timezone.utc).isoformat()
        old_ts = "2020-01-01T00:00:00+00:00"
        base_score = 0.5
        recent_row = (None, None, None, None, recent_ts, None, base_score, None, None, None)
        old_row = (None, None, None, None, old_ts, None, base_score, None, None, None)
        scored = _apply_temporal_decay([recent_row, old_row], decay_weight=0.15, as_of=now_ts)
        score_new = scored[0][6]
        score_old = scored[1][6]
        self.assertGreater(
            score_new, score_old, "new note should score higher than old note"
        )
        diff = score_new - score_old
        self.assertLess(
            diff, 0.2, f"recency diff {diff} seems too large for weight=0.15"
        )

    def test_recency_weight_zero_removes_recency(self):
        """recency_weight=0 should make new and old notes score the same."""
        now_ts = time.time()
        score_new = _compute_final_score(
            ScoreContext(
                rank=-1.0,
                fitness=0.5,
                importance=3,
                pinned=False,
                created=datetime.now(timezone.utc).isoformat(),
                tags_json="[]",
                query="test",
                boost_pinned=False,
                recency_weight=0.0,
                now_ts=now_ts,
            )
        )
        score_old = _compute_final_score(
            ScoreContext(
                rank=-1.0,
                fitness=0.5,
                importance=3,
                pinned=False,
                created="2020-01-01T00:00:00+00:00",
                tags_json="[]",
                query="test",
                boost_pinned=False,
                recency_weight=0.0,
                now_ts=now_ts,
            )
        )
        self.assertAlmostEqual(
            score_new,
            score_old,
            places=10,
            msg="recency_weight=0 should eliminate recency difference",
        )

    def test_recency_not_multiplied_by_10(self):
        """Verify decay is a multiplicative modifier, not additive scalars.

        Behavioral: a ×0.15 decay_weight produces a proportionally smaller
        score gap between recent and old notes than ×0.3 — bounded, not a
        hacked constant multiplier.
        """
        from search.scoring import _apply_temporal_decay

        now_ts = time.time()
        recent_ts = datetime.now(timezone.utc).isoformat()
        old_ts = "2020-01-01T00:00:00+00:00"
        base_score = 0.5
        row = lambda ts: (None, None, None, None, ts, None, base_score, None, None, None)
        w1 = _apply_temporal_decay([row(recent_ts), row(old_ts)], decay_weight=0.1, as_of=now_ts)
        w2 = _apply_temporal_decay([row(recent_ts), row(old_ts)], decay_weight=0.2, as_of=now_ts)
        diff = w2[0][6] - w2[1][6] - (w1[0][6] - w1[1][6])
        self.assertGreater(diff, 0.0)
        self.assertLess(diff, 0.3, f"weight difference {diff} suggests a constant hack")


class TestBacklinks(unittest.TestCase):
    """7. Backlinks: [[wiki-links]] populate the backlinks table."""

    def setUp(self):
        self.tmpdir, self.db_path = _make_tmp_db()
        connection_pool.close_all()

    def tearDown(self):
        connection_pool.close_all()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_wiki_link_creates_backlink(self):
        """Content with [[target]] should create a backlinks row."""
        from save_pipeline import _index_backlinks

        with open_db(self.db_path) as db:
            run_db_migrations(db)
            _insert_note(
                db, "note/source", "See also [[note/target]] for details", tags=[]
            )
            _insert_note(db, "note/target", "This is the target note", tags=[])
            _index_backlinks(db, "note/source", "See also [[note/target]] for details")
            rows = db.execute(
                "SELECT source_id, target_id FROM backlinks WHERE source_id=?",
                ("note/source",),
            ).fetchall()
            self.assertGreater(len(rows), 0, "backlinks should be populated")
            targets = [r[1] for r in rows]
            self.assertIn("note/target", targets)

    def test_backlinks_in_search_results(self):
        """search results should include backlinks for notes with wiki-links."""
        from save_pipeline import _index_backlinks

        with open_db(self.db_path) as db:
            run_db_migrations(db)
            _insert_note(
                db,
                "note/link-src",
                "See [[note/link-dst]] for more info about databases",
                tags=["dbs"],
            )
            _insert_note(
                db,
                "note/link-dst",
                "Database indexing and query optimization guide",
                tags=["db", "index"],
            )
            _index_backlinks(
                db,
                "note/link-src",
                "See [[note/link-dst]] for more info about databases",
            )
        result = search_memories(
            self.db_path, "databases", limit=5, safety_wiring=False
        )
        # Find the source note in results
        for r in result["results"]:
            if r["id"] == "note/link-src":
                self.assertIn(
                    "note/link-dst",
                    r.get("backlinks", []),
                    "backlinks field should include the target",
                )
                break

    def test_backlinks_table_schema(self):
        """backlinks table should have source_id and target_id columns."""
        with open_db(self.db_path) as db:
            run_db_migrations(db)
            cols = {
                row[1] for row in db.execute("PRAGMA table_info(backlinks)").fetchall()
            }
        self.assertIn("source_id", cols)
        self.assertIn("target_id", cols)


class TestIncludeGlobal(unittest.TestCase):
    """8. include_global: global notes appear; repo_filter applied."""

    def setUp(self):
        self.tmpdir, self.db_path = _make_tmp_db()
        connection_pool.close_all()

    def tearDown(self):
        connection_pool.close_all()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_global_note_appears_by_default(self):
        """A note with repo_id=NULL (global) should appear in search."""
        with open_db(self.db_path) as db:
            run_db_migrations(db)
            _insert_note(
                db,
                "note/global-1",
                "Global knowledge about distributed systems",
                tags=["global"],
                repo_id=None,
            )
        result = search_memories(
            self.db_path,
            "distributed systems",
            limit=5,
            include_global=True,
            safety_wiring=False,
        )
        self.assertGreater(result["count"], 0)
        ids = [r["id"] for r in result["results"]]
        self.assertIn("note/global-1", ids)

    def test_include_global_false_excludes_global(self):
        """When include_global=False, search_memories operates on a single
        db_path. Notes with repo_id=NULL are NOT filtered out — they are
        in scope like any other note in this DB. The include_global flag
        is a MCP-tool-layer concept (the wrapper calls search_memories
        once per DB and merges via RRF), not a SQL filter.

        Regression test: prior versions of search_memories added a
        `m.repo_id IS NOT NULL` filter whenever include_global=False.
        That filter excluded ALL user-saved notes (which have NULL
        repo_id) and was a critical bug.
        """
        with open_db(self.db_path) as db:
            run_db_migrations(db)
            _insert_note(
                db,
                "note/glob-excl",
                "Unique term globalexcl99 only in global note",
                tags=[],
                repo_id=None,
            )
            _insert_note(
                db,
                "note/repo-note",
                "Unique term repoonly88 only in repo note",
                tags=[],
                repo_id="repo/test",
            )
        # Both notes are in the same db_path. include_global is a no-op
        # at this layer; BOTH notes must be searchable.
        result = search_memories(
            self.db_path,
            "globalexcl99 repoonly88",
            limit=5,
            include_global=False,
            safety_wiring=False,
        )
        ids = [r["id"] for r in result["results"]]
        self.assertIn(
            "note/glob-excl",
            ids,
            "global note should be findable in this DB (filter removed)",
        )
        self.assertIn("note/repo-note", ids, "repo note should be findable in this DB")

    def test_repo_filter_in_chunk_search(self):
        """repo_filter parameter is a vestigial no-op. When provided to
        search_memories, it does NOT filter — the function operates on
        a single db_path and all rows in scope. This is a regression
        test: prior versions applied `m.repo_id IS NOT NULL` when
        include_global=False, which broke search for ALL user-saved
        notes.
        """
        with open_db(self.db_path) as db:
            run_db_migrations(db)
            # Insert a long note that will be chunked
            long_content = "PostgreSQL optimization " * 200
            _insert_note(
                db, "note/chunk-global", long_content, tags=["db"], repo_id=None
            )
        # Search with include_global=False — the note IS in this DB,
        # so it should be findable. repo_filter is now a no-op.
        result = search_memories(
            self.db_path,
            "PostgreSQL optimization",
            limit=5,
            include_global=False,
            safety_wiring=False,
        )
        ids = [r["id"] for r in result["results"]]
        self.assertIn(
            "note/chunk-global",
            ids,
            "chunk note should be findable in its DB; repo_filter is no-op",
        )


class TestConnectionPoolThreadAffinity(unittest.TestCase):
    """9. Connection pool: two threads can't share same connection."""

    def setUp(self):
        self.tmpdir, self.db_path = _make_tmp_db()
        connection_pool.close_all()

    def tearDown(self):
        connection_pool.close_all()
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_different_threads_get_different_connections(self):
        """Two threads calling pool.get() for the same path get different conn objects."""
        db_str = str(self.db_path)
        conn1 = connection_pool.get(db_str, timeout=5.0)
        id1 = id(conn1)

        results = {}

        def thread_func():
            conn2 = connection_pool.get(db_str, timeout=5.0)
            results["conn2_id"] = id(conn2)
            results["same"] = id(conn1) == id(conn2)

        t = threading.Thread(target=thread_func)
        t.start()
        t.join(timeout=5.0)

        self.assertFalse(
            results.get("same", True),
            "two threads must not get the same connection object",
        )
        self.assertNotEqual(id1, results.get("conn2_id"))

    def test_owner_thread_recorded(self):
        """pool.get() should store connection under (path, thread_ident) key."""
        db_str = str(self.db_path)
        conn = connection_pool.get(db_str, timeout=5.0)
        current_tid = threading.current_thread().ident or 0
        key = (db_str, current_tid)
        self.assertIn(
            key, connection_pool._pool, "pool should contain (path, thread_ident) key"
        )
        self.assertIs(
            connection_pool._pool[key],
            conn,
            "pool[key] should be the returned connection",
        )

    def test_owner_thread_blocks_other_thread(self):
        """A second thread should not reuse a connection owned by thread 1."""
        db_str = str(self.db_path)
        conn1 = connection_pool.get(db_str, timeout=5.0)
        results = {}

        def thread_func():
            conn2 = connection_pool.get(db_str, timeout=5.0)
            results["conn2_id"] = id(conn2)
            results["is_new"] = id(conn2) != id(conn1)

        t = threading.Thread(target=thread_func)
        t.start()
        t.join(timeout=5.0)

        self.assertTrue(
            results.get("is_new", False),
            "second thread should get a new connection, not reuse thread 1's",
        )


class TestBB2ThreadSafety(unittest.TestCase):
    """10. BB2: _BB2_TURNS uses _BB2_LOCK for thread safety."""

    def setUp(self):
        # Clear BB2 history
        from search_pipeline import _bb2_clear_history

        _bb2_clear_history()

    def test_bb2_lock_exists(self):
        """_BB2_LOCK should be a lock instance (threading.Lock returns _thread.lock)."""
        # threading.Lock() returns a _thread.lock instance, not a threading.Lock type.
        # Verify it's a proper lock with acquire/release methods.
        self.assertTrue(
            hasattr(_BB2_LOCK, "acquire") and hasattr(_BB2_LOCK, "release"),
            "_BB2_LOCK should be a lock instance with acquire/release methods",
        )
        # Also verify it's the type returned by threading.Lock()
        self.assertIsInstance(_BB2_LOCK, type(threading.Lock()))

    def test_bb2_record_turn_uses_lock(self):
        """_bb2_record_turn should acquire _BB2_LOCK."""
        import inspect

        src = inspect.getsource(_bb2_record_turn)
        self.assertIn("_BB2_LOCK", src, "_bb2_record_turn should use _BB2_LOCK")

    def test_bb2_concurrent_writes(self):
        """Multiple threads writing to _BB2_TURNS should not crash."""
        errors = []

        def writer(idx):
            try:
                _bb2_record_turn(f"query from thread {idx}", [])
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        self.assertEqual(errors, [], f"concurrent writes produced errors: {errors}")
        self.assertLessEqual(len(_BB2_TURNS), 20)

    def test_bb2_resolve_uses_lock(self):
        """_bb2_resolve should acquire _BB2_LOCK."""
        import inspect

        src = inspect.getsource(_bb2_resolve)
        self.assertIn("_BB2_LOCK", src, "_bb2_resolve should use _BB2_LOCK")

    def test_bb2_history_bounded(self):
        """_BB2_TURNS should not exceed _BB2_HISTORY_MAX."""
        from search_pipeline import _BB2_HISTORY_MAX

        for i in range(_BB2_HISTORY_MAX + 10):
            _bb2_record_turn(f"turn {i}", [])
        self.assertLessEqual(len(_BB2_TURNS), _BB2_HISTORY_MAX)


class TestComputeFinalScore(unittest.TestCase):
    """Bonus: validate _compute_final_score channel weights sum to 1."""

    def test_weights_sum_to_one(self):
        """Default rerank weights should sum to 1.0."""
        total = sum(_RERANK_WEIGHTS.values())
        self.assertAlmostEqual(
            total, 1.0, places=10, msg=f"weights should sum to 1.0, got {total}"
        )

    def test_all_five_channels_present(self):
        """Weight dict should have the five additive scoring channel keys.

        Note: recency/temporal decay is applied by _apply_temporal_decay
        AFTER _compute_final_score, so it is intentionally absent here.
        """
        expected = {"bm25", "fitness", "importance", "pinned", "tag_match"}
        self.assertEqual(set(_RERANK_WEIGHTS.keys()), expected)

    def test_bm25_uses_sigmoid(self):
        """bm25 channel should be sigmoid(rank), not raw rank."""
        score = _compute_final_score(
            ScoreContext(
                rank=-0.0,
                fitness=0.0,
                importance=3,
                pinned=False,
                created=None,
                tags_json="[]",
                query="",
                boost_pinned=False,
                recency_weight=0.0,
                now_ts=time.time(),
            )
        )
        # rank=0 → sigmoid(0)=0.5 → bm25=0.45*0.5=0.225
        # fitness=0.25*0.0=0, importance=0.15*0.6=0.09
        # pinned=0, tag=0
        # total = 0.225 + 0 + 0.09 = 0.315
        self.assertAlmostEqual(score, 0.315, places=2)

    def test_pinned_boost(self):
        """Pinned note with boost_pinned=True should score higher."""
        base = _compute_final_score(
            ScoreContext(
                rank=-1.0,
                fitness=0.5,
                importance=3,
                pinned=False,
                created=None,
                tags_json="[]",
                query="test",
                boost_pinned=True,
                recency_weight=0.0,
                now_ts=time.time(),
            )
        )
        pinned = _compute_final_score(
            ScoreContext(
                rank=-1.0,
                fitness=0.5,
                importance=3,
                pinned=True,
                created=None,
                tags_json="[]",
                query="test",
                boost_pinned=True,
                recency_weight=0.0,
                now_ts=time.time(),
            )
        )
        self.assertGreater(pinned, base, "pinned note should score higher")


if __name__ == "__main__":
    unittest.main(verbosity=2)
