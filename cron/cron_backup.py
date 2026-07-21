#!/usr/bin/env python3
"""Cron wrapper: backup — daily SQLite backup to separate file.

Uses the SQLite backup API (not file copy) for crash-safe snapshots.
Keeps 7 daily backups, rotates older ones.

Usage:
    # Manual backup
    venv/bin/python cron_backup.py [backup_dir]

    # Install daily cron job (runs at 2am)
    venv/bin/python cron_backup.py --install-cron

    # Uninstall cron job
    venv/bin/python cron_backup.py --uninstall-cron

    # Check cron status
    venv/bin/python cron_backup.py --cron-status
"""

from _flock import acquire_lock_or_exit
import os
import sys
import sqlite3
import subprocess
import time
from pathlib import Path

_parent = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(_parent) == "cron":
    _parent = os.path.dirname(_parent)
sys.path.insert(0, _parent)
from infra.memory_common import GLOBAL_MEM_DIR, safe_close_db
from infra.infrastructure import resolve_active_memory_dir
from infra.log import setup_logging

logger = setup_logging(__name__)

BACKUP_DIR_NAME = "backups"
MAX_BACKUPS = 3  # Keep 3 daily backups (compressed, ~50MB each)
CRON_MARKER = "# agentic-memory-backup"
CRON_SCHEDULE = "0 2 * * *"  # Daily at 2am


def do_backup(backup_dir: Path | None = None) -> dict:
    """Run backup. Returns stats dict."""
    env = os.environ.get("MEMORY_DB_PATH")
    db_path = Path(env) if env else resolve_active_memory_dir() / "memory.db"
    if not db_path.exists():
        return {"error": f"memory.db not found at {db_path}"}

    if backup_dir is None:
        backup_dir = db_path.parent / BACKUP_DIR_NAME
    backup_dir.mkdir(parents=True, exist_ok=True)

    # Generate backup filename with date
    date_str = time.strftime("%Y-%m-%d")
    backup_path = backup_dir / f"memory-{date_str}.db"

    # Use SQLite backup API for crash-safe snapshot.
    # Backup API is robust against concurrent writers and doesn't block
    # their activity. We retry once on OperationalError("database is
    # locked") because the live DB is opened by other workers (the
    # background_worker.py every 5 min, the auto_save.py hook, and any
    # in-flight open_db() callers) and busy_timeout=10000 isn't always
    # enough on a contended day. This regressed two OpernationalErrors
    # visible in backup.log: rotating "old backup locked" out of the
    # way and retrying is the standard remediation pattern from the
    # SQLite docs.
    src_conn = None
    dst_conn = None
    wal_sidecar_path = Path(str(backup_path) + "-wal")
    shm_sidecar_path = Path(str(backup_path) + "-shm")
    # If a previous run crashed or was killed mid-backup, stale *.db-wal
    # / *.db-shm sidecars on the *destination* path can confuse the new
    # backup's WAL-mode bring-up. Clear them before connecting.
    for stale in (wal_sidecar_path, shm_sidecar_path):
        try:
            if stale.exists():
                stale.unlink()
        except OSError as exc:
            logger.debug("cron_backup: cannot unlink stale WAL file: %s", exc)
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            src_conn = sqlite3.connect(str(db_path), timeout=15.0)
            src_conn.execute("PRAGMA foreign_keys=ON")
            src_conn.execute("PRAGMA busy_timeout = 15000;")
            dst_conn = sqlite3.connect(str(backup_path), timeout=15.0)
            dst_conn.execute("PRAGMA foreign_keys=ON")
            dst_conn.execute("PRAGMA busy_timeout = 15000;")
            dst_conn.execute("PRAGMA journal_mode = WAL;")
            src_conn.backup(dst_conn)
            safe_close_db(dst_conn)
            dst_conn = None
            safe_close_db(src_conn)
            src_conn = None
            last_err = None
            break
        except sqlite3.OperationalError as exc:
            last_err = exc
            # Clean up any partial state from the failed attempt.
            for conn in (dst_conn, src_conn):
                if conn is not None:
                    try:
                        conn.close()
                    except Exception as e:
                        logger.warning("do_backup failed: %s", e)
            src_conn = None
            dst_conn = None
            # Retry with exponential backoff: 0s, 5s, 15s. The middle
            # attempt usually wins because competing writers unblock by
            # themselves within a few seconds.
            if attempt < 2:
                logger.warning(
                    "backup attempt %d/3 failed (%s); retrying in %ds",
                    attempt + 1,
                    exc,
                    [0, 5, 15][attempt + 1],
                )
                time.sleep([0, 5, 15][attempt + 1])
    if last_err is not None:
        # All retries failed. Surface the error so the operator can
        # diagnose; do NOT continue silently thinking the backup
        # succeeded.
        return {"error": f"backup failed after 3 attempts: {last_err}"}

    # Get backup size
    backup_size = backup_path.stat().st_size

    # Compress the backup with gzip to save ~70% space
    gz_path = Path(str(backup_path) + ".gz")
    try:
        import subprocess

        subprocess.run(["gzip", "-f", str(backup_path)], check=True, timeout=120)
        backup_size = gz_path.stat().st_size
    except Exception as e:
        logger.warning("do_backup failed: %s", e)
        gz_path = backup_path  # fall back to uncompressed

    # Rotate: remove backups older than MAX_BACKUPS days
    all_backups = sorted(
        backup_dir.glob("memory-*.db*"), key=lambda f: f.stat().st_mtime
    )
    removed = 0
    while len(all_backups) > MAX_BACKUPS:
        old = all_backups.pop(0)
        try:
            old.unlink()
            removed += 1
        except OSError as exc:
            logger.debug("cron_backup: cannot unlink old backup %s: %s", old, exc)

    # Also create a symlink without date for "latest" reference
    latest_path = backup_dir / "memory-latest.db.gz"
    try:
        if latest_path.exists():
            latest_path.unlink()
        latest_path.symlink_to(gz_path.name)
    except OSError as exc:
        logger.debug("cron_backup: cannot update latest symlink: %s", exc)
    return {
        "backup_path": str(gz_path),
        "backup_size": backup_size,
        "removed": removed,
        "total_backups": len(all_backups) + 1,
    }


