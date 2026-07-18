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


_DEFAULT_DB = os.environ.get("MEMORY_DB_PATH", "memory/memory.db")
_DEFAULT_LIMIT = 50


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Resolve contradictions cron")
    parser.add_argument("--db", default=_DEFAULT_DB, help="Path to memory.db")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writes")
    parser.add_argument("--limit", type=int, default=_DEFAULT_LIMIT, help="Max pairs to process")
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
        contradictions = detect_contradictions(mem_dir, min_confidence="low")
    except Exception as e:
        print(f"contradiction_resolver: detection failed: {e}", file=sys.stderr)
        return 0

    if not contradictions:
        print(json.dumps({"scanned": 0, "resolved": 0, "message": "no contradictions detected"}))
        return 0

    limit = args.limit
    pairs = contradictions[:limit]
    resolved = failed = 0
    results = []
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
            result = auto_resolve_contradiction_pair(db, src, tgt)
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

    output = {
        "scanned": len(pairs),
        "resolved": resolved,
        "failed": failed,
        "dry_run": args.dry_run,
        "results": results[:10],
    }
    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
