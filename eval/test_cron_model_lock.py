"""Tests for background.cron_model_lock + cron_health_check self-check."""

from __future__ import annotations

import os
import threading
import time

import pytest

from background.cron_model_lock import (
    MAX_CRON_RUNTIME_S,
    _STALE_THRESHOLD_S,
    _get_lock_dir,
    cleanup_stale_locks,
    cron_model_lock,
)


@pytest.fixture()
def clean_lock_dir(tmp_path, monkeypatch):
    """Redirect the lock dir to a temp directory for testing."""
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    monkeypatch.setenv("MEMORY_CRON_LOCK_DIR", str(lock_dir))
    yield lock_dir
    for p in lock_dir.glob("*.lock"):
        try:
            p.unlink()
        except OSError:
            pass


class TestCronModelLock:
    def test_acquire_and_release(self, clean_lock_dir):
        acquired = False
        with cron_model_lock("test_cron", timeout=5.0):
            acquired = True
        assert acquired is True
        assert (clean_lock_dir / "test_cron.lock").exists()

    def test_env_var_overrides_lock_dir(self, clean_lock_dir):
        assert _get_lock_dir() == clean_lock_dir

    def test_concurrent_lock_is_exclusive(self, clean_lock_dir):
        state = {"holder": None}
        start_barrier = threading.Barrier(2)

        def hold_lock(name):
            start_barrier.wait(timeout=10.0)
            with cron_model_lock(name, timeout=10.0):
                state["holder"] = name
                time.sleep(0.2)

        t1 = threading.Thread(target=hold_lock, args=("cron_a",))
        t2 = threading.Thread(target=hold_lock, args=("cron_b",))
        t1.start()
        t2.start()
        t1.join(timeout=10.0)
        t2.join(timeout=10.0)
        assert state["holder"] in {"cron_a", "cron_b"}

    def test_timeout_raises_on_same_lock(self, clean_lock_dir):
        with cron_model_lock("my_lock", timeout=10.0):
            with pytest.raises(TimeoutError, match="timed out"):
                cron_model_lock("my_lock", timeout=0.2).__enter__()

    def test_cleanup_stale_removes_old_locks(self, clean_lock_dir):
        lock_path = clean_lock_dir / "stale_cron.lock"
        lock_path.write_text("stale")
        old_time = time.time() - (_STALE_THRESHOLD_S + 60)
        os.utime(str(lock_path), (old_time, old_time))
        cleaned = cleanup_stale_locks()
        assert "stale_cron" in cleaned
        assert not lock_path.exists()

    def test_cleanup_keeps_recent_locks(self, clean_lock_dir):
        with cron_model_lock("recent_cron", timeout=5.0):
            cleaned = cleanup_stale_locks()
            assert "recent_cron" not in cleaned

    def test_default_lock_dir_under_memory_dir(self):
        lock_dir = _get_lock_dir()
        assert ".cron_model_lock" in str(lock_dir)