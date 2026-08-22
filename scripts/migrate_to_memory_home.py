#!/usr/bin/env python3
"""Data migration script: migrate legacy memory data to MEMORY_HOME layout.

Moves mutable data out of the code tree into the OS-standard application support directory:
  macOS:   ~/Library/Application Support/AgenticMemory/
  Linux:   ~/.local/share/AgenticMemory/
  Windows: %APPDATA%/AgenticMemory/

Directory Layout under MEMORY_HOME:
  data/       — memory.db, sessions/, markdown notes
  logs/       — api-server.log, cron.log, worker logs
  backups/    — memory-YYYY-MM-DD.db.gz
  config/     — memory.toml (user-editable)
  runtime/    — .api_token, lock files, discovery json

Usage:
  # Dry-run preview
  python scripts/migrate_to_memory_home.py --dry-run

  # Execute migration
  python scripts/migrate_to_memory_home.py

  # Force re-migration
  python scripts/migrate_to_memory_home.py --force
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

# Add package root to sys.path
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from infra.memory_config import get_memory_home
from cron.cron_backup import enforce_backup_retention


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate agentic-memory data to MEMORY_HOME layout."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview migration operations without modifying the filesystem.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run migration even if target marker file already exists.",
    )
    parser.add_argument(
        "--legacy-dir",
        type=Path,
        default=Path.home() / ".config" / "agentic-memory" / "memory",
        help="Path to legacy memory directory.",
    )
    parser.add_argument(
        "--target-home",
        type=Path,
        default=None,
        help="Override destination MEMORY_HOME directory.",
    )
    parser.add_argument(
        "--prune-backups",
        action="store_true",
        default=True,
        help="Apply backup retention cap during migration (default: True).",
    )
    return parser.parse_args()


def check_db_integrity(db_path: Path) -> tuple[bool, str]:
    if not db_path.exists():
        return False, "Database file does not exist."
    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()
        cursor.execute("PRAGMA integrity_check;")
        row = cursor.fetchone()
        conn.close()
        if row and row[0] == "ok":
            return True, "ok"
        return False, f"Integrity check failed: {row}"
    except Exception as exc:
        return False, f"Database check error: {exc}"


def migrate(
    legacy_dir: Path,
    target_home: Path,
    dry_run: bool = False,
    force: bool = False,
    prune_backups: bool = True,
) -> int:
    print("=" * 60)
    print("Agentic Memory Migration to MEMORY_HOME")
    print("=" * 60)
    print(f"Source (Legacy Dir): {legacy_dir}")
    print(f"Target (MEMORY_HOME): {target_home}")
    print(f"Mode: {'DRY RUN (no changes)' if dry_run else 'LIVE MIGRATION'}")
    print("-" * 60)

    if not legacy_dir.exists():
        print(f"[!] Legacy directory {legacy_dir} does not exist. Nothing to migrate.")
        return 0

    marker_file = target_home / "runtime" / ".migrated_from_v1"
    if marker_file.exists() and not force:
        print(f"[*] Target already contains migration marker: {marker_file}")
        print("    Use --force to re-run migration.")
        return 0

    # Ensure target subdirectories exist
    data_dir = target_home / "data"
    logs_dir = target_home / "logs"
    backups_dir = target_home / "backups"
    config_dir = target_home / "config"
    runtime_dir = target_home / "runtime"

    subdirs = [data_dir, logs_dir, backups_dir, config_dir, runtime_dir]

    if not dry_run:
        for d in subdirs:
            d.mkdir(parents=True, exist_ok=True)

    # 1. Check source database
    src_db = legacy_dir / "memory.db"
    if src_db.exists() and not src_db.is_symlink():
        ok, msg = check_db_integrity(src_db)
        if not ok:
            print(f"[!] Warning: Source DB integrity check issue: {msg}")
        else:
            print(f"[+] Source DB integrity verified: {src_db}")

    operations: list[dict] = []

    # 2. Database files
    for db_file in ["memory.db", "memory.db-wal", "memory.db-shm"]:
        src = legacy_dir / db_file
        if src.exists() and not src.is_symlink():
            dst = data_dir / db_file
            operations.append({"type": "move", "src": src, "dst": dst, "symlink_legacy": True})

    # 3. Token file
    token_src = legacy_dir / ".api_token"
    if token_src.exists() and not token_src.is_symlink():
        operations.append({
            "type": "move",
            "src": token_src,
            "dst": runtime_dir / ".api_token",
            "symlink_legacy": True,
        })

    # 4. Config file
    cfg_src = legacy_dir.parent / "memory.toml"
    if cfg_src.exists() and not cfg_src.is_symlink():
        operations.append({
            "type": "copy",
            "src": cfg_src,
            "dst": config_dir / "memory.toml",
            "symlink_legacy": False,
        })

    # 5. Directory contents (sessions, notes, backups)
    for entry in legacy_dir.iterdir():
        if entry.is_symlink():
            continue
        if entry.name in ("memory.db", "memory.db-wal", "memory.db-shm", ".api_token"):
            continue
        if entry.name == "backups":
            operations.append({
                "type": "move_dir",
                "src": entry,
                "dst": backups_dir,
                "symlink_legacy": True,
            })
        elif entry.name == "logs":
            operations.append({
                "type": "move_dir",
                "src": entry,
                "dst": logs_dir,
                "symlink_legacy": True,
            })
        elif entry.name.endswith(".log") or "log" in entry.name:
            operations.append({
                "type": "move",
                "src": entry,
                "dst": logs_dir / entry.name,
                "symlink_legacy": True,
            })
        else:
            # Note directories (lessons, decisions, etc.) or sessions/
            dst_target = data_dir / entry.name
            operations.append({
                "type": "move_dir" if entry.is_dir() else "move",
                "src": entry,
                "dst": dst_target,
                "symlink_legacy": True,
            })

    print(f"\nPlanned Operations ({len(operations)} items):")
    for op in operations:
        print(f"  - [{op['type'].upper()}] {op['src']} -> {op['dst']}")

    if dry_run:
        print("\n[✓] Dry run complete. Re-run without --dry-run to execute.")
        return 0

    # Execute operations
    print("\nExecuting migration...")
    for op in operations:
        src: Path = op["src"]
        dst: Path = op["dst"]
        op_type: str = op["type"]

        if op_type == "move":
            if src.exists() and not src.is_symlink():
                shutil.move(str(src), str(dst))
                if op.get("symlink_legacy"):
                    src.symlink_to(dst)
                print(f"  [Moved] {src.name} -> {dst}")
        elif op_type == "copy":
            if src.exists():
                shutil.copy2(str(src), str(dst))
                print(f"  [Copied] {src.name} -> {dst}")
        elif op_type == "move_dir":
            if src.exists() and not src.is_symlink():
                if dst.exists():
                    # Merge directory contents
                    for item in src.iterdir():
                        target_item = dst / item.name
                        if not target_item.exists():
                            shutil.move(str(item), str(target_item))
                    # Remove now-empty source dir
                    try:
                        shutil.rmtree(str(src))
                    except Exception as e:
                        print(f"  [Warn] Could not remove empty dir {src}: {e}")
                else:
                    shutil.move(str(src), str(dst))

                if op.get("symlink_legacy"):
                    try:
                        if not src.exists():
                            src.symlink_to(dst)
                    except Exception as exc:
                        print(f"  [Warn] Could not create legacy symlink at {src}: {exc}")
                print(f"  [Moved dir] {src.name} -> {dst}")

    # 6. Apply backup retention pruning if requested
    if prune_backups and backups_dir.exists():
        print("\nEnforcing backup retention policy on migrated backups...")
        stats = enforce_backup_retention(backups_dir)
        print(f"  [Backups] Pruned {stats['removed']} old backups ({stats['removed_bytes']:,} bytes freed), {stats['total_backups']} retained.")

    # 7. Post-migration verification
    dest_db = data_dir / "memory.db"
    ok, msg = check_db_integrity(dest_db)
    if ok:
        print(f"\n[+] Destination DB integrity check PASSED: {dest_db}")
    else:
        print(f"\n[!] Destination DB check FAILED: {msg}")

    # 8. Write migration marker
    meta = {
        "migrated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source_dir": str(legacy_dir),
        "target_home": str(target_home),
        "verified_db": ok,
    }
    marker_file.write_text(json.dumps(meta, indent=2))
    print(f"[+] Written migration marker: {marker_file}")
    print("\n[✓] Migration to MEMORY_HOME successfully completed!")
    return 0


def main() -> int:
    args = parse_args()
    target_home = args.target_home or get_memory_home()
    return migrate(
        legacy_dir=args.legacy_dir,
        target_home=target_home,
        dry_run=args.dry_run,
        force=args.force,
        prune_backups=args.prune_backups,
    )


if __name__ == "__main__":
    sys.exit(main())
