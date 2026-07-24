"""Integration tests for audit logging — all MCP operations.

Verifies that the with_audit decorator correctly logs tool calls
to the memory_audit_log table with proper fields.
"""

import os
import sqlite3
import sys
import time
from pathlib import Path


_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
sys.path.insert(0, str(Path(__file__).resolve().parent))

_eval_lme = os.path.join(_project_root, "eval", "longmemeval_s")
if _eval_lme in sys.path:
    sys.path.remove(_eval_lme)
_wrong_modules = [k for k in sys.modules if k in ("metrics", "longmemeval_s.metrics")]
for k in _wrong_modules:
    del sys.modules[k]

from infra.memory_common import (
    run_db_migrations,
    _migrate_kg_tables,
    _migrate_memory_audit_log,
)
from fact import ensure_facts_schema
from adaptive_retention import ensure_adaptive_schema

sys.path.insert(0, _project_root)
import infra.audit as audit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_test_db(tmp_path: Path) -> Path:
    """Create a test database with full schema including audit log."""
    db_path = tmp_path / "memory.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON;")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            source_file TEXT,
            tags TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            observed_at TEXT,
            category TEXT,
            title_slug TEXT,
            importance INTEGER DEFAULT 0,
            pinned INTEGER DEFAULT 0,
            fitness_score REAL DEFAULT 0.0,
            deleted_at TEXT,
            valid_to TEXT,
            superseded_by TEXT,
            repo_id TEXT,
            valid_from TEXT,
            metadata TEXT,
            hash TEXT,
            embedding_available INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS backlinks (
            source_id TEXT,
            target_id TEXT,
            PRIMARY KEY (source_id, target_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_id TEXT NOT NULL,
            chunk_idx INTEGER NOT NULL,
            start_offset INTEGER NOT NULL,
            end_offset INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_embeddings (
            memory_id TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            embedding BLOB,
            model_revision TEXT NOT NULL,
            dim INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (memory_id, content_hash)
        )
    """)

    run_db_migrations(conn)
    _migrate_kg_tables(conn)
    _migrate_memory_audit_log(conn)
    ensure_facts_schema(conn)
    ensure_adaptive_schema(conn)

    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# Tests: audit context manager
# ---------------------------------------------------------------------------


class TestAuditContext:
    """Test the audit.audit() context manager."""

    def test_audit_writes_log_entry(self, tmp_path):
        """Audit context manager writes to memory_audit_log."""
        db_path = _create_test_db(tmp_path)

        with audit.audit(
            "test_tool", args={"key": "value"}, db_path=str(db_path)
        ) as ctx:
            ctx["results_count"] = 1
            ctx["top1_id"] = "test-note"

        # Flush audit queue
        audit.flush_audit(timeout=5)

        # Verify log entry
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT tool, args, results_count, top1_id FROM memory_audit_log"
        ).fetchall()
        conn.close()

        assert len(rows) >= 1
        tool_name = rows[-1][0]
        assert tool_name == "test_tool"

    def test_audit_records_latency(self, tmp_path):
        """Audit entry includes non-zero latency."""
        db_path = _create_test_db(tmp_path)

        with audit.audit("slow_tool", db_path=str(db_path)) as ctx:
            # Anti-thundering-herd: ensure the recorded latency is
            # measurably non-zero. 10ms is the smallest interval the
            # audit module rounds to; any less and the test is flaky.
            time.sleep(0.01)
            ctx["results_count"] = 0

        audit.flush_audit(timeout=5)

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT latency_ms FROM memory_audit_log WHERE tool = 'slow_tool'"
        ).fetchone()
        conn.close()

        assert row is not None
        assert row[0] > 0

    def test_audit_records_error(self, tmp_path):
        """Audit entry captures errors."""
        db_path = _create_test_db(tmp_path)

        try:
            with audit.audit("failing_tool", db_path=str(db_path)):
                raise ValueError("test error")
        except ValueError:
            pass

        audit.flush_audit(timeout=5)

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT error FROM memory_audit_log WHERE tool = 'failing_tool'"
        ).fetchone()
        conn.close()

        assert row is not None
        assert "test error" in row[0]

    def test_audit_flush_blocks_until_complete(self, tmp_path):
        """flush_audit blocks until queue is drained."""
        db_path = _create_test_db(tmp_path)

        for i in range(10):
            with audit.audit(f"batch_tool_{i}", db_path=str(db_path)) as ctx:
                ctx["results_count"] = i

        audit.flush_audit(timeout=10)

        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM memory_audit_log").fetchone()[0]
        conn.close()

        assert count >= 10

    def test_audit_queue_size(self):
        """audit_queue_size returns non-negative integer."""
        size = audit.audit_queue_size()
        assert isinstance(size, int)
        assert size >= 0


# ---------------------------------------------------------------------------
# Tests: MCP tool audit integration
# ---------------------------------------------------------------------------


class TestMCPToolAudit:
    """Test that MCP tools produce audit entries."""

    def test_memory_save_creates_audit(self, tmp_path, monkeypatch):
        """memory_save triggers audit logging."""
        from infra.memory_common import reset_rate_limiter
        reset_rate_limiter()
        db_path = _create_test_db(tmp_path)

        # Force all modules (no matter when they were imported) to resolve
        # to the test database.
        monkeypatch.setenv("MEMORY_DB_PATH", str(db_path))

        from mcp_surface.mcp_tools import memory_save

        memory_save(
            content="Audit test note",
            category="lessons",
            title_slug="audit-test",
        )

        audit.flush_audit(timeout=5)

        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT tool FROM memory_audit_log WHERE tool = 'memory_save'"
        ).fetchall()
        conn.close()

        assert len(rows) >= 1

    def test_memory_search_creates_audit(self, tmp_path, monkeypatch):
        """memory_search triggers audit logging."""
        from infra.memory_common import reset_rate_limiter
        reset_rate_limiter()
        db_path = _create_test_db(tmp_path)

        # Force all modules (no matter when they were imported) to resolve
        # to the test database.
        monkeypatch.setenv("MEMORY_DB_PATH", str(db_path))

        from mcp_surface.mcp_tools import memory_search

        # Drain the audit queue (which may hold rows from prior tests in
        # the same session) before running the action we want to verify.
        audit.flush_audit(timeout=30)
        memory_search(query="test query")

        audit.flush_audit(timeout=30)

        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT tool FROM memory_audit_log WHERE tool = 'memory_search'"
        ).fetchall()
        conn.close()

        assert len(rows) >= 1


# ---------------------------------------------------------------------------
# Tests: audit schema
# ---------------------------------------------------------------------------


class TestAuditSchema:
    """Test audit log table schema."""

    def test_audit_table_exists(self, tmp_path):
        """memory_audit_log table is created."""
        db_path = _create_test_db(tmp_path)
        conn = sqlite3.connect(str(db_path))

        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "memory_audit_log" in tables
        conn.close()

    def test_audit_indexes_exist(self, tmp_path):
        """Audit log indexes are created."""
        db_path = _create_test_db(tmp_path)
        conn = sqlite3.connect(str(db_path))

        indexes = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_audit_log_tool_ts" in indexes
        assert "idx_audit_log_ts" in indexes
        conn.close()

    def test_audit_row_schema(self, tmp_path):
        """Audit log rows have all required columns."""
        db_path = _create_test_db(tmp_path)

        with audit.audit("schema_test", db_path=str(db_path)) as ctx:
            ctx["results_count"] = 1

        audit.flush_audit(timeout=5)

        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute("SELECT * FROM memory_audit_log LIMIT 1")
        columns = [desc[0] for desc in cursor.description]
        conn.close()

        required = {"id", "ts", "tool", "latency_ms"}
        assert required.issubset(set(columns))
