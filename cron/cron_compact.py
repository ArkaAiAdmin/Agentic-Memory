#!/usr/bin/env python3
"""Cron wrapper: compact — tier migration + consolidation + rebuild + archive.

Includes a PRAGMA integrity_check after rebuild to catch silent corruption.
"""

from _flock import acquire_lock_or_exit
import os
import re
import sys
import subprocess
import shutil
import time
import sqlite3
from pathlib import Path


_parent = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(_parent) == "cron":
    _parent = os.path.dirname(_parent)
sys.path.insert(0, _parent)
from memory_common import safe_close_db
from memory_config import install_root

os.environ.setdefault("MEMORY_KNOWLEDGE_GRAPH", "1")
# M8 fix: prefer the venv's own python (sys.executable) if we're already
# running from one, fall back to install_root()/venv/bin/python (resolved
# via memory_config.install_root so non-default installs work), and
# allow MEMORY_PYTHON env var to override. The hardcoded path previously
# broke for any user whose venv lives at `.venv/` or
# `/opt/homebrew/bin/python` instead of `./venv/bin/python`.
SCRIPTS = Path(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_VENV_PY = str(install_root() / "venv" / "bin" / "python")
if not os.path.exists(_DEFAULT_VENV_PY):
    _DEFAULT_VENV_PY = str(install_root() / ".venv" / "bin" / "python")
PYTHON = os.environ.get("MEMORY_PYTHON") or (
    sys.executable if Path(sys.executable).parents[1] == SCRIPTS else _DEFAULT_VENV_PY
)


def run(script, args=None, timeout=120):
    script_path = SCRIPTS / script
    if not script_path.exists():
        script_path = SCRIPTS.parent / script
    cmd = [PYTHON, str(script_path)] + (args or [])
    print(f"\n=== {script} ===")
    try:
        out = subprocess.check_output(
            cmd, timeout=timeout, stderr=subprocess.STDOUT, text=True
        )
        print(out)
    except subprocess.CalledProcessError as e:
        print(f"ERROR: {e.output}")
    except subprocess.TimeoutExpired:
        print(f"TIMEOUT after {timeout}s")


def check_integrity(db_path):
    """Run PRAGMA integrity_check and foreign_key_check after rebuild."""
    print("\n=== integrity_check ===")
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()
        status = result[0] if result else "empty_db"
        print(f"PRAGMA integrity_check: {status}")

        fk_result = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk_result:
            print(f"PRAGMA foreign_key_check: {len(fk_result)} violations found")
            for row in fk_result[:5]:
                print(f"  {row}")
        else:
            print("PRAGMA foreign_key_check: OK")
    except Exception as e:
        print(f"integrity_check error: {e}")
    finally:
        safe_close_db(conn)


def main() -> int:
    import argparse
    acquire_lock_or_exit('cron_compact')

    # argparse handles --help and exits cleanly. The pipeline itself
    # takes no flags — the steps are run in fixed order; pass
    # MEMORY_DB_PATH to override the DB path.
    argparse.ArgumentParser(
        description="Cron compact: tier migration + consolidation + rebuild + archive."
    ).parse_args()

    from infrastructure import resolve_active_memory_dir

    env = os.environ.get("MEMORY_DB_PATH")
    if env:
        db_path = Path(env)
        source_dir = db_path.parent
    else:
        source_dir = resolve_active_memory_dir()
        db_path = source_dir / "memory.db"

    # ``cron_consolidate.py`` is the standalone dedup/contradiction
    # pass that lives in this directory.  We invoke it via subprocess
    # so each step has its own connection lifecycle and its own log
    # line in the cron log, and so a slow consolidation can't block
    # the rest of the pipeline.
    run("tier_migration.py")
    run("cron_consolidate.py")
    # rebuild_index.py needs source_dir and db_path args
    run("rebuild_index.py", [str(source_dir), str(db_path)])
    # Backfill KG entities, edges, facts, and backlinks from markdown
    # sources. --incremental is cheap (skips already-populated tables).
    run("backfill_all.py", ["--incremental"])
    # Rebuild vector index (auto — was manual before)
    run("rebuild_vec_index.py", [str(db_path)], timeout=300)
    # KG entity dedup (auto — merge same-name + semantic similar entities)
    run("kg_dedup.py", [str(db_path), "--semantic"])
    # Cross-session learning (extract reusable patterns)
    run("cross_session_learn.py", ["--days=3"])
    # Embedding recomputation (auto-rebuild if model changed)
    run("embedding_recompute.py")

    # Post-rebuild integrity check
    check_integrity(db_path)

    # Archive old sessions (>14 days) — but ONLY auto-*.md files that
    # have already been rolled into a daily-digest. Otherwise a fresh
    # crash dump from yesterday could be moved to the archive while
    # still pending digest, defeating the daily-rollup tool. (Audit M5.)
    active_mem = resolve_active_memory_dir()
    sessions_dir = active_mem / "sessions"
    archive_dir = active_mem / "archive" / "sessions"
    if sessions_dir.exists():
        archive_dir.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - 14 * 86400
        count = 0
        for f in sessions_dir.glob("*.md"):
            if f.stat().st_mtime >= cutoff:
                continue
            # Auto-saves (auto-*.md) need an associated daily-digest
            # before archiving. Daily-digest files are the roll-up; the
            # digest is named sessions/YYYY-MM-DD.md. We include that
            # digest in the same archive decision so a rollback can
            # recover the original auto-saves.
            digest_name = None
            m = re.match(r"auto-(\d{4}-\d{2}-\d{2})_", f.name)
            if m:
                date_str = m.group(1)
                digest_name = f"{date_str}.md"
            if digest_name is not None:
                digest_path = sessions_dir / digest_name
                if not digest_path.exists():
                    # Daily digest hasn't run yet — leave the auto-save
                    # alone so the next crondaily run can
                    # roll it up. It's older than 14d but stale, not lost.
                    continue
            dst = archive_dir / f.name
            if f.stat().st_dev == dst.parent.stat().st_dev:
                os.replace(str(f), str(dst))
            else:
                shutil.move(str(f), str(dst))
            count += 1
        if count:
            print(f"\nArchived {count} old session files")


if __name__ == "__main__":
    main()
