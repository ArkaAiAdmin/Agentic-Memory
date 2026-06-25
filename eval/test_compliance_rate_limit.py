#!/usr/bin/env python3
"""Tests for the pre-push compliance score rate limiter.

Covers memory_common.should_complain_about_score:
  * first call always complains
  * identical score within window does not re-warn
  * score drift beyond threshold re-warns
  * window expiry re-warns
"""

import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path.home() / ".config" / "agentic-memory"))

from memory_common import should_complain_about_score, _compliance_last_warn_path


class TestShouldComplainAboutScore(unittest.TestCase):
    """Unit tests for the rate-limiter state function."""

    def setUp(self):
        self._p = _compliance_last_warn_path()
        self._p.unlink(missing_ok=True)

    def tearDown(self):
        self._p.unlink(missing_ok=True)

    def test_first_call_returns_true(self):
        self.assertTrue(should_complain_about_score(70.0))

    def test_first_call_saves_state(self):
        should_complain_about_score(70.0)
        self.assertTrue(self._p.exists())
        data = json.loads(self._p.read_text())
        self.assertEqual(data["score"], 70.0)
        self.assertIn("ts", data)

    def test_same_score_within_window_returns_false(self):
        with patch("memory_common.time.time", return_value=1000.0):
            should_complain_about_score(70.0)
        with patch("memory_common.time.time", return_value=1000.0 + 3600):
            self.assertFalse(should_complain_about_score(70.0))

    def test_score_drift_exceeds_threshold(self):
        with patch("memory_common.time.time", return_value=1000.0):
            should_complain_about_score(70.0)
        with patch("memory_common.time.time", return_value=2000.0 + 3600):
            # 70 -> 85 = drift 15 > 10 threshold
            self.assertTrue(should_complain_about_score(85.0))

    def test_window_expiry_allows_re_warn(self):
        with patch("memory_common.time.time", return_value=1000.0):
            should_complain_about_score(70.0)
        # Advance 25 hours (> 86400s window)
        with patch("memory_common.time.time", return_value=1000.0 + 25 * 3600):
            self.assertTrue(should_complain_about_score(70.0))

    def test_score_drift_within_threshold_suppressed(self):
        with patch("memory_common.time.time", return_value=1000.0):
            should_complain_about_score(70.0)
        # Advance time but score only drifts by 5 (< 10 threshold)
        with patch("memory_common.time.time", return_value=3600.0):
            self.assertFalse(should_complain_about_score(75.0))

    def test_corrupt_state_file_treated_as_no_prior(self):
        self._p.write_text("not json{{{\n")
        self.assertTrue(should_complain_about_score(70.0))

    def test_short_window_parameter(self):
        with patch("memory_common.time.time", return_value=1000.0):
            should_complain_about_score(70.0)
        # 30-second window: advance 31s
        with patch("memory_common.time.time", return_value=1031.0):
            self.assertTrue(should_complain_about_score(70.0, window_seconds=30.0))

    def test_large_score_change_above_100(self):
        with patch("memory_common.time.time", return_value=1000.0):
            should_complain_about_score(20.0)
        with patch("memory_common.time.time", return_value=2000.0 + 3600):
            self.assertTrue(should_complain_about_score(95.0, change_threshold=50.0))


if __name__ == "__main__":
    unittest.main()
