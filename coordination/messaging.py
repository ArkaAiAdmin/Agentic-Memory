"""Agent messaging system for multi-agent coordination.

Provides inter-agent communication via a shared message queue.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time

logger = logging.getLogger(__name__)


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
    msg_id = cursor.lastrowid

    logger.info("Message sent: %s -> %s [%s] id=%d", from_agent, to_agent, message_type, msg_id)
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


# ── Acknowledgement System ────────────────────────────────────────────────

def acknowledge_message(
    conn: sqlite3.Connection,
    message_id: int,
    agent_id: str,
    note: str | None = None,
) -> bool:
    """Acknowledge receipt and understanding of a message.

    This creates a closed feedback loop: Agent A sends a message,
    Agent B acknowledges it, Agent A knows B received and understood it.
    """
    now = time.time()
    try:
        # Record acknowledgement
        conn.execute(
            "INSERT INTO coordination_audit (action, agent_id, target, detail, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            ("message_acknowledged", agent_id, str(message_id),
             json.dumps({"message_id": message_id, "note": note}), now),
        )
        conn.commit()
        return True
    except Exception as e:
        logger.warning("Failed to acknowledge message: %s", e)
        return False


def acknowledge_and_respond(
    conn: sqlite3.Connection,
    message_id: int,
    agent_id: str,
    response_type: str,
    response_payload: dict | str | None = None,
    note: str | None = None,
) -> int:
    """Acknowledge a message and send a response back.

    This creates a complete conversation loop:
    1. Agent A sends message to Agent B
    2. Agent B acknowledges receipt
    3. Agent B sends response back to Agent A

    Returns the response message ID.
    """
    # Get original message to find sender
    orig = conn.execute(
        "SELECT from_agent FROM agent_messages WHERE id=?",
        (message_id,),
    ).fetchone()

    if not orig:
        logger.warning("Message %d not found", message_id)
        return -1

    # Acknowledge
    acknowledge_message(conn, message_id, agent_id, note)

    # Send response to original sender
    response_id = send_message(
        conn, agent_id, orig[0], response_type, response_payload,
    )

    return response_id


def get_acknowledgements(
    conn: sqlite3.Connection,
    message_id: int | None = None,
    agent_id: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Get acknowledgement records."""
    where = "action='message_acknowledged'"
    params = []

    if message_id:
        where += " AND target=?"
        params.append(str(message_id))
    if agent_id:
        where += " AND agent_id=?"
        params.append(agent_id)

    rows = conn.execute(
        f"SELECT agent_id, target, detail, timestamp "
        f"FROM coordination_audit WHERE {where} "
        f"ORDER BY timestamp DESC LIMIT ?",
        params + [limit],
    ).fetchall()

    return [
        {
            "agent_id": r[0],
            "message_id": r[1],
            "detail": json.loads(r[2]) if r[2] else None,
            "timestamp": r[3],
        }
        for r in rows
    ]


def check_message_delivered(conn: sqlite3.Connection, message_id: int) -> dict:
    """Check if a message has been delivered and acknowledged."""
    msg = conn.execute(
        "SELECT status, delivered_at FROM agent_messages WHERE id=?",
        (message_id,),
    ).fetchone()

    if not msg:
        return {"exists": False}

    ack = conn.execute(
        "SELECT COUNT(*) FROM coordination_audit "
        "WHERE action='message_acknowledged' AND target=?",
        (str(message_id),),
    ).fetchone()

    return {
        "exists": True,
        "status": msg[0],
        "delivered": msg[0] == "delivered",
        "acknowledged": (ack[0] if ack else 0) > 0,
        "ack_count": ack[0] if ack else 0,
    }
