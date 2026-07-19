#!/usr/bin/env python3
"""Coordination hook: enforces multi-agent coordination protocol.

Called by the TS plugin at session start and session end:
- Session start: check pending messages, load project state
- Session end: update project state, send completion notification

Also checks file locks on save operations.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import infra._bootstrap_path as _bootstrap_path  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _log_error import log_error  # noqa: E402


def _get_db_path() -> Path:
    """Get the database path."""
    from infra.infrastructure import resolve_active_memory_dir
    return resolve_active_memory_dir() / "memory.db"


def _get_agent_id() -> str:
    """Get the current agent's ID."""
    agent_id = os.environ.get("MEMORY_AGENT_ID", "default")
    # Validate: max 128 chars, alphanumeric + hyphens + underscores only
    import re
    if not agent_id or len(agent_id) > 128 or not re.match(r'^[a-zA-Z0-9_\-]+$', agent_id):
        return "default"
    return agent_id


def _get_conn():
    """Get a database connection."""
    db_path = _get_db_path()
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _on_session_start(agent_id: str, project_id: str = "default") -> str:
    """Enforce coordination on session start with durability."""
    output = []

    try:
        conn = _get_conn()

        # Durability: cleanup stale state
        from coordination.durability import (
            ensure_durability_tables, release_stale_locks,
            abandon_stale_tasks, update_heartbeat, record_coordination_event,
        )
        ensure_durability_tables(conn)
        release_stale_locks(conn)
        abandon_stale_tasks(conn)

        # Update heartbeat
        update_heartbeat(conn, agent_id, project_id=project_id)
        record_coordination_event(conn, "session_start", agent_id, project_id)

        # 0. Claim pending tasks (the core coordination integration)
        try:
            from coordination.hooks import claim_pending_tasks
            claimed = claim_pending_tasks(agent_id, project_id, limit=3)
            if claimed:
                output.append(f"\n\n**Claimed {len(claimed)} task(s):**")
                for t in claimed:
                    output.append(f"- [{t['task_type']}] {t['description'][:80]}")
        except Exception:
            pass

        # 1. Check pending messages
        messages = conn.execute(
            "SELECT id, from_agent, message_type, payload "
            "FROM agent_messages WHERE (to_agent=? OR to_agent='*') AND status='pending' "
            "ORDER BY created_at ASC LIMIT 10",
            (agent_id,),
        ).fetchall()

        if messages:
            output.append(f"\n\n**{len(messages)} pending message(s):**")
            msg_ids = []
            for msg in messages:
                payload = msg[3][:100] if msg[3] else ""
                output.append(f"- From {msg[1]} [{msg[2]}]: {payload}")
                msg_ids.append(msg[0])

            # Mark as delivered
            if msg_ids:
                placeholders = ",".join("?" for _ in msg_ids)
                conn.execute(
                    f"UPDATE agent_messages SET status='delivered', delivered_at=? WHERE id IN ({placeholders})",
                    [time.time()] + msg_ids,
                )
                conn.commit()

        # 2. Load project state
        state = conn.execute(
            "SELECT key, value, updated_by FROM project_state WHERE project_id=?",
            (project_id,),
        ).fetchall()

        if state:
            output.append(f"\n\n**Project state ({project_id}):**")
            for s in state:
                try:
                    val = json.loads(s[1]) if s[1] else ""
                    if isinstance(val, dict):
                        val = json.dumps(val, indent=None)[:80]
                    output.append(f"- {s[0]}: {val} (by {s[2]})")
                except Exception:
                    output.append(f"- {s[0]}: {s[1][:80]} (by {s[2]})")

        # 3. Check file locks held by other agents
        locks = conn.execute(
            "SELECT file_path, locked_by, expires_at FROM file_locks WHERE locked_by != ? AND expires_at > ?",
            (agent_id, time.time()),
        ).fetchall()

        if locks:
            output.append(f"\n\n**Files locked by other agents:**")
            for lock in locks:
                remaining = max(0, (lock[2] - time.time()))
                output.append(f"- {lock[0]} (locked by {lock[1]}, {remaining:.0f}s remaining)")

        # 4. Safety report
        safety = get_safety_report(conn)
        if safety["safety_score"] < 100:
            output.append(f"\n\n**Safety: {safety['safety_score']}/100**")
            if safety["stale_agents"] > 0:
                output.append(f"- {safety['stale_agents']} stale agent(s) detected")
            if safety["stale_locks"] > 0:
                output.append(f"- {safety['stale_locks']} expired lock(s) cleaned")
            if safety["abandoned_tasks"] > 0:
                output.append(f"- {safety['abandoned_tasks']} abandoned task(s) recovered")

        conn.close()

    except Exception as e:
        log_error(f"coordination session_start failed: {e}")

    return "".join(output)


