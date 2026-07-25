#!/usr/bin/env python3
"""Consolidated cron scheduler — replaces 39 crontab entries with 1.

Runs every 5 minutes via a single crontab entry. Determines which jobs
are due based on their frequency tier and current time, then runs them
sequentially as subprocesses.

Usage:
    python cron/scheduler.py              # run due jobs
    python cron/scheduler.py --dry-run    # show what would run
    python cron/scheduler.py --list       # list all jobs and next run times
    python cron/scheduler.py --status     # show recent cron_runs summary
    python cron/scheduler.py --no-flock   # skip process-singleton lock
                                            (observability via pipeline-coverage)
    MEMORY_CRON_NO_FLOCK=1 python cron/scheduler.py  # same, via env
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Ensure repo root is on sys.path
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

from cron.jobs import JOBS, REPO_ROOT as JOB_ROOT

# Lock to prevent overlapping scheduler instances
try:
    from cron._flock import acquire_lock_or_exit as _sched_lock
except ImportError:
    _sched_lock = None  # flock module not available; continue without lock


# ---------------------------------------------------------------------------
# Frequency matching
# ---------------------------------------------------------------------------

def _is_due(job: dict, now: datetime) -> bool:
    """Check if a job is due to run at the given time."""
    freq = job.get("freq", "5m")
    offset_min = job.get("offset_min", 0)
    current_minute = now.hour * 60 + now.minute

    if freq == "5m":
        # Run every 5 minutes, offset by offset_min
        return current_minute % 5 == (offset_min % 5)

    elif freq == "15m":
        # Run every 15 minutes
        return current_minute % 15 == (offset_min % 15)

    elif freq == "30m":
        return current_minute % 30 == (offset_min % 30)

    elif freq == "1h":
        # Run once per hour at the specified minute offset
        return now.minute == (offset_min % 60)

    elif freq == "6h":
        # Run every 6 hours at the specified minute
        return now.minute == (offset_min % 60) and now.hour % 6 == 0

    elif freq == "1d":
        # Run once per day at the specified minute offset (from midnight)
        target_minute = offset_min % 1440
        return current_minute == target_minute

    elif freq == "1w":
        # Run once per week on the specified day-of-week
        dow = job.get("dow", 0)  # 0=Sunday
        if now.weekday() != dow:
            return False
        target_minute = offset_min % 1440
        return current_minute == target_minute

    elif freq == "1m":
        # Run once per month on the specified day-of-month
        dom = job.get("dom", 1)
        if now.day != dom:
            return False
        target_minute = offset_min % 1440
        return current_minute == target_minute

    return False


def get_due_jobs(now: datetime | None = None) -> list[str]:
    """Return list of job names that are due to run at the given time."""
    if now is None:
        now = datetime.now(timezone.utc)
    return [name for name, job in JOBS.items() if _is_due(job, now)]


# ---------------------------------------------------------------------------
# Job execution
# ---------------------------------------------------------------------------

def _run_job(name: str, job: dict, dry_run: bool = False) -> dict:
    """Run a single job as a subprocess. Returns execution result."""
    script = job.get("script", "")
    args = job.get("args", [])
    env_extra = job.get("env", {})
    timeout = job.get("timeout", 300)

    # Support "-m" module invocation (e.g. script="-m", args=["background.journal_reconciler", ...])
    if script == "-m":
        cmd = [sys.executable, "-m"] + args
    else:
        script_path = REPO_ROOT / script
        if not script_path.exists():
            return {"status": "failed", "error": f"Script not found: {script}"}
        cmd = [sys.executable, str(script_path)] + args

    if dry_run:
        return {"status": "dry_run", "command": " ".join(cmd)}

    # Build environment
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    for k, v in env_extra.items():
        env[k] = v

    # Record start
    try:
        from cron.cron_runs import record_start, record_complete

        row_id = record_start(name)
    except Exception:
        row_id = 0

    t0 = time.time()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=str(REPO_ROOT),
        )
        duration_ms = int((time.time() - t0) * 1000)
        output = (result.stdout + result.stderr)[-500:] if result.stdout or result.stderr else ""

        if result.returncode == 0:
            status = "completed"
            error = None
        else:
            status = "failed"
            error = f"exit code {result.returncode}: {output[-200:]}"

        try:
            record_complete(
                row_id,
                status=status,
                duration_ms=duration_ms,
                error=error,
                output=output,
            )
        except Exception:
            pass

        return {
            "status": status,
            "duration_ms": duration_ms,
            "returncode": result.returncode,
            "output": output,
        }

    except subprocess.TimeoutExpired:
        duration_ms = int((time.time() - t0) * 1000)
        try:
            record_complete(
                row_id,
                status="failed",
                duration_ms=duration_ms,
                error=f"timeout after {timeout}s",
            )
        except Exception:
            pass
        return {"status": "failed", "error": f"timeout after {timeout}s"}

    except Exception as e:
        duration_ms = int((time.time() - t0) * 1000)
        try:
            record_complete(
                row_id,
                status="failed",
                duration_ms=duration_ms,
                error=str(e),
            )
        except Exception:
            pass
        return {"status": "failed", "error": str(e)}


# ---------------------------------------------------------------------------
# Status output
# ---------------------------------------------------------------------------

def _write_status(due_jobs: list[str], results: dict[str, dict]) -> None:
    """Write consolidated status to .cron_status.json."""
    status_path = Path(JOB_ROOT) / "memory" / ".cron_status.json"
    try:
        from cron.cron_runs import query_recent

        recent = query_recent(hours=24)
    except Exception:
        recent = {}

    status = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "scheduler_run": {
            "due_jobs": due_jobs,
            "results": {k: {"status": v.get("status"), "duration_ms": v.get("duration_ms")} for k, v in results.items()},
        },
        "recent_24h": recent,
    }

    try:
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(json.dumps(status, indent=2, default=str))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    args = sys.argv[1:]
    no_flock = "--no-flock" in args
    dry_run = "--dry-run" in args
    list_mode = "--list" in args
    status_mode = "--status" in args

    # Configure logging so --list / --status / dry-run output is visible.
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        stream=sys.stderr,
    )

    # Step 8 (cron-pipeline-no-flock): when the process-singleton lock is
    # skipped, overlapping scheduler instances are observable + recoverable
    # via the pipeline-coverage health check (cron_pipeline_health.py) instead
    # of being hard-gated by flock. Default behaviour keeps the lock.
    # Read-only modes (--list, --status) skip the lock since they don't mutate.
    _skip_sched_lock = no_flock or list_mode or status_mode or os.environ.get("MEMORY_CRON_NO_FLOCK", "") == "1"
    if _sched_lock is not None and not _skip_sched_lock:
        lock_wait_s = int(os.environ.get("MEMORY_SCHEDULER_LOCK_WAIT_S", "0"))
        if lock_wait_s > 0:
            _sched_lock(
                "cron_pipeline_scheduler",
                max_attempts=max(5, lock_wait_s + 5),
            )
        else:
            _sched_lock("cron_pipeline_scheduler")

    now = datetime.now(timezone.utc)

    if list_mode:
        logger.info(f"{'Job':<30} {'Freq':<8} {'Next offset':<12} {'Due now':<8}")
        logger.info("-" * 60)
        for name, job in sorted(JOBS.items()):
            freq = job.get("freq", "?")
            offset = job.get("offset_min", 0)
            due = _is_due(job, now)
            logger.info(f"{name:<30} {freq:<8} +{offset:<10}m {'YES' if due else '':<8}")
        return 0

    if status_mode:
        try:
            from cron.cron_runs import query_recent

            recent = query_recent(hours=24)
            logger.info("Last 24h: %d runs, %d ok, %d failed", recent['total_runs'], recent['successful'], recent['failed'])
            if recent.get("last_failure"):
                lf = recent["last_failure"]
                logger.info("Last failure: %s at %s", lf.get('job'), lf.get('at'))
            for name, info in recent.get("jobs", {}).items():
                logger.info("  %s: %d runs, %d failed, last=%s", name, info['runs'], info['failed'], info.get('last_run', '?'))
        except Exception as e:
            logger.error("Error reading cron_runs: %s", e)
        return 0

    due_jobs = get_due_jobs(now)

    if not due_jobs:
        if not dry_run:
            return 0
        logger.info("No jobs due at %s", now.isoformat())
        return 0

    if dry_run:
        logger.info("Jobs due at %s:", now.isoformat())
        for name in due_jobs:
            job = JOBS[name]
            logger.info("  %s: %s %s", name, job.get('script', '?'), ' '.join(job.get('args', [])))
        return 0

    results: dict[str, dict] = {}
    for name in due_jobs:
        job = JOBS[name]
        logger.info("[scheduler] running %s...", name)
        result = _run_job(name, job)
        results[name] = result
        status = result.get("status", "unknown")
        dur = result.get("duration_ms", 0)
        logger.info("[scheduler] %s: %s (%dms)", name, status, dur)

    _write_status(due_jobs, results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
