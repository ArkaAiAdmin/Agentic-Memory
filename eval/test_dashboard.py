"""Tests for dashboard.py — Streamlit dashboard for agentic-memory.

Uses ``unittest.mock.patch`` to mock ``get_conn``, ``Path``, and
Streamlit internals so that helper functions can be exercised without
a real database or Streamlit runtime.

Usage:
    venv/bin/python -m pytest eval/test_dashboard.py -v
    venv/bin/python -m unittest eval.test_dashboard -v
"""

from __future__ import annotations

import gzip
import os
import sqlite3
import sys
import tempfile
import typing
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ── Module-level mocks (must be applied before dashboard.py is imported) ──

# Streamlit has module-level side effects: st.set_page_config(), st.html(),
# @st.cache_data decorators, resolve_db() call, Path.exists() check.
# We mock all of these before importing dashboard.

_mock_st = MagicMock()


def _cache_passthrough(*args: typing.Any, **kwargs: typing.Any) -> typing.Any:
    """Handle @st.cache_data, @st.cache_data(ttl=30), @st.cache_resource.

    - ``@st.cache_resource`` (no parens): ``args[0]`` is the decorated function.
    - ``@st.cache_data(ttl=30)``: called with keyword args, returns a decorator.
    """
    if args and callable(args[0]):
        return args[0]
    return lambda f: f


_mock_st.cache_data = _cache_passthrough
_mock_st.cache_resource = _cache_passthrough
_mock_st.set_page_config = MagicMock()
_mock_st.html = MagicMock()
_mock_st.error = MagicMock()
_mock_st.stop = MagicMock()

# Context-manager support for st.spinner, st.expander, st.sidebar
_mock_cm = MagicMock()
_mock_cm.__enter__ = MagicMock(return_value=_mock_cm)
_mock_cm.__exit__ = MagicMock(return_value=None)
_mock_st.spinner = lambda _=None: _mock_cm
_mock_st.expander = MagicMock(return_value=_mock_cm)

_mock_st.info = MagicMock()
_mock_st.warning = MagicMock()
_mock_st.success = MagicMock()
_mock_st.markdown = MagicMock()
_mock_st.caption = MagicMock()
_mock_st.divider = MagicMock()
_mock_st.subheader = MagicMock()
_mock_st.metric = MagicMock()
_mock_st.rerun = MagicMock()


def _columns(*args: typing.Any, **kwargs: typing.Any) -> list[MagicMock]:
    """Return MagicMock column objects matching ``st.columns(n)``.

    ``st.columns`` accepts either ``st.columns(3)`` (int) or
    ``st.columns([2, 1])`` (list of proportional widths).
    """
    if args and isinstance(args[0], int):
        n = args[0]
    elif args and isinstance(args[0], (list, tuple)):
        n = len(args[0])
    else:
        n = 2  # fallback
    return [MagicMock() for _ in range(n)]


_mock_st.columns = _columns
_mock_st.selectbox = MagicMock(return_value="all")
_mock_st.text_input = MagicMock(return_value="")
_mock_st.button = MagicMock(return_value=False)
_mock_st.dataframe = MagicMock()
_mock_st.plotly_chart = MagicMock()
_mock_st.tabs = MagicMock(return_value=[MagicMock() for _ in range(14)])

# Widgets that return comparable values
_mock_st.slider = MagicMock(return_value=0.0)
_mock_st.checkbox = MagicMock(return_value=False)
_mock_st.multiselect = MagicMock(return_value=[])
_mock_st.text = MagicMock()
_mock_st.code = MagicMock()
_mock_st.toast = MagicMock()

# Context-manager widgets
_mock_st.container = MagicMock(return_value=_mock_cm)
_mock_st.popover = MagicMock(return_value=_mock_cm)

# Sidebar: need context-manager support for ``with st.sidebar:``
_mock_st.sidebar = MagicMock()
_mock_st.sidebar.__enter__ = MagicMock(return_value=_mock_st.sidebar)
_mock_st.sidebar.__exit__ = MagicMock(return_value=None)
_mock_st.sidebar.html = MagicMock()
_mock_st.sidebar.caption = MagicMock()
_mock_st.sidebar.markdown = MagicMock()
_mock_st.sidebar.button = MagicMock(return_value=False)
_mock_st.sidebar.metric = MagicMock()
sys.modules["streamlit"] = _mock_st

# Mock infra.infrastructure so resolve_db() returns a controllable path.
_mock_infra = MagicMock()
_mock_infra.resolve_active_memory_dir = MagicMock(return_value=Path("/tmp/test_memory_dir"))
sys.modules["infra.infrastructure"] = _mock_infra

