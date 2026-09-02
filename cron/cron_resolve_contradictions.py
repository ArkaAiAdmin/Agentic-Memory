"""Cron job: scan for unresolved contradictions and auto-resolve them.

Scans all notes with contradicting pairs that are not yet superseded,
then runs each pair through the LLM contradiction resolver (gated by
``MEMORY_CONTRADICTION_AUTO_RESOLVE_LLM=1``). Falls back to deterministic
newer-wins resolution unconditionally — always runs.

Usage:
    python cron_resolve_contradictions.py [--db <path>] [--dry-run] [--limit N]
"""

from __future__ import annotations

import logging
logger = logging.getLogger(__name__)

from _flock import acquire_lock_or_exit
import argparse
import json
import os
import sys
from infra.memory_config import get_global_memory_dir


_DEFAULT_DB = os.environ.get("MEMORY_DB_PATH") or str(get_global_memory_dir() / "memory.db")
_DEFAULT_LIMIT = 50


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Resolve contradictions cron")
    parser.add_argument("--db", default=_DEFAULT_DB, help="Path to memory.db")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writes")
    parser.add_argument("--limit", type=int, default=_DEFAULT_LIMIT, help="Max pairs to process")
    parser.add_argument(
        "--tenant",
        default=os.environ.get("MEMORY_CRON_TENANT_ID"),
        help="Restrict contradiction detection to one tenant (prevents cross-tenant false positives in shared DBs)",
    )
    return parser.parse_args()


def main() -> int:
    acquire_lock_or_exit("cron_resolve_contradictions")
    args = _parse_args()
    db = args.db
    if not os.path.exists(db):
        print(f"contradiction_resolver: DB not found at {db}", file=sys.stderr)
        return 0

    try:
        from kg.contradiction_detector import detect_contradictions
        from kg.contradiction_resolver import auto_resolve_contradiction_pair
        from pathlib import Path
        mem_dir = str(Path(db).parent)
    except ImportError as e:
        print(f"contradiction_resolver: import error: {e}", file=sys.stderr)
        return 0

    try:
        contradictions = detect_contradictions(
            mem_dir, min_confidence="low", tenant_id=args.tenant
        )
    except Exception as e:
        print(f"contradiction_resolver: detection failed: {e}", file=sys.stderr)
        return 0

    if not contradictions:
        print(json.dumps({"scanned": 0, "resolved": 0, "message": "no contradictions detected"}))
        return 0

    # Coordination: auto-create tasks for contradictions that need agent attention
    try:
        from coordination.hooks import create_contradiction_tasks
        tasks_created = create_contradiction_tasks(contradictions)
    except Exception:
        tasks_created = 0

    limit = args.limit
    pairs = contradictions[:limit]
    resolved = failed = 0
    results = []

    # Open ONE session and resolve all pairs through it. Previously each
    # auto_resolve_contradiction_pair opened its own DB session, re-running
    # run_schema_setup + saga crash recovery per pair; with an unindexed
    # saga_log that scan cost ~3s per open, so 50 pairs (~700s) blew past
    # the background worker's 300s timeout and wedged it in a respawn loop.
    # Also enable global scope: detection runs cross-tenant when --tenant
    # is unset, so the resolver must see all tenants too — otherwise
    # non-default-tenant notes error with "note(s) not found".
    from contextlib import nullcontext
    from infra.db import connection_pool, set_include_global

    shared_session = False
    if args.dry_run:
        session_cm = nullcontext(None)
        shared_conn = None
    else:
        set_include_global(True)
        try:
            # Use the pool's thread-keyed connection instead of open_db():
            # open_db() takes the per-DB-path flock for the whole shared
            # session, which starved the kernel's queue drain + audit flush
            # for the run's duration. Pooled connections are flock-free —
            # writes via conn=shared_conn bypass the lock entirely, so the
            # flock on the shared session was vestigial.
            shared_conn = connection_pool.get(str(db))
            shared_session = True
        except Exception as e:
            print(f"contradiction_resolver: failed to open shared session, falling back per-pair: {e}", file=sys.stderr)
            shared_conn = None
            set_include_global(False)

    try:
        for c in pairs:
            src = c.get("source", "")
            tgt = c.get("target", "")
            if not src or not tgt or src == tgt:
                continue
            if args.dry_run:
                results.append({"source": src, "target": tgt, "action": "dry_run", "confidence": c.get("confidence")})
                resolved += 1
                continue
            try:
                result = auto_resolve_contradiction_pair(db, src, tgt, conn=shared_conn)
                action = result.get("action", "unknown")
                results.append({"source": src, "target": tgt, "action": action, "strategy": result.get("strategy")})
                if action not in ("error",):
                    resolved += 1
                else:
                    failed += 1
            except Exception as e:
                logger.warning("main failed: %s", e)
                failed += 1
                results.append({"source": src, "target": tgt, "action": "error", "error": str(e)})
    finally:
        if shared_session:
            try:
                connection_pool.put(shared_conn)
            except Exception as e:
                logger.warning("shared session close failed: %s", e)
        if not args.dry_run:
            set_include_global(False)

    output = {
        "scanned": len(pairs),
        "resolved": resolved,
        "failed": failed,
        "dry_run": args.dry_run,
        "tasks_created": tasks_created,
        "results": results[:10],
    }
    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
