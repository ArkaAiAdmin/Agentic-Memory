#!/usr/bin/env python3
"""Metrics collection and Prometheus export for Agentic Memory.

Tracks: save/search/delete latency, error rates, throughput.
Data sources:
  - memory_audit_log table (historical, from MCP tools)
  - In-process counters (live session counters)

Usage:
    # View stats
    venv/bin/python metrics.py

    # Export Prometheus format
    venv/bin/python metrics.py --prometheus

    # Reset counters
    venv/bin/python metrics.py --reset
"""
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from infra.memory_common import open_db


class _RuntimeCounters:
    """Thread-safe in-process counters for live metrics."""

    def __init__(self):
        self._lock = threading.Lock()
        self._counters: dict[str, int] = defaultdict(int)
        self._latencies: dict[str, list[float]] = defaultdict(list)
        self._start_time = time.time()

    def inc(self, name: str, n: int = 1):
        with self._lock:
            self._counters[name] += n

    def record(self, name: str, latency_ms: float):
        with self._lock:
            self._counters[f"{name}_total"] += 1
            self._latencies[name].append(latency_ms)
            # Keep only last 1000 latencies per metric
            if len(self._latencies[name]) > 1000:
                self._latencies[name] = self._latencies[name][-1000:]

    def snapshot(self) -> dict:
        with self._lock:
            uptime = time.time() - self._start_time
            counters = dict(self._counters)
            histograms = {}
            for name, lats in self._latencies.items():
                if lats:
                    sorted_lats = sorted(lats)
                    n = len(sorted_lats)
                    histograms[name] = {
                        "count": n,
                        "avg_ms": round(sum(lats) / n, 2),
                        "p50_ms": round(sorted_lats[n // 2], 2),
                        "p95_ms": round(sorted_lats[int(n * 0.95)], 2),
                        "p99_ms": round(sorted_lats[int(n * 0.99)], 2),
                        "max_ms": round(max(lats), 2),
                    }
            return {"uptime_seconds": round(uptime, 1), "counters": counters, "histograms": histograms}

    def reset(self):
        with self._lock:
            self._counters.clear()
            self._latencies.clear()
            self._start_time = time.time()


_runtime = _RuntimeCounters()


def record_event(name: str, latency_ms: float = 0, error: bool = False):
    """Record a runtime event. Called from MCP tools / search / save."""
    _runtime.inc("events_total")
    _runtime.inc(f"events_{name}")
    if error:
        _runtime.inc("errors_total")
        _runtime.inc(f"errors_{name}")
    if latency_ms > 0:
        _runtime.record(name, latency_ms)


def get_stats(db_path: Path | None = None) -> dict:
    """Aggregate metrics from audit log."""
    if db_path is None:
        from infra.infrastructure import resolve_active_memory_dir
        db_path = resolve_active_memory_dir() / "memory.db"

    with open_db(db_path, timeout=10.0) as conn:
        stats = {}

        rows = conn.execute("""
            SELECT tool,
                   COUNT(*) as total,
                   AVG(latency_ms) as avg_latency,
                   MAX(latency_ms) as max_latency,
                   MIN(latency_ms) as min_latency,
                   SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) as errors
            FROM memory_audit_log
            GROUP BY tool
        """).fetchall()

        for tool, total, avg_lat, max_lat, min_lat, errors in rows:
            stats[tool] = {
                "total": total,
                "avg_latency_ms": round(avg_lat, 2) if avg_lat else 0,
                "max_latency_ms": round(max_lat, 2) if max_lat else 0,
                "min_latency_ms": round(min_lat, 2) if min_lat else 0,
                "errors": errors,
                "error_rate": round(errors / total * 100, 2) if total > 0 else 0,
            }

        time_range_row = conn.execute("""
            SELECT MIN(ts), MAX(ts) FROM memory_audit_log
        """).fetchone()

        if time_range_row is not None and time_range_row[0] and time_range_row[1]:
            window_seconds = time_range_row[1] - time_range_row[0]
            total_ops = sum(s["total"] for s in stats.values())
            stats["_summary"] = {
                "total_operations": total_ops,
                "window_seconds": round(window_seconds, 1),
                "throughput_ops_per_sec": round(total_ops / window_seconds, 4) if window_seconds > 0 else 0,
                "total_errors": sum(s["errors"] for s in stats.values()),
                "overall_error_rate": round(
                    sum(s["errors"] for s in stats.values()) / total_ops * 100, 2
                ) if total_ops > 0 else 0,
                "first_event": time_range_row[0],
                "last_event": time_range_row[1],
            }
        else:
            stats["_summary"] = {"total_operations": 0, "total_errors": 0}

        db_stats = {}
        for table in ["memories", "memories_fts", "memory_embeddings", "kg_entities", "kg_edges"]:
            try:
                count_row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                count = int(count_row[0]) if count_row is not None else 0
                db_stats[table] = count
            except Exception:
                db_stats[table] = -1
        stats["_db"] = db_stats

        return stats


def format_prometheus(stats: dict) -> str:
    """Export stats in Prometheus text format."""
    lines = []

    # Runtime counters (in-process)
    rt = _runtime.snapshot()
    if rt["counters"]:
        lines.append("# HELP memory_runtime_events_total Runtime event counters")
        lines.append("# TYPE memory_runtime_events_total counter")
        for k, v in sorted(rt["counters"].items()):
            if k.endswith("_total"):
                lines.append(f'memory_runtime_events_total{{event="{k.replace("_total", "")}"}} {v}')

    if rt["histograms"]:
        lines.append("")
        lines.append("# HELP memory_runtime_latency_ms Runtime latency percentiles")
        lines.append("# TYPE memory_runtime_latency_ms gauge")
        for name, h in sorted(rt["histograms"].items()):
            lines.append(f'memory_runtime_latency_ms{{op="{name}",percentile="avg"}} {h["avg_ms"]}')
            lines.append(f'memory_runtime_latency_ms{{op="{name}",percentile="p50"}} {h["p50_ms"]}')
            lines.append(f'memory_runtime_latency_ms{{op="{name}",percentile="p95"}} {h["p95_ms"]}')
            lines.append(f'memory_runtime_latency_ms{{op="{name}",percentile="p99"}} {h["p99_ms"]}')

    lines.append("")
    lines.append("# HELP memory_uptime_seconds Process uptime")
    lines.append("# TYPE memory_uptime_seconds gauge")
    lines.append(f"memory_uptime_seconds {rt['uptime_seconds']}")

    # Audit-log stats
    lines.append("")
    lines.append("# HELP memory_operations_total Total operations by tool")
    lines.append("# TYPE memory_operations_total counter")
    for tool, data in stats.items():
        if tool.startswith("_"):
            continue
        lines.append(f'memory_operations_total{{tool="{tool}"}} {data["total"]}')

    lines.append("")
    lines.append("# HELP memory_latency_ms Average latency by tool")
    lines.append("# TYPE memory_latency_ms gauge")
    for tool, data in stats.items():
        if tool.startswith("_"):
            continue
        lines.append(f'memory_latency_ms{{tool="{tool}"}} {data["avg_latency_ms"]}')

    lines.append("")
    lines.append("# HELP memory_errors_total Total errors by tool")
    lines.append("# TYPE memory_errors_total counter")
    for tool, data in stats.items():
        if tool.startswith("_"):
            continue
        lines.append(f'memory_errors_total{{tool="{tool}"}} {data["errors"]}')

    lines.append("")
    lines.append("# HELP memory_error_rate Error rate percentage by tool")
    lines.append("# TYPE memory_error_rate gauge")
    for tool, data in stats.items():
        if tool.startswith("_"):
            continue
        lines.append(f'memory_error_rate{{tool="{tool}"}} {data["error_rate"]}')

    summary = stats.get("_summary", {})
    lines.append("")
    lines.append("# HELP memory_total_operations Total operations across all tools")
    lines.append("# TYPE memory_total_operations counter")
    lines.append(f"memory_total_operations {summary.get('total_operations', 0)}")

    lines.append("")
    lines.append("# HELP memory_throughput_ops_per_sec Operations per second")
    lines.append("# TYPE memory_throughput_ops_per_sec gauge")
    lines.append(f"memory_throughput_ops_per_sec {summary.get('throughput_ops_per_sec', 0)}")

    db = stats.get("_db", {})
    lines.append("")
    lines.append("# HELP memory_count Number of records by table")
    lines.append("# TYPE memory_count gauge")
    for table, count in db.items():
        lines.append(f'memory_count{{table="{table}"}} {count}')

    return "\n".join(lines) + "\n"


def format_text(stats: dict) -> str:
    """Format stats as human-readable text."""
    lines = []
    lines.append("=== Agentic Memory Metrics ===\n")

    # Runtime counters
    rt = _runtime.snapshot()
    lines.append(f"Uptime: {rt['uptime_seconds']}s")
    if rt["counters"]:
        lines.append("\n--- Runtime Counters ---")
        for k, v in sorted(rt["counters"].items()):
            lines.append(f"  {k}: {v}")
    if rt["histograms"]:
        lines.append("\n--- Runtime Latency ---")
        for name, h in sorted(rt["histograms"].items()):
            lines.append(f"  {name}: avg={h['avg_ms']}ms p50={h['p50_ms']}ms p95={h['p95_ms']}ms p99={h['p99_ms']}ms max={h['max_ms']}ms (n={h['count']})")

    summary = stats.get("_summary", {})
    lines.append("\n--- Audit Log Stats ---")
    lines.append(f"Total operations: {summary.get('total_operations', 0)}")
    lines.append(f"Total errors: {summary.get('total_errors', 0)}")
    lines.append(f"Overall error rate: {summary.get('overall_error_rate', 0)}%")
    lines.append(f"Throughput: {summary.get('throughput_ops_per_sec', 0)} ops/sec")

    lines.append("\n--- Per-Tool Stats ---")
    for tool, data in sorted(stats.items()):
        if tool.startswith("_"):
            continue
        lines.append(f"\n  {tool}:")
        lines.append(f"    Total:      {data['total']}")
        lines.append(f"    Avg Latency: {data['avg_latency_ms']}ms")
        lines.append(f"    Max Latency: {data['max_latency_ms']}ms")
        lines.append(f"    Errors:     {data['errors']} ({data['error_rate']}%)")

    db = stats.get("_db", {})
    lines.append("\n--- Database Stats ---")
    for table, count in db.items():
        lines.append(f"  {table}: {count:,}")

    return "\n".join(lines)


def main():
    if "--prometheus" in sys.argv:
        stats = get_stats()
        print(format_prometheus(stats))
    elif "--runtime" in sys.argv:
        rt = _runtime.snapshot()
        print(f"Uptime: {rt['uptime_seconds']}s")
        for k, v in sorted(rt["counters"].items()):
            print(f"  {k}: {v}")
        for name, h in sorted(rt["histograms"].items()):
            print(f"  {name}: avg={h['avg_ms']}ms p50={h['p50_ms']}ms p95={h['p95_ms']}ms p99={h['p99_ms']}ms")
    elif "--reset" in sys.argv:
        _runtime.reset()
        from infra.infrastructure import resolve_active_memory_dir
        db_path = resolve_active_memory_dir() / "memory.db"
        with open_db(db_path, timeout=10.0) as conn:
            conn.execute("DELETE FROM memory_audit_log")
            conn.commit()
            print("Audit log and runtime counters cleared.")
    else:
        stats = get_stats()
        print(format_text(stats))


if __name__ == "__main__":
    main()
