"""Pipeline end-to-end health check (non-blocking, two-phase).

Each run:
  1. Enqueues a fresh sentinel task (HIGH priority).
  2. Evaluates the PREVIOUS run's sentinel instead of blocking on this
     one — the queue drain cadence is minutes (~5 min via launchd
     interval), so any in-process poll window shorter than that reports
     "worker appears down" for a healthy pipeline 100% of the time.
     The previous sentinel's outcome is the true end-to-end signal:

       completed within HEALTHY_MAX_LATENCY_S  -> healthy (0)
       completed but slow                      -> healthy + warn
       failed                                  -> unhealthy (1)
       still pending past STALE_PENDING_S      -> unhealthy (1)
       in-flight (young)                       -> healthy (0)

Also reports: failures in last 24h, pending queue depth, CQRS journal
backlog. Self-heals by attempting to restart the background worker when
the queue looks stalled.

Returns 0 if healthy, 1 if not.
"""

import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from _flock import acquire_lock_or_exit

logger = logging.getLogger(__name__)

def _log_structured(level: str | int, event: str, **fields: Any) -> None:
    import json as _json
    if isinstance(level, int):
        level = logging.getLevelName(level).lower()
    log_entry = {"event": event, **fields}
    getattr(logger, level)(_json.dumps(log_entry))

SENTINEL_TYPE = "cron_pipeline_sentinel"

# Observed queue drain latency is ~4-5 min (launchd StartInterval).
# A sentinel completing within 15 min is healthy; one still pending
# after 30 min means the worker is genuinely not draining.
HEALTHY_MAX_LATENCY_S = 900.0
STALE_PENDING_S = 1800.0

_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_repo_root)
_parent = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(_parent) == "cron":
    _parent = os.path.dirname(_parent)
sys.path.insert(0, _parent)

from infra.memory_config import GLOBAL_MEM_DIR


def _resolve_db() -> Path:
    env_path = os.environ.get("MEMORY_DB_PATH")
    if env_path:
        return Path(env_path)
    # Use GLOBAL_MEM_DIR directly (CWD-independent). The default
    # resolve_active_memory_dir() is CWD-relative and resolves to the
    # wrong dir when this script chdir's into cron/ (see top of file),
    # silently pointing the health check at an empty local DB.
    return GLOBAL_MEM_DIR / "memory.db"


def _enqueue_sentinel(conn) -> int:
    from background.background_queue import init_task_queue, enqueue_task

    # The sentinel is enqueued at HIGH priority so it jumps any backlog
    # (heavy cron_health_check / backfill tasks) instead of waiting behind
    # them — the whole point is to probe worker liveness, not queue order.
    # Retry the enqueue briefly in case the worker holds the write lock
    # mid-task; a 30s busy_timeout + this loop lets us slip in.
    last_exc: Exception | None = None
    for _attempt in range(20):
        try:
            init_task_queue(conn)
            task_id = enqueue_task(
                conn, SENTINEL_TYPE, payload={"_sentinel": True}, priority=1000
            )
            if isinstance(task_id, dict):
                raise RuntimeError(f"sentinel enqueue failed: {task_id.get('reason', '?')}")
            return task_id
        except Exception as exc:  # pragma: no cover
            last_exc = exc
            time.sleep(0.5)
    raise RuntimeError(f"sentinel enqueue failed after retries: {last_exc}")


def _open_db(db_path: Path):
    """Open memory.db directly with a generous busy_timeout.

    Uses a plain sqlite3 connection (NOT sqlite_write_queue.start_session)
    so the health check doesn't fight the long-lived background_worker
    for the exclusive per-DB-path flock. The worker releases the
    flock on idle (see background_worker._worker_loop); a 30s
    busy_timeout + the _enqueue_sentinel retry loop lets the check
    slip the INSERT in between the worker's polls/drains.
    """
    import sqlite3

    c = sqlite3.connect(str(db_path))
    c.execute("PRAGMA busy_timeout=30000")
    return c


