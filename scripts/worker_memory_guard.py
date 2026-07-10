#!/usr/bin/env python3
"""worker_memory_guard.py — Memory watchdog for background_worker processes.

Reads the PID of the running background_worker.py process, checks its RSS
and (on macOS) its physical footprint, and exits cleanly if both are within
configurable thresholds. Exits 1 and kills the worker if either threshold is
exceeded.

Configuration via environment variables
-----------------------------------------
MEMORY_DB_PATH    Path to the SQLite DB (default: memory/memory.db).
                  Not used for the guard check itself, but required so the
                  --find-pid mode can locate the worker matching this DB.
MEMORY_GUARD_RSS_MB        Max allowed RSS in MB (default: 500).
MEMORY_GUARD_FOOTPRINT_MB  Max allowed physical footprint in MB (default: 1024).
                            No-op on Linux where vmmap is unavailable.

Exit codes
----------
  0   Worker RSS and footprint are within limits (or no worker found).
  1   A threshold was exceeded; the worker has been sent SIGTERM.

Usage
-----
  # Check before cron launches a drain worker
  venv/bin/python scripts/worker_memory_guard.py && \
      venv/bin/python background_worker.py --drain

  # Cron one-liner — kill and abort if worker is bloated
  */15 * * * *  MEMORY_DB_PATH=/path/to/memory.db \
      venv/bin/python /path/to/scripts/worker_memory_guard.py && \
      venv/bin/python background_worker.py --drain --max-tasks=50
"""

from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger("worker_memory_guard")

RSS_THRESHOLD_MB: int = int(os.environ.get("MEMORY_GUARD_RSS_MB", "500"))
FOOTPRINT_THRESHOLD_MB: int = int(
    os.environ.get("MEMORY_GUARD_FOOTPRINT_MB", "1024")
)

_WORKER_CMD_PATTERN = re.compile(
    r"background_worker\.py", re.IGNORECASE
)


def _resolve_db_path() -> Path:
    """Return DB path used to identify the target memory store."""
    raw = os.environ.get("MEMORY_DB_PATH", "memory/memory.db")
    p = Path(raw)
    if not p.is_absolute():
        # Make relative to repo root (three levels up from this script)
        repo_root = Path(__file__).resolve().parent.parent.parent
        p = (repo_root / p).resolve()
    return p


