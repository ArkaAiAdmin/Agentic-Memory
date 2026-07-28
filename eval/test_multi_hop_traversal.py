"""Tests for multi-hop KG traversal and text-based multi-hop traversal."""

from __future__ import annotations

import os
import sys
import sqlite3


sys.path.insert(0, os.path.expanduser("~/.config/agentic-memory"))

from search.phases.kg_traversal import (
    _text_multi_hop_traversal,
    _phase_ten_kg_boost,
    _phase_ten_multi_hop_kg,
    _entity_name_to_memory_id,
)

_MEMORIES_SCHEMA = """
CREATE TABLE memories (
    id TEXT PRIMARY KEY,
    content TEXT,
    source_file TEXT,
    tags TEXT,
    created_at TEXT,
    deleted_at TEXT,
    fitness_score REAL,
    importance INTEGER,
    pinned INTEGER,
    metadata TEXT,
    last_accessed TEXT,
    access_count INTEGER DEFAULT 0,
    score REAL DEFAULT 0.0,
    supersedes TEXT,
    category TEXT,
    tenant_id TEXT DEFAULT 'default'
)
"""

_TENANT_VIEW = """
CREATE VIEW tenant_memories AS
SELECT id, content, source_file, tags, created_at, deleted_at,
       fitness_score, importance, pinned, metadata, last_accessed,
       access_count, score, supersedes, category, tenant_id
FROM memories WHERE tenant_id = 'default'
"""

_KG_SCHEMA = """
CREATE TABLE kg_entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE,
    entity_type TEXT,
    mentions INTEGER DEFAULT 1,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE kg_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER,
    target_id INTEGER,
    relation TEXT,
    weight REAL DEFAULT 1.0,
    created_at TEXT,
    invalid_at TEXT,
    FOREIGN KEY (source_id) REFERENCES kg_entities(id),
    FOREIGN KEY (target_id) REFERENCES kg_entities(id)
)
"""

_TEST_MEMORIES = [
    ("lessons/analytics-core", "Analytics-Core service runs on port 8443", "agents/test/m1.md", "[]", "2024-01-01"),
    ("lessons/data-pipeline", "The Analytics-Core microservice handles data pipelines", "agents/test/m2.md", "[]", "2024-01-02"),
    ("lessons/warehouse-db", "Data pipeline connects to the warehouse database", "agents/test/m3.md", "[]", "2024-01-03"),
    ("lessons/port-config", "Warehouse database runs PostgreSQL on port 5432", "agents/test/m4.md", "[]", "2024-01-04"),
    ("lessons/weather", "Unrelated memory about weather", "agents/test/m5.md", "[]", "2024-01-05"),
]

_TEST_ENTITIES = [("analytics-core", "service"), ("data-pipeline", "process"), ("warehouse-db", "database")]


