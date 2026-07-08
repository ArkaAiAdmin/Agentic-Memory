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

import logging

import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from save.pipeline import SaveRequest

logger = logging.getLogger(__name__)

# Maximum journal DB size (bytes). When the on-disk journal DB (including
# its WAL/SHM sidecars) exceeds this, the enqueue/init guards prune applied
# entries or refuse new enqueues to prevent unbounded growth
# (OWASP LLM10-001). Configurable module constant.
JOURNAL_MAX_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB

# W5: threshold (seconds) after which an entry stuck in 'processing' is
# re-dispatched.  Bumped from the original 60s to 120s so a legitimate but
# slow materialization (large embedding batch, KG extraction, etc.) is not
# mistaken for a crashed daemon and re-enqueued mid-flight.  Re-dispatch is
# gated on ``started_at`` (set when the entry is claimed), not just
# ``processed_at``.
STUCK_PROCESSING_MAX_AGE_SECONDS = int(os.environ.get("MEMORY_JOURNAL_STUCK_AGE", "120"))

# W3: maximum number of transient-failure retries before an entry is
# dead-lettered to ``journal_failed``.
JOURNAL_MAX_RETRIES = int(os.environ.get("MEMORY_JOURNAL_MAX_RETRIES", "3"))

# Thread-local connections to the journal DB — zero lock contention.
_local = threading.local()


def _get_journal_conn(journal_path: Path, timeout: float = 10.0) -> sqlite3.Connection:
    """Get or create a thread-local connection to the journal DB."""
    key = str(journal_path)
    if not hasattr(_local, "conns"):
        _local.conns = {}
    conn: sqlite3.Connection | None = _local.conns.get(key)
    if conn is None:
        new_conn: sqlite3.Connection = sqlite3.connect(str(journal_path), timeout=timeout)
        new_conn.execute("PRAGMA journal_mode=WAL")
        new_conn.execute("PRAGMA busy_timeout=5000")
        new_conn.execute("PRAGMA synchronous=NORMAL")
        new_conn.row_factory = sqlite3.Row
        _local.conns[key] = new_conn
        conn = new_conn
    return conn


def _clear_local_conns() -> None:
    """Test helper: close and discard all thread-local connections."""
    if hasattr(_local, "conns"):
        for conn in _local.conns.values():
            try:
                conn.close()
            except Exception as e:
                logger.warning("_clear_local_conns failed: %s", e)
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
    # W3/W5/W6: migration-safe schema extensions.  The journal DB is a
    # separate SQLite file (not the main memory.db, so it is NOT covered
    # by the numbered SQL migration runner in infra/migration_runner.py).
    # All journal schema evolution happens here, idempotently, so an older
    # journal.db transparently gains the new columns on first open.
    _ensure_journal_columns(conn)
    conn.commit()
    # Size guard: prune applied entries if the DB has grown past the limit.
    _enforce_journal_size_limit(journal_path, conn=conn)


def _ensure_journal_columns(conn: sqlite3.Connection) -> None:
    """Idempotently add the W3/W5/W6 columns + dead-letter table.

    The journal DB is a separate SQLite file (``journal.db``) that is NOT
    managed by the numbered SQL migration runner in
    ``infra/migration_runner.py`` (that runner only targets the main
    ``memory.db``).  All journal schema evolution happens here so an
    existing journal.db transparently gains the new columns on first
    open, and a fresh one gets them at creation.  Every statement is
    guarded by a ``PRAGMA table_info`` column-existence check so the
    function is safe to call on every ``init_journal_db``.
    """
    cols = {
        r[1]
        for r in conn.execute("PRAGMA table_info(write_journal)").fetchall()
    }
    if "retry_count" not in cols:
        conn.execute(
            "ALTER TABLE write_journal ADD COLUMN retry_count INTEGER DEFAULT 0"
        )
    if "content_hash" not in cols:
        conn.execute("ALTER TABLE write_journal ADD COLUMN content_hash TEXT")
    if "started_at" not in cols:
        # W5: set when an entry is claimed for materialization.  Re-dispatch
        # of stuck entries is gated on this timestamp so a genuinely slow
        # (but live) materialization is not reset mid-flight.
        conn.execute("ALTER TABLE write_journal ADD COLUMN started_at TEXT")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS journal_failed (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            original_id     INTEGER,
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
            error           TEXT,
            retry_count     INTEGER DEFAULT 0,
            content_hash    TEXT,
            failed_at       TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_journal_failed_note ON journal_failed(note_id)"
    )


