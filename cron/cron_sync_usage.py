#!/usr/bin/env python3
"""Cron wrapper: sync_usage — sync MCP call counts from audit log and
measure storage for all active deployments.

Runs periodically (e.g. every 15 minutes) to keep cloud_state.db
usage_records up to date with actual MCP tool call activity and
storage consumption.
"""

from _flock import acquire_lock_or_exit
import os
import sys
from pathlib import Path

_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_repo_root)
_parent = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(_parent) == "cron":
    _parent = os.path.dirname(_parent)
sys.path.insert(0, _parent)

from infra.infrastructure import resolve_active_memory_dir


def main() -> int:
    acquire_lock_or_exit("cron_sync_usage")

    mem_dir = resolve_active_memory_dir()
    audit_db = str(mem_dir / "memory.db")
    cloud_db = mem_dir / "cloud_state.db"

    if not cloud_db.exists():
        print("cloud_state.db not found — skipping usage sync")
        return 0

    from infra_cloud.store import CloudStateStore
    store = CloudStateStore(cloud_db)

    # 1. Sync MCP call counts from audit log
    result = store.sync_usage_from_audit_log(audit_db)
    print(f"MCP usage sync: {result}")

    # 2. Measure storage and audit log size for each active deployment
    import time
    import sqlite3 as _sqlite3
    deps = store.list_deployments()
    for dep in deps:
        dep_id = dep["deployment_id"]
        db_path = dep.get("db_path")
        if db_path and Path(db_path).exists():
            try:
                storage_bytes = Path(db_path).stat().st_size
                # Measure audit log table size via page count
                audit_log_bytes = 0
                try:
                    _conn = _sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
                    # Check if audit log table exists
                    has_table = _conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_audit_log'"
                    ).fetchone()
                    if has_table:
                        page_count = _conn.execute("PRAGMA page_count").fetchone()[0]
                        page_size = _conn.execute("PRAGMA page_size").fetchone()[0]
                        # Approximate: audit log typically ~10-20% of DB
                        # Use actual table stats if available
                        try:
                            stats = _conn.execute(
                                "SELECT SUM(pgsize) FROM dbstat WHERE name='memory_audit_log'"
                            ).fetchone()
                            if stats and stats[0]:
                                audit_log_bytes = stats[0]
                        except Exception:
                            pass
                    _conn.close()
                except Exception:
                    pass
                store.increment_usage(dep_id, storage_bytes=storage_bytes, audit_log_bytes=audit_log_bytes)
            except Exception as e:
                print(f"Storage measurement failed for {dep_id}: {e}")

    print("Usage sync complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
