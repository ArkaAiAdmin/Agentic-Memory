#!/usr/bin/env python3
"""Background worker for agentic-memory task queue.

Polls the task_queue table, dispatches tasks to handlers, and manages
graceful shutdown.  Designed to be run via cron (every 5 min) or as a
long-lived process.

Usage:
    # Single-pass (for cron — processes ONE task):
    venv/bin/python background_worker.py --once

    # Drain mode (process ALL pending tasks, then exit). Use this to
    # burn down a backlog: e.g. after a long downtime, run --drain
    # once to flush the 12K task queue, then resume the --once cron.
    venv/bin/python background_worker.py --drain

    # Continuous loop (for background service):
    venv/bin/python background_worker.py --interval=300

    # Process specific task type only:
    venv/bin/python background_worker.py --once --type=entity_resolution
    venv/bin/python background_worker.py --drain --type=entity_resolution

    # Cap how many tasks --drain processes before exiting (safety
    # belt for runaway handlers):
    venv/bin/python background_worker.py --drain --max-tasks=500

Env vars:
    MEMORY_WORKER_INTERVAL: Poll interval in seconds (default 300 = 5 min)
    MEMORY_DB_PATH: Override database path
    MEMORY_WORKER_MAX_TASKS: Cap on --drain (default 10000, safety belt)
"""

from __future__ import annotations

import logging
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from memory_common import safe_close_db, connection_pool
from infrastructure import resolve_active_memory_dir
from .background_queue import init_task_queue, dequeue_task, complete_task, fail_task

logger = logging.getLogger(__name__)

# Graceful shutdown flag
_shutdown = False

# Module-level keep-alive for the inline flock fd (H-fix 2026-06-22).
# See the ImportError fallback in main() — if cron._flock isn't on
# path, we acquire fcntl.flock directly and must keep the fd alive
# for the worker's lifetime or the lock is released.
_BACKGROUND_WORKER_LOCK_FD = None


def _handle_signal(signum, frame):
    global _shutdown
    _shutdown = True
    logger.info("worker: received signal %d, shutting down after current task", signum)


# ---------------------------------------------------------------------------
# Task handlers
# ---------------------------------------------------------------------------


def handle_entity_resolution(
    payload: dict, conn: sqlite3.Connection, db_path: Path
) -> str:
    """Run semantic entity dedup on the KG."""
    try:
        from kg_dedup import dedup_entities, dedup_entities_semantic

        # Exact dedup first
        exact = dedup_entities(conn)
        # Semantic dedup (uses embedding model)
        semantic = dedup_entities_semantic(conn, threshold=0.92)
        return (
            f"exact: {exact['entities_merged']} merged, "
            f"semantic: {semantic['semantic_entities_merged']} merged"
        )
    except Exception as e:
        raise RuntimeError(f"entity_resolution failed: {e}") from e


def handle_fact_consolidation(
    payload: dict, conn: sqlite3.Connection, db_path: Path
) -> str:
    """Run fact consolidation (merge similar SPO triples)."""
    try:
        from consolidate_facts import consolidate_memory_facts

        consolidate_memory_facts(db_path=db_path)
        return "fact consolidation completed"
    except Exception as e:
        raise RuntimeError(f"fact_consolidation failed: {e}") from e


def handle_semantic_backlinks(
    payload: dict, conn: sqlite3.Connection, db_path: Path
) -> str:
    """Create semantic KG edges between the saved memory and its nearest neighbors."""
    try:
        from save.backlinks import _auto_semantic_backlinks

        memory_id = payload.get("memory_id", "")
        content = payload.get("content", "")
        if memory_id and content:
            _auto_semantic_backlinks(
                conn, memory_id, content, db_path=str(db_path)
            )
            return f"semantic backlinks created for {memory_id}"
        return "skipped: no memory_id or content"
    except Exception as e:
        raise RuntimeError(f"semantic_backlinks failed: {e}") from e


