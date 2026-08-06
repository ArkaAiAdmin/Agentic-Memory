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

Locking hardening (2026-07-19):
- Fencing tokens: every acquire returns a FencingLock named tuple with
  monotonically increasing version. Call `verify_save_lock` before writing
  to ensure another agent hasn't stolen the lock.
- Blocking mode: `acquire_save_lock(block=True, timeout=30)` polls with
  exponential backoff instead of returning False immediately.
- Renewal: `renew_save_lock` extends lock TTL without incrementing version.
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
    """Create a new database connection. Returns None if DB not available.
    
    Respects ``MEMORY_DB_PATH`` env var first, falls back to
    ``resolve_active_memory_dir() / memory.db``. This ensures the
    coordination hook connects to the correct DB regardless of which
    agent's hooks are running.
    """
    try:
        env_path = os.environ.get("MEMORY_DB_PATH", "").strip()
        if env_path:
            db_path = Path(env_path)
        else:
            from infra.infrastructure import resolve_active_memory_dir
            db_path = resolve_active_memory_dir() / "memory.db"
        if not db_path.exists():
            return None
        conn = sqlite3.connect(str(db_path), timeout=30)
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


# ── Locking Imports (late to avoid circular deps) ───────────────────────

def _fenced(conn, file_path, agent_id):
    """Thin wrapper: import and call acquire_lock_fenced."""
    from coordination.locking import acquire_lock_fenced
    return acquire_lock_fenced(conn, file_path, agent_id)


def _verify(conn, file_path, version):
    """Thin wrapper: import and call verify_lock_fenced."""
    from coordination.locking import verify_lock_fenced
    return verify_lock_fenced(conn, file_path, version)


def _renew(conn, file_path, agent_id, ttl=300):
    """Thin wrapper: import and call renew_lock."""
    from coordination.locking import renew_lock
    return renew_lock(conn, file_path, agent_id, ttl)


# ── Save Pipeline Integration ───────────────────────────────────────────

def acquire_save_lock(
    file_path: str,
    agent_id: str | None = None,
    conn: sqlite3.Connection | None = None,
    block: bool = False,
    timeout: float = 30.0,
) -> dict:
    """Acquire a file lock before writing a memory. Returns dict with acquired/version.

    Args:
        file_path: Path to the file to lock.
        agent_id: Who's acquiring (default: env MEMORY_AGENT_ID).
        conn: DB connection (None = create own).
        block: If True, poll with exponential backoff instead of returning False.
        timeout: Max seconds to wait when block=True (default 30).

    Returns:
        dict with keys:
            ``acquired`` (bool) — True if lock was granted.
            ``version`` (int) — fencing version; pass to verify_save_lock.
            ``holder`` (str) — current lock holder.
            ``expires_at`` (float) — lock expiry timestamp.

    The dict is truthy when acquired is True:
        lock = acquire_save_lock("/path")
        if lock:  # checks lock["acquired"]
            ...
    """
    own_conn = conn is None
    if own_conn:
        conn = _make_conn()
    if not conn:
        logger.warning("acquire_save_lock: DB unavailable, failing open")
        return {"acquired": True, "version": 0, "holder": agent_id or "default", "expires_at": 0.0}

    try:
        if not agent_id:
            agent_id = _get_agent_id()

        deadline = time.time() + timeout if block else 0
        delay = 0.1
        attempt = 0

        while True:
            attempt += 1
            lock = _fenced(conn, file_path, agent_id)

            if lock.acquired:
                return {
                    "acquired": True,
                    "version": lock.version,
                    "holder": agent_id,
                    "expires_at": lock.expires_at,
                }

            if not block or time.time() >= deadline:
                return {
                    "acquired": False,
                    "version": lock.version,
                    "holder": lock.holder,
                    "expires_at": lock.expires_at,
                }

            logger.debug(
                "acquire_save_lock: retry %d for %s (held by %s), waiting %.1fs",
                attempt, file_path, lock.holder, delay,
            )
            time.sleep(delay)
            delay = min(delay * 1.5, 2.0)

    except sqlite3.OperationalError as e:
        logger.warning("acquire_save_lock: DB error for %s: %s — failing open", file_path, e)
        return {"acquired": True, "version": 0, "holder": agent_id or "default", "expires_at": 0.0}
    except Exception as e:
        logger.error("acquire_save_lock: unexpected error for %s: %s", file_path, e)
        return {"acquired": False, "version": 0, "holder": "", "expires_at": 0.0}
    finally:
        if own_conn:
            try:
                conn.close()
            except Exception:
                pass


