"""File-backed cache for peer policy_hash responses.

Cache file: memory/.peer_policy_cache.json
Uses atomic_write from infra.memory_common.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import cast

logger = logging.getLogger(__name__)


def _cache_path() -> Path:
    from infra.infrastructure import resolve_active_memory_dir
    return resolve_active_memory_dir() / ".peer_policy_cache.json"


def load_peer_cache() -> dict[str, dict]:
    p = _cache_path()
    if not p.exists():
        return {}
    try:
        return cast("dict[str, dict]", json.loads(p.read_text()))
    except Exception as e:
        logger.debug("policy_hash_cache: load failed: %s", e)
        return {}


def persist_peer_cache(cache: dict[str, dict]) -> None:
    try:
        from infra.memory_common import atomic_write
        atomic_write(_cache_path(), json.dumps(cache, indent=2))
    except Exception as e:
        logger.warning("policy_hash_cache: persist failed: %s", e)


def is_cache_fresh(cache_entry: dict, cache_ttl_s: float) -> bool:
    fetched_at = cast(float, cache_entry.get("fetched_at", 0.0))
    if fetched_at <= 0:
        return False
    return (time.time() - fetched_at) < cache_ttl_s


def filter_stale_entries(
    cache: dict[str, dict], cache_ttl_s: float
) -> tuple[dict[str, dict], dict[str, dict]]:
    """Split cache into (fresh, stale) by TTL."""
    fresh, stale = {}, {}
    for name, entry in cache.items():
        if is_cache_fresh(entry, cache_ttl_s):
            fresh[name] = entry
        else:
            stale[name] = entry
    return fresh, stale