def handle_wal_checkpoint(
    payload: dict, conn: sqlite3.Connection, db_path: Path
) -> str:
    """Run a passive WAL checkpoint.

    S4.3 (2026-06-23): the worker now also drains the WAL
    periodically so the on-disk DB stays close to the working
    set.  ``PRAGMA wal_checkpoint(PASSIVE)`` is non-blocking —
    safe under concurrent readers and writers.  Threshold
    defaults to 10 MiB (matches ``wal_checkpoint_idle`` in
    ``db.py``); can be overridden via payload.

    Payload fields (all optional):
      threshold_mb: float (default 10.0).  Only run a checkpoint
        if the WAL file is larger than this many megabytes.
    """
    from db import wal_checkpoint_idle

    threshold = float(payload.get("threshold_mb", 10.0))
    try:
        result = wal_checkpoint_idle(db_path, wal_size_threshold_mb=threshold)
        return f"wal_checkpoint status={result.get('status')} reason={result.get('reason')}"
    except Exception as e:
        raise RuntimeError(f"wal_checkpoint failed: {e}") from e


def handle_embedding_index(
    payload: dict, conn: sqlite3.Connection, db_path: Path
) -> str:
    """Compute and store embedding for a memory note (deferred from save)."""
    try:
        from save.indexers import _index_embedding

        memory_id = payload.get("memory_id", "")
        content = payload.get("content", "")
        source_file = payload.get("source_file", "")
        if not memory_id or not content:
            return "skipped: no memory_id or content in payload"
        category = memory_id.split("/")[0] if "/" in memory_id else "general"
        _index_embedding(conn, memory_id, content, category, [], source_file)
        conn.commit()
        return f"embedding indexed for {memory_id}"
    except Exception as e:
        raise RuntimeError(f"embedding_index failed: {e}") from e


def handle_kg_and_fact_index(
    payload: dict, conn: sqlite3.Connection, db_path: Path
) -> str:
    """Extract KG entities, facts, and enrich context for a memory (deferred)."""
    try:
        from save.indexers import _index_kg, _index_facts
        from save.post_save_hooks import _enrich_context

        memory_id = payload.get("memory_id", "")
        content = payload.get("content", "")
        if not memory_id or not content:
            return "skipped: no memory_id or content in payload"
        category = memory_id.split("/")[0] if "/" in memory_id else "general"
        _index_kg(conn, memory_id, content)
        _index_facts(conn, memory_id, content)
        _enrich_context(conn, memory_id, content, category, [])
        conn.commit()
        return f"KG+facts+context indexed for {memory_id}"
    except Exception as e:
        raise RuntimeError(f"kg_and_fact_index failed: {e}") from e


