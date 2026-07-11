"""Sprint 4 / P0 #4: tool-call audit log infrastructure.

Append-only logging of every MCP tool invocation. Writers are
fire-and-forget from the tool wrapper (``enqueue_audit``) and a
single background thread batches INSERTs into ``memory_audit_log``
on the call's DB.

Design notes:
  * Per-DB ``memory_audit_log`` (created by ``_migrate_memory_audit_log``
    in memory_common.py). Local and global DBs each have their own.
  * Writer thread groups queued rows by ``db_path`` so each batch
    INSERTs to one DB at a time. This avoids opening the same DB from
    two threads in parallel (SQLite serializes writes per connection
    anyway, but explicit is better).
  * Bounded queue (10000 entries) so a runaway call site can't OOM
    the process. Drops are logged at DEBUG; the main thread never
    blocks on enqueue.
  * Graceful shutdown via atexit + a public ``flush_audit(timeout)``
    helper for tests / final gates.
  * No-op fallback: if ``open_db`` raises (e.g. read-only path), the
    row is logged at DEBUG and dropped. Audit must never break a tool
    call.
"""

from __future__ import annotations

import logging

import atexit
import json
import queue
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

__all__ = [
    "enqueue_audit",
    "flush_audit",
    "audit",
    "is_audit_thread_alive",
    "audit_queue_size",
    "AUDIT_QUEUE_MAXSIZE",
    "AUDIT_FLUSH_INTERVAL_S",
    "AUDIT_BATCH_MAX_ROWS",
]

# Bounded queue; a burst of 10k tool calls is enough headroom for any
# realistic workload without risking OOM if something goes wrong.
AUDIT_QUEUE_MAXSIZE = 10_000

# How often the writer thread wakes up to drain the queue.
AUDIT_FLUSH_INTERVAL_S = 0.1

# Per-flush cap. A larger batch means fewer transactions but bigger
# memory pressure. 1000 rows × ~500 bytes/row = 500KB peak, which is
# fine.
AUDIT_BATCH_MAX_ROWS = 1_000

_AUDIT_QUEUE: "queue.Queue[dict]" = queue.Queue(maxsize=AUDIT_QUEUE_MAXSIZE)
_AUDIT_SHUTDOWN = threading.Event()
_AUDIT_THREAD: Optional[threading.Thread] = None
_AUDIT_LOCK = threading.Lock()
logger = logging.getLogger(__name__)

# --- OWASP A09-001: secret redaction for audit args -----------------------
# Centralized in infra/audit_sink so the local log AND external sinks share
# one redaction implementation. Re-exported here to keep call-site imports
# stable.
from infra.audit_sink import redact_audit_value, REDACTED_MASK

# Backwards-compatible alias (kept so any in-repo caller still works).
_redact_args = redact_audit_value

# Pending counter — incremented on enqueue, decremented after the
# writer thread has finished processing (INSERT or drop). Lets
# ``flush_audit`` block until the writer is genuinely idle, not just
# until the queue is empty (a row pulled from the queue is still
# "pending" until its INSERT completes).
_PENDING = 0
_PENDING_COND = threading.Condition(threading.Lock())


def _ensure_audit_thread() -> None:
    """Start the background writer thread (idempotent).

    Called lazily from ``enqueue_audit`` so importing this module has
    zero side effects (no threads started, no atexit registered) until
    a tool actually wants to log.
    """
    global _AUDIT_THREAD
    with _AUDIT_LOCK:
        if _AUDIT_THREAD is not None and _AUDIT_THREAD.is_alive():
            return
        _AUDIT_SHUTDOWN.clear()
        _AUDIT_THREAD = threading.Thread(
            target=_audit_writer_loop,
            name="memory-audit-writer",
            daemon=True,
        )
        _AUDIT_THREAD.start()
        atexit.register(_shutdown_audit_thread)


