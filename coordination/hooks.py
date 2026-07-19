"""Coordination integration hooks for the agentic-memory system.

Provides lightweight functions that other modules call to:
- Acquire/release file locks during saves (with project state updates)
- Queue messages when locks conflict (agent-to-agent notification)
- Auto-create tasks from cron events (with agent dispatch)
- Claim pending tasks on session start
- Enrich search results with coordination context
- Detect supersessions and create follow-up tasks

All functions are fail-safe: exceptions are caught and logged, never raised.

All functions accept an optional `conn` parameter. When provided (tests),
the caller owns the connection lifecycle. When None (production), the
function creates and closes its own connection.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def _make_conn() -> sqlite3.Connection | None:
    """Create a new database connection. Returns None if DB not available."""
    try:
        from infra.infrastructure import resolve_active_memory_dir
        db_path = resolve_active_memory_dir() / "memory.db"
        if not db_path.exists():
            return None
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn
    except Exception as e:
        logger.debug("coordination hook: cannot create conn: %s", e)
        return None


def _get_agent_id() -> str:
    """Get current agent ID from environment."""
    agent_id = os.environ.get("MEMORY_AGENT_ID", "default")
    if not agent_id or len(agent_id) > 128 or not re.match(r'^[a-zA-Z0-9_\-]+$', agent_id):
        return "default"
    return agent_id


# ── Save Pipeline Integration ───────────────────────────────────────────

def acquire_save_lock(file_path: str, agent_id: str | None = None, conn: sqlite3.Connection | None = None) -> bool:
    """Acquire a file lock before writing a memory. Returns True if acquired.

    Called by memory_save before the saga writes to disk. Prevents two
    agents from writing to the same .md file concurrently.
    """
    own_conn = conn is None
    if own_conn:
        conn = _make_conn()
    if not conn:
        return True  # Fail open if DB unavailable

    try:
        if not agent_id:
            agent_id = _get_agent_id()

        now = time.time()
        expires_at = now + 300  # 5 minute TTL

        # Check existing lock
        existing = conn.execute(
            "SELECT locked_by, expires_at FROM file_locks WHERE file_path=?",
            (file_path,),
        ).fetchone()

        if existing:
            if existing[0] == agent_id:
                conn.execute(
                    "UPDATE file_locks SET locked_at=?, expires_at=? WHERE file_path=?",
                    (now, expires_at, file_path),
                )
                if own_conn:
                    conn.commit()
                return True
            if existing[1] and existing[1] < now:
                conn.execute("DELETE FROM file_locks WHERE file_path=?", (file_path,))
            else:
                logger.warning("Save lock conflict: %s held by %s", file_path, existing[0])
                return False

        conn.execute(
            "INSERT OR REPLACE INTO file_locks (file_path, locked_by, locked_at, expires_at) VALUES (?, ?, ?, ?)",
            (file_path, agent_id, now, expires_at),
        )
        if own_conn:
            conn.commit()
        return True
    except Exception as e:
        logger.debug("acquire_save_lock failed: %s", e)
        return True  # Fail open
    finally:
        if own_conn:
            try:
                conn.close()
            except Exception:
                pass


def release_save_lock(file_path: str, agent_id: str | None = None, conn: sqlite3.Connection | None = None) -> None:
    """Release a file lock after writing a memory."""
    own_conn = conn is None
    if own_conn:
        conn = _make_conn()
    if not conn:
        return

    try:
        if not agent_id:
            agent_id = _get_agent_id()
        conn.execute("DELETE FROM file_locks WHERE file_path=? AND locked_by=?", (file_path, agent_id))
        if own_conn:
            conn.commit()
    except Exception as e:
        logger.debug("release_save_lock failed: %s", e)
    finally:
        if own_conn:
            try:
                conn.close()
            except Exception:
                pass


# ── Cron Task Auto-Creation ─────────────────────────────────────────────

def create_coordination_task(
    task_type: str,
    description: str,
    project_id: str = "default",
    assigned_to: str | None = None,
    created_by: str = "cron",
    conn: sqlite3.Connection | None = None,
) -> int | None:
    """Create a coordination task from a cron event. Returns task ID or None."""
    own_conn = conn is None
    if own_conn:
        conn = _make_conn()
    if not conn:
        return None

    try:
        # Ensure tables exist
        conn.execute("""
            CREATE TABLE IF NOT EXISTS shared_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL, task_type TEXT NOT NULL,
                description TEXT, assigned_to TEXT, status TEXT DEFAULT 'pending',
                created_by TEXT NOT NULL, created_at REAL, updated_at REAL,
                depends_on INTEGER REFERENCES shared_tasks(id)
            )
        """)
        if own_conn:
            conn.commit()

        now = time.time()
        status = "active" if assigned_to else "pending"
        cursor = conn.execute(
            "INSERT INTO shared_tasks (project_id, task_type, description, assigned_to, status, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, task_type, description, assigned_to, status, created_by, now, now),
        )
        if own_conn:
            conn.commit()
        task_id = cursor.lastrowid

        # Passive audit
        try:
            conn.execute(
                "INSERT INTO coordination_audit (action, agent_id, target, detail, timestamp) VALUES (?, ?, ?, ?, ?)",
                ("task_created", created_by, str(task_id),
                 json.dumps({"type": task_type, "assigned_to": assigned_to}), now),
            )
            if own_conn:
                conn.commit()
        except Exception:
            pass

        logger.info("Coordination task created: id=%d type=%s", task_id, task_type)
        return task_id
    except Exception as e:
        logger.debug("create_coordination_task failed: %s", e)
        return None
    finally:
        if own_conn:
            try:
                conn.close()
            except Exception:
                pass


def create_contradiction_tasks(contradictions: list[dict], conn: sqlite3.Connection | None = None) -> int:
    """Create tasks for unresolved contradictions and dispatch to drift-investigator."""
    count = 0
    for c in contradictions:
        src = c.get("source", "")
        tgt = c.get("target", "")
        if not src or not tgt or src == tgt:
            continue
        confidence = c.get("confidence", "unknown")
        desc = f"Resolve contradiction between '{src}' and '{tgt}' (confidence: {confidence})"
        task_id = create_and_dispatch_task(
            task_type="resolve_contradiction",
            description=desc,
            target_agent="drift-investigator",
            project_id="knowledge_graph",
            created_by="cron_contradictions",
            conn=conn,
        )
        if task_id:
            count += 1
    return count


def create_integrity_tasks(findings: list[dict], conn: sqlite3.Connection | None = None) -> int:
    """Create tasks for integrity check findings and dispatch to kg-engineer."""
    count = 0
    for f in findings:
        severity = f.get("severity", "info")
        if severity not in ("critical", "warning"):
            continue
        message = f.get("message", "unknown issue")
        check_name = f.get("check", f.get("code", "integrity"))
        desc = f"[{severity.upper()}] {check_name}: {message}"
        task_id = create_and_dispatch_task(
            task_type="fix_integrity",
            description=desc,
            target_agent="kg-engineer",
            project_id="system",
            created_by="cron_integrity",
            conn=conn,
        )
        if task_id:
            count += 1
    return count


# ── Session Start Task Claiming ─────────────────────────────────────────

def claim_pending_tasks(agent_id: str | None = None, project_id: str = "default", limit: int = 3, conn: sqlite3.Connection | None = None) -> list[dict]:
    """Claim pending tasks for this agent on session start. Returns claimed tasks."""
    own_conn = conn is None
    if own_conn:
        conn = _make_conn()
    if not conn:
        return []

    try:
        if not agent_id:
            agent_id = _get_agent_id()

        if project_id and project_id != "default":
            rows = conn.execute(
                "SELECT id, task_type, description, created_by FROM shared_tasks "
                "WHERE status='pending' AND project_id=? ORDER BY created_at ASC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, task_type, description, created_by FROM shared_tasks "
                "WHERE status='pending' ORDER BY created_at ASC LIMIT ?",
                (limit,),
            ).fetchall()

        if not rows:
            return []

        claimed = []
        now = time.time()
        for row in rows:
            task_id, task_type, description, created_by = row
            conn.execute(
                "UPDATE shared_tasks SET assigned_to=?, status='active', updated_at=? WHERE id=? AND status='pending'",
                (agent_id, now, task_id),
            )
            if conn.total_changes:
                claimed.append({
                    "id": task_id,
                    "task_type": task_type,
                    "description": description,
                    "created_by": created_by,
                })
                try:
                    conn.execute(
                        "INSERT INTO coordination_audit (action, agent_id, target, detail, timestamp) VALUES (?, ?, ?, ?, ?)",
                        ("task_claimed", agent_id, str(task_id),
                         json.dumps({"type": task_type}), now),
                    )
                except Exception:
                    pass

        if own_conn:
            conn.commit()
        return claimed
    except Exception as e:
        logger.debug("claim_pending_tasks failed: %s", e)
        return []
    finally:
        if own_conn:
            try:
                conn.close()
            except Exception:
                pass


# ── Project State Updates ───────────────────────────────────────────────

def update_project_activity(
    file_path: str,
    agent_id: str | None = None,
    activity: str = "writing",
    project_id: str = "default",
    conn: sqlite3.Connection | None = None,
) -> None:
    """Update project state to reflect agent activity during save.

    Called by memory_save to let other agents know what this agent is doing.
    """
    own_conn = conn is None
    if own_conn:
        conn = _make_conn()
    if not conn:
        return

    try:
        if not agent_id:
            agent_id = _get_agent_id()

        now = time.time()
        conn.execute(
            "INSERT OR REPLACE INTO project_state (project_id, key, value, updated_by, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (project_id, f"file:{file_path}", json.dumps({"activity": activity, "agent": agent_id}),
             agent_id, now),
        )
        conn.execute(
            "INSERT OR REPLACE INTO project_state (project_id, key, value, updated_by, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (project_id, f"agent:{agent_id}:status", json.dumps({"activity": activity, "file": file_path}),
             agent_id, now),
        )
        if own_conn:
            conn.commit()
    except Exception as e:
        logger.debug("update_project_activity failed: %s", e)
    finally:
        if own_conn:
            try:
                conn.close()
            except Exception:
                pass


def clear_project_activity(
    file_path: str,
    agent_id: str | None = None,
    project_id: str = "default",
    conn: sqlite3.Connection | None = None,
) -> None:
    """Clear project state activity after save completes."""
    own_conn = conn is None
    if own_conn:
        conn = _make_conn()
    if not conn:
        return

    try:
        if not agent_id:
            agent_id = _get_agent_id()

        conn.execute(
            "DELETE FROM project_state WHERE project_id=? AND key=?",
            (project_id, f"file:{file_path}"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO project_state (project_id, key, value, updated_by, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (project_id, f"agent:{agent_id}:status", json.dumps({"activity": "idle"}),
             agent_id, time.time()),
        )
        if own_conn:
            conn.commit()
    except Exception as e:
        logger.debug("clear_project_activity failed: %s", e)
    finally:
        if own_conn:
            try:
                conn.close()
            except Exception:
                pass


# ── Lock Conflict Messaging ─────────────────────────────────────────────

def queue_lock_conflict_message(
    file_path: str,
    held_by: str,
    requested_by: str,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Send a message to the lock holder when a lock conflict occurs."""
    own_conn = conn is None
    if own_conn:
        conn = _make_conn()
    if not conn:
        return

    try:
        now = time.time()
        payload = json.dumps({
            "type": "lock_conflict",
            "file_path": file_path,
            "waiting_agent": requested_by,
        })
        conn.execute(
            "INSERT INTO agent_messages (from_agent, to_agent, message_type, payload, status, created_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?)",
            (requested_by, held_by, "lock_conflict", payload, now),
        )
        if own_conn:
            conn.commit()
    except Exception as e:
        logger.debug("queue_lock_conflict_message failed: %s", e)
    finally:
        if own_conn:
            try:
                conn.close()
            except Exception:
                pass