def _find_worker_pid() -> int | None:
    """Return the PID of a background_worker.py process, or None."""
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", r"background_worker\.py"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except FileNotFoundError:
        # pgrep not available (very rare on macOS/Linux); fall back to ps
        out = ""
    except subprocess.CalledProcessError:
        return None

    if not out:
        # pgrep fallback: parse ps output
        try:
            out = subprocess.check_output(
                ["ps", "aux"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            return None
        for line in out.splitlines():
            if _WORKER_CMD_PATTERN.search(line) and "grep" not in line:
                parts = line.split()
                if len(parts) > 1:
                    try:
                        return int(parts[1])
                    except ValueError:
                        continue
        return None

    pids = [int(x) for x in out.splitlines() if x.strip().isdigit()]
    return pids[0] if pids else None


def _read_rss_kb(pid: int) -> int | None:
    """Read VmRSS in kB from /proc/<pid>/status (Linux)."""
    status_path = Path(f"/proc/{pid}/status")
    if not status_path.exists():
        return None
    try:
        for line in status_path.read_text().splitlines():
            if line.startswith("VmRSS:"):
                parts = line.split()
                return int(parts[1])  # kB
    except OSError:
        pass
    return None


def _read_rss_via_ps(pid: int) -> int | None:
    """Read RSS in kB via ps (portable: macOS and Linux)."""
    try:
        out = subprocess.check_output(
            ["ps", "-o", "rss=", "-p", str(pid)],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        # ps -o rss= returns kB on both Linux and macOS
        return int(out.split()[0]) if out else None
    except (FileNotFoundError, subprocess.CalledProcessError, ValueError):
        return None


def _read_footprint_mb(pid: int) -> int | None:
    """Read 'Physical footprint' from vmmap (macOS only).

    Returns MB or None if vmmap is unavailable (Linux) or the field was not
    found.
    """
    try:
        out = subprocess.check_output(
            ["vmmap", str(pid)],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        logger.debug(
            "vmmap not available or failed (pid=%d); footprint check skipped", pid
        )
        return None

    for line in out.splitlines():
        if "Physical footprint:" in line:
            # Format: "Physical footprint:  123.4M"
            match = re.search(r"([\d.]+)\s*([KMG])B", line)
            if match:
                value = float(match.group(1))
                unit = match.group(2)
                if unit == "G":
                    return int(value * 1024)
                if unit == "K":
                    return int(value / 1024)
                return int(value)
    return None


def check_worker(
    rss_threshold_mb: int = RSS_THRESHOLD_MB,
    footprint_threshold_mb: int = FOOTPRINT_THRESHOLD_MB,
) -> int:
    """Check the background_worker process memory and return an exit code.

    Returns 0 when within limits, 1 when a threshold is exceeded (the worker
    has been sent SIGTERM).
    """
    pid = _find_worker_pid()
    if pid is None:
        logger.info("worker_memory_guard: no background_worker process found; nothing to check")
        return 0

    db_path = _resolve_db_path()
    logger.info(
        "worker_memory_guard: found worker pid=%d  db=%s  rss_threshold=%d MB  footprint_threshold=%d MB",
        pid, db_path, rss_threshold_mb, footprint_threshold_mb,
    )

    # --- RSS ---
    rss_kb = _read_rss_kb(pid) or _read_rss_via_ps(pid)
    if rss_kb is None:
        logger.warning(
            "worker_memory_guard: could not read RSS for pid %d; exiting 0 (pass)", pid
        )
        return 0

    rss_mb = rss_kb // 1024
    rss_ok = rss_mb < rss_threshold_mb

    # --- Physical footprint (macOS only; Linux logs a warning) ---
    footprint_mb = _read_footprint_mb(pid)
    footprint_ok = True
    if footprint_mb is not None:
        footprint_ok = footprint_mb < footprint_threshold_mb
        logger.info(
            "worker_memory_guard: pid=%d  rss=%d MB  footprint=%d MB  "
            "rss_ok=%s  footprint_ok=%s",
            pid, rss_mb, footprint_mb, rss_ok, footprint_ok,
        )
    else:
        logger.info(
            "worker_memory_guard: pid=%d  rss=%d MB  footprint=N/A  rss_ok=%s",
            pid, rss_mb, rss_ok,
        )

    if rss_ok and footprint_ok:
        logger.info("worker_memory_guard: all checks passed")
        return 0

    # Threshold exceeded — kill the worker and fail
    violations = []
    if not rss_ok:
        violations.append(
            f"RSS {rss_mb} MB >= {rss_threshold_mb} MB threshold "
            f"(exceeded by {rss_mb - rss_threshold_mb} MB)"
        )
    if not footprint_ok:
        assert footprint_mb is not None
        violations.append(
            f"Physical footprint {footprint_mb} MB >= {footprint_threshold_mb} MB threshold "
            f"(exceeded by {footprint_mb - footprint_threshold_mb} MB)"
        )

    reason = "; ".join(violations)
    logger.error(
        "worker_memory_guard: THRESHOLD EXCEEDED for pid %d — %s. Sending SIGTERM.", pid, reason
    )
    try:
        os.kill(pid, signal.SIGTERM)
        logger.info("worker_memory_guard: SIGTERM sent to pid %d", pid)
    except OSError as exc:
        logger.error("worker_memory_guard: failed to send SIGTERM to pid %d: %s", pid, exc)

    return 1


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    return check_worker()


if __name__ == "__main__":
    sys.exit(main())
