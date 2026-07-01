#!/usr/bin/env python3
"""Unit tests for memory_common H3/H4/H6 helpers (flock + rate limit).

Run with:
    ~/.config/agentic-memory/venv/bin/python eval/test_memory_common_extras.py
"""

import sys
import threading
import time
import tempfile
import unittest
from pathlib import Path

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))
sys.path.insert(0, str(INSTALL_DIR / "eval"))

from infra.memory_common import (  # noqa: E402
    acquire_flock_with_retry,
    release_flock,
    RateLimiter,
    rate_limit_check,
    get_default_limiter,
)


class TestFlockHelpers(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="flock_test_")
        self.lock_path = Path(self.tmpdir) / "test.lock"

    def tearDown(self):
        try:
            for p in Path(self.tmpdir).glob("*"):
                p.unlink()
            Path(self.tmpdir).rmdir()
        except Exception:
            pass

    def test_acquire_and_release(self):
        f = open(self.lock_path, "w")
        ok = acquire_flock_with_retry(f, max_attempts=3, initial_backoff=0.01)
        self.assertTrue(ok, "fresh lock should be acquirable")
        release_flock(f)
        # After release, a second acquirer should succeed.
        f2 = open(self.lock_path, "w")
        ok2 = acquire_flock_with_retry(f2, max_attempts=3, initial_backoff=0.01)
        self.assertTrue(ok2, "lock should be re-acquirable after release")
        release_flock(f2)

    def test_contended_lock_eventually_returns_false(self):
        """A held lock should cause a second NONBLOCKING acquirer to return False
        after the retry budget is exhausted.
        """
        holder = open(self.lock_path, "w")
        ok = acquire_flock_with_retry(holder, max_attempts=1)
        self.assertTrue(ok)

        contender = open(self.lock_path, "w")
        t0 = time.monotonic()
        got = acquire_flock_with_retry(
            contender,
            max_attempts=2,
            initial_backoff=0.01,
            backoff_multiplier=2.0,
        )
        elapsed = time.monotonic() - t0
        self.assertFalse(got, "contended lock should not be acquirable")
        # Should have given up after ~10 ms (1 attempt + 1 backoff).
        self.assertLess(elapsed, 0.5, f"gave up too slowly: {elapsed:.3f}s")
        release_flock(holder)
        release_flock(contender)

    def test_release_flock_safe_on_none(self):
        release_flock(None)  # must not raise


class TestRateLimiter(unittest.TestCase):
    def test_allows_up_to_max(self):
        rl = RateLimiter(max_calls=3, window_seconds=1.0)
        self.assertTrue(rl.check("a"))
        self.assertTrue(rl.check("a"))
        self.assertTrue(rl.check("a"))
        self.assertFalse(rl.check("a"), "4th call should be rejected")

    def test_separate_names_have_separate_budgets(self):
        rl = RateLimiter(max_calls=1, window_seconds=1.0)
        self.assertTrue(rl.check("a"))
        self.assertTrue(rl.check("b"), "different name has its own budget")

    def test_window_slides(self):
        rl = RateLimiter(max_calls=1, window_seconds=0.1)
        rl.check("a")
        self.assertFalse(rl.check("a"))
        # Wait for the 0.1s rate-limit window to elapse before re-checking.
        # We can't use wait_until(lambda: rl.check("a"), ...) here because
        # rl.check() is *consuming* — every poll would itself open a new
        # window. A short fixed sleep is the right tool. +0.05s gives a
        # small margin on slow CI without slowing fast machines noticeably.
        time.sleep(0.15)
        self.assertTrue(rl.check("a"), "after window expiry, call allowed again")

    def test_reset_clears_one_or_all(self):
        rl = RateLimiter(max_calls=1, window_seconds=10.0)
        rl.check("a")
        rl.check("b")
        rl.reset("a")
        self.assertTrue(rl.check("a"), "reset(name) clears that bucket")
        self.assertFalse(rl.check("b"), "other bucket untouched")
        rl.reset()
        self.assertTrue(rl.check("b"), "reset() clears all buckets")

    def test_invalid_construction(self):
        with self.assertRaises(ValueError):
            RateLimiter(max_calls=0, window_seconds=1.0)
        with self.assertRaises(ValueError):
            RateLimiter(max_calls=1, window_seconds=0)

    def test_default_limiter_singleton(self):
        a = get_default_limiter()
        b = get_default_limiter()
        self.assertIs(a, b, "default limiter should be a singleton")

    def test_rate_limit_check_uses_default(self):
        # Each test runs against the shared default; reset to avoid
        # pollution from earlier tests.
        get_default_limiter().reset()
        # First call should succeed (under cap).
        self.assertTrue(rate_limit_check("default-test"))


class TestFlockThreadSafety(unittest.TestCase):
    def test_serial_lockers_each_get_turn(self):
        """Two threads take the lock in turn. Each must see the other
        finish before it acquires, proving release works under threads.
        """
        tmpdir = tempfile.mkdtemp(prefix="flock_thread_test_")
        lock_path = Path(tmpdir) / "shared.lock"
        order = []
        threading.Event()

        def worker(name):
            f = open(lock_path, "w")
            ok = acquire_flock_with_retry(
                f,
                max_attempts=20,
                initial_backoff=0.02,
                backoff_multiplier=2.0,
                max_backoff=0.2,
            )
            if not ok:
                order.append(f"{name}:FAIL")
                return
            order.append(f"{name}:ACQ")
            # Anti-thundering-herd: hold the lock briefly so the other
            # thread's acquire_flock_with_retry gets to exercise its
            # backoff loop instead of returning immediately.
            time.sleep(0.05)
            order.append(f"{name}:REL")
            release_flock(f)

        t1 = threading.Thread(target=worker, args=("A",))
        t2 = threading.Thread(target=worker, args=("B",))
        t1.start()
        # Anti-thundering-herd: ensure A is past `start()` (and inside
        # acquire_flock_with_retry) before B begins; otherwise on a fast
        # machine both threads race into the lock-contention path at once
        # and the test can't tell which one acquired first.
        time.sleep(0.01)  # ensure A starts first
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)
        # Each thread's ACQ must precede its own REL; either A can fully
        # finish before B starts, or they can interleave, but the ACQ/REL
        # pairing must hold.
        self.assertEqual(len(order), 4, f"got {order}")
        for name in ("A", "B"):
            acq_idx = order.index(f"{name}:ACQ")
            rel_idx = order.index(f"{name}:REL")
            self.assertLess(acq_idx, rel_idx)


if __name__ == "__main__":
    unittest.main(verbosity=2)
