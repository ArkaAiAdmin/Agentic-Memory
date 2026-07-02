"""Tests for backfill_all.py — universal backfill orchestrator."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
SCRIPT = SCRIPT_DIR / "backfill_all.py"


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Create an isolated temp DB for testing."""
    db_path = tmp_path / "memory.db"
    monkeypatch.setenv("MEMORY_DB_PATH", str(db_path))
    monkeypatch.setenv("MEMORY_BACKFILL_INTERVAL", "10")
    return db_path


@pytest.fixture
def sample_db(isolated_db, monkeypatch):
    """Create a minimal DB with memories table populated."""
    monkeypatch.setenv("MEMORY_KNOWLEDGE_GRAPH", "1")
    import sqlite3
    from infra.db_migrations import run_schema_setup
    conn = sqlite3.connect(str(isolated_db))
    run_schema_setup(conn)
    # Use markdown-formatted content so fact_extraction can extract SPO triples
    conn.execute(
        "INSERT INTO memories (id, content, category, tags, source_file, observed_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'), datetime('now'))",
        ("test-1", "## Python\n**What it does:** Python is a programming language used for scripting", "lessons", "python,code", "test-1.md"),
    )
    conn.execute(
        "INSERT INTO memories (id, content, category, tags, source_file, observed_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'), datetime('now'))",
        ("test-2", "## Database\n**What it does:** PostgreSQL is a database used for storing data", "projects", "database,perf", "test-2.md"),
    )
    conn.commit()
    conn.close()
    return isolated_db


class TestHealthCheck:
    def test_health_check_returns_structure(self, sample_db):
        from backfill.orchestrator import health_check
        result = health_check(sample_db)
        assert "db_path" in result
        assert "tables" in result
        assert "all_healthy" in result
        assert "stale_count" in result

    def test_health_check_detects_memories(self, sample_db):
        from backfill.orchestrator import health_check
        result = health_check(sample_db)
        assert result["tables"]["memories"]["count"] == 2
        assert result["tables"]["memories"]["ok"] is True

    def test_health_check_detects_missing_indexes(self, sample_db):
        from backfill.orchestrator import health_check
        result = health_check(sample_db)
        assert result["stale_count"] > 0
        assert result["all_healthy"] is False

    def test_health_check_no_db(self, isolated_db):
        from backfill.orchestrator import health_check
        result = health_check(isolated_db)
        assert result["all_healthy"] is False
        assert result["stale_count"] > 0

    def test_health_check_cli(self, sample_db):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--health"],
            capture_output=True, text=True, cwd=str(SCRIPT_DIR),
            env={**os.environ, "MEMORY_DB_PATH": str(sample_db)},
        )
        assert "Health Check" in result.stdout or "Health Check" in result.stderr


class TestBackfillIncremental:
    def test_incremental_builds_fts(self, sample_db):
        from backfill.orchestrator import backfill_incremental
        result = backfill_incremental(sample_db)
        assert result["result"] == "completed"
        ops = {op["op"]: op["result"] for op in result["operations"]}
        assert "memories_fts" in ops

    def test_incremental_skips_populated(self, sample_db):
        from backfill.orchestrator import backfill_incremental
        result1 = backfill_incremental(sample_db)
        rebuilt1 = [op["op"] for op in result1["operations"] if op["result"] == "rebuilt"]
        result2 = backfill_incremental(sample_db)
        rebuilt2 = [op["op"] for op in result2["operations"] if op["result"] == "rebuilt"]
        assert len(rebuilt2) <= len(rebuilt1)

    def test_incremental_builds_chunks(self, sample_db):
        from backfill.orchestrator import backfill_incremental
        result = backfill_incremental(sample_db)
        ops = {op["op"]: op["result"] for op in result["operations"]}
        assert "memory_chunks" in ops

    def test_incremental_builds_kg(self, sample_db):
        from backfill.orchestrator import backfill_incremental
        result = backfill_incremental(sample_db)
        ops = {op["op"]: op["result"] for op in result["operations"]}
        assert "kg_facts" in ops
        assert "kg_graph" in ops

    def test_incremental_builds_backlinks(self, sample_db):
        from backfill.orchestrator import backfill_incremental
        result = backfill_incremental(sample_db)
        ops = {op["op"]: op["result"] for op in result["operations"]}
        assert "backlinks" in ops


class TestBackfillFull:
    def test_full_rebuilds_everything(self, sample_db):
        from backfill.orchestrator import backfill_full
        # Full rebuild needs a source dir with markdown files; use sample_db parent
        # which won't have any, but the function should still complete
        result = backfill_full(sample_db, sample_db.parent)
        assert result["result"] == "completed"
        ops = {op["op"]: op["result"] for op in result["operations"]}
        # rebuild_index handles schema + memories + FTS5 + embeddings
        assert "rebuild_index" in ops
        # Remaining indexes are backfilled separately
        assert "memory_chunks" in ops
        assert "kg_facts" in ops
        assert "kg_graph" in ops
        assert "backlinks" in ops


