#!/usr/bin/env python3
"""Prometheus metrics endpoint for Agentic Memory.

Exposes system metrics on port 9464 (Prometheus default for process exporters)
so Grafana or any Prometheus-compatible scraper can ingest them.

Usage:
    # Start the metrics server (daemonize or run in a terminal):
    venv/bin/python metrics_server.py

    # Scrape endpoint:
    curl http://localhost:9464/metrics

    # Query with PromQL (via Grafana):
    memory_notes_total
    memory_embedding_count
    memory_audit_operations_total
"""

import logging
logger = logging.getLogger(__name__)

import argparse
import json
import os
import sys
import sqlite3
from contextlib import closing
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from infra.infrastructure import resolve_active_memory_dir


def collect_gauges(db_path: Path) -> str:
    """Return Prometheus exposition-format text."""
    lines = [
        "# HELP memory_notes_total Total memory notes.",
        "# TYPE memory_notes_total gauge",
        "# HELP memory_pinned_notes Count of pinned notes.",
        "# TYPE memory_pinned_notes gauge",
        "# HELP memory_embedding_count Number of stored embedding vectors.",
        "# TYPE memory_embedding_count gauge",
        "# HELP memory_entity_count Knowledge graph entity count.",
        "# TYPE memory_entity_count gauge",
        "# HELP memory_edge_count Knowledge graph edge count.",
        "# TYPE memory_edge_count gauge",
        "# HELP memory_chunk_count Total memory chunks.",
        "# TYPE memory_chunk_count gauge",
        "# HELP memory_ctr_events_total CTR feedback event count.",
        "# TYPE memory_ctr_events_total gauge",
        "# HELP memory_drift_events_total Concept drift event count.",
        "# TYPE memory_drift_events_total gauge",
        "# HELP memory_audit_operations_total Total MCP tool calls.",
        "# TYPE memory_audit_operations_total gauge",
        "# HELP memory_audit_errors_total Total MCP tool errors.",
        "# TYPE memory_audit_errors_total gauge",
        "# HELP memory_db_size_bytes SQLite database file size.",
        "# TYPE memory_db_size_bytes gauge",
        "# HELP memory_up 1 if the scraper can reach the DB, else 0.",
        "# TYPE memory_up gauge",
    ]

    up = 1
    try:
        with closing(sqlite3.connect(str(db_path), timeout=5)) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            # Tenant scoping (GAP 10): when MEMORY_METRICS_TENANT is set,
            # metrics report only that tenant instead of a cross-tenant
            # aggregate. The value is operator-configured (env), not
            # request-supplied, so interpolation here is safe.
            tenant = os.environ.get("MEMORY_METRICS_TENANT", "").strip()
            tclause = f" WHERE tenant_id = '{tenant}'" if tenant else ""
            # Single query with scalar subqueries replaces 11+ round-trips
            row = conn.execute(f"""
                SELECT
                  (SELECT COUNT(*) FROM memories{tclause}),
                  (SELECT COUNT(*) FROM memories WHERE pinned=1{tclause}),
                  (SELECT COUNT(*) FROM memory_embeddings),
                  (SELECT COUNT(*) FROM kg_entities),
                  (SELECT COUNT(*) FROM kg_edges),
                  (SELECT COUNT(*) FROM memory_chunks),
                  (SELECT COUNT(*) FROM memory_ctr_feedback),
                  (SELECT COUNT(*) FROM concept_drift),
                  (SELECT COUNT(*) FROM memory_audit_log{tclause}),
                  (SELECT COUNT(*) FROM memory_audit_log WHERE error IS NOT NULL{tclause}),
                  (SELECT AVG(latency_ms) FROM memory_audit_log{tclause}),
                  (SELECT MIN(latency_ms) FROM memory_audit_log{tclause}),
                  (SELECT MAX(latency_ms) FROM memory_audit_log{tclause})
            """).fetchone()
            if row:
                cols = [
                    ("memory_notes_total", 0),
                    ("memory_pinned_notes", 1),
                    ("memory_embedding_count", 2),
                    ("memory_entity_count", 3),
                    ("memory_edge_count", 4),
                    ("memory_chunk_count", 5),
                    ("memory_ctr_events_total", 6),
                    ("memory_drift_events_total", 7),
                    ("memory_audit_operations_total", 8),
                    ("memory_audit_errors_total", 9),
                ]
                for name, idx in cols:
                    lines.append(f"{name} {row[idx] or 0}")
                if row[10] is not None:
                    lines.append(f"memory_audit_latency_avg_ms {row[10]:.1f}")
                    lines.append(f"memory_audit_latency_min_ms {row[11]:.1f}")
                    lines.append(f"memory_audit_latency_max_ms {row[12]:.1f}")

            # Tier counts (can't subquery because GROUP BY returns >1 row)
            tiers = conn.execute(
                "SELECT COALESCE(tier,'unassigned'), COUNT(*) "
                "FROM memories GROUP BY tier"
            ).fetchall()
            for t, c in tiers:
                safe = t.replace("-", "_").lower()
                lines.append(f'memory_tier_count{{tier="{safe}"}} {c}')

    except Exception as exc:
        logger.warning("collect_gauges failed: %s", exc)
        up = 0
        lines.append(f"# error: {exc}")

    lines.append(f"memory_up {up}")
    lines.append(
        f"memory_db_size_bytes {db_path.stat().st_size if db_path.exists() else 0}"
    )
    lines.append("# EOF")
    return "\n".join(lines) + "\n"


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            payload = collect_gauges(DB)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.end_headers()
            self.wfile.write(payload.encode("utf-8"))
        elif self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            alive = db_up()
            self.wfile.write(
                json.dumps(
                    {
                        "status": "ok" if alive else "degraded",
                        "db_path": str(DB),
                        "db_exists": DB.exists(),
                    }
                ).encode("utf-8")
            )
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[metrics_server] {args[0]} {args[1]} {args[2]}\n")


def db_up() -> bool:
    try:
        with closing(sqlite3.connect(str(DB), timeout=5)) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("SELECT 1").fetchone()
            return True
    except Exception as e:
        logger.warning("db_up failed: %s", e)
        return False


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Agentic Memory Prometheus exporter")
    p.add_argument("--port", type=int, default=9464, help="HTTP port (default: 9464)")
    args = p.parse_args()

    DB = resolve_active_memory_dir() / "memory.db"
    print(f"[metrics_server] DB: {DB}", file=sys.stderr)
    print(f"[metrics_server] Listening on :{args.port}/metrics", file=sys.stderr)

    server = HTTPServer(("0.0.0.0", args.port), MetricsHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[metrics_server] Shutting down.", file=sys.stderr)
        server.server_close()
