"""Cache utilities extracted from memory_mcp.py (God-object split Step 2).

Contains:
- Search result cache state and key generation.
- ``cache_stats()`` function.
- ``safety_wiring`` flag (BLK-1 production default).

Exports (via __all__):
    SEARCH_CACHE_MAX, SEARCH_CACHE_TTL, SEARCH_CACHE_TTL_ENABLED,
    safety_wiring, _search_cache, make_cache_key, cache_stats
"""

from __future__ import annotations

import hashlib
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path

__all__ = [
    "SEARCH_CACHE_MAX",
    "SEARCH_CACHE_TTL",  # noqa: F822 — dynamically resolved via __getattr__
    "SEARCH_CACHE_TTL_ENABLED",  # noqa: F822 — dynamically resolved via __getattr__
    "safety_wiring",
    "_search_cache",
    "make_cache_key",
    "cache_stats",
]

# ---------------------------------------------------------------------------
# Search result cache state
# ---------------------------------------------------------------------------

SEARCH_CACHE_MAX: int = 50
_SEARCH_CACHE_QUERY_MAX: int = 128
# SEARCH_CACHE_TTL is dynamically resolved via __getattr__
# SEARCH_CACHE_TTL_ENABLED is dynamically resolved via __getattr__

# BLK-1 (2026-06-07): flipped to True so the prompt-injection demotion
# runs in production search paths by default. Tests and CLI scripts that
# explicitly want the pre-Wave-7 order can still pass safety_wiring=False
# (or set this constant back to False at the top of their module).
safety_wiring: bool = True

# The actual LRU cache instance.  Importable and directly mutated by
# search/save/clear paths in search_pipeline, save_pipeline, etc.
_search_cache: OrderedDict = OrderedDict()
_search_cache_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Cache key generation
# ---------------------------------------------------------------------------


def make_cache_key(
    db_path: Path,
    fts_query: str,
    limit: int,
    rerank: bool,
    boost_pinned: bool,
    recency_weight: float,
    include_invalid: bool,
    include_global: bool = True,
    deep_rerank: bool = False,
    hybrid: bool = False,
    synthesize: bool = False,
) -> str:
    """Build a stable, length-bounded cache key for search results.

    M6 fix: the previous key embedded ``str(db_path)`` and the full
    ``fts_query`` verbatim. Long queries bloated the key, and
    ``repr(Path)`` is not stable across pickle/unpickle round-trips.
    Now the query portion is truncated to ``_SEARCH_CACHE_QUERY_MAX``
    chars, and the path is stringified with ``as_posix()`` for a
    stable representation. The full query is preserved in the cached
    result, so no information is lost.

    The key also folds in the four major scoring flags
    (quality_gates, ctr_tuning, forgetting_curve, user_profile) so two
    searches with the same query but different flag settings get
    distinct cache entries.
    """
    q_part = fts_query
    if len(q_part) > _SEARCH_CACHE_QUERY_MAX:
        q_part = q_part[:_SEARCH_CACHE_QUERY_MAX] + f"...<{len(fts_query)}>"
    flag_part = ":".join(
        f"{name}={int(_flag_enabled(name))}"
        for name in (
            "quality_gates",
            "ctr_tuning",
            "forgetting_curve",
            "user_profile",
        )
    )
    flag_hash = hashlib.sha1(flag_part.encode("utf-8")).hexdigest()[:8]
    return (
        f"{db_path.as_posix() if hasattr(db_path, 'as_posix') else str(db_path)}"
        f":{q_part}:{limit}:{rerank}:{boost_pinned}"
        f":{recency_weight}:{include_invalid}:{include_global}"
        f":{deep_rerank}:{hybrid}:{synthesize}"
        f":{flag_hash}"
    )


def _flag_enabled(name: str) -> bool:
    """Resolve a config boolean flag by dotted name without throwing."""
    try:
        from _lazy_imports import get_config

        return bool(getattr(get_config(), name, False))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Cache stats
# ---------------------------------------------------------------------------


def cache_stats() -> dict:
    """Return stats for both FTS5 and query embedding caches."""
    # FTS5 cache stats.
    fts5_entries = len(_search_cache)
    now = time.time()
    fts5_expired = 0
    fts5_active = 0
    this_mod = sys.modules[__name__]
    for ts, _ in _search_cache.values():
        if this_mod.SEARCH_CACHE_TTL_ENABLED and (now - ts) > this_mod.SEARCH_CACHE_TTL:
            fts5_expired += 1
        else:
            fts5_active += 1
    return {
        "fts5_cache": {
            "entries": fts5_entries,
            "max": SEARCH_CACHE_MAX,
            "ttl_enabled": this_mod.SEARCH_CACHE_TTL_ENABLED,
            "ttl_seconds": this_mod.SEARCH_CACHE_TTL,
            "active": fts5_active,
            "expired": fts5_expired,
        },
        "fts5_cache_cleared_on_write": True,
    }


def clear_all_caches() -> None:
    """Clear both the FTS5 search cache and the embedding vec cache.

    Call this after any write that could stale either cache (save,
    delete, backfill, rebuild).  Uses a lazy import to break the
    circular dependency between ``cache.py`` and ``embedding_search.py``.
    """
    with _search_cache_lock:
        _search_cache.clear()
    try:
        from embedding_search import clear_vec_cache

        clear_vec_cache()
    except ImportError:
        pass


from memory_common import make_lazy_getattr

__getattr__ = make_lazy_getattr(
    {
        "SEARCH_CACHE_TTL": "fts5_cache_ttl",
        "SEARCH_CACHE_TTL_ENABLED": "fts5_cache",
    }
)