def _on_session_end(agent_id: str, project_id: str = "default") -> str:
    """Enforce coordination on session end with durability."""
    output = []

    try:
        conn = _get_conn()
        now = time.time()

        # Durability: record session end
        from coordination.durability import record_coordination_event
        record_coordination_event(conn, "session_end", agent_id, project_id)

        # 1. Update project state with last activity
        conn.execute(
            "INSERT OR REPLACE INTO project_state (project_id, key, value, updated_by, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (project_id, f"agent:{agent_id}:last_active", now, agent_id, now),
        )
        conn.commit()

        # 2. Release any locks held by this agent (crash recovery)
        released = conn.execute(
            "DELETE FROM file_locks WHERE locked_by=?", (agent_id,)
        ).rowcount
        if released:
            conn.commit()
            output.append(f"\n\nReleased {released} file lock(s).")
            record_coordination_event(conn, "locks_released", agent_id, detail=f"{released} locks")

        # 3. Notify other agents of session end
        conn.execute(
            "INSERT INTO agent_messages (from_agent, to_agent, message_type, payload, status, created_at) "
            "VALUES (?, '*', 'session_end', ?, 'pending', ?)",
            (agent_id, json.dumps({"agent_id": agent_id, "project_id": project_id}), now),
        )
        conn.commit()

        conn.close()

    except Exception as e:
        log_error(f"coordination session_end failed: {e}")

    return "".join(output)


def _check_file_lock(file_path: str, agent_id: str) -> bool:
    """Check if a file is locked by another agent. Returns True if safe to proceed."""
    try:
        conn = _get_conn()
        now = time.time()

        # Check for active locks by other agents
        lock = conn.execute(
            "SELECT locked_by, expires_at FROM file_locks WHERE file_path=?",
            (file_path,),
        ).fetchone()

        conn.close()

        if not lock:
            return True  # No lock, safe to proceed

        if lock[0] == agent_id:
            return True  # Locked by this agent, safe

        if lock[1] and lock[1] < now:
            return True  # Lock expired, safe

        logger.warning("File %s locked by %s, cannot write", file_path, lock[0])
        return False

    except Exception as e:
        log_error(f"coordination lock check failed: {e}")
        return False  # Fail closed — don't allow writes if coordination is broken


def _acquire_file_lock(file_path: str, agent_id: str, ttl: int = 300) -> bool:
    """Acquire file lock. Returns True if acquired."""
    try:
        conn = _get_conn()
        now = time.time()

        # Check existing
        existing = conn.execute(
            "SELECT locked_by, expires_at FROM file_locks WHERE file_path=?",
            (file_path,),
        ).fetchone()

        if existing:
            if existing[0] == agent_id:
                conn.execute(
                    "UPDATE file_locks SET locked_at=?, expires_at=? WHERE file_path=?",
                    (now, now + ttl, file_path),
                )
                conn.commit()
                conn.close()
                return True
            if existing[1] and existing[1] < now:
                conn.execute("DELETE FROM file_locks WHERE file_path=?", (file_path,))
            else:
                conn.close()
                return False

        conn.execute(
            "INSERT OR REPLACE INTO file_locks (file_path, locked_by, locked_at, expires_at) VALUES (?, ?, ?, ?)",
            (file_path, agent_id, now, now + ttl),
        )
        conn.commit()
        conn.close()
        return True

    except Exception as e:
        log_error(f"coordination lock acquire failed: {e}")
        return False  # Fail closed — don't allow lock acquisition if coordination is broken


# ── Main entry point ─────────────────────────────────────────────────────

def main():
    """Hook entry point. Reads action from stdin JSON."""
    try:
        raw = sys.stdin.read()
        data = {}
        if raw.strip():
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                pass

        action = data.get("action", "session_start")
        agent_id = data.get("agent_id", _get_agent_id())
        project_id = data.get("project_id", "default")

        if action == "session_start":
            output = _on_session_start(agent_id, project_id)
            print(output)

        elif action == "session_end":
            output = _on_session_end(agent_id, project_id)
            print(output)

        elif action == "check_lock":
            file_path = data.get("file_path", "")
            safe = _check_file_lock(file_path, agent_id)
            print(json.dumps({"safe": safe, "file_path": file_path}))

        elif action == "acquire_lock":
            file_path = data.get("file_path", "")
            acquired = _acquire_file_lock(file_path, agent_id)
            print(json.dumps({"acquired": acquired, "file_path": file_path}))

        else:
            print(json.dumps({"error": f"Unknown action: {action}"}))

    except Exception as e:
        log_error(f"coordination hook failed: {e}")
        print(json.dumps({"error": str(e)}))


if __name__ == "__main__":
    main()
