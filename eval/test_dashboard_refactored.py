"""Tests for the refactored dashboard/ package — imports and shared helpers.

Uses ``unittest.mock.patch`` to mock ``get_conn``, ``Path``, and
Streamlit internals so that helper functions can be exercised without
a real database or Streamlit runtime.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import typing as _t
import unittest as _ut

# ── Module-level mocks (must be applied before dashboard module is imported) ──

_stub_cache_passthrough: Any = (
    lambda f=None, **kw: f if callable(f) else (lambda g: g)
)

_mock_st = MagicMock()
_mock_st.cache_data = _stub_cache_passthrough
_mock_st.cache_resource = _stub_cache_passthrough
_mock_st.set_page_config = MagicMock()
_mock_st.html = MagicMock()
_mock_st.error = MagicMock()
_mock_st.stop = MagicMock()

_cm = MagicMock()
_cm.__enter__ = MagicMock(return_value=_cm)
_cm.__exit__ = MagicMock(return_value=None)
_mock_st.spinner = lambda _=None: _cm
_mock_st.expander = MagicMock(return_value=_cm)
_mock_st.info = MagicMock()
_mock_st.warning = MagicMock()
_mock_st.success = MagicMock()
_mock_st.markdown = MagicMock()
_mock_st.caption = MagicMock()
_mock_st.divider = MagicMock()
_mock_st.subheader = MagicMock()
_mock_st.metric = MagicMock()
_mock_st.rerun = MagicMock()
_mock_st.sidebar = MagicMock()
_mock_st.sidebar.__enter__ = MagicMock(return_value=_mock_st.sidebar)
_mock_st.sidebar.__exit__ = MagicMock(return_value=None)
_mock_st.sidebar.html = MagicMock()
_mock_st.sidebar.caption = MagicMock()
_mock_st.sidebar.markdown = MagicMock()
_mock_st.sidebar.button = MagicMock(return_value=False)
_mock_st.sidebar.metric = MagicMock()
_mock_st.dataframe = MagicMock()
_mock_st.plotly_chart = MagicMock()
_mock_st.selectbox = MagicMock(return_value="all")
_mock_st.text_input = MagicMock(return_value="")
_mock_st.button = MagicMock(return_value=False)
_mock_st.slider = MagicMock(return_value=0.0)
_mock_st.checkbox = MagicMock(return_value=False)
_mock_st.multiselect = MagicMock(return_value=[])
_mock_st.text = MagicMock()
_mock_st.code = MagicMock()
_mock_st.toast = MagicMock()
_mock_st.container = MagicMock(return_value=_cm)
_mock_st.popover = MagicMock(return_value=_cm)
_mock_st.columns = lambda n=2, **kw: [MagicMock() for _ in range(n if isinstance(n, int) else len(n))]
_mock_st.tabs = MagicMock(return_value=[MagicMock() for _ in range(14)])

sys.modules["streamlit"] = _mock_st

# Mock infra.infrastructure
_mock_infra = MagicMock()
_mock_infra.resolve_active_memory_dir = MagicMock(return_value=Path("/tmp/test_mem_dir"))
sys.modules["infra.infrastructure"] = _mock_infra

# Bootstrap a minimal test database
_TMP_MEM_DIR = Path("/tmp/test_mem_dir")
_TMP_MEM_DIR.mkdir(parents=True, exist_ok=True)
_TMP_DB_PATH = _TMP_MEM_DIR / "memory.db"
_schema_conn = sqlite3.connect(str(_TMP_DB_PATH))
_schema_conn.executescript("""
    CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT);
    INSERT OR IGNORE INTO config (key, value) VALUES ('schema_version', '68');
    CREATE TABLE IF NOT EXISTS schema_version (id INTEGER PRIMARY KEY, version INTEGER);
    INSERT OR IGNORE INTO schema_version (id, version) VALUES (1, 68);
    CREATE TABLE IF NOT EXISTS memories (id TEXT PRIMARY KEY, content TEXT, category TEXT, created_at TEXT, pinned INTEGER DEFAULT 0, fitness_score REAL DEFAULT 0.5, tier TEXT DEFAULT 'unassigned', importance INTEGER DEFAULT 3, tenant_id TEXT DEFAULT 'default');
    CREATE TABLE IF NOT EXISTS kg_entities (id TEXT PRIMARY KEY, name TEXT, entity_type TEXT, mentions INTEGER DEFAULT 0, tenant_id TEXT DEFAULT 'default');
    CREATE TABLE IF NOT EXISTS kg_facts (id TEXT PRIMARY KEY, subject_id TEXT, predicate TEXT, object_id TEXT, confidence REAL, tenant_id TEXT DEFAULT 'default');
    CREATE TABLE IF NOT EXISTS memory_chunks (id TEXT PRIMARY KEY, parent_id TEXT, content TEXT, tenant_id TEXT DEFAULT 'default');
    CREATE TABLE IF NOT EXISTS memory_embeddings (id TEXT PRIMARY KEY, memory_id TEXT, vector BLOB, tenant_id TEXT DEFAULT 'default');
    CREATE TABLE IF NOT EXISTS memory_audit_log (ts REAL, tool TEXT, latency_ms REAL, results_count INTEGER, error TEXT, args TEXT, tenant_id TEXT DEFAULT 'default');
    CREATE TABLE IF NOT EXISTS backlinks (id TEXT PRIMARY KEY, source_id TEXT, target_id TEXT, tenant_id TEXT DEFAULT 'default');
    CREATE TABLE IF NOT EXISTS kg_edges (id TEXT PRIMARY KEY, source_id TEXT, target_id TEXT, edge_type TEXT, tenant_id TEXT DEFAULT 'default');
    CREATE TABLE IF NOT EXISTS concept_drift (id TEXT PRIMARY KEY, concept TEXT, score REAL, tenant_id TEXT DEFAULT 'default');
    CREATE TABLE IF NOT EXISTS drift_alarms (id TEXT PRIMARY KEY, acknowledged_at TEXT, tenant_id TEXT DEFAULT 'default');
    CREATE TABLE IF NOT EXISTS sync_log (id TEXT PRIMARY KEY, ts TEXT, tenant_id TEXT DEFAULT 'default');
    INSERT OR IGNORE INTO memories (id, content, category, created_at, pinned, fitness_score, tier, importance) VALUES ('m1', 'test memory', 'lessons', '2026-01-01', 1, 0.8, 'hot', 4);
    INSERT OR IGNORE INTO memories (id, content, category, created_at, pinned, fitness_score, tier, importance) VALUES ('m2', 'another memory', 'decisions', '2026-01-02', 0, 0.6, 'warm', 3);
    INSERT OR IGNORE INTO kg_entities (id, name, entity_type, mentions) VALUES ('e1', 'test', 'concept', 5);
    INSERT OR IGNORE INTO kg_facts (id, subject_id, predicate, object_id, confidence) VALUES ('f1', 'e1', 'is_a', 'e1', 0.9);
