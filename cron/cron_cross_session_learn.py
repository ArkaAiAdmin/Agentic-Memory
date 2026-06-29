#!/usr/bin/env python3
"""Cron wrapper: cross_session_learn — extract reusable patterns from sessions.

Runs ``cross_session_learn.py`` weekly as a standalone cron. The same
script also runs monthly via ``cron_compact.py``; this wrapper adds
a finer-grained cadence so the user gets fresh cross-session patterns
every Monday instead of every month.

Usage:
    venv/bin/python cron_cross_session_learn.py [--days N]

Default: --days=3 (recent week of sessions). For longer windows, set
``MEMORY_CROSS_SESSION_DAYS=14`` in the cron environment.
"""

from _flock import acquire_lock_or_exit
import sys
import subprocess
import time

# 2026-06-19 fix: bootstrap install_root BEFORE any agentic-memory import
# (same chicken-and-egg protection as the rest of the cron scripts).
import os as _os
import sys as _sys
from pathlib import Path as _Path

_INSTALL_ROOT = _Path(
    _os.environ.get("MEMORY_INSTALL_ROOT") or str(_Path(__file__).resolve().parent)
).resolve()
if _INSTALL_ROOT.name == "cron":
    _INSTALL_ROOT = _INSTALL_ROOT.parent
if not (_INSTALL_ROOT / "memory_config.py").exists():
    _INSTALL_ROOT = _Path.home() / ".config" / "agentic-memory"
_sys.path.insert(0, str(_INSTALL_ROOT))

SCRIPTS = _Path(__file__).resolve().parent
_DEFAULT_VENV_PY = str(_INSTALL_ROOT / "venv" / "bin" / "python")
PYTHON = _os.environ.get("MEMORY_PYTHON") or (
    _sys.executable
    if _Path(_sys.executable).parents[1] == SCRIPTS
    else _DEFAULT_VENV_PY
)


def main() -> int:
    if "--help" in sys.argv[1:] or "-h" in sys.argv[1:]:
        print("usage: %s [-h|--help]" % sys.argv[0], file=sys.stderr)
        print("Cron job — runs the scheduled operation; no flags required.", file=sys.stderr)
        sys.exit(0)

    days = _os.environ.get("MEMORY_CROSS_SESSION_DAYS", "3")
    cmd = [PYTHON, str(_INSTALL_ROOT / "cross_session_learn.py"), f"--days={days}"]
    print(f"[cross_session_learn] running: {' '.join(cmd)}")
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        elapsed = time.time() - t0
        print(f"[cross_session_learn] exit={r.returncode} elapsed={elapsed:.1f}s")
        if r.stdout:
            print(r.stdout)
        if r.stderr and r.returncode != 0:
            print(f"[cross_session_learn] stderr:\n{r.stderr}", file=_sys.stderr)
        return r.returncode
    except subprocess.TimeoutExpired:
        print(
            f"[cross_session_learn] TIMEOUT after {time.time() - t0:.1f}s",
            file=_sys.stderr,
        )
        return 124
    except Exception as e:
        print(f"[cross_session_learn] ERROR: {e}", file=_sys.stderr)
        return 1
    acquire_lock_or_exit('cron_cross_session_learn')


if __name__ == "__main__":
    _sys.exit(main())