def _journal_file_size(journal_path: Path) -> int:
    """Return current on-disk size of the journal DB + WAL/SHM sidecars."""
    total = 0
    for suffix in ("", "-wal", "-shm"):
        sidecar = journal_path.parent / (journal_path.name + suffix)
        try:
            total += sidecar.stat().st_size
        except OSError:
            pass
    return total


def _enforce_journal_size_limit(
    journal_path: Path,
    conn: sqlite3.Connection | None = None,
    size_limit: int = JOURNAL_MAX_SIZE_BYTES,
) -> None:
    """Keep the journal DB under ``size_limit`` by pruning applied entries.

    Oldest applied entries are deleted first (safe to drop — they have
    already been materialized into the main DB by the daemon). This never
    touches pending/processing rows, so the lock-free enqueue design is
    preserved. Stops when under ``size_limit`` or when no applied rows
    remain.
    """
    if _journal_file_size(journal_path) <= size_limit:
        return
    local_conn = conn if conn is not None else _get_journal_conn(journal_path)
    deleted = 0
    while _journal_file_size(journal_path) > size_limit:
        cur = local_conn.execute(
            "SELECT id FROM write_journal WHERE status='applied' "
            "AND processed_at IS NOT NULL ORDER BY processed_at ASC LIMIT 500"
        ).fetchall()
        if not cur:
            break
        ids = [r["id"] for r in cur]
        placeholders = ",".join("?" * len(ids))
        local_conn.execute(
            f"DELETE FROM write_journal WHERE id IN ({placeholders})", ids
        )
        deleted += len(ids)
    if deleted:
        local_conn.commit()
        logger.info("write_journal: pruned %d applied entries to stay under size limit", deleted)


