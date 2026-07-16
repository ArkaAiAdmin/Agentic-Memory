"""Test per-task-type timeout configuration (Step 2 of cron pipeline maturity).

Tests:
- _resolve_task_timeout reads from cron_task_timeouts table
- Falls back to MEMORY_WORKER_TASK_TIMEOUT_S when table/row is missing
- manage_task_timeouts.py CLI works
- Migration 063 is round-trip safe
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Generator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def timeout_db() -> Generator[sqlite3.Connection, None, None]:
    """Create an in-memory DB with the task timeout table."""
    db = sqlite3.connect(":memory:", timeout=10.0)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS cron_task_timeouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_type TEXT NOT NULL UNIQUE,
            timeout_s INTEGER NOT NULL DEFAULT 300,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            auto_retry_after_s INTEGER NOT NULL DEFAULT 900,
            auto_retry_max_extra INTEGER NOT NULL DEFAULT 3,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        INSERT INTO cron_task_timeouts (task_type, timeout_s, max_attempts, auto_retry_after_s)
        VALUES ('test_cron_task', 60, 3, 900);
    """)
    yield db
    db.close()


def test_resolve_task_timeout_reads_from_table(timeout_db: sqlite3.Connection) -> None:
    from background.background_worker import _resolve_task_timeout

    timeout = _resolve_task_timeout(timeout_db, "test_cron_task")
    assert timeout == 60


def test_resolve_task_timeout_fallback(timeout_db: sqlite3.Connection) -> None:
    from background.background_worker import _resolve_task_timeout

    timeout = _resolve_task_timeout(timeout_db, "nonexistent_task")
    assert timeout == 120  # default from env var


def test_resolve_task_timeout_custom_fallback(timeout_db: sqlite3.Connection) -> None:
    from background.background_worker import _resolve_task_timeout

    os.environ["MEMORY_WORKER_TASK_TIMEOUT_S"] = "300"
    try:
        timeout = _resolve_task_timeout(timeout_db, "nonexistent_task")
        assert timeout == 300
    finally:
        os.environ.pop("MEMORY_WORKER_TASK_TIMEOUT_S", None)


def test_resolve_task_timeout_no_table() -> None:
    """Without the cron_task_timeouts table, fall back to env var."""
    from background.background_worker import _resolve_task_timeout

    conn = sqlite3.connect(":memory:", timeout=10.0)
    conn.row_factory = sqlite3.Row
    timeout = _resolve_task_timeout(conn, "any_task")
    assert timeout == 120  # default


def test_manage_task_timeouts_list() -> None:
    """manage_task_timeouts.py --list returns known task types."""
    result = subprocess.run(
        [sys.executable, "cron/manage_task_timeouts.py", "--list", "--json"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert isinstance(data, list)
    assert len(data) > 0
    types = [r["task_type"] for r in data]
    assert "cron_integrity_check" in types
    assert "entity_resolution" in types


def test_manage_task_timeouts_get() -> None:
    """manage_task_timeouts.py --get returns a specific row."""
    result = subprocess.run(
        [sys.executable, "cron/manage_task_timeouts.py", "--get", "cron_backfill_all", "--json"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["task_type"] == "cron_backfill_all"
    assert data["timeout_s"] == 600


def test_manage_task_timeouts_get_missing() -> None:
    """manage_task_timeouts.py --get for nonexistent type returns non-zero."""
    result = subprocess.run(
        [sys.executable, "cron/manage_task_timeouts.py", "--get", "cron_nonexistent", "--json"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode != 0


def test_manage_task_timeouts_set_and_reset() -> None:
    """manage_task_timeouts.py --set updates a row, --reset deletes it."""
    # Set
    set_result = subprocess.run(
        [
            sys.executable, "cron/manage_task_timeouts.py",
            "--set", "test_timeout_set", "--timeout", "500", "--max-attempts", "5", "--json",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(REPO_ROOT),
    )
    assert set_result.returncode == 0, set_result.stderr
    data = json.loads(set_result.stdout)
    assert data["timeout_s"] == 500
    assert data["max_attempts"] == 5

    # Reset
    reset_result = subprocess.run(
        [sys.executable, "cron/manage_task_timeouts.py", "--reset", "test_timeout_set", "--json"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(REPO_ROOT),
    )
    assert reset_result.returncode == 0, reset_result.stderr


def test_migration_063_up_down(tmp_path: Path) -> None:
    """Verify migration 063 is round-trip safe."""
    db_path = tmp_path / "test_migration.db"
    db = sqlite3.connect(str(db_path), timeout=10.0)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")

    from infra.migration_runner import run_migrations

    migration_dir = REPO_ROOT / "migrations"
    up = (migration_dir / "063_cron_task_timeout_policy.sql").read_text()
    db.executescript(up)

    rows = db.execute("SELECT * FROM cron_task_timeouts").fetchall()
    assert len(rows) > 0

    down = (migration_dir / "063_cron_task_timeout_policy.down.sql").read_text()
    db.executescript(down)

    tables = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='cron_task_timeouts'"
    ).fetchall()
    assert len(tables) == 0

    db.close()