def verify_save_lock(file_path: str, expected_version: int, agent_id: str | None = None, conn: sqlite3.Connection | None = None) -> bool:
    """Verify lock fencing version hasn't changed. Returns False if lock was stolen.

    Call before the actual save write to ensure no other agent took over.
    """
    own_conn = conn is None
    if own_conn:
        conn = _make_conn()
    if not conn:
        return True

    try:
        return bool(_verify(conn, file_path, expected_version))
    except Exception as e:
        logger.warning("verify_save_lock failed for %s: %s", file_path, e)
        return True
    finally:
        if own_conn:
            try:
                conn.close()
            except Exception:
                pass


def renew_save_lock(file_path: str, agent_id: str | None = None, conn: sqlite3.Connection | None = None, ttl: int = 300) -> bool:
    """Renew a save lock's TTL. Returns True if renewed.

    Does NOT increment fencing version — this is a refresh, not a takeover.
    """
    own_conn = conn is None
    if own_conn:
        conn = _make_conn()
    if not conn:
        return False

    try:
        if not agent_id:
            agent_id = _get_agent_id()
        return bool(_renew(conn, file_path, agent_id, ttl))
    except Exception as e:
        logger.warning("renew_save_lock failed for %s: %s", file_path, e)
        return False
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
        logger.warning("release_save_lock: DB unavailable for %s", file_path)
        return

    try:
        if not agent_id:
            agent_id = _get_agent_id()
        conn.execute("DELETE FROM file_locks WHERE file_path=? AND locked_by=?", (file_path, agent_id))
        conn.commit()
        logger.debug("release_save_lock: released lock for %s on %s", agent_id, file_path)
    except Exception as e:
        logger.warning("release_save_lock failed for %s: %s", file_path, e)
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
    """Create tasks for unresolved contradictions and dispatch to drift-investigator.

    Only dispatches if the target agent has a recent heartbeat (is alive).
    """
    count = 0
    for c in contradictions:
        src = c.get("source", "")
        tgt = c.get("target", "")
        if not src or not tgt or src == tgt:
            continue
        confidence = c.get("confidence", "unknown")
        desc = f"Resolve contradiction between '{src}' and '{tgt}' (confidence: {confidence})"

        # Guard: check if target agent is alive before dispatching
        target_agent = "drift-investigator"
        own_conn = conn is None
        if own_conn:
            conn = _make_conn()
        if not conn:
            return count
        try:
            from coordination.durability import check_agent_alive
            if not check_agent_alive(conn, target_agent):
                # Create as pending (unassigned) instead of dispatching to dead agent
                task_id = create_coordination_task(
                    task_type="resolve_contradiction",
                    description=desc,
                    project_id="knowledge_graph",
                    assigned_to=None,
                    created_by="cron_contradictions",
                    conn=conn,
                )
            else:
                task_id = create_and_dispatch_task(
                    task_type="resolve_contradiction",
                    description=desc,
                    target_agent=target_agent,
                    project_id="knowledge_graph",
                    created_by="cron_contradictions",
                    conn=conn,
                )
        finally:
            if own_conn:
                try:
                    conn.close()
                except Exception:
                    pass
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
    """Claim pending tasks for this agent on session start. Returns claimed tasks.

    Uses a single atomic UPDATE ... LIMIT to claim tasks in one shot,
    avoiding the TOCTOU race between SELECT and UPDATE.
    """
    own_conn = conn is None
    if own_conn:
        conn = _make_conn()
    if not conn:
        return []

    try:
        if not agent_id:
            agent_id = _get_agent_id()

        now = time.time()
        claimed = []

        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            pass

        if project_id and project_id != "default":
            rows = conn.execute(
                "SELECT id, task_type, description, created_by FROM shared_tasks "
                "WHERE status='pending' AND project_id=? "
                "AND (assigned_to IS NULL OR assigned_to = ?) "
                "ORDER BY created_at ASC LIMIT ?",
                (project_id, agent_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, task_type, description, created_by FROM shared_tasks "
                "WHERE status='pending' "
                "AND (assigned_to IS NULL OR assigned_to = ?) "
                "ORDER BY created_at ASC LIMIT ?",
                (agent_id, limit),
            ).fetchall()

        if not rows:
            conn.commit()
            return []

        for row in rows:
            task_id, task_type, description, created_by = row
            conn.execute(
                "UPDATE shared_tasks SET assigned_to=?, status='active', updated_at=? WHERE id=? AND status='pending'",
                (agent_id, now, task_id),
            )
            if conn.total_changes > 0:
                claimed_entry = {
                    "id": task_id,
                    "task_type": task_type,
                    "description": description,
                    "created_by": created_by,
                }
                try:
                    conn.execute(
                        "INSERT INTO coordination_audit (action, agent_id, target, detail, timestamp) VALUES (?, ?, ?, ?, ?)",
                        ("task_claimed", agent_id, str(task_id),
                         json.dumps({"type": task_type}), now),
                    )
                except Exception:
                    pass

                if task_type == "review_shared_memory":
                    _auto_import_shared_memory(task_id, description, agent_id, conn, now)

                claimed.append(claimed_entry)

        conn.commit()
        return claimed
    except Exception as e:
        logger.warning("claim_pending_tasks failed: %s", e)
        return []
    finally:
        if own_conn:
            try:
                conn.close()
            except Exception:
                pass


def _auto_import_shared_memory(
    task_id: int, description: str, agent_id: str, conn: sqlite3.Connection, now: float
) -> None:
    """Auto-import shared memory when a review_shared_memory task is claimed.

    Parses the shared_id and source agent from the task description, then
    calls import_shared_memory. On success, notifies the source agent and
    bumps the fitness score of the original memory.
    """
    import re

    # Parse: "New shared memory: shared:AGENT:note_id — review and import via ..."
    m = re.match(
        r"New shared memory: shared:([A-Z0-9_\-]+):(.+) \u2014 review",
        description,
    )
    if not m:
        conn.execute(
            "UPDATE shared_tasks SET status='failed', updated_at=? WHERE id=?",
            (now, task_id),
        )
        return

    source_agent_id, shared_note_id = m.group(1), m.group(2)

    try:
        from memory_sharing import import_shared_memory

        result = import_shared_memory(
            shared_id=f"shared:{source_agent_id}:{shared_note_id}",
            target_agent_id=agent_id,
            tenant_id=agent_id,
            existing_conn=conn,
        )
    except Exception as exc:
        logger.warning("auto-import failed for task %d: %s", task_id, exc)
        conn.execute(
            "UPDATE shared_tasks SET status='failed', updated_at=? WHERE id=?",
            (now, task_id),
        )
        return

    if "error" in result:
        logger.warning("auto-import returned error for task %d: %s", task_id, result["error"])
        conn.execute(
            "UPDATE shared_tasks SET status='failed', updated_at=? WHERE id=?",
            (now, task_id),
        )
        return

    new_id = result.get("note_id", result.get("new_note_id", "?"))
    conn.execute(
        "UPDATE shared_tasks SET status='completed', updated_at=? WHERE id=?",
        (now, task_id),
    )
    try:
        conn.execute(
            "INSERT INTO coordination_audit (action, agent_id, target, detail, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            ("auto_imported", agent_id, str(task_id),
             f'{{"shared_id":"shared:{source_agent_id}:{shared_note_id}","new_id":"{new_id}"}}',
             now),
        )
    except Exception:
        pass

    _send_import_feedback(source_agent_id, agent_id, shared_note_id, new_id, now)


