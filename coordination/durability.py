"""Coordination durability layer — crash recovery, heartbeats, audit logging.

Makes the coordination system survive crashes and provide safe operation.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Configuration
HEARTBEAT_INTERVAL = 60  # seconds
HEARTBEAT_TIMEOUT = 300  # seconds (5 minutes)
TASK_ABANDON_TIMEOUT = 600  # seconds (10 minutes)
LOCK_AUTO_RELEASE_TIMEOUT = 300  # seconds (5 minutes)


def ensure_durability_tables(conn: sqlite3.Connection):
    """Create durability tables if they don't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS coordination_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            target TEXT,
            detail TEXT,
            timestamp REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_heartbeats (
            agent_id TEXT PRIMARY KEY,
            last_heartbeat REAL NOT NULL,
            session_id TEXT,
            project_id TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_coordination_audit_action
        ON coordination_audit(action, timestamp)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_coordination_audit_agent
        ON coordination_audit(agent_id, timestamp)
    """)
    conn.commit()


def record_coordination_event(
    conn: sqlite3.Connection,
    action: str,
    agent_id: str,
    target: str | None = None,
    detail: str | None = None,
):
    """Record a coordination event to the audit log."""
    try:
        conn.execute(
            "INSERT INTO coordination_audit (action, agent_id, target, detail, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            (action, agent_id, target, detail, time.time()),
        )
        conn.commit()
    except Exception as e:
        logger.warning("Failed to record coordination event: %s", e)


def update_heartbeat(
    conn: sqlite3.Connection,
    agent_id: str,
    session_id: str | None = None,
    project_id: str | None = None,
):
    """Update agent heartbeat timestamp."""
    try:
        conn.execute(
            "INSERT OR REPLACE INTO agent_heartbeats (agent_id, last_heartbeat, session_id, project_id) "
            "VALUES (?, ?, ?, ?)",
            (agent_id, time.time(), session_id, project_id),
        )
        conn.commit()
    except Exception as e:
        logger.warning("Failed to update heartbeat: %s", e)


def check_agent_alive(conn: sqlite3.Connection, agent_id: str) -> bool:
    """Check if an agent is alive based on heartbeat."""
    row = conn.execute(
        "SELECT last_heartbeat FROM agent_heartbeats WHERE agent_id=?",
        (agent_id,),
    ).fetchone()

    if not row:
        return False  # Never sent a heartbeat

    age = time.time() - row[0]
    return age < HEARTBEAT_TIMEOUT


def get_alive_agents(conn: sqlite3.Connection) -> list[dict]:
    """Get all agents that are currently alive."""
    now = time.time()
    rows = conn.execute(
        "SELECT agent_id, last_heartbeat, session_id, project_id "
        "FROM agent_heartbeats WHERE last_heartbeat > ?",
        (now - HEARTBEAT_TIMEOUT,),
    ).fetchall()

    return [
        {
            "agent_id": r[0],
            "last_heartbeat": r[1],
            "age_s": now - r[1],
            "session_id": r[2],
            "project_id": r[3],
        }
        for r in rows
    ]


def cleanup_stale_agents(conn: sqlite3.Connection) -> int:
    """Remove agents that haven't sent a heartbeat recently."""
    cutoff = time.time() - (HEARTBEAT_TIMEOUT * 2)
    cursor = conn.execute("DELETE FROM agent_heartbeats WHERE last_heartbeat < ?", (cutoff,))
    conn.commit()
    return cursor.rowcount


def release_stale_locks(conn: sqlite3.Connection) -> int:
    """Release locks that have expired."""
    now = time.time()
    cursor = conn.execute("DELETE FROM file_locks WHERE expires_at < ?", (now,))
    conn.commit()
    return cursor.rowcount


def abandon_stale_tasks(conn: sqlite3.Connection) -> int:
    """Abandon tasks that have been active too long without updates."""
    cutoff = time.time() - TASK_ABANDON_TIMEOUT
    cursor = conn.execute(
        "UPDATE shared_tasks SET status='abandoned', assigned_to=NULL, updated_at=? "
        "WHERE status='active' AND updated_at < ?",
        (time.time(), cutoff),
    )
    conn.commit()
    if cursor.rowcount > 0:
        logger.warning("Abandoned %d stale tasks", cursor.rowcount)
    return cursor.rowcount


def cleanup_old_messages(conn: sqlite3.Connection, max_age_days: int = 30) -> int:
    """Remove old delivered messages."""
    cutoff = time.time() - (max_age_days * 86400)
    cursor = conn.execute(
        "DELETE FROM agent_messages WHERE status='delivered' AND delivered_at < ?",
        (cutoff,),
    )
    conn.commit()
    return cursor.rowcount


def cleanup_old_audit_entries(conn: sqlite3.Connection, max_age_days: int = 90) -> int:
    """Remove old audit log entries."""
    cutoff = time.time() - (max_age_days * 86400)
    cursor = conn.execute(
        "DELETE FROM coordination_audit WHERE timestamp < ?",
        (cutoff,),
    )
    conn.commit()
    return cursor.rowcount


def run_durability_maintenance(conn: sqlite3.Connection) -> dict:
    """Run all durability maintenance tasks. Returns summary."""
    ensure_durability_tables(conn)

    results = {
        "stale_locks_released": release_stale_locks(conn),
        "stale_tasks_abandoned": abandon_stale_tasks(conn),
        "old_messages_cleaned": cleanup_old_messages(conn),
        "stale_agents_cleaned": cleanup_stale_agents(conn),
        "old_audit_entries_cleaned": cleanup_old_audit_entries(conn),
    }

    # Record maintenance event
    record_coordination_event(
        conn, "durability_maintenance", "system",
        detail=json.dumps(results),
    )

    return results


def get_coordination_audit(
    conn: sqlite3.Connection,
    action: str | None = None,
    agent_id: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Get coordination audit log entries."""
    where = "1=1"
    params = []

    if action:
        where += " AND action=?"
        params.append(action)
    if agent_id:
        where += " AND agent_id=?"
        params.append(agent_id)

    rows = conn.execute(
        f"SELECT action, agent_id, target, detail, timestamp "
        f"FROM coordination_audit WHERE {where} "
        f"ORDER BY timestamp DESC LIMIT ?",
        params + [limit],
    ).fetchall()

    return [
        {
            "action": r[0], "agent_id": r[1], "target": r[2],
            "detail": r[3], "timestamp": r[4],
        }
        for r in rows
    ]


def get_safety_report(conn: sqlite3.Connection) -> dict:
    """Generate a safety report for the coordination system."""
    ensure_durability_tables(conn)

    now = time.time()

    # Check for stuck agents
    alive_agents = get_alive_agents(conn)
    stale_agents = conn.execute(
        "SELECT agent_id FROM agent_heartbeats WHERE last_heartbeat < ?",
        (now - HEARTBEAT_TIMEOUT,),
    ).fetchall()

    # Check for stale locks
    stale_locks = conn.execute(
        "SELECT COUNT(*) FROM file_locks WHERE expires_at < ?", (now,)
    ).fetchone()[0]

    # Check for abandoned tasks
    abandoned_tasks = conn.execute(
        "SELECT COUNT(*) FROM shared_tasks WHERE status='active' AND updated_at < ?",
        (now - TASK_ABANDON_TIMEOUT,),
    ).fetchone()[0]

    # Check for pending messages
    pending_msgs = conn.execute(
        "SELECT COUNT(*) FROM agent_messages WHERE status='pending'"
    ).fetchone()[0]

    # Safety score
    issues = 0
    if stale_agents:
        issues += len(stale_agents)
    if stale_locks > 0:
        issues += stale_locks
    if abandoned_tasks > 0:
        issues += abandoned_tasks

    safety_score = max(0, 100 - (issues * 10))

    return {
        "safety_score": safety_score,
        "alive_agents": len(alive_agents),
        "stale_agents": len(stale_agents),
        "stale_locks": stale_locks,
        "abandoned_tasks": abandoned_tasks,
        "pending_messages": pending_msgs,
        "timestamp": now,
    }