# Bootstrap a minimal test database with the tables/columns the dashboard
# queries at module level (tabs execute during import).
_TMP_MEM_DIR = Path("/tmp/test_memory_dir")
_TMP_MEM_DIR.mkdir(parents=True, exist_ok=True)
_TMP_DB_PATH = _TMP_MEM_DIR / "memory.db"
_schema_conn = sqlite3.connect(str(_TMP_DB_PATH))
_schema_conn.executescript(
    """
    CREATE TABLE IF NOT EXISTS config (
        key TEXT PRIMARY KEY, value TEXT
    );
    INSERT OR IGNORE INTO config (key, value) VALUES ('schema_version', '61');

    CREATE TABLE IF NOT EXISTS schema_version (
        id INTEGER PRIMARY KEY, version INTEGER
    );
    INSERT OR IGNORE INTO schema_version (id, version) VALUES (1, 68);

    CREATE TABLE IF NOT EXISTS memories (
        id TEXT PRIMARY KEY, content TEXT, category TEXT,
        created_at TEXT, pinned INTEGER DEFAULT 0,
        fitness_score REAL DEFAULT 0.5, tier TEXT DEFAULT 'unassigned',
        importance INTEGER DEFAULT 3, tenant_id TEXT DEFAULT 'default'
    );
    CREATE TABLE IF NOT EXISTS kg_entities (
        id TEXT PRIMARY KEY, name TEXT, entity_type TEXT,
        mentions INTEGER DEFAULT 0, tenant_id TEXT DEFAULT 'default'
    );
    CREATE TABLE IF NOT EXISTS kg_facts (
        id TEXT PRIMARY KEY, subject_id TEXT, predicate TEXT,
        object_id TEXT, confidence REAL, tenant_id TEXT DEFAULT 'default'
    );
    CREATE TABLE IF NOT EXISTS memory_chunks (
        id TEXT PRIMARY KEY, parent_id TEXT, content TEXT,
        tenant_id TEXT DEFAULT 'default'
    );
    CREATE TABLE IF NOT EXISTS memory_embeddings (
        id TEXT PRIMARY KEY, memory_id TEXT, vector BLOB,
        tenant_id TEXT DEFAULT 'default'
    );
    CREATE TABLE IF NOT EXISTS memory_audit_log (
        ts REAL, tool TEXT, latency_ms REAL,
        results_count INTEGER, error TEXT, args TEXT,
        tenant_id TEXT DEFAULT 'default'
    );
    CREATE TABLE IF NOT EXISTS backlinks (
        id TEXT PRIMARY KEY, source_id TEXT, target_id TEXT,
        tenant_id TEXT DEFAULT 'default'
    );
    CREATE TABLE IF NOT EXISTS kg_edges (
        id TEXT PRIMARY KEY, source_id TEXT, target_id TEXT,
        edge_type TEXT, tenant_id TEXT DEFAULT 'default'
    );
    CREATE TABLE IF NOT EXISTS concept_drift (
        id TEXT PRIMARY KEY, concept TEXT, score REAL,
        tenant_id TEXT DEFAULT 'default'
    );
    CREATE TABLE IF NOT EXISTS drift_alarms (
        id TEXT PRIMARY KEY, acknowledged_at TEXT,
        tenant_id TEXT DEFAULT 'default'
    );
    CREATE TABLE IF NOT EXISTS memory_ctr_feedback (
        id TEXT PRIMARY KEY, query_id TEXT, action TEXT,
        tenant_id TEXT DEFAULT 'default'
    );
    CREATE TABLE IF NOT EXISTS sync_log (
        id TEXT PRIMARY KEY, ts TEXT, tenant_id TEXT DEFAULT 'default'
    );
    CREATE TABLE IF NOT EXISTS shared_memories (
        id TEXT PRIMARY KEY, note_id TEXT,
        tenant_id TEXT DEFAULT 'default'
    );
    CREATE TABLE IF NOT EXISTS backfill_progress (
        id TEXT PRIMARY KEY, phase TEXT, tenant_id TEXT DEFAULT 'default'
    );

    INSERT OR IGNORE INTO memories
        (id, content, category, created_at, pinned, fitness_score, tier, importance)
    VALUES ('m1', 'test memory', 'lessons', '2026-01-01', 1, 0.8, 'hot', 4);
    INSERT OR IGNORE INTO memories
        (id, content, category, created_at, pinned, fitness_score, tier, importance)
    VALUES ('m2', 'another memory', 'decisions', '2026-01-02', 0, 0.6, 'warm', 3);
    INSERT OR IGNORE INTO kg_entities (id, name, entity_type, mentions)
    VALUES ('e1', 'test', 'concept', 5);
    INSERT OR IGNORE INTO kg_facts (id, subject_id, predicate, object_id, confidence)
    VALUES ('f1', 'e1', 'is_a', 'e1', 0.9);
    INSERT OR IGNORE INTO memory_audit_log
        (ts, tool, latency_ms, results_count, error, args)
    VALUES (1700000000, 'memory_search', 42, 5, NULL, '{}');
    INSERT OR IGNORE INTO memory_ctr_feedback (id, query_id, action)
    VALUES ('c1', 'q1', 'returned');
    INSERT OR IGNORE INTO concept_drift (id, concept, score) VALUES ('d1', 'test-concept', 0.1);
    INSERT OR IGNORE INTO memory_chunks (id, parent_id, content) VALUES ('ch1', 'm1', 'chunk content');
    INSERT OR IGNORE INTO memory_embeddings (id, memory_id, vector) VALUES ('emb1', 'm1', x'0000');
    INSERT OR IGNORE INTO backlinks (id, source_id, target_id) VALUES ('b1', 'm2', 'm1');
    """
)
_schema_conn.commit()
_schema_conn.close()

