#!/usr/bin/env python3
"""Cron watchdog — capture stack traces of hung cron processes.

Background:
  On 2026-06-26, cron_heartbeat.py hung at 66% CPU after loading the
  Qwen LLM. There was no built-in way to get a stack trace without
  installing py-spy (which wasn't installed) or using dtrace (which
  needs sudo). This watchdog runs as a separate cron job and dumps
  the stack of any cron process that has been running too long.

Usage (in crontab, every 5 minutes):
  */5 * * * * /Users/arka/.config/agentic-memory/venv/bin/python \\
      /Users/arka/.config/agentic-memory/cron/cron_watchdog.py \\
      >> /Users/arka/.config/agentic-memory/memory/watchdog.log 2>&1

The watchdog:
  1. Scans for python processes whose command line is a cron_*.py script.
  2. For each process older than --max-age-seconds (default 600 = 10 min),
     invokes `py-spy dump --pid <pid>` to capture a stack trace.
  3. Saves the trace to memory/stack_traces/<script>_<pid>_<ts>.txt.
  4. Logs a WARNING to watchdog.log.

It does NOT kill the process. That's the operator's call after looking
at the trace. Killing a hung cron would lose the trace; capturing the
trace preserves it for postmortem.

Why one-shot cron instead of a daemon:
  A daemon has the same failure mode as the processes it watches
  (can itself hang, be killed by OOM, etc). A one-shot cron job is
  idempotent — every 5 minutes it tries once and exits. If it
  crashes, the next invocation starts fresh.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CRON_DIR = REPO / "cron"
TRACE_DIR = REPO / "memory" / "stack_traces"
LOG_FILE = REPO / "memory" / "watchdog.log"

DEFAULT_MAX_AGE_SECONDS = 600  # 10 minutes


def log(msg: str) -> None:
    """Log a line to both stdout and watchdog.log (best-effort)."""
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} cron_watchdog: {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        # Don't crash the watchdog if the log can't be written
        pass


def parse_etime_to_seconds(etime: str) -> int | None:
    """Parse ps etime format ([[dd-]hh:]mm:ss or mm:ss) to seconds.

    Examples:
        "01:23"        -> 83
        "1:02:03"      -> 3723
        "2-03:04:05"   -> 2*86400 + 3*3600 + 4*60 + 5 = 183845
    """
    try:
        days = 0
        rest = etime
        if "-" in rest:
            days_part, rest = rest.split("-", 1)
            days = int(days_part)
        parts = rest.split(":")
        if len(parts) == 2:
            minutes, seconds = int(parts[0]), int(parts[1])
            return days * 86400 + minutes * 60 + seconds
        elif len(parts) == 3:
            hours, minutes, seconds = int(parts[0]), int(parts[1]), int(parts[2])
            return days * 86400 + hours * 3600 + minutes * 60 + seconds
    except (ValueError, IndexError):
        return None
    return None


def find_hung_processes(
    max_age_seconds: int,
) -> list[tuple[int, str, str, int]]:
    """Return [(pid, script, cmdline, age_seconds)] for processes over the age limit."""
    result: list[tuple[int, str, str, int]] = []
    try:
        ps = subprocess.run(
            ["ps", "-ax", "-o", "pid=,etime=,command="],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f"ps failed: {e}")
        return result

    cron_scripts = {p.name for p in CRON_DIR.glob("cron_*.py")}

    for line in ps.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        etime = parts[1]
        cmdline = parts[2]
        age = parse_etime_to_seconds(etime)
        if age is None or age < max_age_seconds:
            continue
        for script in cron_scripts:
            if script in cmdline:
                result.append((pid, script, cmdline, age))
                break
    return result


def dump_stack(pid: int, script: str) -> Path | None:
    """Invoke py-spy to capture *pid*'s stack trace.

    Returns the path to the saved trace, or None on failure.
    """
    TRACE_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    trace_path = TRACE_DIR / f"{script}_{pid}_{ts}.txt"

    # py-spy is installed in the same venv as the cron scripts
    venv_py_spy = REPO / "venv" / "bin" / "py-spy"
    if not venv_py_spy.exists():
        # Fall back to `py-spy` on PATH (less reliable)
        py_spy = "py-spy"
    else:
        py_spy = str(venv_py_spy)

    try:
        result = subprocess.run(
            [py_spy, "dump", "--pid", str(pid)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f"py-spy dump failed for pid {pid}: {e}")
        return None

    try:
        trace_path.write_text(
            f"# py-spy dump of pid {pid} ({script})\n"
            f"# captured {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"# exit={result.returncode}\n"
            f"# stdout:\n{result.stdout}\n"
            f"# stderr:\n{result.stderr}\n",
            encoding="utf-8",
        )
    except OSError as e:
        log(f"failed to write trace file: {e}")
        return None

    return trace_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Capture stack traces of hung cron processes via py-spy."
    )
    parser.add_argument(
        "--max-age-seconds",
        type=int,
        default=DEFAULT_MAX_AGE_SECONDS,
        help=f"Flag processes older than this as hung (default {DEFAULT_MAX_AGE_SECONDS})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be flagged without dumping stacks",
    )
    args = parser.parse_args()

    if not shutil.which("ps"):
        log("ps not on PATH; cannot enumerate processes")
        return 1

    hung = find_hung_processes(args.max_age_seconds)
    if not hung:
        return 0

    log(f"found {len(hung)} cron process(es) older than {args.max_age_seconds}s:")
    for pid, script, cmdline, age in hung:
        age_str = (
            f"{age // 60}m{age % 60}s"
            if age < 86400
            else f"{age // 86400}d{(age % 86400) // 3600}h"
        )
        log(f"  pid={pid} age={age_str} script={script}")

    if args.dry_run:
        return 0

    for pid, script, _cmdline, _age in hung:
        trace = dump_stack(pid, script)
        if trace:
            log(f"  -> captured stack to {trace}")
        else:
            log(f"  -> failed to capture stack for pid {pid}")

    return 0


if __name__ == "__main__":
    # Per-cron flock — required by the cron-scripts contract. See
    # _scripts/add_flock_to_crons.py and eval/test_add_flock_to_crons.py.
    # The watchdog itself is read-only and fast, but the flock keeps
    # it from racing against other cron jobs that may be inspecting
    # the same process table.
    from _flock import acquire_lock_or_exit

    acquire_lock_or_exit("cron_watchdog")
    sys.exit(main())
