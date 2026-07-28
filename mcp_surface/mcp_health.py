"""MCP tool: memory_system_health — comprehensive green/yellow/red health check.

Consolidates 6 health dimensions into one response with actionable next steps.
Runs alongside (not replacing) the existing memory_health_check tool.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from infra.infrastructure import with_audit
from mcp_surface.mcp_instance import mcp

logger = logging.getLogger(__name__)

# Lazy import to avoid startup cost — the MCP server imports all mcp_*.py
# at module level, so heavy imports go inside functions.


def _check_database(db_path: Path) -> dict[str, Any]:
    """Check database accessibility and basic integrity."""
    if not db_path.exists():
        return {"status": "red", "details": "memory.db not found", "action": "Run: memory_organize(target='full')"}

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", timeout=5.0, uri=True)
        conn.execute("PRAGMA journal_mode=WAL")

        # Schema version
        try:
            row = conn.execute("SELECT version FROM schema_version WHERE id=1").fetchone()
            schema_ver = row[0] if row else "unknown"
        except Exception:
            schema_ver = "unknown"

        # Memory count
        try:
            count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        except Exception:
            count = 0

        # Vec index drift
        try:
            mem_count = conn.execute("SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL").fetchone()[0]
            vec_count = conn.execute("SELECT COUNT(*) FROM memory_vec_keys").fetchone()[0]
            drift = abs(mem_count - vec_count)
        except Exception:
            drift = 0

        conn.close()

        if drift > 100:
            return {
                "status": "yellow",
                "details": f"schema v{schema_ver}, {count} memories, vec drift={drift}",
                "action": "Run: memory_organize(target='rebuild')",
            }
        return {
            "status": "green",
            "details": f"schema v{schema_ver}, {count} memories",
            "action": None,
        }

    except Exception as e:
        return {"status": "red", "details": f"DB error: {e}", "action": "Check disk space and permissions"}


def _check_search(db_path: Path) -> dict[str, Any]:
    """Probe search functionality with a fast FTS-only path.

    Uses mode="fts" + light=True to avoid triggering the heavy hybrid
    pipeline. Skips probe or uses known-index term when write journal
    has pending writes to prevent false alarms or lock contention.
    """
    if not db_path.exists():
        return {"status": "yellow", "details": "skipped (no DB)", "action": None}

    # Check for pending journal writes — skip probe if journal has un-drained backlog
    journal_path = db_path.parent / "journal.db"
    if journal_path.exists():
        try:
            from infra.write_journal import journal_stats
            stats = journal_stats(journal_path)
            pending = stats.get("pending", 0)
            if pending > 0:
                return {
                    "status": "green",
                    "details": f"probe skipped ({pending} journal writes pending)",
                    "action": None,
                }
        except Exception:
            pass

    # Use a known-index term from DB if available, falling back to 'health check probe'
    search_term = "health check probe"
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", timeout=2.0, uri=True)
        row = conn.execute("SELECT category FROM memories WHERE deleted_at IS NULL LIMIT 1").fetchone()
        conn.close()
        if row and row[0]:
            search_term = str(row[0])
    except Exception:
        pass

    try:
        from search.orchestrator import search_memories

        t0 = time.time()
        result = search_memories(
            db_path=db_path,
            query=search_term,
            limit=1,
            include_global=False,
            mode="fts",
            light=True,
            hybrid=False,
            rerank=False,
            deep_rerank=False,
            synthesize=False,
            include_facts=False,
        )
        elapsed_ms = int((time.time() - t0) * 1000)
        count = len(result.get("results", []))

        if elapsed_ms > 5000:
            return {
                "status": "yellow",
                "details": f"probe returned {count} results in {elapsed_ms}ms (slow)",
                "action": "Search pipeline may need optimization",
            }
        return {
            "status": "green",
            "details": f"probe returned {count} results in {elapsed_ms}ms",
            "action": None,
        }

    except Exception as e:
        return {"status": "red", "details": f"search failed: {e}", "action": "Run: memory_organize(target='rebuild') and check embedding model"}


def _check_worker(db_path: Path) -> dict[str, Any]:
    """Check background worker health via cron_runs table."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", timeout=5.0, uri=True)
        row = conn.execute(
            """SELECT started_at, status, duration_ms
               FROM cron_runs
               WHERE job_name = 'background_worker'
               ORDER BY started_at DESC LIMIT 1"""
        ).fetchone()
        conn.close()

        if row is None:
            return {
                "status": "yellow",
                "details": "no background_worker runs recorded",
                "action": "Run: bash cron/install_crontab.sh to install scheduler",
            }

        last_run_str, status, duration_ms = row
        # Parse relative time
        try:
            last_run = datetime.fromisoformat(last_run_str)
            minutes_ago = int((datetime.now(timezone.utc) - last_run).total_seconds() / 60)
        except Exception:
            minutes_ago = -1

        if status == "failed":
            return {
                "status": "red",
                "details": f"last run FAILED ({minutes_ago}min ago, {duration_ms}ms)",
                "action": "Check memory/worker.log for errors",
            }
        if minutes_ago > 60:
            return {
                "status": "red",
                "details": f"last run {minutes_ago}min ago (>60min)",
                "action": "background_worker cron may not be running — check crontab",
            }
        if minutes_ago > 15:
            return {
                "status": "yellow",
                "details": f"last run {minutes_ago}min ago",
                "action": None,
            }
        return {
            "status": "green",
            "details": f"last run {minutes_ago}min ago, {duration_ms}ms",
            "action": None,
        }

    except Exception:
        # cron_runs table may not exist yet
        return {"status": "yellow", "details": "cron_runs table not available", "action": None}


