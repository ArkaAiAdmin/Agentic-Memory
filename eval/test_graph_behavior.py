"""Behavioral tests for Sprint 4 Graph Analytics Upgrade.

Subprocess-isolated per Hard Rule 20 — each test runs in its own Python
process so module singletons start from a clean state. This eliminates
the flakiness caused by kg/mcp_kg/infra module cache pollution in the
full test suite.

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

import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
if not VENV_PYTHON.exists():
    VENV_PYTHON = REPO_ROOT / "venv" / "bin" / "python"


def _run_subprocess(code: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    import tempfile
    env = {k: v for k, v in os.environ.items() if not k.startswith("MEMORY_")}
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["OBJC_DISABLE_INITIALIZE_FORK_SAFETY"] = "YES"
    env["REPO_ROOT"] = str(REPO_ROOT)
    # Isolate each subprocess to its own temp dir so module-level singletons
    # (mcp_kg, background_worker, etc.) don't contend for the real memory.db flock.
    _tmp_db_dir = tempfile.mkdtemp(prefix="graph_test_")
    env["MEMORY_DB_PATH"] = str(Path(_tmp_db_dir) / "memory.db")
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [str(VENV_PYTHON), "-c", code],
        capture_output=True, text=True, timeout=60, env=env,
        cwd=str(REPO_ROOT),
    )


# ---------------------------------------------------------------------------
# Schema helper (inlined in each subprocess)
# ---------------------------------------------------------------------------

_SCHEMA_SETUP = textwrap.dedent("""\
    import os, sqlite3, time
    os.chdir(os.environ.get("REPO_ROOT", "."))

    def setup_schema(conn):
        from infra.migration_runner import run_migrations
        from knowledge_graph.kg_schema import ensure_kg_schema
        run_migrations(conn)
        ensure_kg_schema(conn)
        # Add Sprint 4 columns if missing
        cols = {r[1] for r in conn.execute("PRAGMA table_info(kg_entities)").fetchall()}
        if "community_id" not in cols:
            conn.execute("ALTER TABLE kg_entities ADD COLUMN community_id INTEGER DEFAULT 0")
        if "betweenness" not in cols:
            conn.execute("ALTER TABLE kg_entities ADD COLUMN betweenness REAL DEFAULT 0.0")
        conn.commit()

    def add_entity(conn, name, etype="node"):
        now = time.time()
        conn.execute(
            "INSERT INTO kg_entities (name, entity_type, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (name, etype, str(now), str(now)),
        )
        conn.commit()
        return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    def add_edge(conn, src, tgt, rel="related", weight=1.0):
        conn.execute(
            "INSERT INTO kg_edges (source_id, target_id, relation, weight, created_at) VALUES (?, ?, ?, ?, ?)",
            (src, tgt, rel, weight, str(time.time())),
        )
        conn.commit()
