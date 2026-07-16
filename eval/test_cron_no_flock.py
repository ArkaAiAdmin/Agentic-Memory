"""Tests for Step 8 — lock-free mode (feat/cron-pipeline-no-flock).

The pipeline-coverage health check (cron_pipeline_health.py) makes
overlapping scheduler/worker runs observable + recoverable, so the
process-singleton flocks can be skipped via MEMORY_CRON_NO_FLOCK=1
(or --no-flock for the scheduler). Flock stays the default.

Strategy: a flock lock is released when the holder process exits, so we
cannot inspect the lock file after the run. Instead we spawn a *holder*
that acquires the lock and keeps it alive, then assert a concurrent
scheduler/worker invocation is "skipped" under the default (flock on) and
runs freely under MEMORY_CRON_NO_FLOCK=1 / --no-flock.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SCHED_LOCK = "cron_pipeline_scheduler"
WORKER_LOCK = "background_worker"


def _holder_script(lock_name: str, hold_s: float) -> str:
    """Python source that acquires the named flock and holds it alive."""
    return (
        "import sys, time\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from cron._flock import acquire_lock_or_exit\n"
        "acquire_lock_or_exit(sys.argv[2])\n"
        "time.sleep(float(sys.argv[3]))\n"
    )


def _spawn_holder(lock_name: str, hold_s: float = 6.0):
    holder = _holder_script(lock_name, hold_s)
    p = subprocess.Popen(
        [sys.executable, "-c", holder, REPO_ROOT, lock_name, str(hold_s)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(REPO_ROOT),
    )
    # Give the holder time to acquire the lock before the contender starts.
    time.sleep(1.0)
    return p


def _run_scheduler(args, env_extra=None, timeout=20.0):
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "cron/scheduler.py", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(REPO_ROOT),
        env=env,
    )


def _run_worker(args, env_extra=None, timeout=30.0):
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "background/background_worker.py", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(REPO_ROOT),
        env=env,
    )


def _skipped(out: str) -> bool:
    return "skipped" in out.lower()


# --------------------------------------------------------------------------
# Scheduler
# --------------------------------------------------------------------------

def test_scheduler_default_respects_external_lock() -> None:
    """With a held scheduler lock, a default scheduler run is skipped."""
    holder = _spawn_holder(SCHED_LOCK)
    try:
        res = _run_scheduler(["--list"])
        assert res.returncode == 0
        assert _skipped(res.stdout + res.stderr), (
            f"expected scheduler to skip while lock held: {res.stdout}{res.stderr}"
        )
    finally:
        holder.kill()
        holder.wait()


def test_scheduler_no_flock_flag_ignores_external_lock() -> None:
    """With --no-flock, the scheduler runs even while the lock is held."""
    holder = _spawn_holder(SCHED_LOCK)
    try:
        res = _run_scheduler(["--no-flock", "--list"])
        assert res.returncode == 0
        assert not _skipped(res.stdout + res.stderr), (
            f"--no-flock must ignore the held lock: {res.stdout}{res.stderr}"
        )
    finally:
        holder.kill()
        holder.wait()


def test_scheduler_env_no_flock_ignores_external_lock() -> None:
    """With MEMORY_CRON_NO_FLOCK=1, the scheduler runs despite a held lock."""
    holder = _spawn_holder(SCHED_LOCK)
    try:
        res = _run_scheduler(["--list"], {"MEMORY_CRON_NO_FLOCK": "1"})
        assert res.returncode == 0
        assert not _skipped(res.stdout + res.stderr), (
            f"MEMORY_CRON_NO_FLOCK=1 must ignore the held lock: "
            f"{res.stdout}{res.stderr}"
        )
    finally:
        holder.kill()
        holder.wait()


def test_scheduler_env_no_flock_disabled_keeps_lock() -> None:
    """MEMORY_CRON_NO_FLOCK=0 keeps the default lock behaviour."""
    holder = _spawn_holder(SCHED_LOCK)
    try:
        res = _run_scheduler(["--list"], {"MEMORY_CRON_NO_FLOCK": "0"})
        assert res.returncode == 0
        assert _skipped(res.stdout + res.stderr), (
            f"MEMORY_CRON_NO_FLOCK=0 must keep the lock: {res.stdout}{res.stderr}"
        )
    finally:
        holder.kill()
        holder.wait()


# --------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------

def test_worker_default_respects_external_lock() -> None:
    """With a held worker lock, a default worker run is skipped."""
    holder = _spawn_holder(WORKER_LOCK)
    try:
        res = _run_worker(["--once", "--type", "cron_pipeline_sentinel"])
        assert _skipped(res.stdout + res.stderr), (
            f"expected worker to skip while lock held: rc={res.returncode} "
            f"{res.stdout}{res.stderr}"
        )
    finally:
        holder.kill()
        holder.wait()


def test_worker_env_no_flock_ignores_external_lock() -> None:
    """With MEMORY_CRON_NO_FLOCK=1, the worker runs despite a held lock."""
    holder = _spawn_holder(WORKER_LOCK)
    try:
        res = _run_worker(
            ["--once", "--type", "cron_pipeline_sentinel"],
            {"MEMORY_CRON_NO_FLOCK": "1"},
        )
        assert not _skipped(res.stdout + res.stderr), (
            f"MEMORY_CRON_NO_FLOCK=1 must ignore the held lock: "
            f"rc={res.returncode} {res.stdout}{res.stderr}"
        )
    finally:
        holder.kill()
        holder.wait()