def _check_crons(db_path: Path) -> dict[str, Any]:
    """Check cron job execution health from cron_runs table."""
    try:
        from cron.cron_runs import query_recent

        recent = query_recent(hours=24, db_path=db_path)
    except Exception:
        return {"status": "yellow", "details": "cron_runs not available", "action": None}

    total = recent.get("total_runs", 0)
    failed = recent.get("failed", 0)

    if total == 0:
        return {
            "status": "yellow",
            "details": "no cron runs in last 24h",
            "action": "Run: bash cron/install_crontab.sh to install scheduler",
        }

    if failed > 3:
        last_fail = recent.get("last_failure", {})
        return {
            "status": "red",
            "details": f"{failed}/{total} failed in 24h (last: {last_fail.get('job', '?')})",
            "action": f"Run: memory_maintenance(operation='audit_query', tool_name='{last_fail.get('job', '')}')",
        }

    if failed > 0:
        return {
            "status": "yellow",
            "details": f"{failed}/{total} failed in 24h",
            "action": "Check scheduler.log for details",
        }

    return {
        "status": "green",
        "details": f"{total} runs, all successful",
        "action": None,
    }


def _check_auto_save() -> dict[str, Any]:
    """Check auto-save pipeline health."""
    try:
        from background.auto_save import health_check

        result = health_check(minutes=30)
        if result.get("status") == "error":
            return {
                "status": "red",
                "details": result.get("message", "unknown error"),
                "action": "Check auto-save daemon and circuit breaker",
            }

        circuit_open = result.get("circuit_breaker_open", False)
        if circuit_open:
            return {
                "status": "red",
                "details": "circuit breaker OPEN",
                "action": "Auto-save is failing — check memory/.auto_save_daemon_manifest.json",
            }

        recent_saves = result.get("recent_saves", 0)
        return {
            "status": "green",
            "details": f"circuit closed, {recent_saves} saves in last 30min",
            "action": None,
        }

    except Exception as e:
        return {"status": "yellow", "details": f"auto-save check failed: {e}", "action": None}


def _check_disk() -> dict[str, Any]:
    """Check disk space."""
    try:
        usage = shutil.disk_usage("/")
        pct_used = (usage.used / usage.total) * 100
        free_gb = usage.free / (1024**3)

        if pct_used > 95:
            return {
                "status": "red",
                "details": f"{free_gb:.1f} GB free ({pct_used:.0f}% used)",
                "action": "Free disk space — critical for DB writes",
            }
        if pct_used > 80:
            return {
                "status": "yellow",
                "details": f"{free_gb:.1f} GB free ({pct_used:.0f}% used)",
                "action": None,
            }
        return {
            "status": "green",
            "details": f"{free_gb:.1f} GB free ({pct_used:.0f}% used)",
            "action": None,
        }

    except Exception as e:
        return {"status": "yellow", "details": f"disk check failed: {e}", "action": None}


@with_audit("memory_system_health")
@mcp.tool()
def memory_system_health(conn=None) -> str:  # noqa: ARG001
    """Comprehensive system health: green/yellow/red with actionable next steps.

    Consolidates 6 health dimensions into one response:
    - Database: accessibility, schema, vec index drift
    - Search: semantic search probe
    - Worker: background worker liveness
    - Crons: cron job execution success rate
    - Auto-Save: circuit breaker and recent activity
    - Disk: free space

    Each subsystem returns green/yellow/red with details and action.
    """
    from infra.memory_common import GLOBAL_MEM_DIR

    db_path = GLOBAL_MEM_DIR / "memory.db"

    subsystems = {
        "database": _check_database(db_path),
        "search": _check_search(db_path),
        "worker": _check_worker(db_path),
        "crons": _check_crons(db_path),
        "auto_save": _check_auto_save(),
        "disk": _check_disk(),
    }

    # Determine overall status
    statuses = [s["status"] for s in subsystems.values()]
    if "red" in statuses:
        overall = "critical"
    elif "yellow" in statuses:
        overall = "degraded"
    else:
        overall = "healthy"

    # Build action_required list
    action_required = [
        f"[{name}] {sub['action']}"
        for name, sub in subsystems.items()
        if sub.get("action")
    ]

    # Recent crons summary
    try:
        from cron.cron_runs import query_recent

        recent = query_recent(hours=24, db_path=db_path)
    except Exception:
        recent = {"total_runs": 0, "successful": 0, "failed": 0, "last_failure": None}

    result = {
        "overall": overall,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "subsystems": subsystems,
        "recent_crons": {
            "total_runs_24h": recent.get("total_runs", 0),
            "successful": recent.get("successful", 0),
            "failed": recent.get("failed", 0),
            "last_failure": recent.get("last_failure"),
        },
        "action_required": action_required,
    }

    return json.dumps(result, indent=2, default=str)