def _previous_sentinel_outcome(conn) -> dict:
    """Outcome of the most recent sentinel enqueued BEFORE this run.

    Two-phase probe: this run only inspects history, it never blocks.
    Latency/age are computed in SQL via julianday so timezone handling
    stays in SQLite's UTC frame.

    Returns a dict: {state: completed|failed|pending|none,
                     latency_s: float|None, age_s: float|None,
                     task_id: int|None}
    """
    row = conn.execute(
        """SELECT id, status,
                  CASE WHEN completed_at IS NOT NULL AND created_at IS NOT NULL
                       THEN (julianday(completed_at) - julianday(created_at)) * 86400.0
                       ELSE NULL END,
                  (julianday('now') - julianday(created_at)) * 86400.0
           FROM task_queue
           WHERE task_type = ?
           ORDER BY id DESC LIMIT 1""",
        (SENTINEL_TYPE,),
    ).fetchone()
    if row is None:
        return {"state": "none", "latency_s": None, "age_s": None, "task_id": None}
    task_id, status, latency_s, age_s = row
    return {
        "state": status if status in ("completed", "failed") else "pending",
        "latency_s": float(latency_s) if latency_s is not None else None,
        "age_s": float(age_s) if age_s is not None else None,
        "task_id": int(task_id),
    }


def _count_failures(conn, hours: int = 24) -> int:
    row = conn.execute(
        """SELECT COUNT(*) FROM task_queue
           WHERE status = 'failed'
           AND (completed_at IS NOT NULL
                OR julianday('now') - julianday(created_at) < ? / 24.0)""",
        (hours,),
    ).fetchone()
    return row[0] if row else 0


def _pending_depth(conn) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM task_queue WHERE status = 'pending'"
    ).fetchone()
    return row[0] if row else 0


# CQRS write-journal backlog guard (Step 8 follow-up). When
# MEMORY_WRITE_JOURNAL_ENABLED is ON, agents enqueue writes to
# journal.db and the background_worker daemon drains them. If the
# daemon is not live (e.g. cron not installed, or it crashed and
# the watchdog hasn't restarted it yet), pending rows accumulate and
# writes are silently deferred. Surface this through the same health
# channel so pipeline-coverage can alert + the daemon can be restarted.
JOURNAL_PENDING_WARN = 50


def _journal_pending_depth(db_path: Path) -> int | None:
    """Count pending rows in journal.db, or None if the journal is off/absent.

    Opens journal.db directly (read-only SELECT) rather than via
    ``sqlite_write_queue.start_session``, because the journal DB carries
    only the ``write_journal`` table and is not managed by the numbered
    migration runner that ``start_session`` would apply.
    """
    import sqlite3

    journal_path = db_path.parent / "journal.db"
    if not journal_path.exists():
        return None
    try:
        jconn = sqlite3.connect(str(journal_path))
        try:
            row = jconn.execute(
                "SELECT COUNT(*) FROM write_journal WHERE status = 'pending'"
            ).fetchone()
            return row[0] if row else 0
        finally:
            jconn.close()
    except Exception:
        # Journal unreachable (locked, corrupt, no table) — treat as unknown.
        return None


# Recovery: when the sentinel can't complete because no worker is draining
# the queue, attempt a one-shot background_worker drain via direct spawn.
# (launchd supervision retired 2026-08-23 — its label sat dead while cron
# did all draining.) Best-effort: never masks the failure.