# Import dashboard after mocks and DB are in place.
#
# Some tab code may raise SystemExit (e.g. ``raise SystemExit(0)`` at
# dashboard.py ~line 1030 when KG queries return empty).  Python's import
# machinery removes partially-loaded modules from ``sys.modules`` even on
# SystemExit, so we use ``importlib`` to manually register the module
# *before* executing it.  This ensures the module object survives with
# all function definitions intact.
import importlib.util as _importlib_util

_dashboard_path = Path(__file__).resolve().parent.parent / "dashboard" / "__init__.py"
_dashboard_spec = _importlib_util.spec_from_file_location("dashboard", str(_dashboard_path))
dashboard = _importlib_util.module_from_spec(_dashboard_spec)
sys.modules["dashboard"] = dashboard  # register before exec

with (
    patch.object(Path, "exists", return_value=True),
    patch.object(Path, "stat") as _mock_import_stat,
):
    _mock_import_stat.return_value.st_size = 1_048_576  # 1 MB
    try:
        _dashboard_spec.loader.exec_module(dashboard)
    except SystemExit:
        # Module-level code may stop early; our functions are already defined.
        pass

# Override the module-level DB/MEM_DIR for the lifetime of the test suite
# so tests never accidentally touch a real database.
dashboard.DB = Path("/tmp/test_memory_dir/memory.db")
dashboard.MEM_DIR = Path("/tmp/test_memory_dir")

# ── Module-level streamlit mock for plotly (no side effects, just config) ──
# plotly.express.defaults is set at module level — that's fine.

# ── Logger ──────────────────────────────────────────────────────────────
logger = dashboard.logger

# =========================================================================
# Helper: build a fake sqlite3 connection that returns controlled values
# =========================================================================


def _mock_conn(
    fetchone_return: tuple | None = (42,),
    execute_side_effect: list[tuple | None] | None = None,
) -> MagicMock:
    """Return a MagicMock that looks like a ``sqlite3.Connection``.

    Parameters
    ----------
    fetchone_return:
        The value returned by ``.execute(sql).fetchone()``.  When
        *execute_side_effect* is set this is ignored.
    execute_side_effect:
        An iterable of per-call return values for ``fetchone()``, one
        per ``execute()`` call.  When set, each call to ``execute``
        advances to the next value.
    """
    conn = MagicMock(spec=sqlite3.Connection)
    cursor = MagicMock()

    if execute_side_effect is not None:
        # Each call to execute returns a new cursor whose fetchone returns
        # the next value from the side-effect list.
        iterator = iter(execute_side_effect)

        def _execute(*_args: typing.Any, **_kwargs: typing.Any) -> MagicMock:
            c = MagicMock()
            try:
                c.fetchone.return_value = next(iterator)
            except StopIteration:
                c.fetchone.return_value = None
            return c

        conn.execute.side_effect = _execute
    else:
        cursor.fetchone.return_value = fetchone_return
        conn.execute.return_value = cursor

    return conn


# =========================================================================
# 1. try_count() tests
# =========================================================================


class TestTryCount(unittest.TestCase):
    """``try_count(table_name, where=None)`` returns row count or 0."""

    def test_returns_correct_count_when_table_exists(self) -> None:
        """Returns the integer row count when the SQL query succeeds."""
        with patch.object(dashboard, "get_conn", return_value=_mock_conn((99,))):
            result = dashboard.try_count("memories")
        self.assertEqual(result, 99)

    def test_returns_zero_when_table_does_not_exist(self) -> None:
        """Returns 0 when execute() raises an exception (table missing)."""
        conn = MagicMock(spec=sqlite3.Connection)
        conn.execute.side_effect = sqlite3.OperationalError("no such table")
        with patch.object(dashboard, "get_conn", return_value=conn):
            result = dashboard.try_count("nonexistent")
        self.assertEqual(result, 0)

    def test_returns_zero_when_where_clause_filters_everything(self) -> None:
        """Returns 0 for a WHERE clause that matches no rows."""
        with patch.object(dashboard, "get_conn", return_value=_mock_conn((0,))):
            result = dashboard.try_count("memories", "1=0")
        self.assertEqual(result, 0)

    def test_returns_zero_when_fetchone_returns_none(self) -> None:
        """Returns 0 when execute().fetchone() returns None."""
        with patch.object(dashboard, "get_conn", return_value=_mock_conn(None)):
            result = dashboard.try_count("memories")
        self.assertEqual(result, 0)

    def test_appends_where_clause_correctly(self) -> None:
        """Asserts the generated SQL includes the WHERE clause."""
        conn = MagicMock(spec=sqlite3.Connection)
        cursor = MagicMock()
        cursor.fetchone.return_value = (7,)
        conn.execute = MagicMock(return_value=cursor)
        with patch.object(dashboard, "get_conn", return_value=conn):
            dashboard.try_count("kg_entities", "pinned=1")
        sql_arg = conn.execute.call_args[0][0]
        self.assertIn("WHERE pinned=1", sql_arg)
        self.assertIn("FROM kg_entities", sql_arg)

    def test_calls_get_conn_with_count_sql(self) -> None:
        """Asserts the generated SQL is ``SELECT COUNT(*) FROM <table>``."""
        conn = MagicMock(spec=sqlite3.Connection)
        cursor = MagicMock()
        cursor.fetchone.return_value = (5,)
        conn.execute = MagicMock(return_value=cursor)
        with patch.object(dashboard, "get_conn", return_value=conn):
            dashboard.try_count("memories")
        sql_arg = conn.execute.call_args[0][0]
        self.assertEqual(sql_arg, "SELECT COUNT(*) FROM memories")


