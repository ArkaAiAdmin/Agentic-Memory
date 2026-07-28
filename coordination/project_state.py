"""Project state management for multi-agent coordination.

Provides shared state storage so agents can see what others are doing.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any

logger = logging.getLogger(__name__)


def get_state(conn: sqlite3.Connection, project_id: str) -> dict:
    """Get all state for a project."""
    rows = conn.execute(
        "SELECT key, value, updated_by, updated_at FROM project_state WHERE project_id=?",
        (project_id,),
    ).fetchall()

    state = {}
    for r in rows:
        try:
            value = json.loads(r[1]) if r[1] else None
        except json.JSONDecodeError:
            value = r[1]
        state[r[0]] = {
            "value": value,
            "updated_by": r[2],
            "updated_at": r[3],
        }

    return state


def set_state(
    conn: sqlite3.Connection,
    project_id: str,
    key: str,
    value: dict | str | int | float | None,
    agent_id: str,
) -> bool:
    """Set a state value. Returns True if updated."""
    now = time.time()
    value_str = json.dumps(value) if isinstance(value, (dict, list)) else str(value) if value is not None else None

    conn.execute(
        "INSERT OR REPLACE INTO project_state (project_id, key, value, updated_by, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (project_id, key, value_str, agent_id, now),
    )
    conn.commit()
    return True


def delete_state(conn: sqlite3.Connection, project_id: str, key: str) -> bool:
    """Delete a state value."""
    cursor = conn.execute(
        "DELETE FROM project_state WHERE project_id=? AND key=?",
        (project_id, key),
    )
    conn.commit()
    return cursor.rowcount > 0


def get_state_keys(conn: sqlite3.Connection, project_id: str) -> list[str]:
    """Get all state keys for a project."""
    rows = conn.execute(
        "SELECT key FROM project_state WHERE project_id=? ORDER BY key",
        (project_id,),
    ).fetchall()
    return [r[0] for r in rows]


def get_agent_activity(conn: sqlite3.Connection, project_id: str) -> dict:
    """Get a summary of what each agent is doing in a project."""
    state = get_state(conn, project_id)

    activity: dict[str, list[dict[str, Any]]] = {}
    for key, info in state.items():
        agent = info.get("updated_by", "unknown")
        if agent not in activity:
            activity[agent] = []
        activity[agent].append({
            "key": key,
            "value": info["value"],
            "updated_at": info["updated_at"],
        })

    return activity


def get_active_files(conn: sqlite3.Connection, project_id: str) -> list[dict]:
    """Get list of files currently being worked on by agents."""
    rows = conn.execute(
        "SELECT key, value, updated_by, updated_at FROM project_state "
        "WHERE project_id=? AND key LIKE 'file:%' "
        "ORDER BY updated_at DESC",
        (project_id,),
    ).fetchall()

    files = []
    for r in rows:
        try:
            value = json.loads(r[1]) if r[1] else None
        except json.JSONDecodeError:
            value = r[1]
        files.append({
            "file_path": r[0].replace("file:", ""),
            "status": value,
            "agent": r[2],
            "updated_at": r[3],
        })

    return files


def set_agent_status(conn: sqlite3.Connection, project_id: str, agent_id: str, status: str) -> bool:
    """Update agent's current status in the project."""
    return set_state(conn, project_id, f"agent:{agent_id}:status", status, agent_id)


def get_agent_status(conn: sqlite3.Connection, project_id: str, agent_id: str) -> str | None:
    """Get agent's current status."""
    state = get_state(conn, project_id)
    info = state.get(f"agent:{agent_id}:status")
    return info["value"] if info else None