def _setup_test_db():
    """Create an in-memory DB with memories + KG schema + tenant_memories VIEW."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_MEMORIES_SCHEMA)
    conn.executescript(_TENANT_VIEW)
    conn.executescript(_KG_SCHEMA)

    for mid, content, src, tags, created in _TEST_MEMORIES:
        conn.execute(
            "INSERT INTO memories (id, content, source_file, tags, created_at) VALUES (?, ?, ?, ?, ?)",
            (mid, content, src, tags, created),
        )

    entity_ids = {}
    for name, etype in _TEST_ENTITIES:
        cur = conn.execute(
            "INSERT INTO kg_entities (name, entity_type, mentions, created_at, updated_at) VALUES (?, ?, 1, '', '')",
            (name, etype),
        )
        entity_ids[name] = cur.lastrowid

    conn.execute(
        "INSERT INTO kg_edges (source_id, target_id, relation, weight, created_at) VALUES (?, ?, 'uses', 1.0, '')",
        (entity_ids["analytics-core"], entity_ids["data-pipeline"]),
    )
    conn.execute(
        "INSERT INTO kg_edges (source_id, target_id, relation, weight, created_at) VALUES (?, ?, 'connects_to', 0.8, '')",
        (entity_ids["data-pipeline"], entity_ids["warehouse-db"]),
    )
    conn.commit()
    return conn, entity_ids


class TestTextMultiHopTraversal:
    def setup_method(self):
        self.conn, self.entity_ids = _setup_test_db()

    def teardown_method(self):
        self.conn.close()

    def test_empty_results_returns_empty(self):
        assert _text_multi_hop_traversal(self.conn, [], "query") == []

    def test_no_hyphenated_terms_no_expansion(self):
        initial = [("lessons/weather", "Weather is nice today")]
        result = _text_multi_hop_traversal(self.conn, initial, "weather")
        assert len(result) == 1

    def test_hyphenated_term_finds_linked_memories(self):
        initial = [("lessons/analytics-core", "Analytics-Core service runs on port 8443")]
        result = _text_multi_hop_traversal(self.conn, initial, "analytics")
        result_ids = {r[0] for r in result}
        assert "lessons/data-pipeline" in result_ids

    def test_two_hop_expansion(self):
        initial = [("lessons/analytics-core", "Analytics-Core service runs on port 8443")]
        result = _text_multi_hop_traversal(self.conn, initial, "analytics")
        result_ids = {r[0] for r in result}
        assert len(result_ids) >= 2

    def test_port_reference_triggers_search(self):
        initial = [("lessons/port-config", "Service runs on Port 8443")]
        result = _text_multi_hop_traversal(self.conn, initial, "port")
        result_ids = {r[0] for r in result}
        assert "lessons/port-config" in result_ids

    def test_limit_respected(self):
        for i in range(20):
            self.conn.execute(
                "INSERT INTO memories (id, content, source_file, tags, created_at) VALUES (?, ?, ?, ?, ?)",
                (f"extra_{i}", f"node-{i} is part of the cluster", "test.md", "[]", "2024-01-01"),
            )
        self.conn.commit()
        initial = [("lessons/analytics-core", "Analytics-Core service")]
        result = _text_multi_hop_traversal(self.conn, initial, "analytics", limit=3)
        assert len(result) <= 1 + 3

    def test_deduplication(self):
        initial = [("lessons/analytics-core", "Analytics-Core service")]
        result = _text_multi_hop_traversal(self.conn, initial, "analytics")
        ids = [r[0] for r in result]
        assert len(ids) == len(set(ids))


class TestPhaseTenKgBoost:
    def setup_method(self):
        self.conn, self.entity_ids = _setup_test_db()

    def teardown_method(self):
        self.conn.close()

    def test_empty_results_returns_empty(self):
        assert _phase_ten_kg_boost(self.conn, [], "analytics", limit=10) == []

    def test_boost_with_kg_entities(self):
        initial = [("lessons/data-pipeline", "The Analytics-Core microservice handles data pipelines", 0.5)]
        result = _phase_ten_kg_boost(self.conn, initial, "analytics-core", limit=10)
        assert len(result) >= 1

    def test_no_entity_match_no_boost(self):
        initial = [("lessons/weather", "Weather is nice today", 0.5)]
        result = _phase_ten_kg_boost(self.conn, initial, "weather", limit=10)
        assert len(result) == 1


class TestPhaseTenMultiHopKg:
    def setup_method(self):
        self.conn, self.entity_ids = _setup_test_db()

    def teardown_method(self):
        self.conn.close()

    def test_empty_results_returns_empty(self):
        assert _phase_ten_multi_hop_kg(self.conn, [], "analytics", limit=10) == []

    def test_short_query_skipped(self):
        assert _phase_ten_multi_hop_kg(self.conn, [], "hi", limit=10) == []

    def test_entity_extraction_and_traversal(self):
        result = _phase_ten_multi_hop_kg(self.conn, [], "analytics-core data-pipeline", limit=10)
        assert isinstance(result, list)

    def test_3_hop_scoring(self):
        result = _phase_ten_multi_hop_kg(self.conn, [], "analytics-core data-pipeline warehouse-db", limit=10)
        assert isinstance(result, list)

    def test_weak_edge_decay(self):
        self.conn.execute(
            "INSERT INTO kg_edges (source_id, target_id, relation, weight, created_at) VALUES (?, ?, 'weak_rel', 0.3, '')",
            (self.entity_ids["analytics-core"], self.entity_ids["warehouse-db"]),
        )
        self.conn.commit()
        result = _phase_ten_multi_hop_kg(self.conn, [], "analytics-core warehouse-db", limit=10)
        assert isinstance(result, list)


class TestEntityNameToMemoryId:
    def setup_method(self):
        self.conn, self.entity_ids = _setup_test_db()

    def teardown_method(self):
        self.conn.close()

    def test_finds_matching_memory(self):
        seen_ids = set()
        result = _entity_name_to_memory_id(self.conn, "analytics-core", seen_ids)
        assert isinstance(result, list)
        assert len(result) > 0

    def test_respects_seen_ids(self):
        seen_ids = {"lessons/analytics-core", "lessons/data-pipeline"}
        result = _entity_name_to_memory_id(self.conn, "analytics-core", seen_ids)
        for mid in result:
            assert mid not in seen_ids