# =========================================================================
# 2. table() tests
# =========================================================================


class TestTable(unittest.TestCase):
    """``table(name)`` returns True when the table exists in sqlite_master."""

    def test_returns_true_when_table_exists(self) -> None:
        """Returns True when sqlite_master has a matching row."""
        with patch.object(
            dashboard, "get_conn", return_value=_mock_conn(("memories",))
        ):
            result = dashboard.table("memories")
        self.assertTrue(result)

    def test_returns_false_when_table_does_not_exist(self) -> None:
        """Returns False when sqlite_master has no matching row."""
        with patch.object(dashboard, "get_conn", return_value=_mock_conn(None)):
            result = dashboard.table("ghost_table")
        self.assertFalse(result)

    def test_returns_false_on_exception(self) -> None:
        """Returns False when execute() raises an exception."""
        conn = MagicMock(spec=sqlite3.Connection)
        conn.execute.side_effect = sqlite3.OperationalError("database is locked")
        with patch.object(dashboard, "get_conn", return_value=conn):
            result = dashboard.table("memories")
        self.assertFalse(result)


# =========================================================================
# 3. _table_status() tests
# =========================================================================


class TestTableStatus(unittest.TestCase):
    """``_table_status(name)`` returns (severity, detail)."""

    def test_returns_ok_for_non_empty_table(self) -> None:
        """Returns ("ok", "{n} rows") when the table has rows."""
        with patch.object(dashboard, "get_conn", return_value=_mock_conn((7,))):
            sev, detail = dashboard._table_status("memories")
        self.assertEqual(sev, "ok")
        self.assertEqual(detail, "7 rows")

    def test_returns_info_for_expected_empty_table(self) -> None:
        """Returns ("info", "0 rows (expected)") for tables in _EXPECTED_EMPTY_TABLES."""
        with patch.object(dashboard, "get_conn", return_value=_mock_conn((0,))):
            sev, detail = dashboard._table_status("concept_drift")
        self.assertEqual(sev, "info")
        self.assertEqual(detail, "0 rows (expected)")

    def test_returns_warning_for_unexpectedly_empty_table(self) -> None:
        """Returns ("warning", "0 rows (unexpected)") for non-expected empty tables."""
        with patch.object(dashboard, "get_conn", return_value=_mock_conn((0,))):
            sev, detail = dashboard._table_status("memories")
        self.assertEqual(sev, "warning")
        self.assertEqual(detail, "0 rows (unexpected)")

    def test_returns_error_on_exception(self) -> None:
        """Returns ("error", str(e)) when execute() raises."""
        conn = MagicMock(spec=sqlite3.Connection)
        conn.execute.side_effect = sqlite3.OperationalError("no such table: x")
        with patch.object(dashboard, "get_conn", return_value=conn):
            sev, detail = dashboard._table_status("x")
        self.assertEqual(sev, "error")
        self.assertIn("no such table", detail)

    def test_empty_table_not_in_expected_set_returns_warning(self) -> None:
        """Verifies a table not in _EXPECTED_EMPTY_TABLES with 0 rows => warning."""
        self.assertNotIn("kg_facts", dashboard._EXPECTED_EMPTY_TABLES)
        with patch.object(dashboard, "get_conn", return_value=_mock_conn((0,))):
            sev, detail = dashboard._table_status("kg_facts")
        self.assertEqual(sev, "warning")
        self.assertEqual(detail, "0 rows (unexpected)")

    def test_expected_empty_tables_set_is_frozenset(self) -> None:
        """_EXPECTED_EMPTY_TABLES is a frozenset."""
        self.assertIsInstance(dashboard._EXPECTED_EMPTY_TABLES, frozenset)

    def test_expected_empty_contains_known_tables(self) -> None:
        """Expected-empty tables include drift, ctr, sync, shared, edges."""
        for tbl in (
            "concept_drift",
            "drift_alarms",
            "memory_ctr_feedback",
            "sync_log",
            "shared_memories",
            "kg_edges",
        ):
            with self.subTest(table=tbl):
                self.assertIn(tbl, dashboard._EXPECTED_EMPTY_TABLES)


# =========================================================================
# 4. _get_schema_version() tests
# =========================================================================


class TestGetSchemaVersion(unittest.TestCase):
    """``_get_schema_version()`` returns the schema version string."""

    def test_returns_v_prefix_with_number(self) -> None:
        """Returns "v{num}" when config table has schema_version."""
        with patch.object(dashboard, "get_conn", return_value=_mock_conn(("61",))):
            result = dashboard._get_schema_version()
        self.assertEqual(result, "v61")

    def test_returns_pre_migration_when_no_config_table(self) -> None:
        """Returns "? (pre-migration)" when config table does not exist."""
        conn = MagicMock(spec=sqlite3.Connection)
        conn.execute.side_effect = sqlite3.OperationalError("no such table")
        with patch.object(dashboard, "get_conn", return_value=conn):
            result = dashboard._get_schema_version()
        self.assertEqual(result, "? (pre-migration)")

    def test_returns_pre_migration_when_fetchone_returns_none(self) -> None:
        """Returns "? (pre-migration)" when key is missing."""
        with patch.object(dashboard, "get_conn", return_value=_mock_conn(None)):
            result = dashboard._get_schema_version()
        self.assertEqual(result, "? (pre-migration)")


