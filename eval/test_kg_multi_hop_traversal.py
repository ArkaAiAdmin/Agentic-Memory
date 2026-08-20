"""Unit tests for Cross-Session KG Graph Walks & Multi-Hop Traversal (Phase 2)."""

from __future__ import annotations

import sqlite3
import pytest
from pathlib import Path

from kg.kg_traversal import find_cross_session_graph_walk, find_shortest_path, find_neighbors
from knowledge_graph.kg_schema import ensure_kg_schema
from fact.fact_schema import ensure_facts_schema


@pytest.fixture
def graph_db(tmp_path: Path):
    db_path = tmp_path / 'test_graph.db'
    conn = sqlite3.connect(str(db_path))
    ensure_kg_schema(conn)
    ensure_facts_schema(conn)

    # Populate sample graph:
    # A (Agent) -> defines -> B (Module) -> uses -> C (Database)
    cur = conn.cursor()
    cur.execute("INSERT INTO kg_entities (name, entity_type) VALUES ('AgentAlpha', 'agent')")
    e_a = cur.lastrowid
    cur.execute("INSERT INTO kg_entities (name, entity_type) VALUES ('AuthModule', 'code')")
    e_b = cur.lastrowid
    cur.execute("INSERT INTO kg_entities (name, entity_type) VALUES ('PostgresDB', 'infra')")
    e_c = cur.lastrowid

    cur.execute("INSERT INTO kg_edges (source_id, target_id, relation, weight) VALUES (?, ?, 'defines', 1.0)", (e_a, e_b))
    cur.execute("INSERT INTO kg_edges (source_id, target_id, relation, weight) VALUES (?, ?, 'uses', 0.9)", (e_b, e_c))

    # Add associated active fact for PostgresDB
    cur.execute(
        """INSERT INTO kg_facts (subject, predicate, object, confidence, subject_entity_id)
           VALUES ('PostgresDB', 'port', '5432', 1.0, ?)""",
        (e_c,)
    )
    conn.commit()

    yield conn
    conn.close()


def test_cross_session_graph_walk_2hop(graph_db):
    """Verify 2-hop graph traversal from AgentAlpha to PostgresDB with exponential hop decay."""
    results = find_cross_session_graph_walk(
        graph_db,
        seed_entities=['AgentAlpha'],
        max_hops=2,
        decay_factor=0.7,
    )
    assert len(results) >= 2

    # Check 1-hop result (AuthModule)
    hop1 = next(r for r in results if r['entity']['name'] == 'AuthModule')
    assert hop1['hop'] == 1
    assert hop1['score'] == 0.7  # 1.0 * 0.7^1

    # Check 2-hop result (PostgresDB)
    hop2 = next(r for r in results if r['entity']['name'] == 'PostgresDB')
    assert hop2['hop'] == 2
    assert hop2['score'] == round(0.9 * (0.7 ** 2), 4)
    assert len(hop2['connected_facts']) == 1
    assert hop2['connected_facts'][0]['object'] == '5432'


def test_cross_session_graph_walk_empty(graph_db):
    """Verify graceful return for missing or empty seed entities."""
    assert find_cross_session_graph_walk(graph_db, seed_entities=[]) == []
    assert find_cross_session_graph_walk(graph_db, seed_entities=['NonExistent']) == []
