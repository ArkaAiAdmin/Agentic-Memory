"""CQRS write-ahead journal for lock-free multi-agent writes.

The journal is a separate SQLite database (``journal.db``) that agents
append writes to without locking.  A reconciliation daemon (see
``background/background_worker.py``) polls the journal and applies
entries to the main DB via the existing saga.

Schema
------
``write_journal`` table::

    id              INTEGER PRIMARY KEY AUTOINCREMENT
    note_id         TEXT NOT NULL
    agent_id        TEXT NOT NULL
    category        TEXT NOT NULL
    title_slug      TEXT NOT NULL
    content         TEXT NOT NULL
    tags            TEXT                 -- JSON list
    pinned          INTEGER DEFAULT 0
    is_global       INTEGER DEFAULT 0
    importance      INTEGER DEFAULT 3
    tenant_id       TEXT DEFAULT 'default'
    epistemic_source TEXT DEFAULT 'agent'
    belief_status   TEXT DEFAULT 'active'
    asserting_agent_id TEXT DEFAULT ''
    fact_type       TEXT DEFAULT 'observation'
    defer_expensive INTEGER DEFAULT 1
    context         TEXT DEFAULT 'generic'
    status          TEXT NOT NULL DEFAULT 'pending'
                    -- pending | processing | applied | failed
    error           TEXT
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
    processed_at    TEXT

Indexes: ``idx_journal_status``, ``idx_journal_agent``.

Thread safety
-------------
Each thread gets its own connection via ``threading.local()``.
SQLite WAL mode handles concurrent INSERTs with minimal serialisation.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from save.pipeline import SaveRequest

logger = logging.getLogger(__name__)

# Thread-local connections to the journal DB — zero lock contention.
_local = threading.local()


def _get_journal_conn(journal_path: Path, timeout: float = 10.0) -> sqlite3.Connection:
    """Get or create a thread-local connection to the journal DB."""
    key = str(journal_path)
    if not hasattr(_local, "conns"):
        _local.conns = {}
    conn = _local.conns.get(key)
    if conn is None:
        conn = sqlite3.connect(str(journal_path), timeout=timeout)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.row_factory = sqlite3.Row
        _local.conns[key] = conn
    return conn


def _clear_local_conns() -> None:
    """Test helper: close and discard all thread-local connections."""
    if hasattr(_local, "conns"):
        for conn in _local.conns.values():
            try:
                conn.close()
            except Exception:
                pass
        _local.conns = {}


def init_journal_db(journal_path: Path) -> None:
    """Create the write_journal table if not exists. Idempotent."""
    conn = _get_journal_conn(journal_path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS write_journal (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            note_id         TEXT NOT NULL,
            agent_id        TEXT NOT NULL,
            category        TEXT NOT NULL,
            title_slug      TEXT NOT NULL,
            content         TEXT NOT NULL,
            tags            TEXT,
            pinned          INTEGER DEFAULT 0,
            is_global       INTEGER DEFAULT 0,
            importance      INTEGER DEFAULT 3,
            tenant_id       TEXT DEFAULT 'default',
            epistemic_source TEXT DEFAULT 'agent',
            belief_status   TEXT DEFAULT 'active',
            asserting_agent_id TEXT DEFAULT '',
            fact_type       TEXT DEFAULT 'observation',
            defer_expensive INTEGER DEFAULT 1,
            context         TEXT DEFAULT 'generic',
            status          TEXT NOT NULL DEFAULT 'pending',
            error           TEXT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            processed_at    TEXT
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_journal_status ON write_journal(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_journal_agent ON write_journal(agent_id)")
    conn.commit()


def _note_id(category: str, title_slug: str) -> str:
    """Return the canonical note_id for the journal entry.

    Uses the deterministic ``category/title_slug`` format — the same
    ID that the DB uses.  The journal's auto-increment ``id`` column
    provides de-duplication; dequeue returns the row ``id`` for
    atomic ``BEGIN IMMEDIATE`` claiming.
    """
    return f"{category}/{title_slug}"


def enqueue_write(
    journal_path: Path,
    req: SaveRequest,
    agent_id: str,
) -> str:
    """Append a write to the journal. Returns note_id immediately.

    No lock contention: each INSERT is an independent row append.
    SQLite WAL mode handles concurrent INSERTs with minimal serialisation.

    Args:
        journal_path: Path to the journal DB.
        req: The validated SaveRequest.
        agent_id: The CRDT agent id of the caller.

    Returns:
        The pre-allocated ``note_id`` string.
    """
    note_id = f"{req.category}/{req.title_slug}"
    conn = _get_journal_conn(journal_path)
    conn.execute(
        """INSERT OR IGNORE INTO write_journal
           (note_id, agent_id, category, title_slug, content, tags,
            pinned, is_global, importance, tenant_id,
            epistemic_source, belief_status, asserting_agent_id, fact_type,
            defer_expensive, context)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            note_id,
            agent_id,
            req.category,
            req.title_slug,
            req.content,
            json.dumps(req.tags or []),
            1 if req.pinned else 0,
            1 if req.is_global else 0,
            req.importance,
            req.tenant_id,
            req.epistemic_source,
            req.belief_status,
            req.asserting_agent_id,
            req.fact_type,
            1 if req.defer_expensive else 0,
            req.context,
        ),
    )
    conn.commit()
    return note_id


