"""Prometheus-style audit sink, reusing the metrics_server.py shape.

Maintains in-process counters of forwarded audit events keyed by
``(tool, status)`` and renders them in Prometheus exposition format so a
scraper can ingest them alongside the rest of the metrics emitted by
``infra/metrics_server.py``. The latest ``render()`` output is also kept in
a module-level registry (``PROM_AUDIT_SINK``) that an external exporter can
read without re-instrumenting.

This sink is always available (no config required).
"""

from __future__ import annotations

import logging
import threading


logger = logging.getLogger(__name__)

# Module-level registry of the most recently rendered Prometheus text. An
# external exporter (e.g. metrics_server) can read this without importing the
# sink class. Mirrors the "single source of truth" shape used in
# infra/metrics_server.py.
PROM_AUDIT_SINK_TEXT: str = ""


class PrometheusAuditSink:
    """Accumulate per-tool / per-status counters in Prometheus format."""

    def __init__(self) -> None:
        self._counters: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()

    def emit(self, event: dict) -> None:
        tool = str(event.get("tool", "unknown"))
        status = "error" if event.get("error") else "ok"
        with self._lock:
            key = (tool, status)
            self._counters[key] = self._counters.get(key, 0) + 1
        # Keep the shared registry fresh for external scrapers.
        global PROM_AUDIT_SINK_TEXT
        PROM_AUDIT_SINK_TEXT = self.render()

    def render(self) -> str:
        lines = [
            "# HELP memory_audit_sink_events_total Audit events forwarded to sinks.",
            "# TYPE memory_audit_sink_events_total counter",
        ]
        with self._lock:
            for (tool, status), count in sorted(self._counters.items()):
                lines.append(
                    f'memory_audit_sink_events_total{{tool="{tool}",status="{status}"}} {count}'
                )
        return "\n".join(lines) + "\n"

    def flush(self) -> None:
        return

    def snapshot(self) -> dict[tuple[str, str], int]:
        """Test/dev helper: return a copy of the current counters."""
        with self._lock:
            return dict(self._counters)
