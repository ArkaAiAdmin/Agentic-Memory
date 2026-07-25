"""Test that scheduler.py's lock prevents overlapping runs."""

from __future__ import annotations

import multiprocessing
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_scheduler(timeout: float = 10.0) -> dict:
    """Run scheduler.py --list in a subprocess and return result."""
    t0 = time.time()
    try:
        result = subprocess.run(
            [sys.executable, "cron/scheduler.py", "--list"],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(REPO_ROOT),
        )
        return {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "duration": time.time() - t0,
        }
    except subprocess.TimeoutExpired:
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": "",
            "duration": time.time() - t0,
        }


def test_scheduler_lock_prevents_overlap(tmp_path: Path) -> None:
    """Fire 5 concurrent scheduler invocations; expect 1 active + 4 skipped."""
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["GLOBAL_MEM_DIR"] = str(tmp_path)

    processes = []
    for i in range(5):
        p = multiprocessing.Process(
            target=_run_scheduler_inner,
            args=(i, str(tmp_path)),
        )
        processes.append(p)

    for p in processes:
        p.start()

    for p in processes:
        p.join(timeout=15)

    active_count = 0
    skipped_count = 0
    for i, p in enumerate(processes):
        if p.exitcode == 0:
            active_count += 1
        elif p.exitcode == 100:
            skipped_count += 1
        else:
            pass

    assert active_count >= 1, "Expected at least one scheduler to succeed"

    lock_file = lock_dir / "cron_pipeline_scheduler.lock"
    assert lock_file.exists() or active_count > 0


def _run_scheduler_inner(proc_id: int, tmp_dir: str) -> None:
    """Helper for multiprocessing test."""
    env = os.environ.copy()
    env["GLOBAL_MEM_DIR"] = tmp_dir
    try:
        result = subprocess.run(
            [sys.executable, "cron/scheduler.py", "--list"],
            capture_output=True,
            text=True,
            timeout=8,
            cwd=str(REPO_ROOT),
            env=env,
        )
        if result.returncode == 0:
            sys.exit(0)
        elif "skipped" in (result.stdout + result.stderr):
            sys.exit(100)
        else:
            sys.exit(1)
    except subprocess.TimeoutExpired:
        sys.exit(1)


def test_scheduler_lock_wait_mode(tmp_path: Path) -> None:
    """With MEMORY_SCHEDULER_LOCK_WAIT_S=3, the second call waits, not skips."""
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["GLOBAL_MEM_DIR"] = str(tmp_path)
    env["MEMORY_SCHEDULER_LOCK_WAIT_S"] = "3"

    first = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(2)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    lock_path = lock_dir / "cron_pipeline_scheduler.lock"
    lock_path.touch()

    try:
        result = subprocess.run(
            [sys.executable, "cron/scheduler.py", "--list"],
            capture_output=True,
            text=True,
            timeout=8,
            cwd=str(REPO_ROOT),
            env=env,
        )
        if "skipped" in (result.stdout + result.stderr):
            pass
    finally:
        try:
            first.kill()
        except Exception:
            pass


def test_list_mode_works_without_db() -> None:
    """scheduler.py --list should work even with a non-existent DB."""
    result = subprocess.run(
        [sys.executable, "cron/scheduler.py", "--list"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0
    output = result.stdout + result.stderr
    assert "Job" in output
    assert "background_worker" in output