def _audit_writer_loop() -> None:
    """Background loop: drain the queue, group by db_path, write batches.

    Loops until ``_AUDIT_SHUTDOWN`` is set. On shutdown, drains the
    queue one last time and exits. The whole iteration is wrapped in
    a try/except so a single malformed row or transient DB error can
    never kill the thread — losing one batch is acceptable, losing
    the whole audit pipeline is not.
    """
    while not _AUDIT_SHUTDOWN.is_set():
        try:
            rows = _drain_queue(
                max_rows=AUDIT_BATCH_MAX_ROWS,
                timeout=AUDIT_FLUSH_INTERVAL_S,
            )
            if rows:
                try:
                    _flush_audit_rows(rows)
                finally:
                    _mark_rows_processed(len(rows))
        except Exception as e:
            logger.exception("audit writer loop caught exception, will retry: %s", e)
    # Final drain on shutdown
    try:
        final = _drain_queue(max_rows=AUDIT_BATCH_MAX_ROWS, timeout=0.0)
        if final:
            try:
                _flush_audit_rows(final)
            finally:
                _mark_rows_processed(len(final))
    except Exception as e:
        logger.exception("audit final-drain on shutdown failed: %s", e)


def _mark_rows_processed(n: int) -> None:
    """Decrement the pending counter and notify any waiters."""
    with _PENDING_COND:
        global _PENDING
        _PENDING -= n
        _PENDING_COND.notify_all()


def _drain_queue(max_rows: int, timeout: float) -> list:
    """Pull up to ``max_rows`` from the queue, blocking up to ``timeout``.

    Returns an empty list on timeout. Never raises ``queue.Empty``.
    """
    rows: list[Any] = []
    deadline = time.time() + timeout
    while len(rows) < max_rows:
        remaining = max(0.0, deadline - time.time())
        try:
            rows.append(_AUDIT_QUEUE.get(timeout=min(remaining, 0.05)))
        except queue.Empty:
            break
    return rows


def _flush_audit_rows(rows: list) -> None:
    """Group rows by db_path, open one connection per path, write a batch.

    Best-effort: any exception is logged at WARNING and the batch is
    dropped. The audit log must never propagate failures.
    """
    # Local import to avoid circular dependency with memory_common.
    from infra._lazy_imports import open_db

    by_path: dict = {}
    for row in rows:
        by_path.setdefault(row["db_path"], []).append(row)
    for db_path, batch in by_path.items():
        try:
            with open_db(Path(db_path), timeout=5.0) as conn:
                with conn:
                    conn.executemany(
                        "INSERT INTO memory_audit_log "
                        "(ts, tool, args, results_count, top1_id, "
                        "latency_ms, error, request_id, principal_id) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [
                            (
                                r["ts"],
                                r["tool"],
                                r["args"],
                                r["results_count"],
                                r["top1_id"],
                                r["latency_ms"],
                                r["error"],
                                r["request_id"],
                                r["principal_id"],
                            )
                            for r in batch
                        ],
                    )
        except Exception as e:
            logger.warning(
                "audit flush failed for %s (%d rows dropped): %s",
                db_path,
                len(batch),
                e,
            )


def _shutdown_audit_thread() -> None:
    """Set the shutdown flag and wait for the thread to drain + exit.

    Bounded timeout (2s) so a hung thread can't block interpreter
    shutdown forever. Any rows still in the queue at that point are
    lost — the alternative is hanging the process, which is worse.
    """
    _AUDIT_SHUTDOWN.set()
    if _AUDIT_THREAD is not None:
        _AUDIT_THREAD.join(timeout=2.0)


def enqueue_audit(
    db_path: str,
    tool: str,
    args: Any,
    *,
    results_count: Optional[int] = None,
    top1_id: Optional[str] = None,
    latency_ms: float = 0.0,
    error: Optional[str] = None,
    request_id: Optional[str] = None,
    principal_id: Optional[str] = None,
) -> None:
    """Fire-and-forget audit row. Never blocks, never raises.

    Args:
        db_path: Absolute path to the SQLite DB the call was made
            against. Used to route the row to the correct
            ``memory_audit_log`` table.
        tool: MCP tool name (e.g. ``"memory_search"``).
        args: Argument payload — anything JSON-serializable. Fall
            back to ``repr()`` on serialization failure.
        results_count: Number of results returned, or -1 for tools
            that don't produce a count. None = unknown.
        top1_id: ID of the top-ranked result (search tools only).
        latency_ms: Wall-clock latency of the tool call.
        error: Error message string if the call failed. None on
            success.
        request_id: Optional correlation id for distributed tracing.

    Note:
        Safe to call from any thread. If the queue is full the row is
        silently dropped (DEBUG log) — losing one audit row is
        acceptable, blocking the tool call is not.
    """
    _ensure_audit_thread()
    # OWASP A09-001: redact secrets from args before serializing them into
    # the audit log so plaintext credentials never hit memory_audit_log.
    redacted_args = _redact_args(args)
    try:
        args_json = json.dumps(redacted_args, default=str) if redacted_args is not None else None
    except (TypeError, ValueError):
        args_json = repr(redacted_args)
    row = {
        "ts": time.time(),
        "tool": tool,
        "args": args_json,
        "results_count": results_count,
        "top1_id": top1_id,
        "latency_ms": float(latency_ms),
        "error": error,
        "request_id": request_id,
        "principal_id": principal_id,
        "db_path": db_path,
    }
    global _PENDING
    with _PENDING_COND:
        _PENDING += 1
    try:
        _AUDIT_QUEUE.put_nowait(row)
    except queue.Full:
        with _PENDING_COND:
            _PENDING -= 1
            _PENDING_COND.notify_all()
        logger.debug(
            "audit queue full (%d entries), dropping row for %s",
            _AUDIT_QUEUE.qsize(),
            tool,
        )
    # Phase 3: fan the event out to configured sinks (file / prom / http).
    # Fire-and-forget + bounded queue — never blocks the tool call and never
    # raises into the caller. The local memory_audit_log write above remains
    # the source of truth.
    try:
        from infra.audit_sink import dispatch_to_sinks

        dispatch_to_sinks(row)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("audit sink dispatch skipped: %s", exc)


