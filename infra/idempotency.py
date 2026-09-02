"""Thread-safe idempotency tracking and deduplication for Agentic Memory."""

from __future__ import annotations

import threading
import time
from typing import Any, Optional


_IDEMPOTENCY_LOCK = threading.Lock()
_IDEMPOTENCY_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_DEFAULT_TTL_S = 300.0  # 5 minutes deduplication window
_MAX_CACHE_ENTRIES = 10_000


def _cache_key(key: str, tenant_id: str = "default") -> str:
    return f"{tenant_id}:{key}"


def get_idempotent_result(key: Optional[str], tenant_id: str = "default") -> Optional[dict[str, Any]]:
    """Return cached result dict if key was previously executed within TTL, else None."""
    if not key:
        return None
    full_key = _cache_key(key, tenant_id)
    now = time.time()
    with _IDEMPOTENCY_LOCK:
        entry = _IDEMPOTENCY_CACHE.get(full_key)
        if entry is None:
            return None
        expire_at, result = entry
        if now > expire_at:
            _IDEMPOTENCY_CACHE.pop(full_key, None)
            return None
        return result


def set_idempotent_result(
    key: Optional[str],
    result: dict[str, Any],
    tenant_id: str = "default",
    ttl_seconds: float = _DEFAULT_TTL_S,
) -> None:
    """Store the result of an operation keyed by idempotency key."""
    if not key:
        return
    full_key = _cache_key(key, tenant_id)
    now = time.time()
    expire_at = now + ttl_seconds
    with _IDEMPOTENCY_LOCK:
        # Prune if exceeding capacity
        if len(_IDEMPOTENCY_CACHE) >= _MAX_CACHE_ENTRIES:
            expired = [k for k, (exp, _) in _IDEMPOTENCY_CACHE.items() if now > exp]
            for k in expired:
                _IDEMPOTENCY_CACHE.pop(k, None)
            if len(_IDEMPOTENCY_CACHE) >= _MAX_CACHE_ENTRIES:
                # Evict oldest entry
                oldest_k = min(_IDEMPOTENCY_CACHE, key=lambda k: _IDEMPOTENCY_CACHE[k][0])
                _IDEMPOTENCY_CACHE.pop(oldest_k, None)
        _IDEMPOTENCY_CACHE[full_key] = (expire_at, result)


def clear_idempotency_cache() -> None:
    """Clear in-memory cache (for testing)."""
    with _IDEMPOTENCY_LOCK:
        _IDEMPOTENCY_CACHE.clear()