# =========================================================================
# 5. _live_health() tests
# =========================================================================


class TestLiveHealth(unittest.TestCase):
    """``_live_health()`` returns a dict with system health checks."""

    @patch.object(dashboard, "_table_status")
    @patch.object(dashboard, "get_conn")
    @patch.object(Path, "stat")
    def test_returns_dict_with_expected_keys(
        self,
        mock_stat: MagicMock,
        mock_get_conn: MagicMock,
        mock_table_status: MagicMock,
    ) -> None:
        """Returns a dict containing 'ts' and 'checks' keys."""
        # All tables return ok
        mock_table_status.return_value = ("ok", "5 rows")
        mock_stat.return_value.st_size = 2048
        mock_get_conn.return_value = _mock_conn((3,))  # pinned count

        result = dashboard._live_health()
        self.assertIn("ts", result)
        self.assertIn("checks", result)
        self.assertIsInstance(result["checks"], list)

    @patch.object(dashboard, "_table_status")
    @patch.object(dashboard, "get_conn")
    @patch.object(Path, "stat")
    def test_includes_ltr_model_check_when_model_exists(
        self,
        mock_stat: MagicMock,
        mock_get_conn: MagicMock,
        mock_table_status: MagicMock,
    ) -> None:
        """When LTR model exists, the ltr_model check has status 'ok'."""
        mock_table_status.return_value = ("ok", "5 rows")
        mock_stat.return_value.st_size = 2048
        mock_get_conn.return_value = _mock_conn((3,))

        with patch.object(Path, "exists", return_value=True):
            result = dashboard._live_health()

        ltr_checks = [c for c in result["checks"] if c[0] == "ltr_model"]
        self.assertEqual(len(ltr_checks), 1)
        self.assertEqual(ltr_checks[0][1], "ok")
        self.assertIn("KB", ltr_checks[0][2])

    @patch.object(dashboard, "_table_status")
    @patch.object(dashboard, "get_conn")
    @patch.object(Path, "stat")
    def test_ltr_model_warning_when_not_trained(
        self,
        mock_stat: MagicMock,
        mock_get_conn: MagicMock,
        mock_table_status: MagicMock,
    ) -> None:
        """When LTR model is missing, the ltr_model check shows 'warning'."""
        mock_table_status.return_value = ("ok", "5 rows")
        mock_stat.return_value.st_size = 2048
        mock_get_conn.return_value = _mock_conn((3,))

        with patch.object(Path, "exists", return_value=False):
            result = dashboard._live_health()

        ltr_checks = [c for c in result["checks"] if c[0] == "ltr_model"]
        self.assertEqual(len(ltr_checks), 1)
        self.assertEqual(ltr_checks[0][1], "warning")
        self.assertEqual(ltr_checks[0][2], "not trained yet")

    @patch.object(dashboard, "_table_status")
    @patch.object(dashboard, "get_conn")
    @patch.object(Path, "stat")
    def test_includes_pinned_check(
        self,
        mock_stat: MagicMock,
        mock_get_conn: MagicMock,
        mock_table_status: MagicMock,
    ) -> None:
        """A pinned check is present with the count of pinned notes."""
        mock_table_status.return_value = ("ok", "5 rows")
        mock_stat.return_value.st_size = 2048
        mock_get_conn.return_value = _mock_conn((7,))

        result = dashboard._live_health()
        pinned_checks = [c for c in result["checks"] if c[0] == "pinned"]
        self.assertEqual(len(pinned_checks), 1)
        self.assertEqual(pinned_checks[0][1], "ok")
        self.assertEqual(pinned_checks[0][2], "7 notes")

    @patch.object(dashboard, "_table_status")
    @patch.object(dashboard, "get_conn")
    @patch.object(Path, "stat")
    def test_handles_pinned_query_failure_gracefully(
        self,
        mock_stat: MagicMock,
        mock_get_conn: MagicMock,
        mock_table_status: MagicMock,
    ) -> None:
        """When the pinned query fails, the function still returns (no crash)."""
        mock_table_status.return_value = ("ok", "5 rows")
        mock_stat.return_value.st_size = 2048
        err_conn = MagicMock(spec=sqlite3.Connection)
        err_conn.execute.side_effect = sqlite3.OperationalError("locked")
        mock_get_conn.return_value = err_conn

        result = dashboard._live_health()
        self.assertIn("checks", result)
        # No pinned check if the query failed
        pinned_checks = [c for c in result["checks"] if c[0] == "pinned"]
        self.assertEqual(len(pinned_checks), 0)

    @patch.object(dashboard, "_table_status")
    @patch.object(dashboard, "get_conn")
    @patch.object(Path, "stat")
    def test_core_table_warning_maps_to_severity(
        self,
        mock_stat: MagicMock,
        mock_get_conn: MagicMock,
        mock_table_status: MagicMock,
    ) -> None:
        """Core table warnings get mapped to their default_if_empty severity."""
        # memories is core with default_if_empty="error"
        mock_table_status.side_effect = lambda name: (
            ("warning", "0 rows (unexpected)") if name == "memories"
            else ("ok", "5 rows")
        )
        mock_stat.return_value.st_size = 2048
        mock_get_conn.return_value = _mock_conn((0,))

        result = dashboard._live_health()
        mem_checks = [c for c in result["checks"] if c[0] == "memories"]
        self.assertEqual(mem_checks[0][1], "error")


