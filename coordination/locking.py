"""File locking mechanism for multi-agent coordination.

Provides exclusive file locks with fencing tokens and auto-expiry.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import NamedTuple

logger = logging.getLogger(__name__)

DEFAULT_LOCK_TTL = 300  # 5 minutes

# ── Fencing Token ────────────────────────────────────────────────────────
#
# Every lock acquire returns a FencingLock with a monotonically increasing
# version number. Before writing, the holder must verify the version hasn't
# changed — if it has, another agent stole the lock and this agent is stale.
#
# Usage:
#     lock = acquire_lock_fenced(conn, "/path", "agent-a")
#     if lock.acquired:
#         # ... do work ...
#         if not verify_lock_fenced(conn, "/path", lock.version):
#             abort("Lost the lock — another agent took over")


class FencingLock(NamedTuple):
    """Result of a lock acquire with fencing support.

    ``acquired`` — True if the lock was granted.
    ``version``  — Monotonically increasing version; pass to verify_lock_fenced.
    ``holder``   — Current holder of the lock (self, or another agent if denied).
    ``expires_at`` — Epoch timestamp of lock expiry.
    """
    acquired: bool
    version: int = 0
    holder: str = ""
    expires_at: float = 0.0


def _ensure_lock_version_column(conn: sqlite3.Connection) -> None:
    """Add lock_version column to file_locks if missing (soft migration)."""
    try:
        conn.execute("ALTER TABLE file_locks ADD COLUMN lock_version INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass


def _begin_immediate(conn: sqlite3.Connection) -> None:
    """Begin IMMEDIATE transaction; no-op if already in a transaction."""
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        pass


def acquire_lock(conn: sqlite3.Connection, file_path: str, agent_id: str, ttl: int = DEFAULT_LOCK_TTL) -> bool:
    """Acquire exclusive lock on a file. Returns True if acquired.

    Legacy interface — returns bool only. Prefer acquire_lock_fenced.
    """
    return acquire_lock_fenced(conn, file_path, agent_id, ttl).acquired


def acquire_lock_fenced(conn: sqlite3.Connection, file_path: str, agent_id: str, ttl: int = DEFAULT_LOCK_TTL) -> FencingLock:
    """Acquire exclusive lock with fencing token. Returns FencingLock.

    Uses BEGIN IMMEDIATE to prevent TOCTOU races between check and insert.
    """
    _ensure_lock_version_column(conn)
    now = time.time()
    expires_at = now + ttl
    _begin_immediate(conn)

    existing = conn.execute(
        "SELECT locked_by, expires_at, lock_version FROM file_locks WHERE file_path=?",
        (file_path,),
    ).fetchone()

    if existing:
        holder, exp, version = existing
        if holder == agent_id:
            new_version = version + 1
            conn.execute(
                "UPDATE file_locks SET locked_at=?, expires_at=?, lock_version=? WHERE file_path=?",
                (now, expires_at, new_version, file_path),
            )
            conn.commit()
            return FencingLock(True, new_version, agent_id, expires_at)

        if exp and exp < now:
            new_version = version + 1
            conn.execute(
                "UPDATE file_locks SET locked_by=?, locked_at=?, expires_at=?, lock_version=? WHERE file_path=?",
                (agent_id, now, expires_at, new_version, file_path),
            )
            conn.commit()
            logger.info("Lock takeover: %s stole %s on %s (version %d)", agent_id, holder, file_path, new_version)
            return FencingLock(True, new_version, agent_id, expires_at)
        else:
            conn.commit()
            logger.warning("File %s locked by %s, cannot acquire for %s", file_path, holder, agent_id)
            return FencingLock(False, version, holder, exp)

    conn.execute(
        "INSERT INTO file_locks (file_path, locked_by, locked_at, expires_at, lock_version) VALUES (?, ?, ?, ?, 1)",
        (file_path, agent_id, now, expires_at),
    )
    conn.commit()
    return FencingLock(True, 1, agent_id, expires_at)


def verify_lock_fenced(conn: sqlite3.Connection, file_path: str, expected_version: int) -> bool:
    """Check that lock is still held by us (version hasn't changed).

    Returns False if another agent stole the lock — caller should abort.
    """
    _ensure_lock_version_column(conn)
    row = conn.execute(
        "SELECT locked_by, lock_version FROM file_locks WHERE file_path=?",
        (file_path,),
    ).fetchone()

    if not row:
        logger.warning("Lock on %s no longer exists (was version %d)", file_path, expected_version)
        return False

    locked_by, current_version = row
    if current_version != expected_version:
        logger.warning(
            "Lock version mismatch on %s: expected %d, got %d (now held by %s)",
            file_path, expected_version, current_version, locked_by,
        )
        return False

    return True


def renew_lock(conn: sqlite3.Connection, file_path: str, agent_id: str, ttl: int = DEFAULT_LOCK_TTL) -> bool:
    """Renew a lock's TTL. Returns True if renewed.

    The lock must be held by the given agent. Fencing version is NOT
    incremented — renewal is not a takeover.
    """
    now = time.time()
    expires_at = now + ttl
    _begin_immediate(conn)

    existing = conn.execute(
        "SELECT locked_by FROM file_locks WHERE file_path=?",
        (file_path,),
    ).fetchone()

    if not existing or existing[0] != agent_id:
        return False

    conn.execute(
        "UPDATE file_locks SET locked_at=?, expires_at=? WHERE file_path=?",
        (now, expires_at, file_path),
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
    _ensure_lock_version_column(conn)
    row = conn.execute(
        "SELECT locked_by, locked_at, expires_at, lock_version FROM file_locks WHERE file_path=?",
        (file_path,),
    ).fetchone()

    if not row:
        return None

    if row[2] and row[2] < time.time():
        return None

    return {
        "locked_by": row[0],
        "locked_at": row[1],
        "expires_at": row[2],
        "lock_version": row[3],
    }


def cleanup_expired_locks(conn: sqlite3.Connection) -> int:
    """Remove expired locks. Returns count of removed locks."""
    now = time.time()
    cursor = conn.execute("DELETE FROM file_locks WHERE expires_at < ?", (now,))
    conn.commit()
    return cursor.rowcount


def list_locks(conn: sqlite3.Connection) -> list[dict]:
    """List all active locks."""
    _ensure_lock_version_column(conn)
    rows = conn.execute(
        "SELECT file_path, locked_by, locked_at, expires_at, lock_version FROM file_locks ORDER BY locked_at DESC"
    ).fetchall()

    now = time.time()
    locks = []
    for r in rows:
        if r[3] and r[3] < now:
            continue
        locks.append({
            "file_path": r[0],
            "locked_by": r[1],
            "locked_at": r[2],
            "expires_at": r[3],
            "lock_version": r[4],
            "remaining_s": max(0, r[3] - now) if r[3] else 0,
        })

    return locks
