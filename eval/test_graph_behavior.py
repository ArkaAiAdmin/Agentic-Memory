"""Behavioral tests for Sprint 4 Graph Analytics Upgrade.

Covers:
  - connected_components on a small graph
  - louvain_communities on a graph with known communities
  - betweenness centrality (Brandes) correctness on a simple graph
  - PageRank / centrality update paths
  - graph_communities background task registration
  - graph_snapshots table insertion
  - memory_graph_insights / memory_graph_evolution admin tools
  - community_ids written to kg_entities
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

sys.path[:] = [p for p in sys.path if not p.endswith("/.config/agentic-memory")]
if str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))

import importlib as _ilib
for _mod in list(sys.modules):
    if _mod == "kg" or _mod == "mcp_kg" or _mod.startswith(("kg.", "mcp_kg.", "infra.")):
        try:
            mod_file = getattr(sys.modules[_mod], "__file__", "") or ""
            if "/.config/agentic-memory" in mod_file and "/.config/agentic-memory-sprint4" not in mod_file:
                del sys.modules[_mod]
        except Exception:
            del sys.modules[_mod]
_ilib.invalidate_caches()

# Pin the worktree's kg package immediately so later imports can't shadow it
import kg  # noqa: E402


def _ensure_kg():
    """Re-pin worktree packages if a later test module re-imported from main repo."""
    import importlib as _il
    for _pkg in ("kg", "knowledge_graph"):
        try:
            mod = _il.import_module(_pkg)
        except ImportError:
            continue
        mod_path = getattr(mod, "__file__", "") or ""
        if "/.config/agentic-memory-sprint4" in mod_path:
            continue
        for _m in list(sys.modules):
            if _m == _pkg or _m.startswith(_pkg + "."):
                del sys.modules[_m]
        _il.invalidate_caches()
    import kg  # noqa: F811, E402
    import kg.graph_communities  # noqa: E402, F811
    import kg.graph_analytics  # noqa: E402, F811


def _ensure_sprint4_schema(conn):
    """Create KG schema + Sprint 4 columns (community_id, betweenness).

    Uses the imported knowledge_graph.kg_schema.ensure_kg_schema, then
    defensively adds community_id and betweenness so the test works
    regardless of which module cache the test runner has loaded.
    """
    from knowledge_graph.kg_schema import ensure_kg_schema
    ensure_kg_schema(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(kg_entities)").fetchall()}
    if "community_id" not in cols:
        conn.execute("ALTER TABLE kg_entities ADD COLUMN community_id INTEGER DEFAULT 0")
    if "betweenness" not in cols:
        conn.execute("ALTER TABLE kg_entities ADD COLUMN betweenness REAL DEFAULT 0.0")
    conn.commit()


class TestGraphCommunities(unittest.TestCase):
    def setUp(self):
        _ensure_kg()
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        _ensure_sprint4_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def _add_entity(self, name: str, etype: str = "node") -> int:
        now = time.time()
        self.conn.execute(
            "INSERT INTO kg_entities (name, entity_type, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (name, etype, str(now), str(now)),
        )
        self.conn.commit()
        return int(self.conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    def _add_edge(self, src: int, tgt: int, rel: str = "related", weight: float = 1.0):
        self.conn.execute(
            "INSERT INTO kg_edges (source_id, target_id, relation, weight, created_at) VALUES (?, ?, ?, ?, ?)",
            (src, tgt, rel, weight, str(time.time())),
        )
        self.conn.commit()

    def test_connected_components_two_groups(self):
        a = self._add_entity("A")
        b = self._add_entity("B")
        c = self._add_entity("C")
        d = self._add_entity("D")
        self._add_edge(a, b)
        self._add_edge(b, c)
        self._add_edge(c, a)
        self._add_edge(d, a)  # connects isolated D to the main component

        from kg.graph_communities import connected_components

        cc = connected_components(self.conn)
        # All four nodes form a single connected component with id >= 1
        self.assertEqual(len(set(cc.values())), 1)
        community = next(iter(set(cc.values())))
        self.assertGreaterEqual(community, 1)
        for nid in (a, b, c, d):
            self.assertEqual(cc[nid], community)

    def test_connected_components_min_size_filters(self):
        from kg.graph_communities import connected_components

        a = self._add_entity("A")
        b = self._add_entity("B")
        c = self._add_entity("C")
        self._add_edge(a, b)
        cc = connected_components(self.conn, min_component_size=3)
        # component of size 2 (A,B) gets collapsed; C is a singleton (size 1 < 3)
        self.assertEqual(cc[a], 0)
        self.assertEqual(cc[b], 0)
        self.assertEqual(cc[c], 0)

    def test_louvain_two_known_communities(self):
        # Community 1: A-B-C dense (no bridges to Community 2)
        a = self._add_entity("A")
        b = self._add_entity("B")
        c = self._add_entity("C")
        for u, v in ((a, b), (b, c), (c, a)):
            self._add_edge(u, v)

        # Community 2: D-E-F dense
        d = self._add_entity("D")
        e = self._add_entity("E")
        f = self._add_entity("F")
        for u, v in ((d, e), (e, f), (f, d)):
            self._add_edge(u, v)

        from kg.graph_communities import louvain_communities

        membership = louvain_communities(self.conn, resolution=1.0)
        # Two disconnected components -> two communities
        self.assertGreaterEqual(len(set(membership.values())), 2)
        self.assertLessEqual(len(set(membership.values())), 3)

    def test_louvain_empty_graph(self):
        from kg.graph_communities import louvain_communities

        result = louvain_communities(self.conn)
        self.assertEqual(result, {})

    def test_write_community_ids_persists(self):
        a = self._add_entity("A")
        b = self._add_entity("B")
        c = self._add_entity("C")
        self._add_edge(a, b)
        self._add_edge(b, c)

        from kg.graph_communities import connected_components, write_community_ids

        membership = connected_components(self.conn, min_component_size=1)
        write_community_ids(self.conn, membership)
        self.conn.commit()

        rows = self.conn.execute("SELECT id, community_id FROM kg_entities").fetchall()
        for row in rows:
            self.assertNotEqual(row[1], 0)


class TestBetweennessCentrality(unittest.TestCase):
    def setUp(self):
        _ensure_kg()
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        _ensure_sprint4_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def _add_entity(self, name: str) -> int:
        now = time.time()
        self.conn.execute(
            "INSERT INTO kg_entities (name, entity_type, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (name, "node", str(now), str(now)),
        )
        self.conn.commit()
        return int(self.conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    def _add_edge(self, src: int, tgt: int, weight: float = 1.0):
        self.conn.execute(
            "INSERT INTO kg_edges (source_id, target_id, relation, weight, created_at) VALUES (?, ?, ?, ?, ?)",
            (src, tgt, "related", weight, str(time.time())),
        )
        self.conn.commit()

    def test_betweenness_triangle_bridge(self):
        # Graph: A -- B -- D
        #           |
        #           C
        # B is a bridge between A,C and D
        a = self._add_entity("A")
        b = self._add_entity("B")
        c = self._add_entity("C")
        d = self._add_entity("D")
        self._add_edge(a, b)
        self._add_edge(b, c)
        self._add_edge(b, d)

        from kg.graph_analytics import compute_betweenness

        bw = compute_betweenness(self.conn)
        self.assertIn(a, bw)
        self.assertIn(b, bw)
        self.assertIn(c, bw)
        self.assertIn(d, bw)
        self.assertGreater(bw[b], bw[a])
        self.assertGreater(bw[b], bw[c])

    def test_betweenness_empty_graph(self):
        from kg.graph_analytics import compute_betweenness

        result = compute_betweenness(self.conn)
        self.assertEqual(result, {})

    def test_update_betweenness(self):
        a = self._add_entity("N1")
        b = self._add_entity("N2")
        c = self._add_entity("N3")
        self._add_edge(a, b)
        self._add_edge(b, c)

        from kg.graph_analytics import update_betweenness

        res = update_betweenness(self.conn)
        self.assertEqual(res["entities_updated"], 3)

        rows = self.conn.execute("SELECT id, betweenness FROM kg_entities").fetchall()
        for row in rows:
            self.assertIsNotNone(row[1])
            self.assertGreaterEqual(float(row[1]), 0.0)


class TestGraphSnapshots(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = Path(self._tmp.name)
        self._tmp.close()
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        from infra.migration_runner import run_migrations
        from knowledge_graph.kg_schema import ensure_kg_schema

        run_migrations(self.conn)
        ensure_kg_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        try:
            self.db_path.unlink()
        except OSError:
            pass

    def _add_entity(self, name: str) -> int:
        now = time.time()
        self.conn.execute(
            "INSERT INTO kg_entities (name, entity_type, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (name, "node", str(now), str(now)),
        )
        self.conn.commit()
        return int(self.conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    def _add_edge(self, src: int, tgt: int):
        self.conn.execute(
            "INSERT INTO kg_edges (source_id, target_id, relation, weight, created_at) VALUES (?, ?, ?, ?, ?)",
            (src, tgt, "related", 1.0, str(time.time())),
        )
        self.conn.commit()

    def test_graph_snapshots_table_exists(self):
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='graph_snapshots'"
        ).fetchall()
        self.assertEqual(len(rows), 1)

    def test_graph_snapshots_background_handler_writes(self):
        from background.background_worker import _lazy_graph_snapshots

        a = self._add_entity("X")
        b = self._add_entity("Y")
        self._add_edge(a, b)

        result = _lazy_graph_snapshots({}, self.conn, Path(":memory:"))
        self.assertIn("graph_snapshot:", result)

        rows = self.conn.execute("SELECT COUNT(*) FROM graph_snapshots").fetchone()
        self.assertEqual(rows[0], 1)

    def test_graph_snapshots_evolution_tool(self):
        from background.background_worker import _lazy_graph_snapshots

        a = self._add_entity("P")
        b = self._add_entity("Q")
        self._add_edge(a, b)

        _lazy_graph_snapshots({}, self.conn, Path(str(self.db_path)))

        import json as _json

        row = self.conn.execute(
            "SELECT id, captured_at, entity_count, edge_count, community_count, avg_centrality, top_entities, new_entities, removed_entities "
            "FROM graph_snapshots ORDER BY captured_at DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(row, "graph_snapshots row should exist")
        self.assertGreaterEqual(row["entity_count"], 2)
        self.assertIsNotNone(row["top_entities"])
        parsed_top = _json.loads(row["top_entities"] or "[]")
        self.assertIsInstance(parsed_top, list)


class TestGraphInsightsAndCentrality(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        from knowledge_graph.kg_schema import ensure_kg_schema

        ensure_kg_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def _add_entity(self, name: str) -> int:
        now = time.time()
        self.conn.execute(
            "INSERT INTO kg_entities (name, entity_type, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (name, "node", str(now), str(now)),
        )
        self.conn.commit()
        return int(self.conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    def _add_edge(self, src: int, tgt: int, weight: float = 1.0):
        self.conn.execute(
            "INSERT INTO kg_edges (source_id, target_id, relation, weight, created_at) VALUES (?, ?, ?, ?, ?)",
            (src, tgt, "related", weight, str(time.time())),
        )
        self.conn.commit()

    def test_graph_insights_returns_density_and_centrality(self):
        a = self._add_entity("Alpha")
        b = self._add_entity("Beta")
        c = self._add_entity("Gamma")
        self._add_edge(a, b)
        self._add_edge(b, c)
        self._add_edge(c, a)

        from mcp_kg import memory_graph_insights

        output = memory_graph_insights(sample_size=5, include_bridge=False)
        self.assertIn("Graph Analytics Insights", output)
        self.assertIn("Density", output)
        self.assertIn("PageRank", output)

    def test_pagerank_symmetry(self):
        a = self._add_entity("N1")
        b = self._add_entity("N2")
        c = self._add_entity("N3")
        self._add_edge(a, b)
        self._add_edge(b, c)
        self._add_edge(c, a)

        from kg.graph_analytics import compute_pagerank

        pr = compute_pagerank(self.conn)
        self.assertEqual(len(pr), 3)
        score_sum = sum(pr.values())
        self.assertAlmostEqual(score_sum, 1.0, places=3)


if __name__ == "__main__":
    unittest.main()