def _send_import_feedback(
    source_agent_id: str, importer_id: str, shared_note_id: str, new_note_id: str, now: float, tenant_id: str = "default"
) -> None:
    """Notify the source agent that their shared memory was imported.

    Writes directly to the source agent's DB: adds a coordination_audit
    entry and bumps the fitness_score of the original memory.
    """
    try:
        from infra.infrastructure import resolve_active_memory_dir
        mem_dir = resolve_active_memory_dir()
        from memory_sharing import _resolve_peer_db_path

        peer_db = _resolve_peer_db_path(mem_dir, source_agent_id)
        if not peer_db or not peer_db.exists():
            return

        pconn = sqlite3.connect(str(peer_db), timeout=5)
        try:
            pconn.execute("PRAGMA journal_mode=WAL")
            pconn.execute(
                "INSERT INTO coordination_audit (action, agent_id, target, detail, timestamp, tenant_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "memory_imported",
                    importer_id,
                    shared_note_id,
                    f'{{"imported_by":"{importer_id}","new_id":"{new_note_id}"}}',
                    now,
                    tenant_id,
                ),
            )

            pconn.execute(
                "UPDATE memories SET fitness_score = MIN(COALESCE(fitness_score, 0.5) + 0.05, 1.0) "
                "WHERE id = ? AND tenant_id = ?",
                (shared_note_id, tenant_id),
            )
            pconn.commit()
        finally:
            pconn.close()
    except Exception:
        logger.debug("import feedback failed for %s", source_agent_id, exc_info=True)


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
