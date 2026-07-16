"""Tests for graceful worker shutdown (SIGTERM/SIGINT handlers).

Step 3 of the Cron Pipeline Maturity plan:
  background/background_worker.py — _handle_signal, _shutdown_force_exit,
  _cleanup_task_artifacts, and the _SHUTDOWN_GRACE_S constant.
"""

import os
import signal
import threading
import time
from pathlib import Path

import pytest

import background.background_worker as _bw

from background.background_worker import (
    _SHUTDOWN_GRACE_S,
    _cleanup_task_artifacts,
    _handle_signal,
    _shutdown_force_exit,
)


def test_shutdown_grace_s_default():
    assert _SHUTDOWN_GRACE_S == 10


def test_shutdown_grace_s_from_env(monkeypatch):
    monkeypatch.setenv("MEMORY_WORKER_SHUTDOWN_GRACE_S", "30")
    import importlib
    import background.background_worker as bw
    importlib.reload(bw)
    assert bw._SHUTDOWN_GRACE_S == 30


def test_handle_signal_sets_shutdown():
    _shutdown_orig = _bw._shutdown
    try:
        _handle_signal(signal.SIGTERM, None)
        assert _bw._shutdown is True
    finally:
        _bw._shutdown = _shutdown_orig
        _bw._RECONCILER_SHUTDOWN.clear()


def test_handle_signal_idempotent():
    _shutdown_orig = _bw._shutdown
    try:
        _handle_signal(signal.SIGTERM, None)
        first = _bw._shutdown
        _handle_signal(signal.SIGTERM, None)
        assert first is True
        assert _bw._shutdown is True
    finally:
        _bw._shutdown = _shutdown_orig
        _bw._RECONCILER_SHUTDOWN.clear()


def test_shutdown_force_exit_does_not_fire_before_grace(tmp_path):
    _shutdown_orig = _bw._shutdown
    try:
        _bw._shutdown = True
        t0 = time.time()
        thread = threading.Thread(target=_shutdown_force_exit, daemon=True)
        thread.start()
        thread.join(timeout=3)
        elapsed = time.time() - t0
        assert elapsed < _SHUTDOWN_GRACE_S + 2
    finally:
        _bw._shutdown = _shutdown_orig
        _bw._RECONCILER_SHUTDOWN.clear()


def test_cleanup_task_artifacts_removes_temp_dir(tmp_path):
    temp_dir = tmp_path / "subproc_work"
    temp_dir.mkdir(parents=True)
    (temp_dir / "output.json").write_text("{}")
    _cleanup_task_artifacts("run_script", {"temp_dir": str(temp_dir)})
    assert not temp_dir.exists()


def test_cleanup_task_artifacts_handles_missing_dir():
    _cleanup_task_artifacts("run_script", {"temp_dir": "/nonexistent/12345"})


def test_cleanup_task_artifacts_handles_empty_payload():
    _cleanup_task_artifacts("test_type", {})


def test_cleanup_task_artifacts_uses_working_dir_fallback(tmp_path):
    temp_dir = tmp_path / "working"
    temp_dir.mkdir(parents=True)
    (temp_dir / "data.bin").write_text("x")
    _cleanup_task_artifacts("test_type", {"working_dir": str(temp_dir)})
    assert not temp_dir.exists()
