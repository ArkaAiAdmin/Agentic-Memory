"""Tests for kg_traversal.py — Knowledge Graph traversal engine.

Covers: shortest paths via Recursive CTE, neighborhood queries, and sequence pattern traverses.
"""

import os
import sys
import sqlite3
from pathlib import Path
import pytest

sys.path.insert(0, os.path.expandvars("$HOME/.config/agentic-memory") or os.path.expanduser("~/.config/agentic-memory"))

import knowledge_graph as kg
from kg_traversal import find_shortest_path, find_neighbors, traverse_graph
from mcp_kg_traversal import memory_graph_shortest_path, memory_graph_traverse

class TestKGTraversal:
    def setup_method(self):
        self.conn = sqlite3.connect(":memory:")
        kg.ensure_kg_schema(self.conn)

        # Insert test entities
        entities = [
            ("auto_save", "code"),
            ("crdt_field", "code"),
            ("db_migrations", "concept"),
            ("sync_server", "code"),
            ("sync_client", "code"),
            ("disconnected_node", "concept"),
        ]
        self.entity_ids = {}
        for name, etype in entities:
            cursor = self.conn.execute(
                "INSERT INTO kg_entities (name, entity_type, mentions, created_at, updated_at) VALUES (?, ?, 1, '', '')",
                (name, etype)
            )
            self.entity_ids[name] = cursor.lastrowid

        # Insert test edges
        # auto_save -[imports]-> crdt_field
        # sync_server -[imports]-> crdt_field
        # sync_client -[imports]-> crdt_field
        # crdt_field -[uses]-> db_migrations
        # crdt_field -[invalid_rel]-> sync_server (but marked invalid)
        edges = [
            ("auto_save", "crdt_field", "imports", 1.0, None),
            ("sync_server", "crdt_field", "imports", 1.0, None),
            ("sync_client", "crdt_field", "imports", 1.0, None),
            ("crdt_field", "db_migrations", "uses", 1.5, None),
            ("crdt_field", "sync_server", "invalid_rel", 1.0, "2026-06-24T00:00:00Z"),
        ]
        for src, tgt, rel, weight, invalid_at in edges:
            self.conn.execute(
                "INSERT INTO kg_edges (source_id, target_id, relation, weight, created_at, invalid_at) VALUES (?, ?, ?, ?, '', ?)",
                (self.entity_ids[src], self.entity_ids[tgt], rel, weight, invalid_at)
            )
        self.conn.commit()

    def teardown_method(self):
        self.conn.close()

    def test_find_shortest_path_success(self):
        # Path: auto_save -[imports]-> crdt_field -[uses]-> db_migrations
        path = find_shortest_path(self.conn, "auto_save", "db_migrations", max_depth=5)
        assert path is not None
        assert len(path) == 5  # Node, Rel, Node, Rel, Node
        assert path[0]["name"] == "auto_save"
        assert path[1]["relation"] == "imports"
        assert path[2]["name"] == "crdt_field"
        assert path[3]["relation"] == "uses"
        assert path[4]["name"] == "db_migrations"

    def test_find_shortest_path_disconnected(self):
        path = find_shortest_path(self.conn, "auto_save", "disconnected_node", max_depth=5)
        assert path is None

    def test_find_shortest_path_nonexistent(self):
        path = find_shortest_path(self.conn, "auto_save", "nonexistent_node", max_depth=5)
        assert path is None

    def test_find_shortest_path_invalid_edge_ignored(self):
        # crdt_field -[invalid_rel]-> sync_server is invalid, so shortest path should not exist or be longer
        path = find_shortest_path(self.conn, "crdt_field", "sync_server", max_depth=5)
        assert path is None

    def test_find_neighbors_outbound(self):
        neighbors = find_neighbors(self.conn, "crdt_field", direction="out")
        # Should have: crdt_field -[uses]-> db_migrations (invalid_rel is ignored)
        assert len(neighbors) == 1
        assert neighbors[0]["target"]["name"] == "db_migrations"
        assert neighbors[0]["relation"] == "uses"

    def test_find_neighbors_inbound(self):
        neighbors = find_neighbors(self.conn, "crdt_field", direction="in")
        # Should have: auto_save, sync_server, sync_client
        names = {n["source"]["name"] for n in neighbors}
        assert names == {"auto_save", "sync_server", "sync_client"}
        assert all(n["relation"] == "imports" for n in neighbors)

    def test_find_neighbors_both(self):
        neighbors = find_neighbors(self.conn, "crdt_field", direction="both")
        assert len(neighbors) == 4
        targets = {n["target"]["name"] for n in neighbors}
        sources = {n["source"]["name"] for n in neighbors}
        assert "db_migrations" in targets
        assert "auto_save" in sources

    def test_traverse_graph_pattern(self):
        # Pattern: imports -> uses
        paths = traverse_graph(self.conn, "auto_save", ["imports", "uses"])
        assert len(paths) == 1
        path = paths[0]
        assert len(path) == 5
        assert path[0]["name"] == "auto_save"
        assert path[1]["relation"] == "imports"
        assert path[2]["name"] == "crdt_field"
        assert path[3]["relation"] == "uses"
        assert path[4]["name"] == "db_migrations"

    def test_traverse_graph_no_match(self):
        # Pattern: uses -> imports (invalid order)
        paths = traverse_graph(self.conn, "auto_save", ["uses", "imports"])
        assert len(paths) == 0
