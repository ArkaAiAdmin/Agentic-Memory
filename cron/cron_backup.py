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

try:
    from _flock import acquire_lock_or_exit
except ModuleNotFoundError:
    from cron._flock import acquire_lock_or_exit
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
RETENTION_COUNT = 5  # Keep last 5 successful backups
RETENTION_MAX_AGE_DAYS = 14  # Or age <= 14 days, whichever retains more
MAX_BACKUP_DIR_SIZE_BYTES = 15 * 1024 * 1024 * 1024  # 15 GB emergency size cap
CRON_MARKER = "# agentic-memory-backup"
CRON_SCHEDULE = "0 2 * * *"  # Daily at 2am


def enforce_backup_retention(backup_dir: Path) -> dict:
    """Enforce backup retention policy on backup_dir:
    1. Keep last N=5 successful backups OR backups aged <= 14 days, whichever retains more.
    2. Size guard: if total size of backup_dir exceeds 15GB, retain only the most recent 2.
    Returns dict with removed files and total remaining.
    """
    all_files = [
        f for f in backup_dir.glob("memory-*.db*")
        if f.is_file() and not f.is_symlink() and not f.name.endswith(".log")
    ]
    # Sort by mtime descending (most recent first)
    all_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)

    now = time.time()
    max_age_s = RETENTION_MAX_AGE_DAYS * 86400

    # Calculate total size
    total_size = sum(f.stat().st_size for f in all_files)

    if total_size > MAX_BACKUP_DIR_SIZE_BYTES:
        # Emergency size cap: retain only the 2 most recent backups
        logger.warning(
            "Backup dir exceeds 15GB limit (%d bytes); enforcing emergency retention of 2 backups",
            total_size,
        )
        keep_set = set(all_files[:2])
    else:
        # Retain at least 5 most recent, or any with age <= 14 days
        keep_by_count = set(all_files[:RETENTION_COUNT])
        keep_by_age = {f for f in all_files if (now - f.stat().st_mtime) <= max_age_s}
        keep_set = keep_by_count.union(keep_by_age)

    removed = 0
    removed_bytes = 0
    for f in all_files:
        if f not in keep_set:
            try:
                sz = f.stat().st_size
                f.unlink()
                removed += 1
                removed_bytes += sz
                logger.info("Pruned expired backup: %s (%d bytes)", f.name, sz)
            except OSError as exc:
                logger.debug("cron_backup: cannot unlink old backup %s: %s", f, exc)

    remaining = [f for f in all_files if f in keep_set and f.exists()]
    return {
        "removed": removed,
        "removed_bytes": removed_bytes,
        "total_backups": len(remaining),
    }


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
    src_conn = None
    dst_conn = None
    wal_sidecar_path = Path(str(backup_path) + "-wal")
    shm_sidecar_path = Path(str(backup_path) + "-shm")
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
            for conn in (dst_conn, src_conn):
                if conn is not None:
                    try:
                        conn.close()
                    except Exception as e:
                        logger.warning("do_backup failed: %s", e)
            src_conn = None
            dst_conn = None
            if attempt < 2:
                logger.warning(
                    "backup attempt %d/3 failed (%s); retrying in %ds",
                    attempt + 1,
                    exc,
                    [0, 5, 15][attempt],
                )
                time.sleep([0, 5, 15][attempt])
    if last_err is not None:
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

    # Enforce retention policy
    retention_stats = enforce_backup_retention(backup_dir)

    # Also create a symlink without date for "latest" reference
    latest_path = backup_dir / "memory-latest.db.gz"
    try:
        if latest_path.exists() or latest_path.is_symlink():
            latest_path.unlink()
        latest_path.symlink_to(gz_path.name)
    except OSError as exc:
        logger.debug("cron_backup: cannot update latest symlink: %s", exc)
    return {
        "backup_path": str(gz_path),
        "backup_size": backup_size,
        "removed": retention_stats["removed"],
        "total_backups": retention_stats["total_backups"],
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

    if "--prune-existing" in args:
        env = os.environ.get("MEMORY_DB_PATH")
        db_path = Path(env) if env else resolve_active_memory_dir() / "memory.db"
        b_dir = db_path.parent / BACKUP_DIR_NAME if len(args) == 1 else Path(args[0])
        stats = enforce_backup_retention(b_dir)
        logger.info("Pruned %d expired backups (%s bytes freed), %d remaining",
                    stats["removed"], f"{stats['removed_bytes']:,}", stats["total_backups"])
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