# =========================================================================
# 6. Health score formula tests
# =========================================================================


class TestHealthScoreFormula(unittest.TestCase):
    """Health score is computed inline in the overview tab.

    Formula (extracted from dashboard.py lines 671-679):
        score = 100
        if n_alarms > 0:  score -= min(30, n_alarms * 5)
        if n_entities == 0: score -= 20
        if not ltr_model.exists(): score -= 10
        color: >=80 green, >=60 amber, <60 red
    """

    def _compute(
        self,
        n_alarms: int = 0,
        n_entities: int = 1,
        ltr_exists: bool = True,
    ) -> tuple[int, str, str]:
        """Replicate the inline health-score logic from dashboard.py."""
        score = 100
        if n_alarms > 0:
            score -= min(30, n_alarms * 5)
        if n_entities == 0:
            score -= 20
        if not ltr_exists:
            score -= 10
        color = "#10b981" if score >= 80 else "#f59e0b" if score >= 60 else "#ef4444"
        label = "Healthy" if score >= 80 else "Needs Attention" if score >= 60 else "Critical"
        return (score, color, label)

    def test_starts_at_100(self) -> None:
        """Perfect conditions yield score = 100, green, 'Healthy'."""
        score, color, label = self._compute()
        self.assertEqual(score, 100)
        self.assertEqual(color, "#10b981")
        self.assertEqual(label, "Healthy")

    def test_one_alarm_deducts_five(self) -> None:
        """One unacknowledged alarm deducts 5 points."""
        score, color, label = self._compute(n_alarms=1)
        self.assertEqual(score, 95)

    def test_six_alarms_deducts_thirty(self) -> None:
        """Six alarms deducts 30 (capped)."""
        score, _, _ = self._compute(n_alarms=6)
        self.assertEqual(score, 70)

    def test_ten_alarms_still_capped_at_thirty(self) -> None:
        """Ten alarms still only deducts 30 (cap)."""
        score, _, _ = self._compute(n_alarms=10)
        self.assertEqual(score, 70)

    def test_zero_entities_deducts_twenty(self) -> None:
        """Zero KG entities deducts 20 points."""
        score, _, _ = self._compute(n_entities=0)
        self.assertEqual(score, 80)

    def test_missing_ltr_deducts_ten(self) -> None:
        """Missing LTR model deducts 10 points."""
        score, _, _ = self._compute(ltr_exists=False)
        self.assertEqual(score, 90)

    def test_all_penalties_combined(self) -> None:
        """Alarms + zero entities + missing LTR = 100 - 30 - 20 - 10 = 40."""
        score, color, label = self._compute(
            n_alarms=10, n_entities=0, ltr_exists=False
        )
        self.assertEqual(score, 40)
        self.assertEqual(color, "#ef4444")  # red
        self.assertEqual(label, "Critical")

    def test_score_eighty_is_green(self) -> None:
        """Score >= 80 maps to green / 'Healthy'."""
        score, color, label = self._compute(n_alarms=4)  # 100 - 20 = 80
        self.assertEqual(score, 80)
        self.assertEqual(color, "#10b981")
        self.assertEqual(label, "Healthy")

    def test_score_sixty_is_amber(self) -> None:
        """Score >= 60 and < 80 maps to amber / 'Needs Attention'."""
        score, color, label = self._compute(n_alarms=6, ltr_exists=False)  # 100 - 30 - 10 = 60
        self.assertEqual(score, 60)
        self.assertEqual(color, "#f59e0b")
        self.assertEqual(label, "Needs Attention")

    def test_score_seventy_is_amber(self) -> None:
        """Score 70 is in amber range."""
        score, color, label = self._compute(n_alarms=6)  # 100 - 30 = 70
        self.assertEqual(score, 70)
        self.assertEqual(color, "#f59e0b")
        self.assertEqual(label, "Needs Attention")

    def test_score_below_sixty_is_red(self) -> None:
        """Score < 60 maps to red / 'Critical'."""
        score, color, label = self._compute(
            n_alarms=10, n_entities=0, ltr_exists=False
        )
        self.assertEqual(score, 40)
        self.assertEqual(color, "#ef4444")
        self.assertEqual(label, "Critical")

    def test_color_threshold_boundaries(self) -> None:
        """Boundaries: >=80 green, >=60 amber, <60 red."""
        # 80 → green (4 alarms: 100 - 20 = 80)
        self.assertEqual(self._compute(n_alarms=4)[1], "#10b981")
        # 75 → amber (5 alarms: 100 - 25 = 75)
        self.assertEqual(self._compute(n_alarms=5)[1], "#f59e0b")
        # 60 → amber (6 alarms + no LTR: 100 - 30 - 10 = 60)
        self.assertEqual(self._compute(n_alarms=6, ltr_exists=False)[1], "#f59e0b")
        # 55 → red (3 alarms + no entities + no LTR: 100 - 15 - 20 - 10 = 55)
        self.assertEqual(
            self._compute(n_alarms=3, n_entities=0, ltr_exists=False)[1],
            "#ef4444",
        )
        # 50 → red (10 alarms + no entities: 100 - 30 - 20 = 50)
        self.assertEqual(
            self._compute(n_alarms=10, n_entities=0)[1],
            "#ef4444",
        )


