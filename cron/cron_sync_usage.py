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

os.chdir(os.path.dirname(os.path.abspath(__file__)))
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

    # 2. Measure storage for each active deployment
    import time
    day = time.strftime("%Y-%m-%d", time.gmtime())
    deps = store.list_deployments()
    for dep in deps:
        dep_id = dep["deployment_id"]
        db_path = dep.get("db_path")
        if db_path and Path(db_path).exists():
            try:
                storage_bytes = Path(db_path).stat().st_size
                store.increment_usage(dep_id, storage_bytes=storage_bytes)
            except Exception as e:
                print(f"Storage measurement failed for {dep_id}: {e}")

    print("Usage sync complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