def handle_vec_index_rebuild(
    payload: dict, conn: sqlite3.Connection, db_path: Path
) -> str:
    """Rebuild the vector index (memory_vec_idx) from memory_embeddings.

    Triggered by save_pipeline when the threshold of pending writes
    since the last rebuild is reached, or by a manual memory_rebuild
    MCP-tool call. Runs `rebuild_vec_index.py` as a subprocess so the
    usearch binary build doesn't lock our worker connection for long.

    Payload fields (all optional):
      force: bool (default False). If True, rebuild even if the index
        already has the right cardinality. Used by the operator-initiated
        path.
      reason: str (default "scheduled"). Free-form audit string.
    """
    try:
        # Locate rebuild_vec_index.py. Resolution order:
        # 1. MEMORY_REBUILD_VEC_INDEX env var (explicit override)
        # 2. <install_root>/rebuild_vec_index.py (uses MEMORY_INSTALL_ROOT if set,
        #    else ~/.config/agentic-memory/)
        # 3. db_path-relative: walk up from the db (covers ad-hoc test DBs)
        # 4. venv heuristic: parent of sys.executable's parent (legacy fallback)
        from memory_config import install_root

        candidates = []
        if os.environ.get("MEMORY_REBUILD_VEC_INDEX"):
            candidates.append(Path(os.environ["MEMORY_REBUILD_VEC_INDEX"]))
        candidates.append(install_root() / "rebuild_vec_index.py")
        if db_path.parent.name == "memory":
            candidates.append(db_path.parent.parent / "rebuild_vec_index.py")
        # Legacy venv heuristic (kept as last-resort fallback for non-standard layouts).
        # sys.executable = <repo>/venv/bin/python; parent.parent.parent = <repo>.
        import sys as _sys

        candidates.append(Path(_sys.executable).parent.parent.parent / "rebuild_vec_index.py")
        # 2026-06-29 fix: cwd-relative resolution. On CI runners the install
        # root doesn't match `install_root()`'s default (~/.config/agentic-memory)
        # but the script is always next to the test runner's cwd.
        candidates.append(Path.cwd() / "rebuild_vec_index.py")
        script = next((c for c in candidates if c and c.exists()), None)
        if script is None:
            raise RuntimeError("rebuild_vec_index.py not found on disk")
        venv_py = Path(_sys.executable)
        if not venv_py.exists():
            raise RuntimeError(f"venv python not found at {venv_py}")
        # Audit-gap fix (2026-06-22 follow-up): capture the reason
        # up front so the graceful-skip branch below can include it
        # in the result string.  ``payload`` is the task payload
        # (a dict of "force", "reason", etc.).
        reason = (payload or {}).get("reason", "scheduled")
        result = subprocess.run(
            [str(venv_py), str(script), str(db_path), "--subsystems", "vec_idx"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            # Audit-gap fix (2026-06-22 follow-up): rebuild_vec_index
            # holds a cross-process flock; if another rebuild is
            # already running, it logs "Another vec_index rebuild is
            # already running." and exits non-zero.  Detect that case
            # and return a graceful "skipped" message rather than
            # treating it as a task failure (which would mark the
            # background task as failed and retry with backoff).
            combined = (result.stdout or "") + (result.stderr or "")
            if "Another vec_index rebuild is already running" in combined:
                return (
                    f"vec_idx rebuild skipped: {reason}; another rebuild is in progress"
                )
            raise RuntimeError(
                f"rebuild_vec_index exited {result.returncode}: {result.stderr or result.stdout}"
            )
        # Parse the final stats line for the audit log.
        return f"vec_idx rebuilt: {reason}; output={result.stdout.strip()[:300]}"
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"vec_index_rebuild timed out: {e}") from e


# Handler registry
HANDLERS = {
    "entity_resolution": handle_entity_resolution,
    "fact_consolidation": handle_fact_consolidation,
    "embedding_index": handle_embedding_index,
    "kg_and_fact_index": handle_kg_and_fact_index,
    "semantic_backlinks": handle_semantic_backlinks,
    "vec_index_rebuild": handle_vec_index_rebuild,
    "wal_checkpoint": handle_wal_checkpoint,
}


# ---------------------------------------------------------------------------
# Proactive vec-index drift reconciliation
# ---------------------------------------------------------------------------


def _get_vec_rebuild_threshold() -> int:
    """Return the max allowable drift before auto-rebuild.

    Reads from config (memory.toml → search.vec_rebuild_threshold,
    env var MEMORY_VEC_REBUILD_THRESHOLD). Default: 5.
    """
    try:
        from _lazy_imports import get_config

        val = get_config().vec_rebuild_threshold
        return int(val) if val is not None else 5
    except Exception:
        return int(os.environ.get("MEMORY_VEC_REBUILD_THRESHOLD", "5"))


def _check_and_reconcile_vec_drift(conn: sqlite3.Connection, db_path: Path) -> None:
    """Check vector-index drift every worker run. Rebuild if drift > threshold.

    The cron worker runs every 5 minutes. This check costs 2 SELECTs
    in the no-drift case (sub-millisecond). On drift, it runs a full
    rebuild (~1.3s for 4K vectors). Any cron that adds memories
    between rebuild_vec_index runs (cross_session_learn, daily_digest,
    auto_summarize) leaves vec_keys 1-2 rows behind. This catches it
    within 5 minutes.

    Threshold configurable via memory.toml search.vec_rebuild_threshold
    or MEMORY_VEC_REBUILD_THRESHOLD env var (default: 5).
    """
    try:
        n_memories = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL"
        ).fetchone()[0]
        n_vec = conn.execute("SELECT COUNT(*) FROM memory_vec_keys").fetchone()[0]
        n_emb = conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0]
    except sqlite3.OperationalError as e:
        logger.info("vec_drift_check: skipping (table missing: %s)", e)
        return

    if n_vec > n_memories or n_emb > n_memories:
        # B5 fix: memories uses soft-delete (deleted_at).  Only hard-delete
        # vec_keys/embeddings for rows that no longer exist at all (hard
        # deleted).  Soft-deleted rows preserve their embeddings so that
        # un-delete later is cheap.
        n_orphan_vec = conn.execute(
            "DELETE FROM memory_vec_keys WHERE memory_id NOT IN "
            "(SELECT id FROM memories)"
        ).rowcount
        n_orphan_emb = conn.execute(
            "DELETE FROM memory_embeddings WHERE memory_id NOT IN "
            "(SELECT id FROM memories)"
        ).rowcount
        if n_orphan_vec or n_orphan_emb:
            conn.commit()
            logger.info(
                "vec_drift_check: cleaned orphan rows (vec_keys: -%d, embeddings: -%d)",
                n_orphan_vec,
                n_orphan_emb,
            )
        n_vec = conn.execute("SELECT COUNT(*) FROM memory_vec_keys").fetchone()[0]
        n_emb = conn.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0]

    vec_drift = n_memories - n_vec
    emb_drift = n_memories - n_emb
    if max(vec_drift, emb_drift) <= _get_vec_rebuild_threshold():
        return

    logger.info(
        "vec_drift_check: drift vec=%d emb=%d (memories=%d, vec_keys=%d, embeddings=%d). Rebuilding...",
        vec_drift,
        emb_drift,
        n_memories,
        n_vec,
        n_emb,
    )
    try:
        handle_vec_index_rebuild({}, conn, db_path)
        n_vec_after = conn.execute("SELECT COUNT(*) FROM memory_vec_keys").fetchone()[0]
        logger.info(
            "vec_drift_check: rebuild complete (vec_keys: %d -> %d, drift was %d)",
            n_vec,
            n_vec_after,
            vec_drift,
        )
    except Exception as e:
        logger.warning("vec_drift_check: rebuild failed: %s", e)


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------


