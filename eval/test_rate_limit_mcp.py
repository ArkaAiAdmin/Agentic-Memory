"""Tests for BLK-5: rate limiting folded into with_audit decorator.

Covers:
  1. with_audit returns RATE_LIMITED after 60 rapid calls.
  2. rate_limit_check resets after window elapses.
  3. Different tools have independent rate limit buckets.
"""

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))


class TestWithAuditRateLimit(unittest.TestCase):
    """with_audit applies per-tool rate limiting automatically."""

    def test_burst_returns_rate_limited(self):
        """61 rapid calls to the same tool => 61st returns RATE_LIMITED."""
        import memory_common

        limiter = memory_common.get_default_limiter()
        limiter.reset("test_burst_tool")

        from memory_mcp import with_audit

        call_count = 0

        @with_audit("test_burst_tool")
        def dummy_tool():
            nonlocal call_count
            call_count += 1
            return "ok"

        results = []
        for _ in range(61):
            results.append(dummy_tool())

        # First 60 should succeed, 61st should be RATE_LIMITED
        self.assertEqual(len(results), 61)
        self.assertTrue(all(r == "ok" for r in results[:60]))
        self.assertIn("RATE_LIMITED", results[60])

        limiter.reset("test_burst_tool")

    def test_different_tools_independent_buckets(self):
        """Rate limit is per-tool, not global."""
        import memory_common

        limiter = memory_common.get_default_limiter()
        limiter.reset("tool_a")
        limiter.reset("tool_b")

        from memory_mcp import with_audit

        @with_audit("tool_a")
        def tool_a():
            return "a"

        @with_audit("tool_b")
        def tool_b():
            return "b"

        # Exhaust tool_a
        for _ in range(60):
            tool_a()

        # tool_a should be limited
        self.assertIn("RATE_LIMITED", tool_a())

        # tool_b should still work
        self.assertEqual(tool_b(), "b")

        limiter.reset("tool_a")
        limiter.reset("tool_b")

    def test_rate_limit_check_resets_after_window(self):
        """Rate limit resets after the window passes."""
        import memory_common

        # Create a short-window limiter for testing
        short_limiter = memory_common.RateLimiter(max_calls=3, window_seconds=0.1)
        self.assertTrue(short_limiter.check("reset_test"))
        self.assertTrue(short_limiter.check("reset_test"))
        self.assertTrue(short_limiter.check("reset_test"))
        self.assertFalse(short_limiter.check("reset_test"))
        # Wait for the 0.1s window to elapse. We can't use wait_until polling
        # rl.check() because every check consumes a slot. A short fixed sleep
        # is the right tool here.
        time.sleep(0.15)
        self.assertTrue(short_limiter.check("reset_test"))


if __name__ == "__main__":
    unittest.main()
