"""Token-bucket rate limiter for MCP tools.

Per-tool rate limits are configured via ``memory.toml`` ``[rate_limits]``
or the ``MEMORY_RATE_LIMIT_<TOOL>`` env var (RPM = requests per minute).

Usage::

    from infra.rate_limiter import check_rate_limit, record_success, configure_rate_limits

    configure_rate_limits()  # call once at startup
    if not check_rate_limit("memory_save"):
        return {"error": "rate_limited", "retry_after": 12.0}
    ...  # handle the request
    record_success("memory_save")
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Token bucket
# ---------------------------------------------------------------------------


class TokenBucket:
    """Thread-safe token bucket for rate limiting.

    Tokens refill at a constant ``rate`` (tokens per second). A burst of
    up to ``burst`` tokens can be consumed immediately.
    """

    __slots__ = ("rate", "burst", "tokens", "last_refill", "_lock")

    def __init__(self, rate: float, burst: int) -> None:
        self.rate = rate  # tokens per second
        self.burst = burst
        self.tokens = float(burst)
        self.last_refill = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        """Replenish tokens based on elapsed time since last refill."""
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
        self.last_refill = now

    def allow(self) -> bool:
        """Try to consume one token. Returns True if allowed."""
        with self._lock:
            self._refill()
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True
            return False

    def wait_time(self) -> float:
        """Seconds until one token is available. 0.0 if available now."""
        with self._lock:
            self._refill()
            if self.tokens >= 1.0:
                return 0.0
            deficit = 1.0 - self.tokens
            return deficit / self.rate if self.rate > 0 else float("inf")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

# tool_name → TokenBucket; populated by configure_rate_limits()
RATE_LIMITERS: dict[str, TokenBucket] = {}
_rl_lock = threading.Lock()


def _default_limits() -> dict[str, dict[str, Any]]:
    """Return default per-tool rate limits (RPM → converted to per-second)."""
    return {
        "memory_save": {"rate": 100.0 / 60.0, "burst": 20},
        "memory_search": {"rate": 300.0 / 60.0, "burst": 60},
        "memory_delete": {"rate": 50.0 / 60.0, "burst": 10},
        "memory_supersede": {"rate": 50.0 / 60.0, "burst": 10},
        "memory_maintenance": {"rate": 30.0 / 60.0, "burst": 5},
        # catch-all fallback: read-only tools are less constrained
        "_default": {"rate": 600.0 / 60.0, "burst": 100},
    }


def _resolve_tool_limits(
    tool: str, toml_limits: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Resolve effective limits for a single tool.

    Env var ``MEMORY_RATE_LIMIT_<TOOL_UPPER>=<rpm>,<burst>`` overrides
    TOML, which overrides defaults.
    """
    # 1. env var override
    env_key = f"MEMORY_RATE_LIMIT_{tool.upper()}"
    env_val = os.environ.get(env_key)
    if env_val:
        parts = env_val.split(",")
        try:
            rpm = float(parts[0].strip())
            burst = int(parts[1].strip()) if len(parts) > 1 else max(1, int(rpm))
            return {"rate": rpm / 60.0, "burst": burst}
        except (ValueError, IndexError):
            logger.warning("bad %s=%s; using default", env_key, env_val)

    # 2. TOML override
    if toml_limits and tool in toml_limits:
        entry = toml_limits[tool]
        if isinstance(entry, dict):
            return {
                "rate": float(entry.get("rate", 600.0 / 60.0)),
                "burst": int(entry.get("burst", 100)),
            }
        if isinstance(entry, (int, float)):
            # bare number = RPM
            return {"rate": float(entry) / 60.0, "burst": max(1, int(entry))}

    # 3. per-tool TOML default
    defaults = _default_limits()
    if tool in defaults:
        return dict(defaults[tool])

    # 4. catch-all
    return dict(defaults["_default"])


def configure_rate_limits(toml_limits: dict[str, Any] | None = None) -> None:
    """Build the per-tool TokenBucket registry. Call once at startup."""
    try:
        from infra.config import get_config  # late import avoids cycle
        cfg = get_config()
        # cfg.rate_limits is a dict[str, dict] if set, or empty dict
        toml_limits = cfg.rate_limits or toml_limits or {}
    except Exception:
        toml_limits = toml_limits or {}

    known_tools = set(_default_limits()) - {"_default"}
    all_tools = list(known_tools) + ["_default"]

    new_registry: dict[str, TokenBucket] = {}
    for tool in all_tools:
        limits = _resolve_tool_limits(tool, toml_limits)
        new_registry[tool] = TokenBucket(limits["rate"], limits["burst"])

    with _rl_lock:
        RATE_LIMITERS.clear()
        RATE_LIMITERS.update(new_registry)
    logger.info("rate_limits configured for %d tools", len(RATE_LIMITERS))


def check_rate_limit(tool: str) -> bool:
    """Return True if the request for *tool* is within the rate limit.

    Initialises the bucket for unknown tools on first access using the
    catch-all default.
    """
    with _rl_lock:
        if tool not in RATE_LIMITERS:
            RATE_LIMITERS[tool] = TokenBucket(
                _default_limits()["_default"]["rate"],
                _default_limits()["_default"]["burst"],
            )
        bucket = RATE_LIMITERS[tool]
    return bucket.allow()


def record_success(tool: str) -> None:
    """Record a successful request (currently no-op; reserved for metrics)."""


def record_failure(tool: str) -> None:
    """Record a failed request (currently no-op; reserved for metrics)."""


def get_retry_after(tool: str) -> float:
    """Seconds until the next request for *tool* would be allowed."""
    with _rl_lock:
        bucket = RATE_LIMITERS.get(tool)
    if bucket is None:
        return 0.0
    return bucket.wait_time()
