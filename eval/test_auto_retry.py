"""Test auto-retry of permanently failed tasks (Step 5 of cron pipeline maturity).

Tests:
- cron_retry_dead_tasks.py re-enqueues failed tasks past auto_retry_after_s
- Respects auto_retry_max_extra cap
- Dry-run mode previews without changes
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _init_db(db_path: Path) -> sqlite3.Connection:
    """Create a test DB with task_queue and cron_task_timeouts."""
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS task_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_type TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'pending',
            priority INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            started_at TEXT,
            completed_at TEXT,
            error TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            extra_retry_count INTEGER NOT NULL DEFAULT 0
        );
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
        INSERT OR IGNORE INTO cron_task_timeouts
            (task_type, timeout_s, max_attempts, auto_retry_after_s, auto_retry_max_extra)
        VALUES
            ('test_auto_retry_type', 120, 3, 1, 3);
    """)
    conn.commit()
    return conn


def test_auto_retry_re_enqueues_failed_task(tmp_path: Path) -> None:
    """A failed task past auto_retry_after_s gets re-enqueued."""
    db_path = tmp_path / "test.db"
    conn = _init_db(db_path)

    # Insert a failed task from 2 seconds ago
    past_timestamp = time.strftime(
        "%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 2)
    )
    conn.execute(
        "INSERT INTO task_queue (task_type, payload, status, priority, "
        "attempts, max_attempts, extra_retry_count, completed_at) "
        "VALUES (?, '{}', 'failed', 0, 3, 3, 0, ?)",
        ("test_auto_retry_type", past_timestamp),
    )
    conn.commit()
    failed_id = conn.execute(
        "SELECT id FROM task_queue WHERE status = 'failed' LIMIT 1"
    ).fetchone()[0]

    env = os.environ.copy()
    env["MEMORY_DB_PATH"] = str(db_path)

    result = subprocess.run(
        [sys.executable, "cron/cron_retry_dead_tasks.py", "--json"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(REPO_ROOT),
        env=env,
    )
    assert result.returncode == 0, result.stderr

    data = json.loads(result.stdout)
    assert data["total"] >= 1
    assert data["re_enqueued"].get("test_auto_retry_type", 0) >= 1

    conn.close()


def test_auto_retry_dry_run_does_not_enqueue(tmp_path: Path) -> None:
    """Dry-run mode previews without making changes."""
    db_path = tmp_path / "test_dry.db"
    conn = _init_db(db_path)

    past_timestamp = time.strftime(
        "%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 2)
    )
    conn.execute(
        "INSERT INTO task_queue (task_type, payload, status, priority, "
        "attempts, max_attempts, extra_retry_count, completed_at) "
        "VALUES (?, '{}', 'failed', 0, 3, 3, 0, ?)",
        ("test_auto_retry_type", past_timestamp),
    )
    conn.commit()

    env = os.environ.copy()
    env["MEMORY_DB_PATH"] = str(db_path)

    result = subprocess.run(
        [sys.executable, "cron/cron_retry_dead_tasks.py", "--dry-run"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(REPO_ROOT),
        env=env,
    )
    assert result.returncode == 0

    remaining = conn.execute(
        "SELECT COUNT(*) FROM task_queue WHERE status = 'failed'"
    ).fetchone()[0]
    assert remaining > 0

    conn.close()


def test_auto_retry_respects_max_extra(tmp_path: Path) -> None:
    """Tasks at auto_retry_max_extra are not re-enqueued."""
    db_path = tmp_path / "test_cap.db"
    conn = _init_db(db_path)

    past_timestamp = time.strftime(
        "%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 2)
    )
    conn.execute(
        "INSERT INTO task_queue (task_type, payload, status, priority, "
        "attempts, max_attempts, extra_retry_count, completed_at) "
        "VALUES (?, '{}', 'failed', 0, 3, 3, 3, ?)",
        ("test_auto_retry_type", past_timestamp),
    )
    conn.commit()

    env = os.environ.copy()
    env["MEMORY_DB_PATH"] = str(db_path)

    result = subprocess.run(
        [sys.executable, "cron/cron_retry_dead_tasks.py", "--json"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(REPO_ROOT),
        env=env,
    )
    assert result.returncode == 0

    data = json.loads(result.stdout)
    assert data["total"] == 0

    conn.close()


def test_auto_retry_no_config_type(tmp_path: Path) -> None:
    """A task type without cron_task_timeouts config is not touched."""
    db_path = tmp_path / "test_noconfig.db"
    conn = _init_db(db_path)

    past_timestamp = time.strftime(
        "%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 2)
    )
    conn.execute(
        "INSERT INTO task_queue (task_type, payload, status, priority, "
        "attempts, max_attempts, extra_retry_count, completed_at) "
        "VALUES (?, '{}', 'failed', 0, 3, 3, 0, ?)",
        ("unconfigured_type", past_timestamp),
    )
    conn.commit()

    env = os.environ.copy()
    env["MEMORY_DB_PATH"] = str(db_path)

    result = subprocess.run(
        [sys.executable, "cron/cron_retry_dead_tasks.py", "--json"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(REPO_ROOT),
        env=env,
    )
    assert result.returncode == 0

    data = json.loads(result.stdout)
    assert data["total"] == 0

    conn.close()


def test_auto_retry_too_soon(tmp_path: Path) -> None:
    """A recently failed task is not re-enqueued before auto_retry_after_s."""
    db_path = tmp_path / "test_soon.db"
    conn = _init_db(db_path)

    now_timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + 10))
    conn.execute(
        "INSERT INTO task_queue (task_type, payload, status, priority, "
        "attempts, max_attempts, extra_retry_count, completed_at) "
        "VALUES (?, '{}', 'failed', 0, 3, 3, 0, ?)",
        ("test_auto_retry_type", now_timestamp),
    )
    conn.commit()

    env = os.environ.copy()
    env["MEMORY_DB_PATH"] = str(db_path)

    result = subprocess.run(
        [sys.executable, "cron/cron_retry_dead_tasks.py", "--json"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(REPO_ROOT),
        env=env,
    )
    assert result.returncode == 0

    data = json.loads(result.stdout)
    assert data["total"] == 0

    conn.close()