def _maybe_run_wal_checkpoint(conn: sqlite3.Connection, db_path: Path) -> None:
    """S4.3 (2026-06-23): debounced WAL checkpoint.

    Runs a PASSIVE checkpoint if either of:
      - The WAL file is larger than ``threshold_mb`` (default 10).
      - It's been longer than ``interval_s`` since the last run
        (default 300 = 5 min) — i.e. drain the WAL even if
        it's small, periodically.

    S4.4: ``_last_wal_checkpoint_at`` is a module-level float;
    on the first call in a process it's 0, so the first invocation
    will always checkpoint (which is what we want at startup).
    Concurrent invocations across multiple processes (e.g. a cron
    job AND a long-lived worker) are fine: PASSIVE checkpoint is
    safe to overlap.
    """
    global _last_wal_checkpoint_at

    interval_s = int(os.environ.get("MEMORY_WAL_CHECKPOINT_INTERVAL_S", "300"))
    threshold_mb = float(os.environ.get("MEMORY_WAL_CHECKPOINT_THRESHOLD_MB", "10.0"))
    now = time.time()
    if now - _last_wal_checkpoint_at < 60.0:
        # S4.4 debounce: skip if last run was < 60s ago.
        return

    wal_path = Path(str(db_path) + "-wal")
    wal_too_big = False
    if wal_path.exists():
        try:
            size_mb = wal_path.stat().st_size / (1024 * 1024)
            wal_too_big = size_mb >= threshold_mb
        except OSError as exc:
            logger.debug("background_worker: cannot stat WAL %s: %s", wal_path, exc)
            wal_too_big = False
    time_elapsed = now - _last_wal_checkpoint_at >= interval_s
    if not (wal_too_big or time_elapsed):
        return

    try:
        handle_wal_checkpoint({"threshold_mb": threshold_mb}, conn, db_path)
        _last_wal_checkpoint_at = now
    except Exception as e:
        logger.warning("worker: wal_checkpoint failed: %s", e)


_last_wal_checkpoint_at: float = 0.0


