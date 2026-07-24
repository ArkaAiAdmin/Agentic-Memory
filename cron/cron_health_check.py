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
import logging
import os
import shutil
import signal
import sys
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_REPO_ROOT)
sys.path.insert(0, _REPO_ROOT)

from infra.log import setup_logging

logger = setup_logging("cron_health_check")

# Structured logging helper for observability
def _log_structured(level: str | int, event: str, **fields: Any) -> None:
    import json as _json
    if isinstance(level, int):
        level = logging.getLevelName(level).lower()
    log_entry = {"event": event, **fields}
    getattr(logger, level)(_json.dumps(log_entry))

from infra.memory_common import GLOBAL_MEM_DIR
from infra.infrastructure import resolve_active_memory_dir
from memory_integrity import check_index_integrity, find_kg_orphans

# Timeout budget per check (seconds). Total should stay under 90s
# to fit within the 120s cron_task_timeouts with headroom for imports.
_CHECK_TIMEOUTS = {
    "index_integrity": 20,
    "kg_orphans": 15,
    "circuit_breaker": 10,
    "auto_save": 10,
    "semantic_search": 10,
    "task_queue": 10,
}


class _CheckTimeout(Exception):
    pass


def _check_timeout_handler(signum, frame):
    raise _CheckTimeout("check timed out")


def _with_timeout(seconds: int, fn, *args, **kwargs):
    """Run *fn* with a SIGALRM timeout. Returns result or error dict."""
    old_handler = signal.signal(signal.SIGALRM, _check_timeout_handler)
    signal.alarm(seconds)
    try:
        return fn(*args, **kwargs)
    except _CheckTimeout:
        return {"status": "critical", "error": f"Timed out after {seconds}s"}
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


# Minimal lock — non-blocking: skip if another health check holds it
try:
    from _flock import acquire_lock_or_exit
except ImportError:

    def acquire_lock_or_exit(name: str, max_attempts: int = 5) -> None:
        logger.error("cron_health_check: _flock module not available, cannot acquire lock")
        sys.exit(1)


def _try_lock_or_skip(name: str) -> bool:
    """Try to acquire flock; return True if held, False if contended."""
    try:
        from _flock import _lock_path
        lock = _lock_path(name)
        lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o644)
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Keep fd open for the process lifetime (lock auto-releases on exit)
        return True
    except (OSError, BlockingIOError):
        return False


def _check_circuit_breaker() -> dict:
    """Query the circuit breaker state via the MCP tool."""
    try:
        # Lazy import to avoid startup cost
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from mcp_surface.mcp_audit import memory_circuit_breaker_status

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
        logger.warning("_check_circuit_breaker failed: %s", e)
        return {"status": "error", "message": str(e)}