# ── Agent Dispatch ──────────────────────────────────────────────────────

def send_task_notification(
    task_id: int,
    task_type: str,
    description: str,
    target_agent: str,
    created_by: str = "system",
    conn: sqlite3.Connection | None = None,
) -> None:
    """Notify a target agent that a task has been created for them."""
    own_conn = conn is None
    if own_conn:
        conn = _make_conn()
    if not conn:
        return

    try:
        now = time.time()
        payload = json.dumps({
            "type": "task_assigned",
            "task_id": task_id,
            "task_type": task_type,
            "description": description,
        })
        conn.execute(
            "INSERT INTO agent_messages (from_agent, to_agent, message_type, payload, status, created_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?)",
            (created_by, target_agent, "task_assigned", payload, now),
        )
        if own_conn:
            conn.commit()
    except Exception as e:
        logger.debug("send_task_notification failed: %s", e)
    finally:
        if own_conn:
            try:
                conn.close()
            except Exception:
                pass


def create_and_dispatch_task(
    task_type: str,
    description: str,
    target_agent: str,
    project_id: str = "default",
    created_by: str = "system",
    conn: sqlite3.Connection | None = None,
) -> int | None:
    """Create a task AND notify the target agent. Returns task ID or None."""
    task_id = create_coordination_task(
        task_type=task_type,
        description=description,
        project_id=project_id,
        assigned_to=target_agent,
        created_by=created_by,
        conn=conn,
    )
    if task_id:
        send_task_notification(
            task_id=task_id,
            task_type=task_type,
            description=description,
            target_agent=target_agent,
            created_by=created_by,
            conn=conn,
        )
    return task_id


