"""Agent messaging system for multi-agent coordination.

Provides inter-agent communication via a shared message queue.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Callable

logger = logging.getLogger(__name__)

# ── In-Process Subscription Registry ────────────────────────────────────
# Agents register callbacks that fire when a message is delivered.
# This avoids polling: check_messages reads pending messages and triggers
# the registered callback for each one.
#
# Usage:
#     def on_message(msg): print(f"Got: {msg}")
#     subscribe("agent-a", on_message)
#     check_messages(conn, "agent-a")  # fires on_message for each pending msg
#
# Subscriptions are per-agent-id, per-process. Cross-process notification
# requires polling (the daemon pattern).

_subscriptions: dict[str, list[Callable]] = {}


def subscribe(agent_id: str, callback: Callable) -> None:
    """Register a callback for incoming messages.

    The callback receives the message dict.
    """
    _subscriptions.setdefault(agent_id, []).append(callback)


def unsubscribe(agent_id: str, callback: Callable) -> None:
    """Remove a previously registered callback."""
    subs = _subscriptions.get(agent_id, [])
    if callback in subs:
        subs.remove(callback)


def _notify_subscribers(agent_id: str, message: dict) -> None:
    """Fire all callbacks registered for an agent."""
    for cb in _subscriptions.get(agent_id, []):
        try:
            cb(message)
        except Exception as e:
            logger.warning("message subscriber failed for %s: %s", agent_id, e)


def send_message(
    conn: sqlite3.Connection,
    from_agent: str,
    to_agent: str,
    message_type: str,
    payload: dict | str | None = None,
) -> int:
    """Send a message to another agent. Returns message ID."""
    now = time.time()
    payload_str = json.dumps(payload) if isinstance(payload, dict) else payload

    cursor = conn.execute(
        "INSERT INTO agent_messages (from_agent, to_agent, message_type, payload, status, created_at) "
        "VALUES (?, ?, ?, ?, 'pending', ?)",
        (from_agent, to_agent, message_type, payload_str, now),
    )
    conn.commit()
    msg_id: int = cursor.lastrowid or 0

    logger.info("Message sent: %s -> %s [%s] id=%d", from_agent, to_agent, message_type, msg_id)

    if to_agent != "*":
        _notify_subscribers(to_agent, {
            "id": msg_id, "from_agent": from_agent,
            "message_type": message_type, "payload": payload,
            "created_at": now,
        })

    return msg_id


def read_messages(
    conn: sqlite3.Connection,
    agent_id: str,
    limit: int = 50,
    mark_delivered: bool = True,
) -> list[dict]:
    """Read pending messages for an agent."""
    rows = conn.execute(
        "SELECT id, from_agent, message_type, payload, created_at "
        "FROM agent_messages WHERE (to_agent=? OR to_agent='*') AND status='pending' "
        "ORDER BY created_at ASC LIMIT ?",
        (agent_id, limit),
    ).fetchall()

    messages = []
    msg_ids = []
    for r in rows:
        try:
            payload = json.loads(r[3]) if r[3] else None
        except (json.JSONDecodeError, TypeError):
            payload = r[3]  # Keep as plain string if not valid JSON
        messages.append({
            "id": r[0],
            "from_agent": r[1],
            "message_type": r[2],
            "payload": payload,
            "created_at": r[4],
        })
        msg_ids.append(r[0])

    if mark_delivered and msg_ids:
        placeholders = ",".join("?" for _ in msg_ids)
        conn.execute(
            f"UPDATE agent_messages SET status='delivered', delivered_at=? WHERE id IN ({placeholders})",
            [time.time()] + msg_ids,
        )
        # Passive audit: record delivery without agent needing to call ack
        for mid in msg_ids:
            try:
                conn.execute(
                    "INSERT INTO coordination_audit (action, agent_id, target, detail, timestamp) "
                    "VALUES (?, ?, ?, ?, ?)",
                    ("message_delivered", agent_id, str(mid), None, time.time()),
                )
            except Exception:
                pass  # Audit table may not exist yet
        conn.commit()

    return messages


def broadcast_message(
    conn: sqlite3.Connection,
    from_agent: str,
    message_type: str,
    payload: dict | str | None = None,
) -> int:
    """Broadcast a message to all agents."""
    return send_message(conn, from_agent, "*", message_type, payload)


def get_message_history(
    conn: sqlite3.Connection,
    agent_id: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Get message history (all delivered messages)."""
    if agent_id:
        rows = conn.execute(
            "SELECT id, from_agent, to_agent, message_type, payload, created_at, delivered_at "
            "FROM agent_messages WHERE (from_agent=? OR to_agent=?) AND status='delivered' "
            "ORDER BY created_at DESC LIMIT ?",
            (agent_id, agent_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, from_agent, to_agent, message_type, payload, created_at, delivered_at "
            "FROM agent_messages WHERE status='delivered' "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

    results = []
    for r in rows:
        try:
            payload = json.loads(r[4]) if r[4] else None
        except (json.JSONDecodeError, TypeError):
            payload = r[4]
        results.append({
            "id": r[0], "from_agent": r[1], "to_agent": r[2],
            "message_type": r[3], "payload": payload,
            "created_at": r[5], "delivered_at": r[6],
        })
    return results


def get_pending_count(conn: sqlite3.Connection, agent_id: str) -> int:
    """Get count of pending messages for an agent."""
    row = conn.execute(
        "SELECT COUNT(*) FROM agent_messages WHERE (to_agent=? OR to_agent='*') AND status='pending'",
        (agent_id,),
    ).fetchone()
    return row[0] if row else 0


def cleanup_old_messages(conn: sqlite3.Connection, max_age_days: int = 30) -> int:
    """Remove old delivered messages."""
    cutoff = time.time() - (max_age_days * 86400)
    cursor = conn.execute(
        "DELETE FROM agent_messages WHERE status='delivered' AND delivered_at < ?",
        (cutoff,),
    )
    conn.commit()
    return cursor.rowcount


# ── Message Subscriptions ──────────────────────────────────────────────


def check_messages(conn: sqlite3.Connection, agent_id: str, limit: int = 50) -> list[dict]:
    """Read pending messages and notify subscribers.

    Returns the messages (same as read_messages) but also fires
    registered callbacks. Use this instead of read_messages when
    you want push-style notification.
    """
    messages = read_messages(conn, agent_id, limit, mark_delivered=True)
    for msg in messages:
        _notify_subscribers(agent_id, msg)
    return messages


# ── Dead-Letter Processing ──────────────────────────────────────────────


def ensure_dead_letter_table(conn: sqlite3.Connection) -> None:
    """Create dead_letter_messages table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dead_letter_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_id INTEGER,
            from_agent TEXT NOT NULL,
            to_agent TEXT NOT NULL,
            message_type TEXT NOT NULL,
            payload TEXT,
            status TEXT DEFAULT 'dead',
            created_at REAL NOT NULL,
            dead_lettered_at REAL NOT NULL,
            reason TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_dead_letter_created
        ON dead_letter_messages(created_at DESC)
    """)
    conn.commit()


def process_dead_letters(conn: sqlite3.Connection, max_age_days: int = 7) -> int:
    """Move undelivered pending messages older than max_age_days to dead-letter.

    Returns count of messages dead-lettered.
    """
    ensure_dead_letter_table(conn)
    cutoff = time.time() - (max_age_days * 86400)
    now = time.time()

    rows = conn.execute(
        "SELECT id, from_agent, to_agent, message_type, payload, created_at "
        "FROM agent_messages WHERE status='pending' AND created_at < ?",
        (cutoff,),
    ).fetchall()

    if not rows:
        return 0

    count = 0
    for r in rows:
        msg_id, from_agent, to_agent, msg_type, payload, created_at = r
        try:
            conn.execute(
                "INSERT INTO dead_letter_messages (original_id, from_agent, to_agent, message_type, payload, status, created_at, dead_lettered_at, reason) "
                "VALUES (?, ?, ?, ?, ?, 'dead', ?, ?, 'ttl_expired')",
                (msg_id, from_agent, to_agent, msg_type, payload, created_at, now),
            )
            conn.execute(
                "UPDATE agent_messages SET status='dead_lettered' WHERE id=?",
                (msg_id,),
            )
            count += 1
        except Exception as e:
            logger.warning("Failed to dead-letter message %d: %s", msg_id, e)

    conn.commit()
    if count > 0:
        logger.warning("Dead-lettered %d undelivered messages (cutoff: %s)", count, cutoff)
    return count


def get_dead_letters(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """Get dead-letter queue entries."""
    ensure_dead_letter_table(conn)
    rows = conn.execute(
        "SELECT id, original_id, from_agent, to_agent, message_type, payload, created_at, dead_lettered_at, reason "
        "FROM dead_letter_messages ORDER BY dead_lettered_at DESC LIMIT ?",
        (limit,),
    ).fetchall()

    results = []
    for r in rows:
        try:
            payload = json.loads(r[5]) if r[5] else None
        except (json.JSONDecodeError, TypeError):
            payload = r[5]
        results.append({
            "id": r[0],
            "original_id": r[1],
            "from_agent": r[2],
            "to_agent": r[3],
            "message_type": r[4],
            "payload": payload,
            "created_at": r[6],
            "dead_lettered_at": r[7],
            "reason": r[8],
        })
    return results


def replay_dead_letter(conn: sqlite3.Connection, dead_letter_id: int) -> int | None:
    """Re-queue a dead-letter message. Returns new message ID or None."""
    row = conn.execute(
        "SELECT from_agent, to_agent, message_type, payload, created_at "
        "FROM dead_letter_messages WHERE id=?",
        (dead_letter_id,),
    ).fetchone()

    if not row:
        return None

    from_agent, to_agent, msg_type, payload, created_at = row
    new_id = send_message(conn, from_agent, to_agent, msg_type, payload)

    conn.execute(
        "UPDATE dead_letter_messages SET status='replayed' WHERE id=?",
        (dead_letter_id,),
    )
    conn.commit()
    return new_id


def cleanup_dead_letters(conn: sqlite3.Connection, max_age_days: int = 90) -> int:
    """Remove old dead-letter entries."""
    cutoff = time.time() - (max_age_days * 86400)
    cursor = conn.execute(
        "DELETE FROM dead_letter_messages WHERE dead_lettered_at < ?",
        (cutoff,),
    )
    conn.commit()
    return cursor.rowcount


# ── Acknowledgement System ────────────────────────────────────────────────
#
# DESIGN DECISION: Agents acknowledge messages implicitly via task status.
# When Agent B reads a message and updates the task status, that IS the ack.
# No separate ack channel needed — task transitions are the coordination primitive.
#
# The audit log records delivery events passively for observability.