def _check_auto_save_health() -> dict:
    """Check auto-save pipeline health."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from background.auto_save import health_check

        return health_check(minutes=30)
    except Exception as e:
        logger.warning("_check_auto_save_health failed: %s", e)
        return {"status": "error", "message": str(e)}


def _check_semantic_search() -> dict:
    """Quick check that semantic search (usearch) is functional.

    Wrapped in a 10s SIGALRM timeout so a hung embedding/vector probe
    degrades to a 'critical' status instead of blocking the whole check.
    """
    class _Timeout(Exception):
        pass

    def _handler(signum, frame):
        raise _Timeout("Semantic search probe timed out")

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    signal.signal(signal.SIGALRM, _handler)
    signal.alarm(10)
    try:
        try:
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
        except _Timeout:
            return {"status": "critical", "error": "Timed out after 10s"}
        except Exception as e:
            logger.warning("_check_semantic_search failed: %s", e)
            return {"status": "error", "message": str(e)}
    finally:
        signal.alarm(0)


def _check_disk_space(mem_dir: Path) -> dict:
    """Check free disk space on the volume holding the memory directory.

    - free < 1GB  -> critical
    - free < 5GB  -> warning
    - otherwise   -> pass
    """
    try:
        usage = shutil.disk_usage(str(mem_dir))
        free_gb = usage.free / (1024 ** 3)
        total_gb = usage.total / (1024 ** 3)
        used_pct = (usage.used / usage.total) * 100.0 if usage.total else 0.0

        if free_gb < 1.0:
            status = "critical"
        elif free_gb < 5.0:
            status = "warning"
        else:
            status = "pass"

        return {
            "status": status,
            "free_gb": round(free_gb, 2),
            "total_gb": round(total_gb, 2),
            "used_pct": round(used_pct, 2),
        }
    except Exception as e:
        logger.warning("_check_disk_space failed: %s", e)
        return {"status": "error", "message": str(e)}


def main() -> int:
    if "--help" in sys.argv[1:] or "-h" in sys.argv[1:]:
        print("usage: %s [-h|--help]" % sys.argv[0], file=sys.stderr)
        print(
            "Cron job — runs system health checks and writes status to .health_status.json",
            file=sys.stderr,
        )
        sys.exit(0)

    # Non-blocking lock: skip if another health check is already running
    if not _try_lock_or_skip("cron_health_check"):
        print("cron_health_check: another instance holding lock, skipping")
        return 0

    env = os.environ.get("MEMORY_DB_PATH")
    db_path = Path(env) if env else resolve_active_memory_dir() / "memory.db"
    if not db_path.exists():
        print(f"ERROR: no memory.db at {db_path}")
        return 1

    from background.cron_model_lock import cleanup_stale_locks

    cleaned = cleanup_stale_locks()
    if cleaned:
        _log_structured(logging.INFO, "stale_locks_cleaned", count=cleaned)

    report: dict = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "db_path": str(db_path),
        "checks": {},
        "alerts": [],
        "overall_healthy": True,
    }

    # 1. FTS / index integrity (20s budget)
    def _do_index_check():
        idx = check_index_integrity(db_path, deep=False)
        critical = [f for f in idx["findings"] if f["severity"] == "critical"]
        warnings = [f for f in idx["findings"] if f["severity"] == "warning"]
        return {
            "status": "ok" if not critical else "critical",
            "summary": idx["summary"],
            "critical_count": len(critical),
            "warning_count": len(warnings),
            "_critical": critical,
            "_warnings": warnings,
        }

    try:
        idx = _with_timeout(_CHECK_TIMEOUTS["index_integrity"], _do_index_check)
        critical = idx.pop("_critical", [])
        warnings = idx.pop("_warnings", [])
        status = idx.get("status", "error")
        report["checks"]["index_integrity"] = idx
        _log_structured(logging.INFO if status == "ok" else logging.WARNING, "check_index_integrity", status=status, critical=len(critical), warnings=len(warnings))
        if critical:
            report["overall_healthy"] = False
            for c in critical[:5]:
                report["alerts"].append(
                    f"[CRITICAL index] {c.get('check', c.get('code', '?'))}: {c['message']}"
                )
                from infra.alert import alert

                alert("critical", "Health check: index integrity", c['message'])
        if warnings:
            for w in warnings[:3]:
                report["alerts"].append(
                    f"[WARNING index] {w.get('check', w.get('code', '?'))}: {w['message']}"
                )
    except Exception as e:
        _log_structured(logging.WARNING, "check_failed", check="index_integrity", error=str(e))
        report["checks"]["index_integrity"] = {"status": "error", "message": str(e)}
        report["alerts"].append(f"[ERROR index_integrity] {e}")
        report["overall_healthy"] = False

    # 2. KG orphans (15s budget)
    def _do_kg_check():
        import sqlite3 as _sqlite3

        with _sqlite3.connect(str(db_path)) as conn:
            orphans = find_kg_orphans(conn)
        orphan_counts = {k: len(v) for k, v in orphans.items()}
        total_orphans = sum(orphan_counts.values())
        return {
            "status": "ok" if total_orphans == 0 else "warning",
            "counts": orphan_counts,
            "total": total_orphans,
        }

    try:
        kg = _with_timeout(_CHECK_TIMEOUTS["kg_orphans"], _do_kg_check)
        report["checks"]["kg_orphans"] = kg
        total_orphans = kg.get("total", 0)
        orphan_counts = kg.get("counts", {})
        _log_structured(logging.INFO if total_orphans == 0 else logging.WARNING, "check_kg_orphans", status=kg.get("status"), total=total_orphans, counts=orphan_counts)
        if total_orphans > 0:
            report["alerts"].append(
                f"[WARNING kg_orphans] {total_orphans} orphan KG entries "
                f"(kg_edges={orphan_counts.get('kg_edges', 0)}, "
                f"kg_entities={orphan_counts.get('kg_entities', 0)}, "
                f"backlinks={orphan_counts.get('backlinks', 0)})"
            )
    except Exception as e:
        _log_structured(logging.WARNING, "check_failed", check="kg_orphans", error=str(e))
        report["checks"]["kg_orphans"] = {"status": "error", "message": str(e)}
        report["alerts"].append(f"[ERROR kg_orphans] {e}")

    # 3. Circuit breaker (10s budget)
    cb = _with_timeout(_CHECK_TIMEOUTS["circuit_breaker"], _check_circuit_breaker)
    report["checks"]["circuit_breaker"] = cb
    cb_open = cb.get("circuit_breaker_open", False)
    _log_structured(logging.INFO if not cb_open else logging.WARNING, "check_circuit_breaker", circuit_breaker_open=cb_open, open_since=cb.get("open_since"))
    if cb_open:
        report["overall_healthy"] = False
        report["alerts"].append(
            f"[CRITICAL circuit_breaker] Open since {cb.get('open_since', 'unknown')}"
        )
        from infra.alert import alert

        alert(
            "critical",
            "Health check: circuit breaker open",
            f"Open since {cb.get('open_since', 'unknown')}",
        )

    # 4. Auto-save health (10s budget)
    ash = _with_timeout(_CHECK_TIMEOUTS["auto_save"], _check_auto_save_health)
    report["checks"]["auto_save"] = ash
    ash_healthy = ash.get("healthy", True)
    _log_structured(logging.INFO if ash_healthy else logging.WARNING, "check_auto_save", healthy=ash_healthy, recent_autos=ash.get("auto_save_recent"))
    if not ash_healthy:
        report["alerts"].append(
            f"[WARNING auto_save] healthy=False, recent_autos={ash.get('auto_save_recent', '?')}, "
            f"db_error={ash.get('db_error', 'none')}"
        )

    # 5. Task queue watchdog (10s budget)
    def _do_task_queue_check():
        from background.background_queue import reset_stuck_processing_tasks
        import sqlite3 as _sqlite3_q

        with _sqlite3_q.connect(str(db_path)) as _qconn:
            _qconn.row_factory = _sqlite3_q.Row
            _stuck = reset_stuck_processing_tasks(_qconn, max_age_minutes=10)
        return {"status": "ok" if _stuck == 0 else "warning", "stuck_reset": _stuck}

    try:
        tq = _with_timeout(_CHECK_TIMEOUTS["task_queue"], _do_task_queue_check)
        report["checks"]["task_queue"] = tq
        _stuck = tq.get("stuck_reset", 0)
        _log_structured(logging.INFO if tq["status"] == "ok" else logging.WARNING, "check_task_queue", status=tq["status"], stuck_reset=_stuck)
        if _stuck:
            report["alerts"].append(
                f"[WARNING task_queue] reset {_stuck} stuck processing tasks"
            )
    except Exception as _tq_err:
        _log_structured(logging.WARNING, "check_failed", check="task_queue", error=str(_tq_err))
        report["checks"]["task_queue"] = {"status": "error", "message": str(_tq_err)}
        report["alerts"].append(f"[ERROR task_queue] {_tq_err}")

    # 6. Semantic search probe (10s budget — already has internal SIGALRM)
    ss = _with_timeout(_CHECK_TIMEOUTS["semantic_search"], _check_semantic_search)
    report["checks"]["semantic_search"] = ss
    ss_status = ss.get("status", "error")
    _log_structured(logging.INFO if ss_status == "ok" else logging.WARNING, "check_semantic_search", status=ss_status, results_count=ss.get("results_count"))
    if ss_status == "error":
        report["alerts"].append(
            f"[WARNING semantic_search] {ss.get('message', 'unknown')}"
        )

    # 7. Disk space (fast, no timeout needed)
    ds = _check_disk_space(db_path.parent)
    report["checks"]["disk_space"] = ds
    ds_status = ds.get("status", "error")
    _log_structured(
        logging.INFO if ds_status == "pass" else logging.WARNING,
        "check_disk_space",
        status=ds_status,
        free_gb=ds.get("free_gb"),
        used_pct=ds.get("used_pct"),
    )
    if ds_status == "critical":
        report["overall_healthy"] = False
        report["alerts"].append(
            f"[CRITICAL disk_space] only {ds.get('free_gb')}GB free on {db_path.parent}"
        )
        from infra.alert import alert

        alert(
            "critical",
            "Health check: disk space critical",
            f"only {ds.get('free_gb')}GB free on {db_path.parent}",
        )
    elif ds_status == "warning":
        report["alerts"].append(
            f"[WARNING disk_space] only {ds.get('free_gb')}GB free on {db_path.parent}"
        )

    # Write status file
    status_path = GLOBAL_MEM_DIR / ".health_status.json"
    try:
        status_path.write_text(json.dumps(report, indent=2))
    except OSError as exc:
        logger.warning("cron_health_check: cannot write health status file: %s", exc)

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

    if not report["overall_healthy"]:
        from infra.alert import alert

        alert("error", "System health degraded", "; ".join(report["alerts"][:5]))

    return 0 if report["overall_healthy"] else 1


if __name__ == "__main__":
    sys.exit(main())
