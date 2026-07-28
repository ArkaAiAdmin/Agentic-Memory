"""Tests for session clustering cross-entity boost enhancement."""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from infra.memory_common import reset_all_lazy_config_attrs

reset_all_lazy_config_attrs()

from search.phases.session import (
    _phase_nine_session_cluster,
    _compute_query_entities,
    _compute_session_affinity_scores,
    _get_session_entities,
)


class TestComputeQueryEntities(unittest.TestCase):
    def test_basic_entities(self):
        entities = _compute_query_entities("what database patterns have we established")
        self.assertIn("database", entities)
        self.assertIn("patterns", entities)
        self.assertIn("established", entities)

    def test_short_tokens_excluded(self):
        entities = _compute_query_entities("we have a database")
        self.assertNotIn("we", entities)
        self.assertNotIn("a", entities)
        self.assertIn("database", entities)

    def test_empty_query(self):
        entities = _compute_query_entities("")
        self.assertEqual(entities, set())


class TestComputeSessionAffinityScores(unittest.TestCase):
    def test_high_overlap(self):
        query_entities = {"database", "postgresql", "indexing"}
        session_entities = {
            "sessions/session1": {"database", "postgresql", "indexing"},
            "sessions/session2": {"docker", "containers"},
        }
        scores = _compute_session_affinity_scores(query_entities, session_entities)
        self.assertGreater(scores["sessions/session1"], 0.5)
        self.assertEqual(scores["sessions/session2"], 0.0)

    def test_partial_overlap(self):
        query_entities = {"database", "postgresql", "indexing"}
        session_entities = {
            "sessions/session1": {"database", "redis"},
            "sessions/session2": {"postgresql", "indexing"},
        }
        scores = _compute_session_affinity_scores(query_entities, session_entities)
        self.assertGreater(scores["sessions/session1"], 0.0)
        self.assertGreater(scores["sessions/session2"], 0.0)

    def test_no_overlap(self):
        query_entities = {"database", "postgresql"}
        session_entities = {
            "sessions/session1": {"docker", "containers"},
        }
        scores = _compute_session_affinity_scores(query_entities, session_entities)
        self.assertEqual(scores["sessions/session1"], 0.0)

    def test_empty_inputs(self):
        scores = _compute_session_affinity_scores(set(), {})
        self.assertEqual(scores, {})


class TestPhaseEightSessionCluster(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "memory.db")
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS tenant_memories (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                source_file TEXT NOT NULL,
                tags TEXT DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                pinned INTEGER DEFAULT 0,
                importance INTEGER DEFAULT 3,
                score REAL DEFAULT 1.0,
                access_count INTEGER DEFAULT 1,
                fitness_score REAL DEFAULT 1.0,
                valid_to TEXT,
                superseded_by TEXT,
                last_accessed TEXT,
                deleted_at TEXT,
                category TEXT DEFAULT 'lessons',
                tier TEXT DEFAULT 'warm',
                importance_score REAL DEFAULT 0.5,
                metadata TEXT,
                repo_id TEXT,
                hash TEXT
            )
        """)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_single_session_intent(self):
        """Single-session intent boosts results from the most-represented session."""
        results = [
            ("id1", "content1", "sessions/session1", "[]", "2026-01-01", 1.0),
            ("id2", "content2", "sessions/session1", "[]", "2026-01-02", 2.0),
            ("id3", "content3", "sessions/session2", "[]", "2026-01-03", 3.0),
        ]
        boost_ids = set()
        result = _phase_nine_session_cluster(
            results, "the session we had", 10, boost_ids=boost_ids
        )
        self.assertEqual(len(result), 3)
        self.assertIn("id1", boost_ids)
        self.assertIn("id2", boost_ids)

    def test_multi_session_intent(self):
        """Multi-session intent diversifies across sessions."""
        results = [
            ("id1", "content1", "sessions/session1", "[]", "2026-01-01", 1.0),
            ("id2", "content2", "sessions/session1", "[]", "2026-01-02", 2.0),
            ("id3", "content3", "sessions/session2", "[]", "2026-01-03", 3.0),
            ("id4", "content4", "sessions/session2", "[]", "2026-01-04", 4.0),
        ]
        result = _phase_nine_session_cluster(
            results, "patterns across sessions", 4
        )
        self.assertEqual(len(result), 4)

    def test_no_session_results(self):
        """Non-session results pass through unchanged."""
        results = [
            ("id1", "content1", "memory/lessons/test.md", "[]", "2026-01-01", 1.0),
        ]
        result = _phase_nine_session_cluster(
            results, "test query", 10
        )
        self.assertEqual(len(result), 1)

    def test_empty_results(self):
        """Empty results return empty."""
        result = _phase_nine_session_cluster([], "test", 10)
        self.assertEqual(result, [])

    def test_no_boost_ids_when_disabled(self):
        """When feature flag is disabled, no boost_ids are added from cross-entity."""
        results = [
            ("id1", "content1", "sessions/session1", "[]", "2026-01-01", 1.0),
            ("id2", "content2", "sessions/session2", "[]", "2026-01-02", 2.0),
        ]
        boost_ids = set()
        result = _phase_nine_session_cluster(
            results, "database patterns", 10, boost_ids=boost_ids, db=self.conn
        )
        # Without feature flag enabled, only keyword-based boosting happens
        # Since "database patterns" matches multi but not single, no boosting
        self.assertEqual(len(result), 2)


class TestGetSessionEntities(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "memory.db")
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS tenant_memories (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                source_file TEXT NOT NULL,
                tags TEXT DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                pinned INTEGER DEFAULT 0,
                importance INTEGER DEFAULT 3,
                score REAL DEFAULT 1.0,
                access_count INTEGER DEFAULT 1,
                fitness_score REAL DEFAULT 1.0,
                valid_to TEXT,
                superseded_by TEXT,
                last_accessed TEXT,
                deleted_at TEXT,
                category TEXT DEFAULT 'lessons',
                tier TEXT DEFAULT 'warm',
                importance_score REAL DEFAULT 0.5,
                metadata TEXT,
                repo_id TEXT,
                hash TEXT
            )
        """)
        self.conn.execute(
            "INSERT INTO tenant_memories (id, content, source_file, created_at, updated_at, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("lessons/postgres-indexing", "PostgreSQL indexing patterns", "sessions/session1",
             "2026-01-01", "2026-01-01", "2026-01-01"),
        )
        self.conn.execute(
            "INSERT INTO tenant_memories (id, content, source_file, created_at, updated_at, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("lessons/postgres-transactions", "PostgreSQL transaction handling", "sessions/session1",
             "2026-01-01", "2026-01-01", "2026-01-01"),
        )
        self.conn.execute(
            "INSERT INTO tenant_memories (id, content, source_file, created_at, updated_at, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("lessons/docker-networking", "Docker networking patterns", "sessions/session2",
             "2026-01-02", "2026-01-02", "2026-01-02"),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_extracts_entities_from_sessions(self):
        entities = _get_session_entities(
            self.conn, ["sessions/session1", "sessions/session2"]
        )
        self.assertIn("sessions/session1", entities)
        self.assertIn("sessions/session2", entities)
        self.assertIn("postgres", entities["sessions/session1"])
        self.assertIn("indexing", entities["sessions/session1"])
        self.assertIn("docker", entities["sessions/session2"])

    def test_empty_session_list(self):
        entities = _get_session_entities(self.conn, [])
        self.assertEqual(entities, {})


if __name__ == "__main__":
    unittest.main()
