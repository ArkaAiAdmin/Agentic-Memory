#!/usr/bin/env python3
"""Cron daemon watchdog — restart the auto-save daemon if it is dead.

The auto-save daemon (background/auto_save.py daemon) drains the async
inbox. If it crashes or is killed, new tool-complete events queue up in
the inbox but are never persisted. This watchdog:

  1. Checks the daemon PID file via inbox._is_daemon_running().
  2. If the daemon is not running, starts a new one as a background
     subprocess (no shell, no venv activation needed because this
     script runs inside the venv).
  3. Logs to memory/watchdog-daemon.log.

Usage (in crontab, every 5 minutes):
  */5 * * * * $VENV_PY $ROOT/cron/cron_daemon_watchdog.py >> $LOG_DIR/watchdog-daemon.log 2>&1
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import cast

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from infra.infrastructure import resolve_active_memory_dir
from infra.log import setup_logging
logger = setup_logging("cron_daemon_watchdog")
LOG_FILE = resolve_active_memory_dir() / "watchdog-daemon.log"


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} cron_daemon_watchdog: {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def is_daemon_running() -> bool:
    sys.path.insert(0, str(REPO / "background"))
    from inbox import _is_daemon_running, _is_daemon_lock_held

    # Use flock as the primary liveness signal. The PID file has a race
    # window: between writing the PID and the daemon actually starting,
    # the watchdog can see a stale or missing PID and spawn a duplicate.
    # The flock is held immediately at daemon startup, so it is the
    # authoritative signal. Only fall back to PID check if the flock
    # file doesn't exist yet (no daemon ever started).
    if _is_daemon_lock_held():
        return True
    return cast(bool, _is_daemon_running())


def start_daemon() -> bool:
    daemon_script = REPO / "background" / "auto_save.py"
    python_exe = sys.executable
    try:
        subprocess.Popen(
            [python_exe, str(daemon_script), "daemon"],
            cwd=str(REPO),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
        log("started auto-save daemon")
        return True
    except OSError as e:
        log(f"failed to start daemon: {e}")
        return False


def main() -> int:
    if not is_daemon_running():
        log("daemon is NOT running — restarting")
        if not start_daemon():
            return 1
    return 0


if __name__ == "__main__":
    setup_logging("cron_daemon_watchdog", level="INFO", fmt="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        from _flock import acquire_lock_or_exit

        acquire_lock_or_exit("cron_daemon_watchdog")
    except ImportError:
        pass
    sys.exit(main())
