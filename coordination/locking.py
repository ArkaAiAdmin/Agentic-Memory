"""File locking mechanism for multi-agent coordination.

Provides exclusive file locks with auto-expiry to prevent concurrent edits.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_LOCK_TTL = 300  # 5 minutes


def acquire_lock(conn: sqlite3.Connection, file_path: str, agent_id: str, ttl: int = DEFAULT_LOCK_TTL) -> bool:
    """Acquire exclusive lock on a file. Returns True if acquired.

    Uses a transaction to prevent race conditions between check and insert.
    """
    now = time.time()
    expires_at = now + ttl

    try:
        conn.execute("BEGIN IMMEDIATE")
    except Exception:
        pass  # Already in a transaction (e.g. called from save pipeline)

    # Check existing lock
    existing = conn.execute(
        "SELECT locked_by, expires_at FROM file_locks WHERE file_path=?",
        (file_path,),
    ).fetchone()

    if existing:
        if existing[0] == agent_id:
            # Refresh existing lock
            conn.execute(
                "UPDATE file_locks SET locked_at=?, expires_at=? WHERE file_path=?",
                (now, expires_at, file_path),
            )
            conn.commit()
            return True

        # Check if expired
        if existing[1] and existing[1] < now:
            conn.execute("DELETE FROM file_locks WHERE file_path=?", (file_path,))
        else:
            conn.commit()
            logger.warning("File %s locked by %s, cannot acquire for %s", file_path, existing[0], agent_id)
            return False

    conn.execute(
        "INSERT OR REPLACE INTO file_locks (file_path, locked_by, locked_at, expires_at) VALUES (?, ?, ?, ?)",
        (file_path, agent_id, now, expires_at),
    )
    conn.commit()
    return True


def release_lock(conn: sqlite3.Connection, file_path: str, agent_id: str) -> bool:
    """Release a file lock. Returns True if released."""
    existing = conn.execute(
        "SELECT locked_by FROM file_locks WHERE file_path=?",
        (file_path,),
    ).fetchone()

    if existing and existing[0] != agent_id:
        logger.warning("Cannot release: file %s locked by %s, not %s", file_path, existing[0], agent_id)
        return False

    conn.execute("DELETE FROM file_locks WHERE file_path=?", (file_path,))
    conn.commit()
    return True


def check_lock(conn: sqlite3.Connection, file_path: str) -> dict | None:
    """Check if a file is locked. Returns lock info or None if unlocked."""
    row = conn.execute(
        "SELECT locked_by, locked_at, expires_at FROM file_locks WHERE file_path=?",
        (file_path,),
    ).fetchone()

    if not row:
        return None

    # Check if expired — return None but do NOT delete (read should be side-effect free)
    if row[2] and row[2] < time.time():
        return None

    return {
        "locked_by": row[0],
        "locked_at": row[1],
        "expires_at": row[2],
    }


def cleanup_expired_locks(conn: sqlite3.Connection) -> int:
    """Remove expired locks. Returns count of removed locks."""
    now = time.time()
    cursor = conn.execute("DELETE FROM file_locks WHERE expires_at < ?", (now,))
    conn.commit()
    return cursor.rowcount


def list_locks(conn: sqlite3.Connection) -> list[dict]:
    """List all active locks."""
    rows = conn.execute(
        "SELECT file_path, locked_by, locked_at, expires_at FROM file_locks ORDER BY locked_at DESC"
    ).fetchall()

    now = time.time()
    locks = []
    for r in rows:
        if r[3] and r[3] < now:
            continue  # Skip expired
        locks.append({
            "file_path": r[0],
            "locked_by": r[1],
            "locked_at": r[2],
            "expires_at": r[3],
            "remaining_s": max(0, r[3] - now) if r[3] else 0,
        })

    return locks