def dequeue_pending(
    journal_path: Path,
    batch_size: int = 10,
) -> list[dict[str, Any]]:
    """Read and claim the next batch of pending entries (atomic UPDATE).

    Uses ``BEGIN IMMEDIATE`` to claim entries so two concurrent
    daemon processes don't race on the same batch.

    Returns:
        A list of row dicts with status='processing'.  Empty list
        if nothing is pending.
    """
    conn = _get_journal_conn(journal_path)
    conn.execute("BEGIN IMMEDIATE")
    rows = conn.execute(
        "SELECT id, note_id, agent_id, category, title_slug, content, "
        "tags, pinned, is_global, importance, tenant_id, "
        "epistemic_source, belief_status, asserting_agent_id, fact_type, "
        "defer_expensive, context, status, created_at "
        "FROM write_journal WHERE status='pending' ORDER BY id LIMIT ?",
        (batch_size,),
    ).fetchall()
    if rows:
        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" * len(ids))
        conn.execute(
            f"UPDATE write_journal SET status='processing', processed_at=datetime('now') WHERE id IN ({placeholders})",
            ids,
        )
    conn.commit()
    entries = [dict(r) for r in rows]
    # Reflect the status update in the returned dicts (the SELECT
    # ran before the UPDATE, so rows still have 'pending').
    for e in entries:
        e["status"] = "processing"
    return entries


def mark_applied(journal_path: Path, entry_id: int) -> None:
    """Mark a journal entry as successfully applied."""
    conn = _get_journal_conn(journal_path)
    conn.execute(
        "UPDATE write_journal SET status='applied', processed_at=datetime('now') WHERE id=?",
        (entry_id,),
    )
    conn.commit()


def mark_failed(journal_path: Path, entry_id: int, error: str) -> None:
    """Mark a journal entry as failed with an error message."""
    conn = _get_journal_conn(journal_path)
    conn.execute(
        "UPDATE write_journal SET status='failed', error=? WHERE id=?",
        (error[:500], entry_id),
    )
    conn.commit()


def get_pending_by_agent(journal_path: Path, agent_id: str) -> list[dict[str, Any]]:
    """Return pending+processing entries for a specific agent.

    Used by the optional journal-aware read supplement so agents can
    see their own writes before the daemon processes them.
    """
    conn = _get_journal_conn(journal_path)
    rows = conn.execute(
        "SELECT id, note_id, agent_id, category, title_slug, content, "
        "tags, pinned, is_global, importance, "
        "epistemic_source, belief_status, asserting_agent_id, fact_type, "
        "defer_expensive, context, status, created_at "
        "FROM write_journal WHERE agent_id=? AND status IN ('pending','processing') "
        "ORDER BY id",
        (agent_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_entry_by_note_id(journal_path: Path, note_id: str) -> dict[str, Any] | None:
    """Return a single entry by note_id, or None."""
    conn = _get_journal_conn(journal_path)
    row = conn.execute(
        "SELECT id, note_id, agent_id, category, title_slug, content, "
        "tags, pinned, is_global, importance, tenant_id, "
        "epistemic_source, belief_status, asserting_agent_id, fact_type, "
        "defer_expensive, context, status, error, created_at, processed_at "
        "FROM write_journal WHERE note_id=?",
        (note_id,),
    ).fetchone()
    return dict(row) if row else None


def wait_for_note_id(
    journal_path: Path,
    note_id: str,
    timeout: float = 5.0,
    poll_interval: float = 0.02,
) -> dict[str, Any]:
    """Poll the journal until an entry reaches 'applied' or 'failed'.

    Args:
        journal_path: Path to the journal DB.
        note_id: The note ID to wait for.
        timeout: Maximum wait time in seconds.
        poll_interval: Seconds between polls.

    Returns:
        The entry dict.

    Raises:
        TimeoutError: if the entry is not applied within timeout.
        RuntimeError: if the entry reached 'failed' status.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        entry = get_entry_by_note_id(journal_path, note_id)
        if entry is None:
            time.sleep(poll_interval)
            continue
        if entry["status"] == "applied":
            return entry
        if entry["status"] == "failed":
            raise RuntimeError(f"save {note_id} failed: {entry.get('error', 'unknown')}")
        time.sleep(poll_interval)
    raise TimeoutError(f"save {note_id} not applied within {timeout}s")


def reset_stuck_processing(journal_path: Path, max_age_seconds: int = 60) -> int:
    """Reset entries stuck in 'processing' back to 'pending'.

    Handles the case where the daemon crashes mid-batch.  Entries
    that have been 'processing' longer than ``max_age_seconds``
    are reset so a new daemon can pick them up.

    Returns the number of entries reset.
    """
    conn = _get_journal_conn(journal_path)
    reset = conn.execute(
        "UPDATE write_journal SET status='pending', error=NULL "
        "WHERE status='processing' "
        "AND (processed_at IS NULL OR "
        "datetime(processed_at, ?) <= datetime('now'))",
        (f"+{max_age_seconds} seconds",),
    ).rowcount
    if reset:
        conn.commit()
        logger.info("write_journal: reset %d stuck processing entries", reset)
    return reset


def purge_applied(journal_path: Path, max_age_days: int = 7) -> int:
    """Delete applied entries older than max_age_days.

    Prevents unbounded journal growth.  Run via cron.
    """
    conn = _get_journal_conn(journal_path)
    purged = conn.execute(
        "DELETE FROM write_journal WHERE status='applied' "
        "AND processed_at IS NOT NULL "
        "AND datetime(processed_at, ?) <= datetime('now')",
        (f"+{max_age_days} days",),
    ).rowcount
    if purged:
        conn.commit()
        logger.info("write_journal: purged %d applied entries older than %d days", purged, max_age_days)
    return purged


def journal_stats(journal_path: Path) -> dict[str, int]:
    """Return counts by status for monitoring."""
    conn = _get_journal_conn(journal_path)
    rows = conn.execute(
        "SELECT status, COUNT(*) AS cnt FROM write_journal GROUP BY status"
    ).fetchall()
    stats: dict[str, int] = {r["status"]: r["cnt"] for r in rows}
    stats["total"] = sum(stats.values())
    return stats
