#!/usr/bin/env python3
"""Cron: log retention — keep log files under control.

Archives and truncates any log > 1MB. Keeps at most 2 archive
generations per log file. Run daily from crontab.

Usage:
    venv/bin/python cron/cron_log_retention.py
"""

from _flock import acquire_lock_or_exit
import shutil
from pathlib import Path

MEMORY_DIR = Path(__file__).resolve().parent.parent / "memory"
ARCHIVE_DIR = MEMORY_DIR / "log-archive"
MAX_LOG_BYTES = 1_000_000
MAX_GENERATIONS = 2


def rotate_log(log_path: Path) -> bool:
    """Archive and truncate a log file if it exceeds MAX_LOG_BYTES."""
    if not log_path.exists() or log_path.stat().st_size < MAX_LOG_BYTES:
        return False

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    base = log_path.name

    # Shift generations: gen2 -> discard, gen1 -> gen2, current -> gen1
    gen2 = ARCHIVE_DIR / f"{base}.2"
    gen1 = ARCHIVE_DIR / f"{base}.1"
    if gen2.exists():
        gen2.unlink()
    if gen1.exists():
        shutil.move(str(gen1), str(gen2))
        gen2.stat().st_size

    shutil.copy2(str(log_path), str(gen1))
    gen1.stat().st_size

    with open(log_path, "w") as f:
        f.truncate(0)

    return True


def main() -> int:
    acquire_lock_or_exit("cron_log_retention")

    # Match both *.log and *.jsonl log sinks
    log_patterns = list(MEMORY_DIR.glob("*.log")) + list(MEMORY_DIR.glob("*.jsonl"))

    # Also check system root logs
    root_dir = Path(__file__).resolve().parent.parent
    for p in list(root_dir.glob("*.log")) + list(root_dir.glob("*.jsonl")):
        if p not in log_patterns:
            log_patterns.append(p)

    rotated = 0
    for log_path in sorted(log_patterns):
        if rotate_log(log_path):
            rotated += 1
            size_mb = log_path.stat().st_size / 1_000_000
            print(f"  Rotated: {log_path.name} (now {size_mb:.1f} MB)")

    # Prune old .drift_cron_*.json files older than 14 days in cron/
    import time
    cron_dir = root_dir / "cron"
    now = time.time()
    pruned_drift = 0
    if cron_dir.exists():
        for df in cron_dir.glob(".drift_cron_*.json"):
            try:
                if now - df.stat().st_mtime > 14 * 86400:
                    df.unlink(missing_ok=True)
                    pruned_drift += 1
            except Exception:
                pass

    print(f"Log retention: {rotated} files rotated, {pruned_drift} old drift snapshots pruned")
    return 0


if __name__ == "__main__":
    main()
