"""Sprint 1: session memory v22 migration tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from infra.migration_runner import run_migrations, migrate_down, SCHEMA_VERSION


def _conn(db_path: Path):
    c = sqlite3.connect(db_path)
    c.execute("PRAGMA foreign_keys = ON")
    return c


def test_forward_creates_all_tables(tmp_path):
    db = tmp_path / "migrate_forward.db"
    conn = _conn(db)
    run_migrations(conn)
    for table in ("sessions", "decision_threads", "thread_events", "session_compaction_log"):
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        assert row is not None, f"Table {table} not created"
    conn.close()


def test_version_vector_column_present(tmp_path):
    db = tmp_path / "version_vector.db"
    conn = _conn(db)
    run_migrations(conn)
    for table in ("sessions", "decision_threads", "thread_events", "session_compaction_log"):
        cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
        names = {c[1] for c in cols}
        assert "version_vector" in names, f"version_vector missing from {table}"
    conn.close()


def test_metadata_json_constraint(tmp_path):
    db = tmp_path / "json_meta.db"
    conn = _conn(db)
    run_migrations(conn)
    conn.execute(
        "INSERT INTO sessions (id, started_at, metadata) VALUES (?, ?, ?)",
        ("s1", "2026-01-01T00:00:00", '{"key": 1}'),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO sessions (id, started_at, metadata) VALUES (?, ?, ?)",
            ("s-bad", "2026-01-01T00:00:00", "not-json"),
        )
    conn.close()


def test_fk_rejects_invalid_session_ref(tmp_path):
    db = tmp_path / "fk_test.db"
    conn = _conn(db)
    run_migrations(conn)
    conn.execute(
        "INSERT INTO sessions (id, started_at) VALUES (?, ?)", ("s1", "2026-01-01T00:00:00")
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO decision_threads (id, session_id, title, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("t-bad", "s-noexist", "ghost", "2026-01-01T00:00:00"),
        )
    conn.close()


def test_idempotent_double_run(tmp_path):
    db = tmp_path / "idempotent.db"
    conn = _conn(db)
    run_migrations(conn)
    conn.close()
    conn = _conn(db)
    run_migrations(conn)
    row = conn.execute("SELECT version FROM schema_version WHERE id=1").fetchone()
    assert row[0] == SCHEMA_VERSION
    conn.close()


def test_down_migration_restores_clean_schema(tmp_path):
    db = tmp_path / "down_test.db"
    conn = _conn(db)
    run_migrations(conn)
    conn.close()
    conn = _conn(db)
    # Ensure KG schema exists before rolling back, as down migrations
    # reference kg_entities in FK constraints
    from knowledge_graph.kg_schema import ensure_kg_schema
    ensure_kg_schema(conn)
    migrate_down(conn, target_version=21)
    for table in ("sessions", "decision_threads", "thread_events", "session_compaction_log"):
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        assert row is None, f"Table {table} still exists after downgrade"
    conn.close()
