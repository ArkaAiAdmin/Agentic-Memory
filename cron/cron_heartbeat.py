#!/usr/bin/env python3
"""Cron wrapper: heartbeat — decay, tier assignment, archive stale notes."""

from _flock import acquire_lock_or_exit
import os
import sqlite3
import sys
from pathlib import Path

# TOML is the source of truth for feature flags (see memory.toml).
# The crontab entry already passes MEMORY_SELF_DIRECTED=1,
# MEMORY_KNOWLEDGE_GRAPH=1, MEMORY_ADAPTIVE_RETENTION=1 so cron
# processes get these from the environment.  The setdefault calls
# were removed because they silently override TOML values even
# when running manually (see A2 fix, contradiction-report).
os.chdir(os.path.dirname(os.path.abspath(__file__)))
import os

_parent = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(_parent) == "cron":
    _parent = os.path.dirname(_parent)
sys.path.insert(0, _parent)
from infra.infrastructure import resolve_active_memory_dir
from self_directed import run_heartbeat


def main() -> int:
    acquire_lock_or_exit("cron_heartbeat")

    # argparse handles --help and exits cleanly. The pipeline itself
    # takes no flags.
    if "--help" in sys.argv[1:] or "-h" in sys.argv[1:]:
        print("usage: %s [-h|--help]" % sys.argv[0], file=sys.stderr)
        print(
            "Cron job — runs the scheduled operation; no flags required.",
            file=sys.stderr,
        )
        sys.exit(0)

    env = os.environ.get("MEMORY_DB_PATH")
    db_path = Path(env) if env else resolve_active_memory_dir() / "memory.db"
    if not db_path.exists():
        print(f"ERROR: no memory.db at {db_path}")
        sys.exit(1)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000;")
    try:
        result = run_heartbeat(conn, dry_run=False, db_path=str(db_path))
        print(
            f"Heartbeat complete: {result['evaluated']} evaluated, "
            f"{result['tier_changes']} tier changes "
            f"({result.get('promoted', 0)} promoted), "
            f"{result['archived']} archived."
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    main()