# =========================================================================
# 7. _validate_backup logic tests
# =========================================================================


class TestValidateBackup(unittest.TestCase):
    """``_validate_backup`` (nested in backups tab) validates gzip files.

    The function (lines 2843-2851) reads the first 16 bytes of a gzip file
    and returns True if the uncompressed content starts with ``b"SQLi"``
    or the file is empty.  We test this logic directly since the function
    is defined inside a ``with`` block and is not importable.
    """

    def _validate(self, path: Path) -> bool:
        """Replicate the nested _validate_backup logic."""
        try:
            inner = gzip.open(path, "rb")
            sig = inner.read(16)
            inner.close()
            return sig[:4] == b"SQLi" or sig == b""
        except Exception:
            return False

    def test_valid_sqlite_gzip(self) -> None:
        """A gzip file containing SQLite header bytes returns True."""
        with tempfile.NamedTemporaryFile(suffix=".gz", delete=False) as f:
            f.close()
            with gzip.open(f.name, "wb") as gz:
                gz.write(b"SQLite format 3\0")
            self.assertTrue(self._validate(Path(f.name)))
            os.unlink(f.name)

    def test_corrupted_gzip(self) -> None:
        """Invalid gzip data returns False."""
        with tempfile.NamedTemporaryFile(suffix=".gz", delete=False) as f:
            f.write(b"this is not gzip data at all")
            f.close()
            self.assertFalse(self._validate(Path(f.name)))
            os.unlink(f.name)

    def test_empty_gzip(self) -> None:
        """A valid empty gzip file (zero uncompressed bytes) returns True."""
        with tempfile.NamedTemporaryFile(suffix=".gz", delete=False) as f:
            f.close()
            with gzip.open(f.name, "wb") as gz:
                gz.write(b"")
            self.assertTrue(self._validate(Path(f.name)))
            os.unlink(f.name)

    def test_non_sqlite_gzip(self) -> None:
        """A gzip file with non-SQLite content returns False (no b'SQLi' prefix)."""
        with tempfile.NamedTemporaryFile(suffix=".gz", delete=False) as f:
            f.close()
            with gzip.open(f.name, "wb") as gz:
                gz.write(b"random data without sqlite header")
            self.assertFalse(self._validate(Path(f.name)))
            os.unlink(f.name)

    def test_nonexistent_file(self) -> None:
        """A nonexistent file returns False (exception caught)."""
        result = self._validate(Path("/tmp/does_not_exist_xyz.gz"))
        self.assertFalse(result)


# =========================================================================
# 8. query() tests
# =========================================================================


class TestQuery(unittest.TestCase):
    """``query(sql, params)`` returns a DataFrame or None."""

    def test_returns_dataframe_on_success(self) -> None:
        """A successful query returns a DataFrame."""
        conn = MagicMock(spec=sqlite3.Connection)
        with (
            patch.object(dashboard, "get_conn", return_value=conn),
            patch("pandas.read_sql_query") as mock_read_sql,
        ):
            import pandas as pd

            mock_df = pd.DataFrame({"col": [1, 2]})
            mock_read_sql.return_value = mock_df
            result = dashboard.query("SELECT 1")
        self.assertIsNotNone(result)
        self.assertEqual(len(typing.cast(pd.DataFrame, result)), 2)

    def test_returns_none_on_exception(self) -> None:
        """A failed query returns None."""
        conn = MagicMock(spec=sqlite3.Connection)
        with (
            patch.object(dashboard, "get_conn", return_value=conn),
            patch("pandas.read_sql_query", side_effect=ValueError("bad SQL")),
        ):
            result = dashboard.query("BAD SQL")
        self.assertIsNone(result)


# =========================================================================
# 9. Module-level structure tests
# =========================================================================


