"""Multi-agent coordination MCP tool.

Provides task management, file locking, agent messaging, and project state
for coordinating multiple agents on shared projects.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from mcp_surface.mcp_instance import mcp
from mcp_surface.mcp_common import _err, ErrorCode, with_audit, _resolve_memory_dir

logger = logging.getLogger(__name__)


def _get_conn():
    """Get a pooled database connection."""
    from infra.infrastructure import resolve_active_memory_dir
    from infra.db import connection_pool
    db_path = str(resolve_active_memory_dir() / "memory.db")
    return connection_pool.get(db_path, timeout=10.0)


@mcp.tool()
@with_audit("memory_coordinate")
def memory_coordinate(
    action: str = "get_project_state",
    project_id: str = "default",
    task_id: int | None = None,
    task_type: str | None = None,
    description: str | None = None,
    assigned_to: str | None = None,
    status: str | None = None,
    file_path: str | None = None,
    to_agent: str | None = None,
    message_type: str | None = None,
    payload: str | None = None,
    key: str | None = None,
    value: str | None = None,
) -> str:
    """Multi-agent coordination tool for task management, file locking, and messaging.

    Actions:
        create_task: Create a new task
        claim_task: Reserve a task for this agent
        update_task_status: Update task status (the primary coordination primitive)
        release_task: Release a task back to the pool
        complete_task: Mark task done, share result
        list_tasks: List tasks for a project
        lock_file: Acquire exclusive lock on a file
        unlock_file: Release file lock
        check_lock: Check if a file is locked
        send_message: Send message to another agent
        read_messages: Read pending messages
        get_project_state: See what others are doing
        update_project_state: Share what you're doing

    Coordination model:
        Messages are notifications. Task status transitions are the ack.
        When Agent B reads a message and calls update_task_status, that IS
        the acknowledgement. No separate ack channel needed.
    """
    try:
        conn = _get_conn()
        agent_id = _get_agent_id()

        if action == "create_task":
            return _create_task(conn, project_id, task_type, description, assigned_to, agent_id)
        elif action == "claim_task":
            return _claim_task(conn, task_id, agent_id)
        elif action == "update_task_status":
            return _update_task_status(conn, task_id, status, agent_id)
        elif action == "release_task":
            return _release_task(conn, task_id, agent_id)
        elif action == "complete_task":
            return _complete_task(conn, task_id, agent_id)
        elif action == "list_tasks":
            return _list_tasks(conn, project_id, status)
        elif action == "lock_file":
            return _lock_file(conn, file_path, agent_id)
        elif action == "unlock_file":
            return _unlock_file(conn, file_path, agent_id)
        elif action == "check_lock":
            return _check_lock(conn, file_path)
        elif action == "send_message":
            return _send_message(conn, agent_id, to_agent, message_type, payload)
        elif action == "read_messages":
            return _read_messages(conn, agent_id)
        elif action == "get_project_state":
            return _get_project_state(conn, project_id)
        elif action == "update_project_state":
            return _update_project_state(conn, project_id, key, value, agent_id)
        else:
            return _err(ErrorCode.INVALID_ARGUMENT, f"Unknown action: {action}")
    except Exception as e:
        logger.exception("memory_coordinate failed")
        return _err(ErrorCode.DB_ERROR, f"coordination failed: {e}")
    finally:
        try:
            from infra.db import safe_close_db
            safe_close_db(conn)
        except Exception:
            pass


def _get_agent_id() -> str:
    """Get the current agent's ID."""
    try:
        from agent_context import get_agent
        ctx = get_agent()
        agent_id = ctx.agent_id or "default"
    except (ImportError, Exception):
        agent_id = os.environ.get("MEMORY_AGENT_ID", "default")

    # Validate: max 128 chars, alphanumeric + hyphens + underscores only
    import re
    if not agent_id or len(agent_id) > 128 or not re.match(r'^[a-zA-Z0-9_\-]+$', agent_id):
        return "default"
    return agent_id