def _get_python_path() -> str:
    """Get the venv Python path for cron job."""
    venv_python = Path(__file__).resolve().parent.parent / "venv" / "bin" / "python"
    if not venv_python.exists():
        venv_python = (
            Path(__file__).resolve().parent.parent / ".venv" / "bin" / "python"
        )
    if venv_python.exists():
        return str(venv_python)
    return sys.executable


def _get_cron_line() -> str:
    """Generate the cron line for daily backup."""
    python = _get_python_path()
    script = str(Path(__file__).resolve())
    log = str(GLOBAL_MEM_DIR / "backups" / "cron.log")
    return f"{CRON_SCHEDULE} {python} {script} >> {log} 2>&1 {CRON_MARKER}"


def install_cron() -> dict:
    """Install daily cron job for backups."""
    cron_line = _get_cron_line()
    # Get existing crontab
    try:
        result = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, timeout=5
        )
        existing = result.stdout if result.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {"error": "Could not read crontab. Is crontab available?"}

    # Check if already installed
    if CRON_MARKER in existing:
        return {"status": "already_installed", "cron_line": cron_line}

    # Append new cron line
    new_crontab = existing.rstrip() + "\n" + cron_line + "\n"
    try:
        proc = subprocess.run(
            ["crontab", "-"],
            input=new_crontab,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode != 0:
            return {"error": f"Failed to install cron: {proc.stderr}"}
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {"error": "Could not write crontab. Is crontab available?"}

    return {"status": "installed", "schedule": CRON_SCHEDULE, "cron_line": cron_line}


def uninstall_cron() -> dict:
    """Remove the agentic-memory backup cron job."""
    try:
        result = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return {"status": "no_crontab"}
        existing = result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {"error": "Could not read crontab."}

    if CRON_MARKER not in existing:
        return {"status": "not_found"}

    # Remove lines with our marker
    lines = [line for line in existing.splitlines() if CRON_MARKER not in line]
    new_crontab = "\n".join(lines) + "\n" if lines else ""
    try:
        proc = subprocess.run(
            ["crontab", "-"],
            input=new_crontab,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode != 0:
            return {"error": f"Failed to uninstall cron: {proc.stderr}"}
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {"error": "Could not write crontab."}

    return {"status": "uninstalled"}


def cron_status() -> dict:
    """Check if cron job is installed."""
    try:
        result = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return {"installed": False, "reason": "no crontab"}
        if CRON_MARKER in result.stdout:
            for line in result.stdout.splitlines():
                if CRON_MARKER in line:
                    return {
                        "installed": True,
                        "schedule": line.split(CRON_MARKER)[0].strip(),
                    }
        return {"installed": False}
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {"installed": False, "reason": "crontab not available"}


def main() -> int:
    # argparse handles --help and exits cleanly. The pipeline itself
    # takes no flags.
    if "--help" in sys.argv[1:] or "-h" in sys.argv[1:]:
        print("usage: %s [-h|--help]" % sys.argv[0], file=sys.stderr)
        print(
            "Cron job — runs the scheduled operation; no flags required.",
            file=sys.stderr,
        )
        sys.exit(0)

    args = sys.argv[1:]
    acquire_lock_or_exit("cron_backup")

    if "--install-cron" in args:
        result = install_cron()
        if "error" in result:
            logger.error("ERROR: %s", result['error'])
            sys.exit(1)
        logger.info("Cron: %s", result['status'])
        if "cron_line" in result:
            logger.info("  %s", result['cron_line'])
        return 0

    if "--uninstall-cron" in args:
        result = uninstall_cron()
        if "error" in result:
            logger.error("ERROR: %s", result['error'])
            sys.exit(1)
        logger.info("Cron: %s", result['status'])
        return 0

    if "--cron-status" in args:
        result = cron_status()
        logger.info("Cron installed: %s", result['installed'])
        if "schedule" in result:
            logger.info("  Schedule: %s", result['schedule'])
        if "reason" in result:
            logger.info("  Reason: %s", result['reason'])
        return 0

    backup_dir = None
    if args:
        backup_dir = Path(args[0])

    stats = do_backup(backup_dir)
    if "error" in stats:
        logger.error("ERROR: %s", stats['error'])
        sys.exit(1)

    logger.info("Backup complete: %s", stats['backup_path'])
    logger.info("  Size: %s bytes", f"{stats['backup_size']:,}")
    logger.info("  Removed: %d old backups", stats['removed'])
    logger.info("  Total backups: %d", stats['total_backups'])
    return 0


if __name__ == "__main__":
    main()
