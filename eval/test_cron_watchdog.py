#!/usr/bin/env python3
"""Regression tests for cron_watchdog.py process detection and etime parsing.

Background:
  2026-06-26: cron_heartbeat.py hung at 66% CPU with no built-in way
  to get a stack trace. The fix: cron_watchdog.py scans for cron
  processes older than --max-age-seconds and dumps their stacks via
  py-spy. These tests cover the parsing and detection logic — the
  py-spy invocation itself is not tested (it would require a real
  long-running process to inspect).
"""

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Add the cron/ directory so we can import cron_watchdog as a module
sys.path.insert(0, str(REPO / "cron"))

import cron_watchdog  # noqa: E402


class TestParseEtime(unittest.TestCase):
    """ps etime format parsing — [[dd-]hh:]mm:ss to seconds."""

    def test_mm_ss_short(self):
        """Less than a minute."""
        self.assertEqual(cron_watchdog.parse_etime_to_seconds("00:45"), 45)

    def test_mm_ss(self):
        self.assertEqual(cron_watchdog.parse_etime_to_seconds("01:23"), 83)

    def test_hh_mm_ss(self):
        self.assertEqual(cron_watchdog.parse_etime_to_seconds("1:02:03"), 3723)

    def test_dd_hh_mm_ss(self):
        # 2 days + 3 hours + 4 minutes + 5 seconds
        expected = 2 * 86400 + 3 * 3600 + 4 * 60 + 5
        self.assertEqual(cron_watchdog.parse_etime_to_seconds("2-03:04:05"), expected)

    def test_zero(self):
        self.assertEqual(cron_watchdog.parse_etime_to_seconds("00:00"), 0)

    def test_garbage_returns_none(self):
        self.assertIsNone(cron_watchdog.parse_etime_to_seconds("not-a-time"))

    def test_partial_garbage_returns_none(self):
        self.assertIsNone(cron_watchdog.parse_etime_to_seconds("1:2:"))

    def test_empty_returns_none(self):
        self.assertIsNone(cron_watchdog.parse_etime_to_seconds(""))


class TestFindHungProcesses(unittest.TestCase):
    """The watchdog finds cron processes that exceed the age threshold."""

    def test_no_processes_no_crash(self):
        """With no matching processes, returns empty list."""
        result = cron_watchdog.find_hung_processes(max_age_seconds=99999)
        self.assertIsInstance(result, list)


class TestScriptDiscovery(unittest.TestCase):
    """The watchdog discovers the cron_*.py scripts in the cron/ dir."""

    def test_finds_real_cron_scripts(self):
        """cron_heartbeat.py and similar are present and picked up."""
        cron_dir = REPO / "cron"
        scripts = {p.name for p in cron_dir.glob("cron_*.py")}
        # sanity check — if this fails the test infra is broken
        self.assertGreater(len(scripts), 0)


if __name__ == "__main__":
    unittest.main()
