"""Coordination integration hooks for the agentic-memory system.

Provides lightweight functions that other modules call to:
- Acquire/release file locks during saves
- Auto-create tasks from cron events
- Claim pending tasks on session start

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
    """Create tasks for unresolved contradictions. Returns count of tasks created."""
    count = 0
    for c in contradictions:
        src = c.get("source", "")
        tgt = c.get("target", "")
        if not src or not tgt or src == tgt:
            continue
        confidence = c.get("confidence", "unknown")
        desc = f"Resolve contradiction between '{src}' and '{tgt}' (confidence: {confidence})"
        task_id = create_coordination_task(
            task_type="resolve_contradiction",
            description=desc,
            project_id="knowledge_graph",
            created_by="cron_contradictions",
            conn=conn,
        )
        if task_id:
            count += 1
    return count


def create_integrity_tasks(findings: list[dict], conn: sqlite3.Connection | None = None) -> int:
    """Create tasks for integrity check findings. Returns count of tasks created."""
    count = 0
    for f in findings:
        severity = f.get("severity", "info")
        if severity not in ("critical", "warning"):
            continue
        message = f.get("message", "unknown issue")
        check_name = f.get("check", f.get("code", "integrity"))
        desc = f"[{severity.upper()}] {check_name}: {message}"
        task_id = create_coordination_task(
            task_type="fix_integrity",
            description=desc,
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
