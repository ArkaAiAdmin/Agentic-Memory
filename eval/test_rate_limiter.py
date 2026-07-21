"""Tests for infra.rate_limiter."""

from __future__ import annotations

from unittest import mock

import pytest

from infra.rate_limiter import (
    TokenBucket,
    check_rate_limit,
    configure_rate_limits,
    get_retry_after,
    record_failure,
    record_success,
)


class TestTokenBucket:
    """Unit-level TokenBucket tests (no config dependency)."""

    def test_initial_burst_available(self):
        bucket = TokenBucket(rate=10.0, burst=5)
        for _ in range(5):
            assert bucket.allow() is True
        assert bucket.allow() is False

    def test_tokens_refill_over_time(self):
        bucket = TokenBucket(rate=10.0, burst=5)
        for _ in range(5):
            bucket.allow()
        assert bucket.allow() is False
        t_refill = bucket.last_refill + 0.15
        with mock.patch("time.monotonic", return_value=t_refill):
            assert bucket.allow() is True

    def test_wait_time_zero_when_available(self):
        bucket = TokenBucket(rate=10.0, burst=5)
        assert bucket.wait_time() == 0.0

    def test_wait_time_positive_when_empty(self):
        bucket = TokenBucket(rate=10.0, burst=1)
        bucket.allow()
        wt = bucket.wait_time()
        assert 0.08 < wt < 0.15

    def test_burst_never_exceeds_max(self):
        bucket = TokenBucket(rate=100.0, burst=3)
        for _ in range(10):
            bucket.allow()
        t_refill = bucket.last_refill + 0.05
        with mock.patch("time.monotonic", return_value=t_refill):
            bucket.allow()
        assert bucket.tokens <= 3.0


class TestConfigureRateLimits:
    """Integration with MemoryConfig."""

    def test_configure_populates_registry(self):
        configure_rate_limits()
        from infra.rate_limiter import RATE_LIMITERS

        assert "memory_save" in RATE_LIMITERS
        assert "memory_search" in RATE_LIMITERS
        assert "_default" in RATE_LIMITERS

    def test_env_var_overrides_default(self, monkeypatch):
        monkeypatch.setenv("MEMORY_RATE_LIMIT_MEMORY_SAVE", "120,30")
        configure_rate_limits()
        from infra.rate_limiter import RATE_LIMITERS

        bucket = RATE_LIMITERS["memory_save"]
        assert abs(bucket.rate - 120.0 / 60.0) < 1e-9
        assert bucket.burst == 30

    def test_check_rate_limit_allows_within_budget(self):
        configure_rate_limits()
        assert check_rate_limit("memory_save") is True

    def test_record_success_and_failure_are_noops(self):
        configure_rate_limits()
        record_success("memory_save")
        record_failure("memory_save")
        assert check_rate_limit("memory_save") is True

    def test_get_retry_after_returns_non_negative(self):
        configure_rate_limits()
        wt = get_retry_after("memory_save")
        assert wt >= 0.0

    def test_unknown_tool_uses_default(self):
        configure_rate_limits()
        assert check_rate_limit("nonexistent_tool_xyz") is True
