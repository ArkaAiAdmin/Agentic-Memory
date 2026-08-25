#!/usr/bin/env python3
"""Pin the overall-status predicate for memory_health_check.

2026-08-25 incident: 1,318 journal dead letters + a stale background worker
were reported as ``overall: healthy`` for three days because the predicate
only inspected db/vec/disk. These tests lock the journal-aware behavior:
dead-letter backlogs and unattended pending queues must degrade the status,
and every degradation must carry a machine-readable reason.
"""

import sys
import unittest
from pathlib import Path

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))


def _healthy_status() -> dict:
    return {
        "db": {"accessible": True},
        "vec_index": {"drift": 0},
        "disk": {"pct_used": 50.0},
        "journal": {"failed": 0, "pending": 0},
        "worker": {"alive": True},
    }


class TestComputeOverallStatus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Import lazily so the heavy mcp tool-registration imports stay out
        # of collection; the predicate itself is a pure function.
        from mcp_surface.mcp_maintenance import _compute_overall_status

        cls.fn = staticmethod(_compute_overall_status)

    def test_all_green_is_healthy(self):
        overall, reasons = self.fn(_healthy_status(), drift_threshold=50, disk_threshold=95)
        self.assertEqual(overall, "healthy")
        self.assertEqual(reasons, [])

    def test_dead_letter_backlog_degrades_with_reason(self):
        status = _healthy_status()
        status["journal"]["failed"] = 1318
        overall, reasons = self.fn(status, drift_threshold=50, disk_threshold=95)
        self.assertEqual(overall, "degraded")
        self.assertTrue(any(r.startswith("journal_failed") for r in reasons), reasons)

    def test_small_dead_letter_count_stays_healthy(self):
        status = _healthy_status()
        status["journal"]["failed"] = 9
        overall, _ = self.fn(status, drift_threshold=50, disk_threshold=95)
        self.assertEqual(overall, "healthy")

    def test_unattended_pending_queue_with_dead_worker_degrades(self):
        status = _healthy_status()
        status["journal"]["pending"] = 500
        status["worker"]["alive"] = False
        overall, reasons = self.fn(status, drift_threshold=50, disk_threshold=95)
        self.assertEqual(overall, "degraded")
        self.assertTrue(any("unattended" in r for r in reasons), reasons)

    def test_pending_with_live_worker_does_not_degrade(self):
        status = _healthy_status()
        status["journal"]["pending"] = 500
        status["worker"]["alive"] = True
        overall, _ = self.fn(status, drift_threshold=50, disk_threshold=95)
        self.assertEqual(overall, "healthy")

    def test_legacy_conditions_still_degrade(self):
        for mutate in (
            lambda s: s["db"].__setitem__("accessible", False),
            lambda s: s["vec_index"].__setitem__("drift", 99),
            lambda s: s["disk"].__setitem__("pct_used", 97.0),
        ):
            status = _healthy_status()
            mutate(status)
            overall, reasons = self.fn(status, drift_threshold=50, disk_threshold=95)
            self.assertEqual(overall, "degraded", msg=reasons)
            self.assertTrue(reasons)


if __name__ == "__main__":
    unittest.main()
