"""Cron execution tracking — record, query, and clean up cron_runs rows.

Used by the consolidated scheduler (scheduler.py) and the
memory_system_health MCP tool (mcp_health.py).
"""

from __future__ import annotations

import logging
import sqlite3
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _get_db(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Open a connection to the memory DB."""
    if db_path is None:
        from infra.memory_common import GLOBAL_MEM_DIR

        db_path = Path(os.environ.get("MEMORY_DB_PATH", GLOBAL_MEM_DIR / "memory.db"))
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _ensure_table(db_path: str | Path | None = None) -> None:
    """Create the cron_runs table if it doesn't exist."""
    conn = _get_db(db_path)
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS cron_runs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                job_name     TEXT NOT NULL,
                started_at   TEXT NOT NULL DEFAULT (datetime('now')),
                completed_at TEXT,
                status       TEXT NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running', 'completed', 'failed', 'skipped')),
                duration_ms  INTEGER,
                error        TEXT,
                output       TEXT
            )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cron_runs_job ON cron_runs(job_name, started_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cron_runs_started ON cron_runs(started_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cron_runs_status ON cron_runs(status, started_at DESC)"
        )
        conn.commit()
    except Exception as e:
        logger.warning("cron_runs._ensure_table failed: %s", e)
    finally:
        conn.close()


def record_start(
    job_name: str,
    db_path: str | Path | None = None,
) -> int:
    """Record that a cron job started. Returns the row ID."""
    _ensure_table(db_path)
    conn = _get_db(db_path)
    try:
        cursor = conn.execute(
            "INSERT INTO cron_runs (job_name, status) VALUES (?, 'running')",
            (job_name,),
        )
        conn.commit()
        return cursor.lastrowid or 0
    except Exception as e:
        logger.warning("cron_runs.record_start failed for %s: %s", job_name, e)
        return 0
    finally:
        conn.close()


def record_complete(
    row_id: int,
    *,
    status: str = "completed",
    duration_ms: int | None = None,
    error: str | None = None,
    output: str | None = None,
    db_path: str | Path | None = None,
) -> None:
    """Update a cron_runs row with completion status."""
    if row_id <= 0:
        return
    conn = _get_db(db_path)
    try:
        conn.execute(
            """UPDATE cron_runs
               SET completed_at = datetime('now'),
                   status = ?,
                   duration_ms = ?,
                   error = ?,
                   output = ?
               WHERE id = ?""",
            (
                status,
                duration_ms,
                (error[:500] if error else None),
                (output[-500:] if output else None),
                row_id,
            ),
        )
        conn.commit()
    except Exception as e:
        logger.warning("cron_runs.record_complete failed for row %d: %s", row_id, e)
    finally:
        conn.close()


def query_recent(
    hours: int = 24,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Query cron runs from the last N hours. Returns summary dict."""
    conn = _get_db(db_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT job_name, status, COUNT(*) as cnt,
                      MAX(started_at) as last_run,
                      AVG(duration_ms) as avg_duration_ms
               FROM cron_runs
               WHERE started_at >= datetime('now', ?)
               GROUP BY job_name, status
               ORDER BY job_name, status""",
            (f"-{hours} hours",),
        ).fetchall()

        # Build per-job summary
        jobs: dict[str, dict] = {}
        total_runs = 0
        total_failed = 0
        total_completed = 0
        last_failure: dict[str, Any] | None = None

        for row in rows:
            name = row["job_name"]
            status = row["status"]
            cnt = row["cnt"]
            total_runs += cnt

            if name not in jobs:
                jobs[name] = {"runs": 0, "failed": 0, "last_run": None}
            jobs[name]["runs"] += cnt
            if jobs[name]["last_run"] is None or (row["last_run"] or "") > (
                jobs[name]["last_run"] or ""
            ):
                jobs[name]["last_run"] = row["last_run"]

            if status == "completed":
                total_completed += cnt
            elif status == "failed":
                total_failed += cnt
                jobs[name]["failed"] += cnt
                if last_failure is None or (row["last_run"] or "") > (
                    last_failure.get("at") or ""
                ):
                    last_failure = {
                        "job": name,
                        "at": row["last_run"],
                    }

        return {
            "total_runs": total_runs,
            "successful": total_completed,
            "failed": total_failed,
            "jobs": jobs,
            "last_failure": last_failure,
        }
    except Exception as e:
        logger.warning("cron_runs.query_recent failed: %s", e)
        return {
            "total_runs": 0,
            "successful": 0,
            "failed": 0,
            "jobs": {},
            "last_failure": None,
        }
    finally:
        conn.close()


def cleanup_old(days: int = 30, db_path: str | Path | None = None) -> int:
    """Delete cron_runs older than N days. Returns count deleted."""
    conn = _get_db(db_path)
    try:
        cursor = conn.execute(
            "DELETE FROM cron_runs WHERE started_at < datetime('now', ?)",
            (f"-{days} days",),
        )
        conn.commit()
        deleted = cursor.rowcount
        if deleted > 0:
            logger.info("cron_runs: cleaned up %d rows older than %d days", deleted, days)
        return deleted
    except Exception as e:
        logger.warning("cron_runs.cleanup_old failed: %s", e)
        return 0
    finally:
        conn.close()