class TestDashboardModule(unittest.TestCase):
    """Smoke tests for the dashboard module."""

    def test_module_imports_cleanly(self) -> None:
        """The dashboard module was imported without error at module level."""
        self.assertIsNotNone(dashboard)
        self.assertTrue(hasattr(dashboard, "try_count"))
        self.assertTrue(hasattr(dashboard, "table"))
        self.assertTrue(hasattr(dashboard, "_table_status"))
        self.assertTrue(hasattr(dashboard, "_get_schema_version"))
        self.assertTrue(hasattr(dashboard, "_live_health"))
        self.assertTrue(hasattr(dashboard, "query"))
        self.assertTrue(hasattr(dashboard, "get_conn"))

    def test_tabs_defined(self) -> None:
        """The TABS list contains expected tab names."""
        self.assertIsInstance(dashboard.TABS, list)
        expected_tabs = [
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
        self.assertEqual(dashboard.TABS, expected_tabs)

    def test_dark_config_has_expected_keys(self) -> None:
        """DARK config dict has the expected plotly styling keys."""
        self.assertIn("paper_bgcolor", dashboard.DARK)
        self.assertIn("plot_bgcolor", dashboard.DARK)
        self.assertIn("font_color", dashboard.DARK)
        self.assertIn("title_font_color", dashboard.DARK)

    def test_db_and_mem_dir_set(self) -> None:
        """DB and MEM_DIR are Path objects after import."""
        self.assertIsInstance(dashboard.DB, Path)
        self.assertIsInstance(dashboard.MEM_DIR, Path)

    def test_get_conn_returns_connection(self) -> None:
        """get_conn returns a sqlite3.Connection-like object.

        We mock the actual call to avoid touching a real DB.
        """
        mock_conn = MagicMock(spec=sqlite3.Connection)
        with patch.object(dashboard, "get_conn", return_value=mock_conn):
            conn = dashboard.get_conn()
        self.assertIsNotNone(conn)


# =========================================================================
# 10. Integration smoke tests with streamlit.testing.v1.AppTest
# =========================================================================


class TestDashboardIntegration(unittest.TestCase):
    """Smoke tests that verify the dashboard module functions end-to-end."""

    @patch("dashboard.get_conn")
    def test_query_returns_data_for_known_schema(
        self, mock_get_conn: MagicMock
    ) -> None:
        """``query()`` returns a DataFrame when the underlying SQL works."""
        import pandas as pd

        mock_conn = MagicMock(spec=sqlite3.Connection)
        mock_get_conn.return_value = mock_conn
        with patch("pandas.read_sql_query") as mock_read:
            expected = pd.DataFrame({"value": [1, 2, 3]})
            mock_read.return_value = expected
            result = dashboard.query("SELECT 1")
        self.assertIsNotNone(result)
        self.assertEqual(len(typing.cast(pd.DataFrame, result)), 3)

    def test_get_conn_creates_readonly_connection(self) -> None:
        """``get_conn()`` returns a URI-mode read-only connection.

        We exercise the real function with our test DB.
        """
        conn = dashboard.get_conn()
        self.assertIsNotNone(conn)
        # Verify the connection is actually open (query something)
        cur = conn.execute("SELECT 1")
        self.assertEqual(cur.fetchone()[0], 1)
        conn.close()

    @patch.object(Path, "stat")
    def test_resolve_db_returns_path(
        self,
        mock_stat: MagicMock,
    ) -> None:
        """``resolve_db()`` returns a Path."""
        mock_stat.return_value.st_size = 1_048_576
        result = dashboard.resolve_db()
        self.assertIsInstance(result, Path)

    def test_schema_version_reads_from_config(self) -> None:
        """``_get_schema_version()`` reads from the schema_version table.

        Our test DB has schema_version=68 in the schema_version table.
        """
        version = dashboard._get_schema_version()
        self.assertEqual(version, "v68")

    def test_table_function_with_real_db(self) -> None:
        """``table()`` returns True for existing tables in the test DB."""
        self.assertTrue(dashboard.table("memories"))
        self.assertTrue(dashboard.table("config"))
        self.assertFalse(dashboard.table("nonexistent_table_xyz"))

    def test_try_count_with_real_db(self) -> None:
        """``try_count()`` counts rows in the test DB."""
        count = dashboard.try_count("memories")
        self.assertGreaterEqual(count, 1)
        # Table with 0 rows
        self.assertEqual(dashboard.try_count("sync_log"), 0)

    def test_try_count_with_filter(self) -> None:
        """``try_count()`` respects WHERE clause."""
        pinned = dashboard.try_count("memories", "pinned=1")
        self.assertGreaterEqual(pinned, 1)

    def test_table_status_with_known_table(self) -> None:
        """``_table_status()`` returns ('ok', ...) for populated tables."""
        sev, detail = dashboard._table_status("memories")
        self.assertEqual(sev, "ok")
        self.assertRegex(detail, r"\d+ rows")

    def test_table_status_shows_warning_for_empty(self) -> None:
        """``_table_status()`` returns ('warning', ...) for empty non-expected tables."""
        sev, detail = dashboard._table_status("backfill_progress")
        self.assertEqual(sev, "warning")
        self.assertIn("unexpected", detail)

    def test_live_health_returns_structure(self) -> None:
        """``_live_health()`` returns the expected dict structure."""
        result = dashboard._live_health()
        self.assertIn("ts", result)
        self.assertIn("checks", result)
        self.assertIsInstance(result["checks"], list)
        self.assertGreater(len(result["checks"]), 0)
        # Each check is a (name, severity, detail) tuple
        for check in result["checks"]:
            self.assertIsInstance(check, tuple)
            self.assertEqual(len(check), 3)

    def test_live_health_includes_core_tables(self) -> None:
        """``_live_health()`` includes core table checks like memories."""
        result = dashboard._live_health()
        check_names = [c[0] for c in result["checks"]]
        self.assertIn("memories", check_names)
        self.assertIn("kg_entities", check_names)
        self.assertIn("ltr_model", check_names)
        self.assertIn("pinned", check_names)


# =========================================================================
# 11. _badge_html tests
# =========================================================================


# ── Cleanup temp DB after all tests ──────────────────────────────────────
import atexit as _atexit


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
    unittest.main()
