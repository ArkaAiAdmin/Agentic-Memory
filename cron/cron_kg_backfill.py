#!/usr/bin/env python3
"""Cron wrapper: weekly KG (knowledge graph) backfill.

Runs ``backfill_all.py --incremental`` to refresh kg_facts, kg_entities,
and kg_edges without dropping the data. Designed for weekly execution
as a complement to:

  - daily:    cron_rebuild_fts.py (FTS5 B-tree refresh)
  - weekly:   this script (KG reconciliation + entity filter)
  - monthly:  cron_compact.py (full rebuild + tier migration)

Safety properties (2026-06-19 P3.4):
  - Uses --incremental (NOT --full) so it never wipes kg_* tables
  - Per-batch commits every 25 memories (survives kill/OOM)
  - Progress markers every 100 memories (visible in cron logs)
  - LLM extraction is opt-in via MEMORY_LLM_EXTRACTION=1 (default: regex
    only via MEMORY_LLM_HYBRID_THRESHOLD + force-off). The cron run
    never blocks on the LLM.
  - Pre-flight integrity check before touching data
  - Post-flight row-count diff so we can see what changed

Recommended schedule: Sunday 03:30 (after cron_heartbeat at 03:00,
before cron_consolidate on Sunday at 04:00).

Threshold tuning (corpus analysis 2026-06-19, 6,357 memories):
  - 96.9% of memories cluster at importance_score 0.30-0.40
  - 15 pinned memories (always LLM-extracted regardless of threshold)
  - At default 0.5: ~20 memories get LLM (15 pinned + 5 outliers)
  - At 0.7: 15 memories get LLM (pinned only)
  - At 0.3: 6,192 memories get LLM (97.4% — full corpus, ~17 hours)

The cron defaults (set via env.setdefault in main):
  - MEMORY_LLM_HYBRID_THRESHOLD=0.7 — LLM only for pinned (15 memories)
  - MEMORY_LLM_FORCE=0 — never force LLM (prevents accidental 18h run)

Estimated LLM time per weekly cron: ~2.5 min (well under 1h timeout).

To run with LLM on the full pinned + outlier tier (0.5 threshold):
    MEMORY_LLM_HYBRID_THRESHOLD=0.5 venv/bin/python cron_kg_backfill.py

To skip LLM entirely (regex-only, fastest):
    MEMORY_LLM_HYBRID=0 venv/bin/python cron_kg_backfill.py

To force LLM on every memory (DANGEROUS — 18h ETA):
    MEMORY_LLM_FORCE=1 venv/bin/python cron_kg_backfill.py

Usage:
    venv/bin/python cron_kg_backfill.py
    venv/bin/python cron_kg_backfill.py --dry-run    # log only, no writes
    venv/bin/python cron_kg_backfill.py --commit-every 50 --progress-every 200
"""

from __future__ import annotations

from _flock import acquire_lock_or_exit
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_parent = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(_parent) == "cron":
    _parent = os.path.dirname(_parent)
sys.path.insert(0, _parent)
from infra.infrastructure import resolve_active_memory_dir

from infra.log import setup_logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.db import AnyConnection


logger = setup_logging("cron_kg_backfill", level="INFO", fmt="%(asctime)s [%(levelname)s] %(message)s")


# Hard-coded cron defaults — Sunday 03:00, after FTS rebuild at 02:30.
# Override via MEMORY_KG_CRON_DAY / MEMORY_KG_CRON_HOUR env vars for testing.
DEFAULT_CRON_DAY = "sun"  # 0=Sun, 1=Mon, ... 6=Sun (we use named day)
DEFAULT_CRON_HOUR = 3
DEFAULT_CRON_MINUTE = 0


def _resolve_db_path() -> Path:
    env = os.environ.get("MEMORY_DB_PATH")
    if env:
        return Path(env)
    active_dir = resolve_active_memory_dir()
    return active_dir / "memory.db"


def _table_count(conn: AnyConnection, table: str) -> int:
    try:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return int(row[0]) if row else 0
    except sqlite3.OperationalError:
        return -1


def preflight_stats(db_path: Path) -> dict:
    """Snapshot row counts before the backfill."""
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        return {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "db_path": str(db_path),
            "kg_facts": _table_count(conn, "kg_facts"),
            "kg_entities": _table_count(conn, "kg_entities"),
            "kg_edges": _table_count(conn, "kg_edges"),
            "memories": _table_count(conn, "memories"),
        }
    finally:
        conn.close()