def materialize_security_scan(content: str, category: str, title_slug: str) -> None:
    """Re-run prompt-injection validation on a journal entry before the
    reconciliation daemon materializes it into the main memory DB.

    The enqueue path (``save_memory_journal``) already scans content once,
    but a journal entry can sit ``pending`` for a long time and the rule set
    or model can change in the meantime, so we re-validate at materialization
    time. Raises :class:`SaveValidationError` to quarantine the entry
    (``mark_failed`` inside ``materialize_journal_entry``) when it fails.

    Fails closed: if the scanner itself raises a non-validation error the
    exception propagates and the entry is quarantined rather than persisted.
    """
    from infra._lazy_imports import scan_for_injection
    from save.pipeline import SaveValidationError, ErrorCode

    inj = scan_for_injection(content)
    if inj["is_suspicious"] and inj["risk_score"] >= 0.5:
        raise SaveValidationError(
            ErrorCode.INJECTION_DETECTED,
            f"Journal entry rejected: injection risk score {inj['risk_score']:.2f} "
            f"(category: {inj['category']}). "
            f"If this is legitimate, rephrase to avoid instruction-like patterns.",
        )
    elif inj["is_suspicious"]:
        logger.info(
            "write_journal: low-risk injection patterns in %s/%s "
            "(risk_score=%.2f, matches=%s) — allowing materialization",
            category,
            title_slug,
            inj["risk_score"],
            inj["matches"],
        )


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
    # Size guard: refuse new enqueues if the journal DB is still over the
    # limit after pruning applied entries. This bounds disk usage and
    # surfaces the condition with a clear error instead of silently
    # growing without bound (OWASP LLM10-001).
    if _journal_file_size(journal_path) > JOURNAL_MAX_SIZE_BYTES:
        _enforce_journal_size_limit(journal_path, conn=conn)
        if _journal_file_size(journal_path) > JOURNAL_MAX_SIZE_BYTES:
            current = _journal_file_size(journal_path)
            raise RuntimeError(
                f"write_journal is full: size {current} bytes exceeds "
                f"JOURNAL_MAX_SIZE_BYTES ({JOURNAL_MAX_SIZE_BYTES}). "
                "Refusing new enqueue. Drain pending entries via the "
                "reconciliation daemon, raise JOURNAL_MAX_SIZE_BYTES, or "
                "rebuild the journal DB."
            )
    # W6: per-row SHA256 integrity.  Stored at enqueue time and verified
    # at materialization so a corrupted journal row is detected (and
    # dead-lettered) rather than silently materialized into memory.db.
    content_hash = hashlib.sha256(req.content.encode("utf-8")).hexdigest()
    conn.execute(
        """INSERT OR IGNORE INTO write_journal
           (note_id, agent_id, category, title_slug, content, tags,
             pinned, is_global, importance, tenant_id,
             epistemic_source, belief_status, asserting_agent_id, fact_type,
             defer_expensive, context, content_hash, retry_count)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
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
            content_hash,
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
            f"UPDATE write_journal SET status='processing', processed_at=datetime('now'), "
            f"started_at=datetime('now') WHERE id IN ({placeholders})",
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
    """Mark a journal entry as failed with an error message.

    Kept for backward compatibility.  New callers should prefer
    :func:`mark_retry` (transient) or :func:`mark_dead_letter` (permanent)
    so the entry is either retried or moved to ``journal_failed`` rather
    than left in a terminal ``failed`` state that the daemon never
    re-visits (W3).
    """
    conn = _get_journal_conn(journal_path)
    conn.execute(
        "UPDATE write_journal SET status='failed', error=? WHERE id=?",
        (error[:500], entry_id),
    )
    conn.commit()


def mark_retry(journal_path: Path, entry_id: int, error: str) -> int:
    """Record a transient failure and return the new retry_count.

    Increments ``retry_count`` and resets the entry to ``pending`` so the
    reconciliation loop picks it up again (W3).  The daemon applies
    backoff between attempts based on the returned count.
    """
    conn = _get_journal_conn(journal_path)
    conn.execute(
        "UPDATE write_journal SET status='pending', error=?, "
        "retry_count = retry_count + 1, started_at=NULL, "
        "processed_at=datetime('now') WHERE id=?",
        (error[:500], entry_id),
    )
    row = conn.execute(
        "SELECT retry_count FROM write_journal WHERE id=?", (entry_id,)
    ).fetchone()
    conn.commit()
    return int(row["retry_count"]) if row is not None else 0


def mark_dead_letter(journal_path: Path, entry_id: int, error: str) -> None:
    """Permanently fail an entry: copy it to ``journal_failed`` and remove it.

    Used when an entry exhausts its retries (W3) or fails a hard
    validation (e.g. content-hash mismatch in W6).  The original row is
    deleted so it is never re-dispatched; the copy in ``journal_failed``
    preserves the payload + error for operator inspection.  The material
    caller is responsible for emitting the alert (see
    ``materialize_journal_entry``).
    """
    conn = _get_journal_conn(journal_path)
    conn.execute(
        """INSERT INTO journal_failed
           (original_id, note_id, agent_id, category, title_slug, content,
            tags, pinned, is_global, importance, tenant_id,
            epistemic_source, belief_status, asserting_agent_id, fact_type,
            defer_expensive, context, error, retry_count, content_hash)
           SELECT id, note_id, agent_id, category, title_slug, content,
                  tags, pinned, is_global, importance, tenant_id,
                  epistemic_source, belief_status, asserting_agent_id, fact_type,
                  defer_expensive, context, ?, retry_count, content_hash
           FROM write_journal WHERE id=?""",
        (error[:500], entry_id),
    )
    conn.execute("DELETE FROM write_journal WHERE id=?", (entry_id,))
    conn.commit()


def verify_content_hash(content: str, stored_hash: str | None) -> bool:
    """Return True if *content*'s SHA256 matches *stored_hash* (W6).

    A ``None``/empty stored hash (legacy rows written before W6) is
    treated as a pass so we don't dead-letter historical entries that
    were never hashed.
    """
    if not stored_hash:
        return True
    return hashlib.sha256(content.encode("utf-8")).hexdigest() == stored_hash


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


def reset_stuck_processing(
    journal_path: Path, max_age_seconds: int | None = None
) -> int:
    """Reset entries stuck in 'processing' back to 'pending'.

    Handles the case where the daemon crashes mid-batch.  Entries whose
    ``started_at`` timestamp is older than ``max_age_seconds`` are reset
    so a new daemon can pick them up.

    W5: gated on ``started_at`` (set when the entry is claimed) rather
    than ``processed_at`` so a legitimate but slow materialization is not
    re-dispatched while it is still making progress.  The default
    threshold is :data:`STUCK_PROCESSING_MAX_AGE_SECONDS` (120s), raised
    from the original 60s.

    Returns the number of entries reset.
    """
    if max_age_seconds is None:
        max_age_seconds = STUCK_PROCESSING_MAX_AGE_SECONDS
    conn = _get_journal_conn(journal_path)
    reset = conn.execute(
        "UPDATE write_journal SET status='pending', error=NULL, started_at=NULL "
        "WHERE status='processing' "
        "AND (started_at IS NULL "
        "OR datetime(started_at, ?) <= datetime('now'))",
        (f"+{max_age_seconds} seconds",),
    ).rowcount
    if reset:
        conn.commit()
        logger.info(
            "write_journal: reset %d stuck processing entries (age>=%ds)",
            reset,
            max_age_seconds,
        )
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
