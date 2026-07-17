"""Tests for BLK-5: rate limiting folded into with_audit decorator.

Covers:
  1. with_audit returns RATE_LIMITED after burst exceeds token bucket.
  2. rate_limit_check resets after tokens refill.
  3. Different tools have independent rate limit buckets.
"""

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))


class TestWithAuditRateLimit(unittest.TestCase):
    """with_audit applies per-tool rate limiting via TokenBucket."""

    def test_burst_returns_rate_limited(self):
        """Rapid calls exceeding burst => RATE_LIMITED."""
        from infra.rate_limiter import configure_rate_limits, reset_rate_limiter

        # Configure a tight limit for testing
        import os
        os.environ["MEMORY_RATE_LIMIT_TEST_BURST_TOOL"] = "60,60"
        configure_rate_limits()
        reset_rate_limiter("test_burst_tool")

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

        # First 60 should succeed (burst=60), 61st should be RATE_LIMITED
        self.assertEqual(len(results), 61)
        self.assertTrue(all(r == "ok" for r in results[:60]))
        self.assertIn("RATE_LIMITED", results[60])

        reset_rate_limiter("test_burst_tool")
        os.environ.pop("MEMORY_RATE_LIMIT_TEST_BURST_TOOL", None)

    def test_different_tools_independent_buckets(self):
        """Rate limit is per-tool, not global."""
        from infra.rate_limiter import configure_rate_limits, reset_rate_limiter

        import os
        os.environ["MEMORY_RATE_LIMIT_TOOL_A"] = "60,60"
        os.environ["MEMORY_RATE_LIMIT_TOOL_B"] = "60,60"
        configure_rate_limits()
        reset_rate_limiter("tool_a")
        reset_rate_limiter("tool_b")

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

        reset_rate_limiter("tool_a")
        reset_rate_limiter("tool_b")
        os.environ.pop("MEMORY_RATE_LIMIT_TOOL_A", None)
        os.environ.pop("MEMORY_RATE_LIMIT_TOOL_B", None)

    def test_rate_limit_check_resets_after_window(self):
        """Token bucket refills after time passes."""
        from infra.rate_limiter import TokenBucket

        # Create a bucket with rate=10 tokens/sec, burst=3
        bucket = TokenBucket(rate=10.0, burst=3)
        self.assertTrue(bucket.allow())
        self.assertTrue(bucket.allow())
        self.assertTrue(bucket.allow())
        self.assertFalse(bucket.allow())
        # Wait for refill (0.4s at 10 tokens/sec = 4 tokens refilled)
        time.sleep(0.4)
        self.assertTrue(bucket.allow())


if __name__ == "__main__":
    unittest.main()