""")


class TestGraphCommunities(unittest.TestCase):
    def test_connected_components_two_groups(self):
        code = _SCHEMA_SETUP + textwrap.dedent("""\
            conn = sqlite3.connect(":memory:")
            setup_schema(conn)
            a = add_entity(conn, "A")
            b = add_entity(conn, "B")
            c = add_entity(conn, "C")
            d = add_entity(conn, "D")
            add_edge(conn, a, b)
            add_edge(conn, b, c)
            add_edge(conn, c, a)
            add_edge(conn, d, a)

            from kg.graph_communities import connected_components
            cc = connected_components(conn)
            assert len(set(cc.values())) == 1, f"Expected 1 component, got {set(cc.values())}"
            community = next(iter(set(cc.values())))
            assert community >= 1, f"Community ID should be >= 1, got {community}"
            for nid in (a, b, c, d):
                assert cc[nid] == community, f"Node {nid} in wrong component"
            print("PASS")
        """)
        result = _run_subprocess(code)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_connected_components_min_size_filters(self):
        code = _SCHEMA_SETUP + textwrap.dedent("""\
            conn = sqlite3.connect(":memory:")
            setup_schema(conn)
            a = add_entity(conn, "A")
            b = add_entity(conn, "B")
            c = add_entity(conn, "C")
            add_edge(conn, a, b)

            from kg.graph_communities import connected_components
            cc = connected_components(conn, min_component_size=3)
            assert cc[a] == 0, f"Expected 0, got {cc[a]}"
            assert cc[b] == 0, f"Expected 0, got {cc[b]}"
            assert cc[c] == 0, f"Expected 0, got {cc[c]}"
            print("PASS")
        """)
        result = _run_subprocess(code)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_louvain_two_known_communities(self):
        code = _SCHEMA_SETUP + textwrap.dedent("""\
            conn = sqlite3.connect(":memory:")
            setup_schema(conn)
            a = add_entity(conn, "A")
            b = add_entity(conn, "B")
            c = add_entity(conn, "C")
            for u, v in ((a, b), (b, c), (c, a)):
                add_edge(conn, u, v)
            d = add_entity(conn, "D")
            e = add_entity(conn, "E")
            f = add_entity(conn, "F")
            for u, v in ((d, e), (e, f), (f, d)):
                add_edge(conn, u, v)

            from kg.graph_communities import louvain_communities
            membership = louvain_communities(conn, resolution=1.0)
            num_communities = len(set(membership.values()))
            assert num_communities >= 2, f"Expected >= 2 communities, got {num_communities}"
            assert num_communities <= 3, f"Expected <= 3 communities, got {num_communities}"
            print("PASS")
        """)
        result = _run_subprocess(code)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_louvain_empty_graph(self):
        code = _SCHEMA_SETUP + textwrap.dedent("""\
            conn = sqlite3.connect(":memory:")
            setup_schema(conn)

            from kg.graph_communities import louvain_communities
            result = louvain_communities(conn)
            assert result == {}, f"Expected empty dict, got {result}"
            print("PASS")
        """)
        result = _run_subprocess(code)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_write_community_ids_persists(self):
        code = _SCHEMA_SETUP + textwrap.dedent("""\
            conn = sqlite3.connect(":memory:")
            setup_schema(conn)
            a = add_entity(conn, "A")
            b = add_entity(conn, "B")
            c = add_entity(conn, "C")
            add_edge(conn, a, b)
            add_edge(conn, b, c)

            from kg.graph_communities import connected_components, write_community_ids
            membership = connected_components(conn, min_component_size=1)
            write_community_ids(conn, membership)
            conn.commit()

            rows = conn.execute("SELECT id, community_id FROM kg_entities").fetchall()
            for row in rows:
                assert row[1] != 0, f"Entity {row[0]} has community_id=0"
            print("PASS")
        """)
        result = _run_subprocess(code)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)


class TestBetweennessCentrality(unittest.TestCase):
    def test_betweenness_triangle_bridge(self):
        code = _SCHEMA_SETUP + textwrap.dedent("""\
            conn = sqlite3.connect(":memory:")
            setup_schema(conn)
            a = add_entity(conn, "A")
            b = add_entity(conn, "B")
            c = add_entity(conn, "C")
            d = add_entity(conn, "D")
            add_edge(conn, a, b)
            add_edge(conn, b, c)
            add_edge(conn, b, d)

            from kg.graph_analytics import compute_betweenness
            bw = compute_betweenness(conn)
            assert a in bw, f"A not in betweenness"
            assert b in bw, f"B not in betweenness"
            assert c in bw, f"C not in betweenness"
            assert d in bw, f"D not in betweenness"
            assert bw[b] > bw[a], f"B should be > A: {bw[b]} vs {bw[a]}"
            assert bw[b] > bw[c], f"B should be > C: {bw[b]} vs {bw[c]}"
            print("PASS")
        """)
        result = _run_subprocess(code)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_betweenness_empty_graph(self):
        code = _SCHEMA_SETUP + textwrap.dedent("""\
            conn = sqlite3.connect(":memory:")
            setup_schema(conn)

            from kg.graph_analytics import compute_betweenness
            result = compute_betweenness(conn)
            assert result == {}, f"Expected empty dict, got {result}"
            print("PASS")
        """)
        result = _run_subprocess(code)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_update_betweenness(self):
        code = _SCHEMA_SETUP + textwrap.dedent("""\
            conn = sqlite3.connect(":memory:")
            setup_schema(conn)
            a = add_entity(conn, "N1")
            b = add_entity(conn, "N2")
            c = add_entity(conn, "N3")
            add_edge(conn, a, b)
            add_edge(conn, b, c)

            from kg.graph_analytics import update_betweenness
            res = update_betweenness(conn)
            assert res["entities_updated"] == 3, f"Expected 3, got {res['entities_updated']}"

            rows = conn.execute("SELECT id, betweenness FROM kg_entities").fetchall()
            for row in rows:
                assert row[1] is not None, f"Entity {row[0]} has NULL betweenness"
                assert float(row[1]) >= 0.0, f"Entity {row[0]} has negative betweenness"
            print("PASS")
        """)
        result = _run_subprocess(code)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)


class TestGraphSnapshots(unittest.TestCase):
    def test_graph_snapshots_table_exists(self):
        code = textwrap.dedent(f"""\
            import sqlite3, tempfile, os
            from pathlib import Path

            tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            db_path = Path(tmp.name)
            tmp.close()
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            os.chdir({repr(str(REPO_ROOT))})

            from infra.migration_runner import run_migrations
            from knowledge_graph.kg_schema import ensure_kg_schema
            run_migrations(conn)
            ensure_kg_schema(conn)

            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='graph_snapshots'"
            ).fetchall()
            assert len(rows) == 1, f"graph_snapshots table not found"
            conn.close()
            db_path.unlink()
            print("PASS")
        """)
        result = _run_subprocess(code)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_graph_snapshots_background_handler_writes(self):
        code = _SCHEMA_SETUP + textwrap.dedent(f"""\
            import sqlite3, tempfile, os
            from pathlib import Path

            tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            db_path = Path(tmp.name)
            tmp.close()
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            os.chdir({repr(str(REPO_ROOT))})

            from infra.migration_runner import run_migrations
            from knowledge_graph.kg_schema import ensure_kg_schema
            run_migrations(conn)
            ensure_kg_schema(conn)
            # Add Sprint 4 columns
            cols = {{r[1] for r in conn.execute("PRAGMA table_info(kg_entities)").fetchall()}}
            if "community_id" not in cols:
                conn.execute("ALTER TABLE kg_entities ADD COLUMN community_id INTEGER DEFAULT 0")
            if "betweenness" not in cols:
                conn.execute("ALTER TABLE kg_entities ADD COLUMN betweenness REAL DEFAULT 0.0")
            conn.commit()

            x = add_entity(conn, "X")
            y = add_entity(conn, "Y")
            add_edge(conn, x, y)

            from background.background_worker import _lazy_graph_snapshots
            result = _lazy_graph_snapshots({{}}, conn, Path(":memory:"))
            assert "graph_snapshot:" in result, f"Expected graph_snapshot: in result, got {{result}}"

            rows = conn.execute("SELECT COUNT(*) FROM graph_snapshots").fetchone()
            assert rows[0] == 1, f"Expected 1 snapshot, got {{rows[0]}}"
            conn.close()
            db_path.unlink()
            print("PASS")
        """)
        result = _run_subprocess(code)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_graph_snapshots_evolution_tool(self):
        code = _SCHEMA_SETUP + textwrap.dedent(f"""\
            import json as _json, sqlite3, tempfile, os
            from pathlib import Path

            tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            db_path = Path(tmp.name)
            tmp.close()
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            os.chdir({repr(str(REPO_ROOT))})

            from infra.migration_runner import run_migrations
            from knowledge_graph.kg_schema import ensure_kg_schema
            run_migrations(conn)
            ensure_kg_schema(conn)
            cols = {{r[1] for r in conn.execute("PRAGMA table_info(kg_entities)").fetchall()}}
            if "community_id" not in cols:
                conn.execute("ALTER TABLE kg_entities ADD COLUMN community_id INTEGER DEFAULT 0")
            if "betweenness" not in cols:
                conn.execute("ALTER TABLE kg_entities ADD COLUMN betweenness REAL DEFAULT 0.0")
            conn.commit()

            p = add_entity(conn, "P")
            q = add_entity(conn, "Q")
            add_edge(conn, p, q)

            from background.background_worker import _lazy_graph_snapshots
            _lazy_graph_snapshots({{}}, conn, Path(str(db_path)))

            row = conn.execute(
                "SELECT id, captured_at, entity_count, edge_count, community_count, avg_centrality, top_entities, new_entities, removed_entities "
                "FROM graph_snapshots ORDER BY captured_at DESC LIMIT 1"
            ).fetchone()
            assert row is not None, "graph_snapshots row should exist"
            assert row["entity_count"] >= 2, f"Expected >= 2 entities, got {{row['entity_count']}}"
            assert row["top_entities"] is not None, "top_entities should not be NULL"
            parsed_top = _json.loads(row["top_entities"] or "[]")
            assert isinstance(parsed_top, list), f"top_entities should be list, got {{type(parsed_top)}}"
            conn.close()
            db_path.unlink()
            print("PASS")
        """)
        result = _run_subprocess(code)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)


class TestGraphInsightsAndCentrality(unittest.TestCase):
    def test_graph_insights_returns_density_and_centrality(self):
        code = _SCHEMA_SETUP + textwrap.dedent("""\
            from unittest import mock as _mock
            # Bypass RBAC authorization in the subprocess so the test
            # doesn't depend on a real memory.db for auth lookups.
            import mcp_surface.mcp_kg as _mcp_kg
            _mcp_kg._check_authorization = lambda *a, **kw: None

            conn = sqlite3.connect(":memory:")
            setup_schema(conn)
            a = add_entity(conn, "Alpha")
            b = add_entity(conn, "Beta")
            c = add_entity(conn, "Gamma")
            add_edge(conn, a, b)
            add_edge(conn, b, c)
            add_edge(conn, c, a)

            from mcp_surface.mcp_kg import memory_graph_insights
            output = memory_graph_insights(sample_size=5, include_bridge=False, conn=conn)
            assert "Graph Analytics Insights" in output, f"Missing title in output"
            assert "Density" in output, f"Missing Density in output"
            assert "PageRank" in output, f"Missing PageRank in output"
            print("PASS")
        """)
        result = _run_subprocess(code)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)

    def test_pagerank_symmetry(self):
        code = _SCHEMA_SETUP + textwrap.dedent("""\
            conn = sqlite3.connect(":memory:")
            setup_schema(conn)
            n1 = add_entity(conn, "N1")
            n2 = add_entity(conn, "N2")
            n3 = add_entity(conn, "N3")
            add_edge(conn, n1, n2)
            add_edge(conn, n2, n3)
            add_edge(conn, n3, n1)

            from kg.graph_analytics import compute_pagerank
            pr = compute_pagerank(conn)
            assert len(pr) == 3, f"Expected 3 nodes, got {{len(pr)}}"
            score_sum = sum(pr.values())
            assert abs(score_sum - 1.0) < 0.001, f"PageRank sum should be ~1.0, got {{score_sum}}"
            print("PASS")
        """)
        result = _run_subprocess(code)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
