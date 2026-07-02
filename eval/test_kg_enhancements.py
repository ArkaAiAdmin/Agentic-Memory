#!/usr/bin/env python3
"""Unit tests for Knowledge Graph enhancements (entity linking, centrality, aliases)."""

import sys
import unittest
import sqlite3
from pathlib import Path

# Add repo root to sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from knowledge_graph.kg_schema import ensure_kg_schema
from knowledge_graph.kg_db import _jaccard_similarity, _upsert_entity
from kg.graph_analytics import compute_pagerank, update_graph_analytics


class TestKGEnhancements(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        ensure_kg_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_jaccard_similarity(self):
        self.assertAlmostEqual(_jaccard_similarity("google llc", "google llc"), 1.0)
        self.assertAlmostEqual(_jaccard_similarity("google llc", "microsoft"), 0.0)
        # Check partial overlap
        sim = _jaccard_similarity("google corp", "google corporation")
        self.assertGreater(sim, 0.5)

    def test_fuzzy_entity_linking_and_aliases(self):
        now = 1234567.0
        # 1. Insert base entity
        id1 = _upsert_entity(self.conn, "Google LLC", "company", now)
        
        # 2. Insert fuzzy entity - should link to the base entity ID
        id2 = _upsert_entity(self.conn, "Google L.L.C.", "company", now)
        self.assertEqual(id1, id2)

        # 3. Check that alias was stored
        alias_row = self.conn.execute(
            "SELECT entity_id, alias FROM kg_entity_aliases WHERE alias = ?",
            ("google l.l.c.",)
        ).fetchone()
        self.assertIsNotNone(alias_row)
        self.assertEqual(alias_row[0], id1)

        # 4. Insert exact alias - should immediately resolve
        id3 = _upsert_entity(self.conn, "Google L.L.C.", "company", now)
        self.assertEqual(id1, id3)

    def test_pagerank_and_centrality_updates(self):
        now = 1234567.0
        # Create 3 entities forming a cycle: A -> B -> C -> A
        id_a = _upsert_entity(self.conn, "Node A", "node", now)
        id_b = _upsert_entity(self.conn, "Node B", "node", now)
        id_c = _upsert_entity(self.conn, "Node C", "node", now)

        # Insert edges
        self.conn.execute(
            "INSERT INTO kg_edges (source_id, target_id, relation, weight) VALUES (?, ?, 'links', 1.0)",
            (id_a, id_b)
        )
        self.conn.execute(
            "INSERT INTO kg_edges (source_id, target_id, relation, weight) VALUES (?, ?, 'links', 1.0)",
            (id_b, id_c)
        )
        self.conn.execute(
            "INSERT INTO kg_edges (source_id, target_id, relation, weight) VALUES (?, ?, 'links', 1.0)",
            (id_c, id_a)
        )

        # Compute pagerank scores
        pr = compute_pagerank(self.conn)
        self.assertEqual(len(pr), 3)
        # In a symmetric ring, all nodes should have equal PageRank
        self.assertAlmostEqual(pr[id_a], 1.0 / 3.0)
        self.assertAlmostEqual(pr[id_b], 1.0 / 3.0)
        self.assertAlmostEqual(pr[id_c], 1.0 / 3.0)

        # Update graph centrality and verify db updates
        res = update_graph_analytics(self.conn)
        self.assertEqual(res["entities_updated"], 3)

        rows = self.conn.execute("SELECT id, centrality FROM kg_entities").fetchall()
        for eid, score in rows:
            self.assertAlmostEqual(score, 1.0 / 3.0, places=5)


if __name__ == "__main__":
    unittest.main()