def process_one_task(
    conn: sqlite3.Connection, db_path: Path, task_type: str | None = None
) -> bool:
    """Dequeue and process one task. Returns True if a task was processed."""
    task = dequeue_task(conn, task_type=task_type)
    if not task:
        return False

    task_id = task["id"]
    ttype = task["task_type"]
    payload = task["payload"]
    handler = HANDLERS.get(ttype)
    if not handler:
        fail_task(conn, task_id, f"unknown task type: {ttype}")
        logger.warning("worker: unknown task type %s (id=%d)", ttype, task_id)
        return True

    # Watchdog: warn + fail on per-task hang. The 99.9% CPU incident
    # on 2026-06-22 was a single task stuck in a regex search loop for
    # 28+ minutes. If a task runs >120s, log a warning and mark it
    # failed so the queue can progress. Threshold is conservative —
    # KG extraction on 3K memories legitimately takes 5-10s.
    import signal as _sig

    _PER_TASK_TIMEOUT_S = int(os.environ.get("MEMORY_WORKER_TASK_TIMEOUT_S", "120"))

    class _TaskTimeout(Exception):
        pass

    def _timeout_handler(signum, frame):
        raise _TaskTimeout(f"task exceeded {_PER_TASK_TIMEOUT_S}s timeout")

    # P0-10 fix (2026-06-23): signal.alarm only works in the main thread.
    # If this function is invoked from a non-main thread (e.g., from a
    # test or from another worker), signal.SIGALRM will raise a
    # ValueError. Guard with a thread check so the worker can run in
    # non-main threads without crashing; in that case we skip the
    # signal-based timeout (the run is bounded by the outer cron
    # timeout anyway).
    _use_signal_timeout = (
        _sig.getsignal(_sig.SIGALRM) is not _sig.SIG_DFL
        or threading.current_thread() is threading.main_thread()
    )

    if _use_signal_timeout:
        old_handler = _sig.signal(_sig.SIGALRM, _timeout_handler)
        _sig.alarm(_PER_TASK_TIMEOUT_S)
    t_start = time.time()
    try:
        result = handler(payload, conn, db_path)
        complete_task(conn, task_id)
        elapsed = time.time() - t_start
        logger.info(
            "worker: completed task %d (%s) in %.2fs: %s",
            task_id,
            ttype,
            elapsed,
            result,
        )
    except _TaskTimeout:
        elapsed = time.time() - t_start
        fail_task(conn, task_id, f"timeout after {elapsed:.1f}s")
        logger.error(
            "worker: task %d (%s) TIMED OUT after %.1fs — likely runaway regex or loop",
            task_id,
            ttype,
            elapsed,
        )
    except Exception as e:
        elapsed = time.time() - t_start
        error_msg = str(e)[:500]
        fail_task(conn, task_id, error_msg)
        logger.warning(
            "worker: task %d (%s) failed after %.2fs: %s",
            task_id,
            ttype,
            elapsed,
            error_msg,
        )
    finally:
        if _use_signal_timeout:
            _sig.alarm(0)
        _sig.signal(_sig.SIGALRM, old_handler)

    return True