def _worker_alive() -> bool:
    """Best-effort liveness check for a background_worker process.

    True if a ``background_worker.py`` process with an active drain flag
    is running (pgrep). Used to decide whether recovery is needed before
    declaring the pipeline unhealthy on a stale sentinel.
    """
    import subprocess

    # Tightened match: only match actual worker invocations (--drain,
    # --once, --interval), not editors or import-only processes.
    try:
        r = subprocess.run(
            ["pgrep", "-f", "background_worker.py.*(--drain|--interval|--once)"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.stdout.strip():
            return True
    except Exception:
        pass
    return False


def _try_start_worker() -> bool:
    """Attempt to start a one-shot background_worker drain. Returns True
    if the spawn succeeded. Failure is non-fatal.

    The cron scheduler owns worker scheduling (5m --drain); launchd
    supervision was retired 2026-08-23 after its label sat dead
    (exit 1, no PID) while cron did all draining. We spawn ``--once``
    directly: it drains up to 50 tasks and exits, no supervisor needed.
    """
    import subprocess

    try:
        repo_root = Path(__file__).resolve().parent.parent
        venv_py = repo_root / "venv" / "bin" / "python"
        worker = repo_root / "background" / "background_worker.py"
        if venv_py.exists() and worker.exists():
            subprocess.Popen(
                [str(venv_py), str(worker), "--once", "--max-tasks=50"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=str(repo_root),
            )
            _log_structured(logging.INFO, "worker_recovered", method="spawn")
            return True
        _log_structured(logging.WARNING, "worker_recovery_failed", method="spawn",
                        error="venv python or worker script missing")
    except Exception as e:  # pragma: no cover
        _log_structured(logging.WARNING, "worker_recovery_failed", method="spawn", error=str(e))
    return False


def main() -> int:
    acquire_lock_or_exit("cron_pipeline_health")
    db_path = _resolve_db()
    if not db_path.exists():
        print(f"CRITICAL: database not found at {db_path}", file=sys.stderr)
        return 1

    conn = _open_db(db_path)
    failures = pending = 0
    try:
        # Phase 1 — evaluate the PREVIOUS sentinel (never blocks).
        prev = _previous_sentinel_outcome(conn)

        # Phase 2 — enqueue a fresh sentinel for the NEXT run to judge.
        task_id = _enqueue_sentinel(conn)
        _log_structured(logging.INFO, "sentinel_enqueued", task_id=task_id,
                        previous_state=prev["state"],
                        previous_latency_s=prev["latency_s"],
                        previous_age_s=prev["age_s"])

        failures = _count_failures(conn, hours=24)
        pending = _pending_depth(conn)
        journal_pending = _journal_pending_depth(db_path)

        print(f"journal_pending: {journal_pending}", flush=True)
        print(
            f"previous_sentinel: state={prev['state']} "
            f"latency_s={prev['latency_s']} age_s={prev['age_s']}",
            flush=True,
        )
        _log_structured(
            logging.INFO,
            "pipeline_status",
            failed_last_24h=failures,
            pending_queue_depth=pending,
            journal_pending=journal_pending,
            previous_sentinel=prev["state"],
            previous_sentinel_latency_s=prev["latency_s"],
        )

        if journal_pending is not None and journal_pending > JOURNAL_PENDING_WARN:
            _log_structured(logging.WARNING, "journal_backlog", journal_pending=journal_pending, threshold=JOURNAL_PENDING_WARN)

        # Self-heal: attempt worker restart on any stall signal.
        stalled = (
            prev["state"] == "pending"
            and (prev["age_s"] or 0.0) > STALE_PENDING_S
        ) or (prev["state"] == "failed")
        if stalled and not _worker_alive():
            if _try_start_worker():
                from infra.alert import alert

                alert(
                    "warning",
                    "Background worker restarted",
                    "Pipeline health check initiated worker restart",
                )

        # Verdict from the PREVIOUS sentinel.
        if prev["state"] in ("none",):
            # First run after install/cleanup: nothing to judge yet.
            return 0
        if prev["state"] == "completed":
            latency = prev["latency_s"] or 0.0
            if latency > HEALTHY_MAX_LATENCY_S:
                _log_structured(
                    logging.WARNING,
                    "sentinel_slow",
                    latency_s=round(latency, 1),
                    threshold_s=HEALTHY_MAX_LATENCY_S,
                )
            return 0
        if prev["state"] == "failed":
            raise RuntimeError(f"previous sentinel task {prev['task_id']} failed")
        # Pending: healthy while young, unhealthy once stale.
        if (prev["age_s"] or 0.0) > STALE_PENDING_S:
            raise TimeoutError(
                f"sentinel task {prev['task_id']} still pending after "
                f"{int(prev['age_s'])}s (stale threshold {int(STALE_PENDING_S)}s)"
            )
        return 0
    except Exception as exc:
        _log_structured(logging.ERROR, "pipeline_health_failed", error=str(exc), failed_tasks=failures, pending=pending)
        from infra.alert import alert

        alert(
            "critical",
            "Pipeline health check failed",
            f"error={exc}, failed_tasks={failures}, pending={pending}",
        )
        return 1
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