class TestAutoBackfill:
    def test_auto_skips_when_interval_not_reached(self, sample_db, monkeypatch):
        monkeypatch.setenv("MEMORY_BACKFILL_INTERVAL", "100")
        from backfill.orchestrator import auto_backfill
        import backfill.orchestrator as backfill_all
        backfill_all._save_counter = 0
        result = auto_backfill(sample_db)
        assert result is None

    def test_auto_triggers_at_interval(self, sample_db, monkeypatch):
        monkeypatch.setenv("MEMORY_BACKFILL_INTERVAL", "2")
        from backfill.orchestrator import auto_backfill
        import backfill.orchestrator as backfill_all
        backfill_all._save_counter = 0
        result1 = auto_backfill(sample_db)
        assert result1 is None
        result2 = auto_backfill(sample_db)
        assert result2 is not None
        assert result2["result"] == "completed"

    def test_auto_resets_counter_after_trigger(self, sample_db, monkeypatch):
        monkeypatch.setenv("MEMORY_BACKFILL_INTERVAL", "2")
        from backfill.orchestrator import auto_backfill
        import backfill.orchestrator as backfill_all
        backfill_all._save_counter = 0
        auto_backfill(sample_db)
        auto_backfill(sample_db)
        assert backfill_all._save_counter == 0

    def test_auto_disabled_by_default(self, sample_db, monkeypatch):
        monkeypatch.delenv("MEMORY_BACKFILL_INTERVAL", raising=False)
        from backfill.orchestrator import auto_backfill
        import backfill.orchestrator as backfill_all
        backfill_all._save_counter = 0
        result = auto_backfill(sample_db)
        assert result is None


class TestCLI:
    def test_cli_health(self, sample_db):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--health"],
            capture_output=True, text=True, cwd=str(SCRIPT_DIR),
            env={**os.environ, "MEMORY_DB_PATH": str(sample_db)},
        )
        assert "Health Check" in result.stdout or "Health Check" in result.stderr

    def test_cli_incremental(self, sample_db):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--incremental"],
            capture_output=True, text=True, cwd=str(SCRIPT_DIR),
            env={**os.environ, "MEMORY_DB_PATH": str(sample_db)},
        )
        assert "Backfill" in result.stdout or "Backfill" in result.stderr

    def test_cli_full(self, sample_db):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--full", "--db", str(sample_db), "--source", str(sample_db.parent)],
            capture_output=True, text=True, cwd=str(SCRIPT_DIR),
            env={**os.environ, "MEMORY_DB_PATH": str(sample_db)},
        )
        assert "Backfill" in result.stdout or "Backfill" in result.stderr

    def test_cli_no_args_runs_health(self, sample_db):
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            capture_output=True, text=True, cwd=str(SCRIPT_DIR),
            env={**os.environ, "MEMORY_DB_PATH": str(sample_db)},
        )
        assert result.returncode == 0


class TestIndexIntegrity:
    def test_fts_populated_after_backfill(self, sample_db):
        from backfill.orchestrator import backfill_incremental
        backfill_incremental(sample_db)
        import sqlite3
        conn = sqlite3.connect(str(sample_db))
        count = conn.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0]
        conn.close()
        assert count == 2

    def test_chunks_populated_after_backfill(self, sample_db):
        from backfill.orchestrator import backfill_incremental
        backfill_incremental(sample_db)
        import sqlite3
        conn = sqlite3.connect(str(sample_db))
        count = conn.execute("SELECT COUNT(*) FROM memory_chunks").fetchone()[0]
        conn.close()
        assert count > 0

    def test_kg_entities_populated_after_backfill(self, sample_db, monkeypatch):
        monkeypatch.setenv("MEMORY_KNOWLEDGE_GRAPH", "1")
        from backfill.orchestrator import backfill_incremental
        backfill_incremental(sample_db)
        import sqlite3
        conn = sqlite3.connect(str(sample_db))
        count = conn.execute("SELECT COUNT(*) FROM kg_entities").fetchone()[0]
        conn.close()
        assert count > 0

    def test_kg_facts_populated_after_backfill(self, sample_db, monkeypatch):
        monkeypatch.setenv("MEMORY_KNOWLEDGE_GRAPH", "1")
        from backfill.orchestrator import backfill_incremental
        backfill_incremental(sample_db)
        import sqlite3
        conn = sqlite3.connect(str(sample_db))
        count = conn.execute("SELECT COUNT(*) FROM kg_facts").fetchone()[0]
        conn.close()
        assert count > 0