def run_worker(
    db_path: Path,
    interval: int = 300,
    task_type: str | None = None,
    once: bool = False,
    drain: bool = False,
    max_tasks: int = 10000,
) -> None:
    """Run the worker loop.

    Args:
        db_path: Path to memory.db
        interval: Seconds between polls (default 300 = 5 min)
        task_type: If set, only process tasks of this type
        once: If True, process one task and exit (for cron)
        drain: If True, process all pending tasks until empty or
            ``max_tasks`` reached, then exit. Use this to burn down
            a backlog (e.g. 12K tasks accumulated during downtime).
        max_tasks: Safety cap on drain mode (default 10000).
    """
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info(
        "worker: starting (db=%s, interval=%ds, once=%s, drain=%s, max_tasks=%d)",
        db_path,
        interval,
        once,
        drain,
        max_tasks,
    )

    conn = connection_pool.get(str(db_path), timeout=30.0)
    try:
        init_task_queue(conn)
    except Exception as e:
        logger.error("worker: failed to init task queue: %s", e)
        safe_close_db(conn)
        return

    # Proactive vec-index drift check — runs every worker invocation
    # (~5 min) regardless of task queue state. Catches incremental
    # drift from auto_save/cross_session_learn that never reaches
    # the save_pipeline's threshold of 50.
    _check_and_reconcile_vec_drift(conn, db_path)

    # S4.3 (2026-06-23): proactive WAL checkpoint.  Same shape as
    # the vec drift check above — runs every worker invocation and
    # is debounced by reading the WAL file size.  S4.4: if the
    # last checkpoint was < 60s ago (e.g. a parallel cron-driven
    # checkpoint), skip.
    _maybe_run_wal_checkpoint(conn, db_path)

    try:
        if drain:
            # Drain mode: process tasks back-to-back until the queue
            # is empty, max_tasks is hit, OR a wall-clock cap is reached.
            # The wall-clock cap (default 10 min) prevents the worker
            # from getting stuck for hours if individual tasks
            # legitimately slow but the queue keeps re-filling itself
            # (seen on 2026-06-22 before the timeout fix).
            _DRAIN_MAX_WALL_S = int(
                os.environ.get("MEMORY_WORKER_DRAIN_MAX_WALL_S", "600")
            )
            processed = 0
            t_drain = time.time()
            while not _shutdown and processed < max_tasks:
                # Wall-clock guard
                if time.time() - t_drain > _DRAIN_MAX_WALL_S:
                    logger.warning(
                        "worker: drain hit wall-clock cap of %ds after %d tasks — exiting",
                        _DRAIN_MAX_WALL_S,
                        processed,
                    )
                    break
                ok = process_one_task(conn, db_path, task_type=task_type)
                if not ok:
                    # Queue empty (or all tasks of this type are done)
                    break
                processed += 1
                if processed % 50 == 0:
                    elapsed = time.time() - t_drain
                    rate = processed / elapsed if elapsed > 0 else 0
                    logger.info(
                        "worker: drain progress %d/%d (%.1f tasks/sec)",
                        processed,
                        max_tasks,
                        rate,
                    )
            elapsed = time.time() - t_drain
            logger.info(
                "worker: drain complete — processed %d tasks in %.1fs (%.1f tasks/sec)",
                processed,
                elapsed,
                processed / elapsed if elapsed > 0 else 0,
            )
        else:
            while not _shutdown:
                processed = process_one_task(conn, db_path, task_type=task_type)
                if once:
                    break
                if not processed:
                    # No tasks — sleep then re-poll
                    for _ in range(interval):
                        if _shutdown:
                            break
                        time.sleep(1)
    finally:
        safe_close_db(conn)
        logger.info("worker: stopped")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    import argparse

    # H-fix (2026-06-22): acquire flock BEFORE arg parsing so two
    # cron ticks that fire 5 minutes apart don't both run the worker
    # concurrently. Without this, --drain mode would accumulate one
    # worker per cron tick (32 workers seen in 90 minutes on a busy
    # system, all racing on the same SQLite WAL).
    try:
        from cron._flock import acquire_lock_or_exit

        acquire_lock_or_exit("background_worker")
    except ImportError:
        # Best-effort: if flock module isn't on path, fall back to
        # a lightweight inline lock using fcntl directly.
        # NOTE: do NOT re-import `from pathlib import Path` here —
        # the import at module scope (line 43) already makes Path
        # available. A local import makes `Path` a function-local
        # name in the entire main(), which would then raise
        # UnboundLocalError on the try-success path (when the cron
        # flock module IS importable) at `Path(args.db)` on line 612+.
        import fcntl

        _lock_path = (
            Path.home()
            / ".config"
            / "agentic-memory"
            / "memory"
            / "locks"
            / "background_worker.lock"
        )
        _lock_path.parent.mkdir(parents=True, exist_ok=True)
        _lock_fd = open(_lock_path, "w")
        try:
            fcntl.flock(_lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.info("background_worker: another instance holds the lock, exiting")
            _lock_fd.close()
            return 0
        # Hold the fd alive for the lifetime of the worker
        global _BACKGROUND_WORKER_LOCK_FD
        _BACKGROUND_WORKER_LOCK_FD = _lock_fd

    parser = argparse.ArgumentParser(description="Agentic memory background worker")
    parser.add_argument("--once", action="store_true", help="Process one task and exit")
    parser.add_argument(
        "--drain",
        action="store_true",
        help="Process all pending tasks until queue is empty (or --max-tasks "
        "hit), then exit. Use to burn down a backlog.",
    )
    parser.add_argument(
        "--interval", type=int, default=None, help="Poll interval in seconds"
    )
    parser.add_argument(
        "--type", type=str, default=None, help="Only process this task type"
    )
    parser.add_argument(
        "--max-tasks",
        type=int,
        default=None,
        help="Safety cap on --drain mode (default 10000 from env or built-in)",
    )
    parser.add_argument("--db", type=str, default=None, help="Database path")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )

    if args.db:
        db_path = Path(args.db)
    else:
        env = os.environ.get("MEMORY_DB_PATH")
        if env:
            db_path = Path(env)
        else:
            db_path = resolve_active_memory_dir() / "memory.db"

    interval = args.interval or int(os.environ.get("MEMORY_WORKER_INTERVAL", "300"))

    if args.max_tasks is not None:
        max_tasks = args.max_tasks
    else:
        max_tasks = int(os.environ.get("MEMORY_WORKER_MAX_TASKS", "10000"))

    run_worker(
        db_path,
        interval=interval,
        task_type=args.type,
        once=args.once,
        drain=args.drain,
        max_tasks=max_tasks,
    )


if __name__ == "__main__":
    main()