def run_backfill(
    db_path: Path,
    commit_every: int = 25,
    progress_every: int = 100,
    dry_run: bool = False,
    incremental: bool = False,
) -> dict:
    """Run backfill_all.py --incremental and capture results.

    Returns a dict with timing, exit code, and parsed stats from stdout.
    """
    scripts_dir = Path(__file__).resolve().parent
    if scripts_dir.name == "cron":
        scripts_dir = scripts_dir.parent
    venv_python = scripts_dir / "venv" / "bin" / "python3.14"
    if not venv_python.exists():
        venv_python = scripts_dir / ".venv" / "bin" / "python3.14"
    if not venv_python.exists():
        venv_python = Path(sys.executable)

    cmd = [
        str(venv_python),
        str(scripts_dir / "backfill_all.py"),
    ]
    if incremental:
        cmd.append("--incremental")
    cmd.extend([
        f"--commit-every={commit_every}",
        f"--progress-every={progress_every}",
    ])
    if dry_run:
        cmd.append("--health")  # dry run = health check only

    env = os.environ.copy()
    # Cron defaults (tuned for the 6,357-memory corpus, 2026-06-19):
    # - threshold 0.7: LLM only for pinned (15 memories, ~2.5 min)
    # - force 0: never run the 18h full-corpus LLM extraction
    # User can override these by setting env vars before running.
    env.setdefault("MEMORY_LLM_HYBRID_THRESHOLD", "0.7")
    env.setdefault("MEMORY_LLM_FORCE", "0")

    logger.info("Running: %s", " ".join(cmd))
    t_start = time.time()
    try:
        result = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=3600,  # 1 hour max
        )
        elapsed = time.time() - t_start
        return {
            "exit_code": result.returncode,
            "elapsed_seconds": elapsed,
            "stdout_tail": result.stdout[-2000:] if result.stdout else "",
            "stderr_tail": result.stderr[-2000:] if result.stderr else "",
            "command": cmd,
        }
    except subprocess.TimeoutExpired:
        return {
            "exit_code": -1,
            "elapsed_seconds": time.time() - t_start,
            "stdout_tail": "",
            "stderr_tail": "TIMEOUT after 3600s",
            "command": cmd,
        }


def postflight_stats(db_path: Path, pre: dict) -> dict:
    """Snapshot row counts after the backfill; return deltas."""
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        post = {
            "kg_facts": _table_count(conn, "kg_facts"),
            "kg_entities": _table_count(conn, "kg_entities"),
            "kg_edges": _table_count(conn, "kg_edges"),
            "memories": _table_count(conn, "memories"),
        }
    finally:
        conn.close()
    deltas: dict[str, int] = {k: post[k] - pre.get(k, 0) for k in post}
    return {**post, "deltas": deltas}


def main() -> int:
    import argparse
    acquire_lock_or_exit('cron_kg_backfill')

    parser = argparse.ArgumentParser(description="Weekly KG backfill cron")
    parser.add_argument("--dry-run", action="store_true", help="Health check only")
    parser.add_argument("--incremental", action="store_true", default=False, help="Incremental backfill (safe, no table drops)")
    parser.add_argument("--commit-every", type=int, default=25)
    parser.add_argument("--progress-every", type=int, default=100)
    parser.add_argument(
        "--log-file",
        default=None,
        help="Path to JSON log file (default: memory/kg-backfill-cron.log)",
    )
    args = parser.parse_args()

    db_path = _resolve_db_path()
    if not db_path.exists():
        logger.error("No memory.db at %s", db_path)
        sys.exit(1)

    log_file = (
        Path(args.log_file)
        if args.log_file
        else db_path.parent / "kg-backfill-cron.log"
    )

    logger.info("=== Weekly KG backfill starting (db=%s) ===", db_path)
    pre = preflight_stats(db_path)
    logger.info(
        "Pre: kg_facts=%d, kg_entities=%d, kg_edges=%d, memories=%d",
        pre["kg_facts"],
        pre["kg_entities"],
        pre["kg_edges"],
        pre["memories"],
    )

    from background.cron_model_lock import cron_model_lock
    with cron_model_lock("kg_backfill", timeout=600.0):
        result = run_backfill(
            db_path,
            commit_every=args.commit_every,
            progress_every=args.progress_every,
            dry_run=args.dry_run,
            incremental=args.incremental,
        )

    post = postflight_stats(db_path, pre)
    logger.info(
        "Post: kg_facts=%d (%+d), kg_entities=%d (%+d), kg_edges=%d (%+d), memories=%d (%+d)",
        post["kg_facts"],
        post["deltas"]["kg_facts"],
        post["kg_entities"],
        post["deltas"]["kg_entities"],
        post["kg_edges"],
        post["deltas"]["kg_edges"],
        post["memories"],
        post["deltas"]["memories"],
    )

    logger.info(
        "Backfill exit_code=%d in %.1fs",
        result["exit_code"],
        result["elapsed_seconds"],
    )

    # Write structured JSON log entry
    log_entry = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": args.dry_run,
        "pre": pre,
        "post": post,
        "result": result,
    }
    try:
        with open(log_file, "a") as f:
            f.write(json.dumps(log_entry) + "\n")
        logger.info("Wrote log entry to %s", log_file)
    except OSError as exc:
        logger.warning("Failed to write log file: %s", exc)

    if result["exit_code"] != 0:
        logger.error(
            "Backfill failed (exit %d). Stderr tail:\n%s",
            result["exit_code"],
            result["stderr_tail"],
        )
        sys.exit(1)

    logger.info("=== Weekly KG backfill complete ===")
    return 0


if __name__ == "__main__":
    main()