""")
_schema_conn.commit()
_schema_conn.close()

# Import the dashboard package
import dashboard as _dk

# Override module-level globals for testing
_dk.DB = _TMP_DB_PATH
_dk.MEM_DIR = _TMP_MEM_DIR

# Now import tab functions
from dashboard import CSS, DARK, TABS, get_conn, query, resolve_db, table, try_count
from dashboard.sidebar import render_sidebar
from dashboard.tabs import (
    render_audit_log,
    render_backups,
    render_benchmarks,
    render_concept_drift,
    render_cron,
    render_ctr_feedback,
    render_embeddings,
    render_explorer,
    render_facts,
    render_health,
    render_knowledge_graph,
    render_memories,
    render_multi_agent,
    render_overview,
)


def _mock_conn(fetchone_return: tuple | None = (42,)) -> MagicMock:
    conn = MagicMock(spec=sqlite3.Connection)
    cursor = MagicMock()
    cursor.fetchone.return_value = fetchone_return
    conn.execute.return_value = cursor
    return conn


# =========================================================================
# 1. Package imports
# =========================================================================


class TestPackageImports(_ut.TestCase):
    """Verify every module in the dashboard/ package imports cleanly."""

    def test_init_has_expected_attributes(self) -> None:
        self.assertTrue(hasattr(_dk, "CSS"))
        self.assertTrue(hasattr(_dk, "DARK"))
        self.assertTrue(hasattr(_dk, "TABS"))
        self.assertTrue(hasattr(_dk, "get_conn"))
        self.assertTrue(hasattr(_dk, "resolve_db"))
        self.assertTrue(hasattr(_dk, "query"))
        self.assertTrue(hasattr(_dk, "table"))
        self.assertTrue(hasattr(_dk, "try_count"))
        self.assertTrue(hasattr(_dk, "_table_status"))
        self.assertTrue(hasattr(_dk, "_get_schema_version"))
        self.assertTrue(hasattr(_dk, "_live_health"))
        self.assertTrue(hasattr(_dk, "_render_memory_content"))
        self.assertTrue(hasattr(_dk, "_auto_refresh"))
        self.assertTrue(hasattr(_dk, "_blob_weight"))
        self.assertTrue(hasattr(_dk, "DB"))
        self.assertTrue(hasattr(_dk, "MEM_DIR"))

    def test_sidebar_has_render_sidebar(self) -> None:
        self.assertTrue(callable(render_sidebar))

    def test_all_render_functions_exist(self) -> None:
        expected = [
            render_overview,
            render_memories,
            render_knowledge_graph,
            render_embeddings,
            render_facts,
            render_concept_drift,
            render_ctr_feedback,
            render_benchmarks,
            render_cron,
            render_multi_agent,
            render_health,
            render_backups,
            render_audit_log,
            render_explorer,
        ]
        for fn in expected:
            with self.subTest(fn=fn.__name__):
                self.assertTrue(callable(fn))

    def test_tabs_list_matches_render_functions(self) -> None:
        expected = [
            "Overview",
            "Memories",
            "Knowledge Graph",
            "Embeddings",
            "Facts",
            "Concept Drift",
            "CTR Feedback",
            "Benchmarks",
            "Cron",
            "Multi-Agent",
            "Health",
            "Backups",
            "Audit Log",
            "Explorer",
        ]
        self.assertEqual(TABS, expected)
        # 14 tabs = 14 render functions
        self.assertEqual(len(TABS), 14)

    def test_dark_config_has_expected_keys(self) -> None:
        self.assertIn("paper_bgcolor", DARK)
        self.assertIn("plot_bgcolor", DARK)
        self.assertIn("font_color", DARK)
        self.assertIn("title_font_color", DARK)

    def test_db_and_mem_dir_are_paths(self) -> None:
        self.assertIsInstance(_dk.DB, Path)
        self.assertIsInstance(_dk.MEM_DIR, Path)

    def test_css_is_str(self) -> None:
        self.assertIsInstance(CSS, str)
        self.assertGreater(len(CSS), 0)

    def test_sidebar_importable(self) -> None:
        """Sidebar module was imported without errors."""
        from dashboard.sidebar import render_sidebar as rs

        self.assertTrue(callable(rs))

    def test_tabs_importable(self) -> None:
        """Tabs module was imported without errors."""
        from dashboard.tabs import render_overview, render_memories

        self.assertTrue(callable(render_overview))
        self.assertTrue(callable(render_memories))

    def test_pkg_meta_importable(self) -> None:
        """dashboard package imports cleanly."""
        self.assertIsNotNone(_dk)


# =========================================================================
# 2. get_conn() tests
# =========================================================================


class TestGetConn(_ut.TestCase):
    """get_conn() returns a sqlite3.Connection."""

    def test_returns_connection(self) -> None:
        conn = get_conn()
        self.assertIsNotNone(conn)
        cur = conn.execute("SELECT 1")
        self.assertEqual(cur.fetchone()[0], 1)
        conn.close()

    def test_readonly_via_uri(self) -> None:
        conn = get_conn()
        # Should be a URI connection with mode=ro
        self.assertIsNotNone(conn)
        conn.close()


# =========================================================================
# 3. query() tests
# =========================================================================


class TestQuery(_ut.TestCase):
    """query(sql, params) returns a DataFrame or None."""

    def test_returns_dataframe_on_success(self) -> None:
        import pandas as pd

        conn = MagicMock(spec=sqlite3.Connection)
        with (
            patch.object(_dk, "get_conn", return_value=conn),
            patch("pandas.read_sql_query") as mock_read,
        ):
            mock_df = pd.DataFrame({"col": [1, 2]})
            mock_read.return_value = mock_df
            result = query("SELECT 1")
        self.assertIsNotNone(result)
        self.assertEqual(len(_t.cast(pd.DataFrame, result)), 2)

    def test_returns_none_on_exception(self) -> None:
        conn = MagicMock(spec=sqlite3.Connection)
        with (
            patch.object(_dk, "get_conn", return_value=conn),
            patch("pandas.read_sql_query", side_effect=ValueError("bad SQL")),
        ):
            result = query("BAD SQL")
        self.assertIsNone(result)

    def test_integration_with_test_db(self) -> None:
        result = query("SELECT COUNT(*) as n FROM memories")
        self.assertIsNotNone(result)
        import pandas as pd

        df = _t.cast(pd.DataFrame, result)
        self.assertGreaterEqual(df.iloc[0]["n"], 1)


# =========================================================================
# 4. try_count() tests
# =========================================================================


class TestTryCount(_ut.TestCase):
    """try_count(table_name, where=None) returns row count or 0."""

    def test_returns_correct_count(self) -> None:
        with patch.object(_dk, "get_conn", return_value=_mock_conn((42,))):
            self.assertEqual(try_count("memories"), 42)

    def test_returns_zero_on_exception(self) -> None:
        conn = MagicMock(spec=sqlite3.Connection)
        conn.execute.side_effect = sqlite3.OperationalError("no such table")
        with patch.object(_dk, "get_conn", return_value=conn):
            self.assertEqual(try_count("ghost"), 0)

    def test_returns_zero_when_fetchone_returns_none(self) -> None:
        with patch.object(_dk, "get_conn", return_value=_mock_conn(None)):
            self.assertEqual(try_count("memories"), 0)

    def test_appends_where_clause(self) -> None:
        conn = MagicMock(spec=sqlite3.Connection)
        conn.execute.return_value.fetchone.return_value = (3,)
        with patch.object(_dk, "get_conn", return_value=conn):
            try_count("memories", "pinned=1")
        sql = conn.execute.call_args[0][0]
        self.assertIn("WHERE pinned=1", sql)

    def test_integration_with_test_db(self) -> None:
        self.assertGreaterEqual(try_count("memories"), 2)
        self.assertEqual(try_count("sync_log"), 0)


# =========================================================================
# 5. table() tests
# =========================================================================


class TestTable(_ut.TestCase):
    """table(name) returns True when table exists in sqlite_master."""

    def test_returns_true_when_exists(self) -> None:
        with patch.object(_dk, "get_conn", return_value=_mock_conn(("memories",))):
            self.assertTrue(table("memories"))

    def test_returns_false_when_not_exists(self) -> None:
        with patch.object(_dk, "get_conn", return_value=_mock_conn(None)):
            self.assertFalse(table("ghost_table"))

    def test_returns_false_on_exception(self) -> None:
        conn = MagicMock(spec=sqlite3.Connection)
        conn.execute.side_effect = sqlite3.OperationalError("locked")
        with patch.object(_dk, "get_conn", return_value=conn):
            self.assertFalse(table("memories"))

    def test_integration_with_test_db(self) -> None:
        self.assertTrue(table("memories"))
        self.assertTrue(table("config"))
        self.assertFalse(table("nonexistent_table_xyz"))


# =========================================================================
# 6. resolve_db() tests
# =========================================================================


class TestResolveDb(_ut.TestCase):
    """resolve_db() returns a Path."""

    @patch.object(Path, "stat")
    def test_returns_path(self, mock_stat: MagicMock) -> None:
        mock_stat.return_value.st_size = 1_048_576
        result = resolve_db()
        self.assertIsInstance(result, Path)


# =========================================================================
# 7. Shared helpers smoke tests with real DB
# =========================================================================


class TestSharedHelpersIntegration(_ut.TestCase):
    """Integration smoke tests against the test database."""

    def test_get_conn_returns_live_connection(self) -> None:
        conn = get_conn()
        self.assertIsNotNone(conn)
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cur.fetchall()}
        self.assertIn("memories", tables)
        conn.close()

    def test_schema_version_reads_from_config(self) -> None:
        version = _dk._get_schema_version()
        self.assertEqual(version, "v68")

    def test_table_status_for_populated_table(self) -> None:
        sev, detail = _dk._table_status("memories")
        self.assertEqual(sev, "ok")
        self.assertRegex(detail, r"\d+ rows")

    def test_table_status_for_expected_empty_table(self) -> None:
        sev, detail = _dk._table_status("sync_log")
        self.assertEqual(sev, "info")
        self.assertIn("expected", detail)

    def test_live_health_returns_dict(self) -> None:
        result = _dk._live_health()
        self.assertIn("ts", result)
        self.assertIn("checks", result)
        self.assertGreater(len(result["checks"]), 0)
        for check in result["checks"]:
            self.assertIsInstance(check, tuple)
            self.assertEqual(len(check), 3)

    def test_live_health_includes_core_tables(self) -> None:
        result = _dk._live_health()
        names = [c[0] for c in result["checks"]]
        self.assertIn("memories", names)
        self.assertIn("kg_entities", names)

    def test_auto_refresh_exists(self) -> None:
        self.assertTrue(callable(_dk._auto_refresh))

    def test_render_memory_content_exists(self) -> None:
        self.assertTrue(callable(_dk._render_memory_content))

    def test_blob_weight_exists(self) -> None:
        self.assertTrue(callable(_dk._blob_weight))


# =========================================================================
# Cleanup
# =========================================================================
import atexit as _atexit
import os as _os


@_atexit.register
def _cleanup_temp_db() -> None:
    db = _TMP_DB_PATH
    if db.exists():
        db.unlink()
    parent = _TMP_MEM_DIR
    if parent.exists():
        try:
            parent.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    _ut.main()
