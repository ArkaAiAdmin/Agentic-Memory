"""Pipeline end-to-end health check.

Enqueues a sentinel task and waits for the background worker to pick
it up and complete it (short poll, ~30s max).  Reports:

  - elapsed (s) for sentinel completion
  - total failed tasks in the last 24h
  - pending queue depth

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


def _poll_sentinel(conn, task_id: int, timeout_s: float = 30.0) -> float:
    """Wait for the sentinel to complete.

    The deadline is self-extending up to a bounded cap: if the worker is
    still alive but busy draining a heavy backlog (e.g. a 30s
    cron_health_check task), we keep waiting past the nominal window rather
    than declaring failure. We only give up when either the sentinel
    completes or the worker is confirmed dead — in which case the sentinel
    will never be picked up.

    The extension is bounded so callers that expect a hard short timeout
    (notably the unit tests passing timeout_s=1) still time out promptly:
    a live worker extends the soft window by at most ``timeout_s`` once,
    and the absolute ceiling is ``2 * timeout_s``.
    """
    soft_deadline = time.time() + timeout_s
    hard_deadline = time.time() + 2.0 * timeout_s
    while time.time() < hard_deadline:
        row = conn.execute(
            "SELECT status, completed_at FROM task_queue WHERE id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"sentinel task {task_id} not found")
        status = row[0]
        if status == "completed":
            return time.time()
        if status == "failed":
            raise RuntimeError(f"sentinel task {task_id} failed")
        # Worker alive → extend the wait modestly; it WILL get to the
        # sentinel (enqueued at high priority). Cap at 2x the requested
        # timeout so a short timeout still fails promptly when the worker
        # is down.
        if _worker_alive():
            soft_deadline = max(soft_deadline, time.time() + timeout_s)
            if soft_deadline > hard_deadline:
                soft_deadline = hard_deadline
        elif time.time() > soft_deadline:
            # Worker dead and past the soft window → genuine failure.
            raise TimeoutError(
                f"sentinel task {task_id} not completed: worker appears down"
            )
        time.sleep(1)
    raise TimeoutError(f"sentinel task {task_id} not completed within {hard_deadline}s")


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
# the queue, attempt to (re)start the background_worker daemon. launchd is
# the preferred supervisor (auto-restart + boot), with a direct subprocess
# spawn as a last-resort fallback. Best-effort: never masks the failure.
PLIST_NAME = "com.agentic-memory.background-worker.plist"


def _worker_alive() -> bool:
    """Best-effort liveness check for the background_worker daemon.

    True if launchd reports it loaded (PID column non-zero) or a
    ``background_worker.py`` process is running. Used to decide whether a
    sentinel timeout means "worker is down" (real failure) or merely
    "worker is alive but busy draining a heavy backlog" (keep waiting).
    """
    import shutil
    import subprocess

    # launchctl: third column is the PID; a dash means not running.
    if shutil.which("launchctl"):
        try:
            r = subprocess.run(
                ["launchctl", "list"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            for line in (r.stdout + r.stderr).splitlines():
                if PLIST_NAME in line:
                    pid_field = line.split()[0]
                    if pid_field not in ("-", "0"):
                        return True
        except Exception:
            pass

    # Process scan fallback (covers non-launchd / manual invocations).
    # Tightened match: only match actual worker invocations (--drain, --once,
    # --interval), not editors or import-only processes.
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
    """Attempt to start the background_worker daemon. Returns True if a
    launchd load OR a spawn succeeded. Failure is non-fatal."""
    import shutil
    import subprocess

    plist = Path.home() / "Library" / "LaunchAgents" / PLIST_NAME
    if plist.exists() and shutil.which("launchctl"):
        try:
            # load is idempotent enough; on "already loaded" it's fine.
            r = subprocess.run(
                ["launchctl", "load", str(plist)],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if r.returncode == 0:
                _log_structured(logging.INFO, "worker_recovered", method="launchctl")
                return True
            # Already loaded → treat as started.
            if "already loaded" in (r.stderr + r.stdout).lower():
                return True
        except Exception as e:  # pragma: no cover
            _log_structured(logging.WARNING, "worker_recovery_failed", method="launchctl", error=str(e))

    # Fallback: spawn the worker directly (--once to drain, then exit).
    # This unblocks the current backlog even without launchd.
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
    try:
        t0 = time.time()

        task_id = _enqueue_sentinel(conn)
        _log_structured(logging.INFO, "sentinel_enqueued", task_id=task_id)

        failures = _count_failures(conn, hours=24)
        pending = _pending_depth(conn)
        journal_pending = _journal_pending_depth(db_path)

        # Report depth probes BEFORE the sentinel poll so they are always
        # emitted even when the worker is down (poll would raise/timeout).
        # The journal_pending line is the CQRS-backlog signal: a growing
        # count means the background_worker daemon isn't draining journal.db.
        # Keep the human-readable stdout line (consumed by tests/operators)
        # alongside the structured event.
        print(f"journal_pending: {journal_pending}", flush=True)
        _log_structured(logging.INFO, "pipeline_status", failed_last_24h=failures, pending_queue_depth=pending, journal_pending=journal_pending)

        if journal_pending is not None and journal_pending > JOURNAL_PENDING_WARN:
            _log_structured(logging.WARNING, "journal_backlog", journal_pending=journal_pending, threshold=JOURNAL_PENDING_WARN)

        # Self-heal: if the queue already shows a backlog, try to start the
        # worker before the (likely-failing) sentinel poll, so recovery runs
        # even if the poll times out below.
        if pending > 0 or not _worker_alive():
            if _try_start_worker():
                from infra.alert import alert

                alert(
                    "warning",
                    "Background worker restarted",
                    "Pipeline health check initiated worker restart",
                )
                # Give the spawned worker time to start and pick up the sentinel
                time.sleep(3)

        try:
            _poll_sentinel(conn, task_id, timeout_s=30.0)
        except Exception:
            # Sentinel didn't complete — worker likely down. Make one
            # final recovery attempt, then report the failure (don't mask it).
            if _try_start_worker():
                from infra.alert import alert

                alert(
                    "warning",
                    "Background worker restarted",
                    "Pipeline health check initiated worker restart",
                )
            raise

        elapsed = time.time() - t0
        _log_structured(logging.INFO, "sentinel_completed", elapsed_s=round(elapsed, 2), elapsed_ms=round(elapsed * 1000.0, 2))

        # A slow-but-successful sentinel (worker was draining a heavy
        # backlog) is HEALTHY, not a failure — don't fail on latency.
        # Only warn so pipeline-coverage can surface the backlog signal.
        if elapsed > 60.0:
            _log_structured(logging.WARNING, "sentinel_slow", elapsed_s=round(elapsed, 1))

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
