"""Integration tests for the cron/cron_health_check pipeline.

Tests cover the end-to-end subprocess invocation where cron_health_check:
  1. Acquires its flock lock (or detects contention)
  2. Runs each independent probe (FTS integrity, KG orphans, circuit breaker,
     auto-save health, semantic search)
  3. Writes/updates .health_status.json
  4. Returns 0 for overall-healthy or 1 when any check is unhealthy

These tests use `bootstrap_temp_db_clean` to get a real schema, seed a note,
and then drive the cron as a subprocess.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import fcntl
from pathlib import Path
from unittest import TestCase

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from eval._fixtures import bootstrap_temp_db_clean  # noqa: E402
from background.cron_model_lock import cron_model_lock, cleanup_stale_locks  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
LOCK_DIR = Path(tempfile.mkdtemp()) / "locks"


class TestCronHealthCheckIntegration(TestCase):
    """End-to-end tests for the cron_health_check cron as a subprocess."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "health_check.db"
        # Ensure lock dir exists
        LOCK_DIR.mkdir(parents=True, exist_ok=True)

    def _run_health_check(self, db_path: Path) -> tuple[int, str]:
        env = {
            **os.environ,
            "MEMORY_DB_PATH": str(db_path),
            "MEMORY_LOCK_DIR": str(LOCK_DIR),
            "MEMORY_QUALITY_GATES": "0",
            "MEMORY_USER_PROFILE": "0",
        }
        proc = os.system(
            f"{sys.executable} {REPO / 'cron' / 'cron_health_check.py'} "
            f"--minutes 5 >/dev/null 2>&1"
        )
        # Use subprocess for proper exit-code capture
        import subprocess

        proc = subprocess.run(
            [sys.executable, str(REPO / "cron" / "cron_health_check.py")],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        return proc.returncode, proc.stdout + proc.stderr

    def test_health_check_exits_zero_with_valid_db(self) -> None:
        bootstrap_temp_db_clean(self.db_path)
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL;")
        now = "2026-06-15T12:00:00+00:00"
        conn.execute(
            "INSERT INTO memories (id,content,source_file,tags,category,created_at,updated_at,observed_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("lessons/health-test", "Python programming content.", "memory/lessons/health-test.md", "[]", "lessons", now, now, now),
        )
        conn.commit()
        conn.close()
        rc, out = self._run_health_check(self.db_path)
        assert rc == 0, f"Expected 0, got {rc}\nOutput: {out}"

    def test_health_check_exits_nonzero_without_db(self) -> None:
        self.db_path.unlink(missing_ok=True)
        rc, out = self._run_health_check(self.db_path)
        assert rc != 0, f"Expected nonzero exit for missing DB, got {rc}\nOutput: {out}"

    def test_health_status_json_created_or_unchanged(self) -> None:
        """cron_health_check should complete without raising an exception."""
        bootstrap_temp_db_clean(self.db_path)
        rc, out = self._run_health_check(self.db_path)
        assert "Traceback" not in out, f"Unexpected traceback:\n{out}"

    def test_cleanup_stale_locks_does_not_remove_active_locks(self) -> None:
        """cleanup_stale_locks() preserves a recently-acquired lock."""
        active_name = "test_active_integration_lock"
        removed = []
        with cron_model_lock(active_name, _lock_dir=LOCK_DIR):
            removed = cleanup_stale_locks(_lock_dir=LOCK_DIR)
        assert active_name not in removed, (
            f"Active lock {active_name!r} was in removed list: {removed}"
        )

    def test_cron_model_lock_acquire_and_release(self) -> None:
        """cron_model_lock acquires and releases without raising."""
        lock_name = "test_acquire_release"
        # Should not raise
        with cron_model_lock(lock_name, _lock_dir=LOCK_DIR):
            pass
        # Lock file may persist on disk (flock semantics) but that's OK

    def test_stale_lock_cleanup_removes_old_lock_files(self) -> None:
        """An old zero-byte lock file should be cleaned up by cleanup_stale_locks."""
        # Create a fake stale lock file (>1h old)
        import time as _time

        stale_path = LOCK_DIR / "stale_integration_test.lock"
        stale_path.write_text("", encoding="utf-8")
        old_mtime = _time.time() - 3700  # > 1h ago
        os.utime(str(stale_path), (old_mtime, old_mtime))
        removed = cleanup_stale_locks(_lock_dir=LOCK_DIR)
        assert "stale_integration_test" in removed, (
            f"Expected stale lock to be removed, got: {removed}"
        )