def flush_audit(timeout: float = 5.0) -> bool:
    """Block until all enqueued rows have been processed, or timeout.

    Waits on the pending counter (incremented on enqueue, decremented
    after the writer finishes the INSERT or drops the row). Returns
    True if the counter reached 0 within ``timeout``, False otherwise.

    Intended for tests and the final-gate perf check. The writer
    thread is the only consumer of the queue, so when ``_PENDING==0``
    every row that was ever enqueued is either persisted to the DB
    or has been dropped (queue full).
    """
    deadline = time.time() + timeout
    with _PENDING_COND:
        while _PENDING > 0:
            remaining = deadline - time.time()
            if remaining <= 0:
                return False
            _PENDING_COND.wait(timeout=remaining)
    return True


def is_audit_thread_alive() -> bool:
    """Whether the writer thread is currently running. Test-only."""
    return _AUDIT_THREAD is not None and _AUDIT_THREAD.is_alive()


def audit_queue_size() -> int:
    """Current depth of the audit queue. Test-only."""
    return _AUDIT_QUEUE.qsize()


@contextmanager
def audit(
    tool: str,
    *,
    args: Any = None,
    db_path: str,
    request_id: Optional[str] = None,
    principal_id: Optional[str] = None,
) -> Iterator[dict]:
    """Context manager that records an audit row on exit.

    Wall-clock latency is measured from entry to exit. Exceptions
    inside the ``with`` block are captured into the ``error`` column
    of the audit row AND re-raised — audit never swallows real errors.

    The yielded ``ctx`` dict is for the caller to fill in optional
    fields (results_count, top1_id, request_id, etc.). Any keys
    present at exit are forwarded to ``enqueue_audit``.

    Usage::

        with audit("memory_search", args={"q": q, "limit": 5},
                   db_path=str(db_path)) as ctx:
            rows = search(...)
            ctx["results_count"] = len(rows)
            ctx["top1_id"] = rows[0]["id"] if rows else None

    Args:
        tool: MCP tool name.
        args: Argument payload (will be JSON-serialized).
        db_path: Absolute path to the SQLite DB the call hit. The
            writer routes the row to ``memory_audit_log`` in that DB.
        request_id: Optional correlation id for distributed tracing.

    Yields:
        A dict the caller can mutate; values are forwarded to the
        audit row on exit.
    """
    start = time.time()
    ctx: dict = {"request_id": request_id}
    error: Optional[str] = None
    try:
        yield ctx
    except BaseException as e:
        # Use repr to keep one-line, never lossy. Truncate very long
        # error strings so the row doesn't blow up the DB.
        logger.warning("audit failed: %s", e)
        error = repr(e)
        if len(error) > 2000:
            error = error[:2000] + "...<truncated>"
        raise
    finally:
        latency_ms = (time.time() - start) * 1000.0
        # Pull any caller-set fields off ctx; fall back to None.
        enqueue_audit(
            db_path=db_path,
            tool=tool,
            args=args,
            results_count=ctx.get("results_count"),
            top1_id=ctx.get("top1_id"),
            latency_ms=latency_ms,
            error=error,
            request_id=ctx.get("request_id"),
            principal_id=principal_id,
        )