# ── Search Context Enrichment ───────────────────────────────────────────

def get_coordination_context(
    project_id: str = "default",
    conn: sqlite3.Connection | None = None,
) -> dict:
    """Get coordination context to enrich search results."""
    own_conn = conn is None
    if own_conn:
        conn = _make_conn()
    if not conn:
        return {}

    try:
        now = time.time()
        ctx = {}

        try:
            locks = conn.execute(
                "SELECT file_path, locked_by, expires_at FROM file_locks WHERE expires_at > ?",
                (now,),
            ).fetchall()
            ctx["active_locks"] = [
                {"file": r[0], "agent": r[1], "expires_in": max(0, r[2] - now)}
                for r in locks
            ]
        except Exception:
            ctx["active_locks"] = []

        try:
            activity = conn.execute(
                "SELECT key, value, updated_by FROM project_state "
                "WHERE project_id=? AND key LIKE 'agent:%:status'",
                (project_id,),
            ).fetchall()
            ctx["agent_activity"] = []
            for r in activity:
                try:
                    val = json.loads(r[1]) if r[1] else {}
                except Exception:
                    val = {}
                ctx["agent_activity"].append({
                    "agent": r[2],
                    "activity": val.get("activity", "unknown"),
                    "file": val.get("file"),
                })
        except Exception:
            ctx["agent_activity"] = []

        try:
            tasks = conn.execute(
                "SELECT id, task_type, description, assigned_to FROM shared_tasks "
                "WHERE status IN ('pending', 'active') ORDER BY created_at DESC LIMIT 10",
            ).fetchall()
            ctx["active_tasks"] = [
                {"id": r[0], "type": r[1], "description": r[2][:80], "assigned_to": r[3]}
                for r in tasks
            ]
        except Exception:
            ctx["active_tasks"] = []

        return ctx
    except Exception as e:
        logger.debug("get_coordination_context failed: %s", e)
        return {}
    finally:
        if own_conn:
            try:
                conn.close()
            except Exception:
                pass


# ── Supersession Task Creation ──────────────────────────────────────────

def detect_supersession_tasks(
    note_id: str,
    category: str,
    created_by: str = "save_pipeline",
    conn: sqlite3.Connection | None = None,
) -> int | None:
    """Check if a newly saved memory supersedes existing notes and create tasks."""
    own_conn = conn is None
    if own_conn:
        conn = _make_conn()
    if not conn:
        return None

    try:
        row = conn.execute(
            "SELECT supersedes FROM memories WHERE note_id=?",
            (note_id,),
        ).fetchone()

        if not row or not row[0]:
            return None

        supersedes = row[0]
        desc = (
            f"Note '{note_id}' supersedes '{supersedes}'. "
            f"Review and update any references to the old note."
        )
        task_id = create_coordination_task(
            task_type="update_references",
            description=desc,
            project_id="knowledge_graph",
            created_by=created_by,
            conn=conn,
        )
        return task_id
    except Exception as e:
        logger.debug("detect_supersession_tasks failed: %s", e)
        return None
    finally:
        if own_conn:
            try:
                conn.close()
            except Exception:
                pass