def _create_task(conn, project_id, task_type, description, assigned_to, agent_id) -> str:
    """Create a new task."""
    if not task_type:
        return _err(ErrorCode.INVALID_ARGUMENT, "task_type is required")

    now = time.time()
    cursor = conn.execute(
        "INSERT INTO shared_tasks (project_id, task_type, description, assigned_to, status, created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (project_id, task_type, description, assigned_to, "pending" if not assigned_to else "active", agent_id, now, now),
    )
    conn.commit()
    task_id = cursor.lastrowid

    return json.dumps({
        "ok": True,
        "task_id": task_id,
        "project_id": project_id,
        "task_type": task_type,
        "assigned_to": assigned_to,
        "status": "active" if assigned_to else "pending",
    })


def _claim_task(conn, task_id, agent_id) -> str:
    """Claim a task for this agent."""
    if not task_id:
        return _err(ErrorCode.INVALID_ARGUMENT, "task_id is required")

    row = conn.execute("SELECT status, assigned_to FROM shared_tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        return _err(ErrorCode.NOT_FOUND, f"Task {task_id} not found")

    if row[0] == "active" and row[1] and row[1] != agent_id:
        return _err(ErrorCode.CONFLICT, f"Task already claimed by {row[1]}")

    conn.execute(
        "UPDATE shared_tasks SET assigned_to=?, status='active', updated_at=? WHERE id=?",
        (agent_id, time.time(), task_id),
    )
    conn.commit()

    return json.dumps({"ok": True, "task_id": task_id, "assigned_to": agent_id, "status": "active"})


def _update_task_status(conn, task_id, status, agent_id) -> str:
    """Update task status. This is the primary coordination primitive.

    Valid transitions:
        pending -> active (claiming)
        active -> completed (finishing)
        active -> blocked (blocked by dependency)
        blocked -> active (unblocked)
        active -> pending (releasing back to pool)
    """
    if not task_id:
        return _err(ErrorCode.INVALID_ARGUMENT, "task_id is required")
    if not status:
        return _err(ErrorCode.INVALID_ARGUMENT, "status is required")

    valid_statuses = {"pending", "active", "completed", "blocked", "abandoned"}
    if status not in valid_statuses:
        return _err(ErrorCode.INVALID_ARGUMENT, f"status must be one of: {valid_statuses}")

    row = conn.execute("SELECT status, assigned_to FROM shared_tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        return _err(ErrorCode.NOT_FOUND, f"Task {task_id} not found")

    # Only the assigned agent or creator can update status
    if row[1] and row[1] != agent_id:
        return _err(ErrorCode.CONFLICT, f"Task assigned to {row[1]}, not {agent_id}")

    conn.execute(
        "UPDATE shared_tasks SET status=?, updated_at=? WHERE id=?",
        (status, time.time(), task_id),
    )

    # Passive audit: record status transition
    try:
        conn.execute(
            "INSERT INTO coordination_audit (action, agent_id, target, detail, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            ("task_status_changed", agent_id, str(task_id),
             json.dumps({"from": row[0], "to": status}), time.time()),
        )
    except Exception:
        pass

    conn.commit()

    return json.dumps({
        "ok": True, "task_id": task_id, "status": status,
        "from_status": row[0], "updated_by": agent_id,
    })


def _release_task(conn, task_id, agent_id) -> str:
    """Release a task back to the pool."""
    if not task_id:
        return _err(ErrorCode.INVALID_ARGUMENT, "task_id is required")

    conn.execute(
        "UPDATE shared_tasks SET assigned_to=NULL, status='pending', updated_at=? WHERE id=? AND assigned_to=?",
        (time.time(), task_id, agent_id),
    )
    conn.commit()

    return json.dumps({"ok": True, "task_id": task_id, "status": "pending"})


def _complete_task(conn, task_id, agent_id) -> str:
    """Mark a task as completed."""
    if not task_id:
        return _err(ErrorCode.INVALID_ARGUMENT, "task_id is required")

    conn.execute(
        "UPDATE shared_tasks SET status='completed', updated_at=? WHERE id=?",
        (time.time(), task_id),
    )
    conn.commit()

    return json.dumps({"ok": True, "task_id": task_id, "status": "completed"})


def _list_tasks(conn, project_id, status) -> str:
    """List tasks for a project."""
    where = "WHERE project_id=?"
    params = [project_id]
    if status:
        where += " AND status=?"
        params.append(status)

    rows = conn.execute(
        f"SELECT id, project_id, task_type, description, assigned_to, status, created_by, created_at, updated_at "
        f"FROM shared_tasks {where} ORDER BY created_at DESC",
        params,
    ).fetchall()

    tasks = []
    for r in rows:
        tasks.append({
            "id": r[0], "project_id": r[1], "task_type": r[2],
            "description": r[3], "assigned_to": r[4], "status": r[5],
            "created_by": r[6], "created_at": r[7], "updated_at": r[8],
        })

    return json.dumps({"ok": True, "tasks": tasks, "count": len(tasks)})


def _validate_file_path(file_path: str) -> str | None:
    """Validate file path. Returns normalized path or None if invalid."""
    if not file_path or len(file_path) > 4096:
        return None
    # Block path traversal
    if ".." in file_path:
        return None
    return file_path


def _lock_file(conn, file_path, agent_id) -> str:
    """Acquire exclusive lock on a file."""
    file_path = _validate_file_path(file_path)
    if not file_path:
        return _err(ErrorCode.INVALID_ARGUMENT, "file_path is required and must not contain '..'")

    # Check if already locked
    existing = conn.execute(
        "SELECT locked_by, expires_at FROM file_locks WHERE file_path=?",
        (file_path,),
    ).fetchone()

    if existing:
        if existing[0] == agent_id:
            # Already locked by this agent — refresh
            conn.execute(
                "UPDATE file_locks SET locked_at=?, expires_at=? WHERE file_path=?",
                (time.time(), time.time() + 300, file_path),
            )
            conn.commit()
            return json.dumps({"ok": True, "file_path": file_path, "locked_by": agent_id, "refreshed": True})

        # Check if lock expired
        if existing[1] and existing[1] < time.time():
            conn.execute("DELETE FROM file_locks WHERE file_path=?", (file_path,))
        else:
            return _err(ErrorCode.CONFLICT, f"File locked by {existing[0]}")

    try:
        conn.execute("BEGIN IMMEDIATE")
    except Exception:
        pass

    conn.execute(
        "INSERT OR REPLACE INTO file_locks (file_path, locked_by, locked_at, expires_at) VALUES (?, ?, ?, ?)",
        (file_path, agent_id, time.time(), time.time() + 300),
    )
    conn.commit()

    return json.dumps({"ok": True, "file_path": file_path, "locked_by": agent_id, "expires_in": 300})


def _unlock_file(conn, file_path, agent_id) -> str:
    """Release file lock."""
    file_path = _validate_file_path(file_path)
    if not file_path:
        return _err(ErrorCode.INVALID_ARGUMENT, "file_path is required and must not contain '..'")

    existing = conn.execute(
        "SELECT locked_by FROM file_locks WHERE file_path=?",
        (file_path,),
    ).fetchone()

    if existing and existing[0] != agent_id:
        return _err(ErrorCode.CONFLICT, f"File locked by {existing[0]}, not {agent_id}")

    conn.execute("DELETE FROM file_locks WHERE file_path=?", (file_path,))
    conn.commit()

    return json.dumps({"ok": True, "file_path": file_path, "unlocked_by": agent_id})


def _check_lock(conn, file_path) -> str:
    """Check if a file is locked."""
    file_path = _validate_file_path(file_path)
    if not file_path:
        return _err(ErrorCode.INVALID_ARGUMENT, "file_path is required and must not contain '..'")

    row = conn.execute(
        "SELECT locked_by, locked_at, expires_at FROM file_locks WHERE file_path=?",
        (file_path,),
    ).fetchone()

    if not row:
        return json.dumps({"ok": True, "locked": False, "file_path": file_path})

    # Check if expired
    if row[2] and row[2] < time.time():
        conn.execute("DELETE FROM file_locks WHERE file_path=?", (file_path,))
        conn.commit()
        return json.dumps({"ok": True, "locked": False, "file_path": file_path, "expired": True})

    return json.dumps({
        "ok": True,
        "locked": True,
        "locked_by": row[0],
        "locked_at": row[1],
        "expires_at": row[2],
        "file_path": file_path,
    })


def _send_message(conn, from_agent, to_agent, message_type, payload) -> str:
    """Send message to another agent."""
    if not to_agent or not message_type:
        return _err(ErrorCode.INVALID_ARGUMENT, "to_agent and message_type are required")

    # Validate message_type
    import re
    if not re.match(r'^[a-zA-Z0-9_\-]+$', message_type) or len(message_type) > 64:
        return _err(ErrorCode.INVALID_ARGUMENT, "message_type must be alphanumeric, max 64 chars")

    # Validate payload size (max 1MB)
    if payload and len(payload) > 1_048_576:
        return _err(ErrorCode.INVALID_ARGUMENT, "payload too large (max 1MB)")

    # Validate to_agent
    if to_agent != "*" and (len(to_agent) > 128 or not re.match(r'^[a-zA-Z0-9_\-]+$', to_agent)):
        return _err(ErrorCode.INVALID_ARGUMENT, "to_agent must be alphanumeric, max 128 chars")

    now = time.time()
    conn.execute(
        "INSERT INTO agent_messages (from_agent, to_agent, message_type, payload, status, created_at) "
        "VALUES (?, ?, ?, ?, 'pending', ?)",
        (from_agent, to_agent, message_type, payload, now),
    )
    conn.commit()

    return json.dumps({
        "ok": True,
        "from_agent": from_agent,
        "to_agent": to_agent,
        "message_type": message_type,
    })


def _read_messages(conn, agent_id) -> str:
    """Read pending messages for this agent."""
    rows = conn.execute(
        "SELECT id, from_agent, message_type, payload, created_at "
        "FROM agent_messages WHERE (to_agent=? OR to_agent='*') AND status='pending' "
        "ORDER BY created_at ASC LIMIT 50",
        (agent_id,),
    ).fetchall()

    messages = []
    msg_ids = []
    for r in rows:
        messages.append({
            "id": r[0], "from_agent": r[1], "message_type": r[2],
            "payload": r[3], "created_at": r[4],
        })
        msg_ids.append(r[0])

    # Mark as delivered
    if msg_ids:
        placeholders = ",".join("?" for _ in msg_ids)
        conn.execute(
            f"UPDATE agent_messages SET status='delivered', delivered_at=? WHERE id IN ({placeholders})",
            [time.time()] + msg_ids,
        )
        conn.commit()

    return json.dumps({"ok": True, "messages": messages, "count": len(messages)})


def _get_project_state(conn, project_id) -> str:
    """Get project state."""
    rows = conn.execute(
        "SELECT key, value, updated_by, updated_at FROM project_state WHERE project_id=?",
        (project_id,),
    ).fetchall()

    state = {}
    for r in rows:
        state[r[0]] = {"value": r[1], "updated_by": r[2], "updated_at": r[3]}

    return json.dumps({"ok": True, "project_id": project_id, "state": state})


def _update_project_state(conn, project_id, key, value, agent_id) -> str:
    """Update project state."""
    if not key:
        return _err(ErrorCode.INVALID_ARGUMENT, "key is required")

    now = time.time()
    conn.execute(
        "INSERT OR REPLACE INTO project_state (project_id, key, value, updated_by, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (project_id, key, value, agent_id, now),
    )
    conn.commit()

    return json.dumps({"ok": True, "project_id": project_id, "key": key, "updated_by": agent_id})
