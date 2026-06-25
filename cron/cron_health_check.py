#!/usr/bin/env python3
"""Cron wrapper: system health check — FTS drift, KG orphans, circuit breaker,
auto-save health, and semantic search health. Writes a status JSON to
memory/.health_status.json and prints a summary.

This implements Phase 1 of the reliability plan:
  Rule #9 (FTS drift)   → checked here
  Rule #10 (KG orphans)  → checked here
  Rule #11 (circuit breaker) → checked here
  Rule #5 (auto_save_status) → checked here

Inspired by cron_integrity_check.py pattern.
"""

import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("MEMORY_KNOWLEDGE_GRAPH", "1")
os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory_common import GLOBAL_MEM_DIR
from infrastructure import resolve_active_memory_dir
from memory_integrity import check_index_integrity, find_kg_orphans

# Minimal lock — don't overlap with other health checks
try:
    from _flock import acquire_lock_or_exit
except ImportError:

    def acquire_lock_or_exit(name):  # type: ignore[misc]
        pass


def _check_circuit_breaker() -> dict:
    """Query the circuit breaker state via the MCP tool."""
    try:
        # Lazy import to avoid startup cost
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from mcp_audit import memory_circuit_breaker_status

        result = memory_circuit_breaker_status(limit=5)
        # Parse the result string
        try:
            data = json.loads(result)
            return {
                "status": "ok",
                "circuit_breaker_open": data.get("circuit_breaker_open", False),
                "recent_events": data.get("recent_events", []),
                "open_since": data.get("open_since"),
            }
        except (json.JSONDecodeError, AttributeError):
            return {"status": "ok", "raw": str(result)[:500]}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def _check_auto_save_health() -> dict:
    """Check auto-save pipeline health."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from auto_save import health_check

        return health_check(minutes=30)
    except Exception as e:
        return {"status": "error", "message": str(e)}


def _check_semantic_search() -> dict:
    """Quick check that semantic search (usearch) is functional."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from search.orchestrator import search_memories

        db_path = GLOBAL_MEM_DIR / "memory.db"
        if not db_path.exists():
            return {"status": "skipped", "reason": "no_db"}

        result = search_memories(
            db_path=db_path,
            query="health check probe",
            limit=1,
            include_global=False,
        )
        return {
            "status": "ok",
            "results_count": len(result.get("results", [])),
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


def main() -> int:
    if "--help" in sys.argv[1:] or "-h" in sys.argv[1:]:
        print("usage: %s [-h|--help]" % sys.argv[0], file=sys.stderr)
        print(
            "Cron job — runs system health checks and writes status to .health_status.json",
            file=sys.stderr,
        )
        sys.exit(0)

    env = os.environ.get("MEMORY_DB_PATH")
    db_path = Path(env) if env else resolve_active_memory_dir() / "memory.db"
    if not db_path.exists():
        print(f"ERROR: no memory.db at {db_path}")
        return 1

    report: dict = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "db_path": str(db_path),
        "checks": {},
        "alerts": [],
        "overall_healthy": True,
    }

    # 1. FTS / index integrity
    try:
        idx = check_index_integrity(db_path, deep=False)
        critical = [f for f in idx["findings"] if f["severity"] == "critical"]
        warnings = [f for f in idx["findings"] if f["severity"] == "warning"]
        report["checks"]["index_integrity"] = {
            "status": "ok" if not critical else "critical",
            "summary": idx["summary"],
            "critical_count": len(critical),
            "warning_count": len(warnings),
        }
        if critical:
            report["overall_healthy"] = False
            for c in critical[:5]:
                report["alerts"].append(
                    f"[CRITICAL index] {c.get('check', c.get('code', '?'))}: {c['message']}"
                )
        if warnings:
            for w in warnings[:3]:
                report["alerts"].append(
                    f"[WARNING index] {w.get('check', w.get('code', '?'))}: {w['message']}"
                )
    except Exception as e:
        report["checks"]["index_integrity"] = {"status": "error", "message": str(e)}
        report["alerts"].append(f"[ERROR index_integrity] {e}")
        report["overall_healthy"] = False

    # 2. KG orphans
    try:
        import sqlite3 as _sqlite3

        with _sqlite3.connect(str(db_path)) as conn:
            orphans = find_kg_orphans(conn)
        orphan_counts = {k: len(v) for k, v in orphans.items()}
        total_orphans = sum(orphan_counts.values())
        report["checks"]["kg_orphans"] = {
            "status": "ok" if total_orphans == 0 else "warning",
            "counts": orphan_counts,
            "total": total_orphans,
        }
        if total_orphans > 0:
            report["alerts"].append(
                f"[WARNING kg_orphans] {total_orphans} orphan KG entries "
                f"(kg_edges={orphan_counts.get('kg_edges', 0)}, "
                f"kg_entities={orphan_counts.get('kg_entities', 0)}, "
                f"backlinks={orphan_counts.get('backlinks', 0)})"
            )
    except Exception as e:
        report["checks"]["kg_orphans"] = {"status": "error", "message": str(e)}
        report["alerts"].append(f"[ERROR kg_orphans] {e}")

    # 3. Circuit breaker
    cb = _check_circuit_breaker()
    report["checks"]["circuit_breaker"] = cb
    if cb.get("circuit_breaker_open"):
        report["overall_healthy"] = False
        report["alerts"].append(
            f"[CRITICAL circuit_breaker] Open since {cb.get('open_since', 'unknown')}"
        )

    # 4. Auto-save health
    ash = _check_auto_save_health()
    report["checks"]["auto_save"] = ash
    if not ash.get("healthy", True):
        report["alerts"].append(
            f"[WARNING auto_save] healthy=False, recent_autos={ash.get('auto_save_recent', '?')}, "
            f"db_error={ash.get('db_error', 'none')}"
        )

    # 5. Semantic search probe
    ss = _check_semantic_search()
    report["checks"]["semantic_search"] = ss
    if ss.get("status") == "error":
        report["alerts"].append(
            f"[WARNING semantic_search] {ss.get('message', 'unknown')}"
        )

    # Write status file
    status_path = GLOBAL_MEM_DIR / ".health_status.json"
    try:
        status_path.write_text(json.dumps(report, indent=2))
    except OSError:
        pass

    # Print summary
    print(f"Health check: {'HEALTHY' if report['overall_healthy'] else 'UNHEALTHY'}")
    print(f"  DB: {report['db_path']}")
    for check_name, check_data in report["checks"].items():
        status = check_data.get("status", "?")
        print(f"  {check_name}: {status}")
    if report["alerts"]:
        print(f"  Alerts ({len(report['alerts'])}):")
        for a in report["alerts"][:10]:
            print(f"    - {a}")
    else:
        print("  No alerts.")

    acquire_lock_or_exit("cron_health_check")
    return 0 if report["overall_healthy"] else 1


if __name__ == "__main__":
    sys.exit(main())
