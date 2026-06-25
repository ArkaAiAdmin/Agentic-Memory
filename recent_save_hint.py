"""recent_save_hint — small in-process state that records recently-saved note
ids so the search side can verify post-save visibility.

The AgenticMemory MVP previously had a bug where save-then-search within the
same MCP process returned 5 unrelated older notes instead of the freshly-saved
one. Root cause: an SQL filter `m.repo_id IS NOT NULL` excluded all global
notes. Even after fixing that root cause, this hint serves as a fast-path
defense-in-depth so any future regression in the FTS5 / search path is
caught: if a query fires within a short window after a save of N, and N
matches by id and is in the FTS5 index, but the search result list does not
contain N, we surface N as a top-1 floater.

Design:
  * Lightweight: a single OrderedDict keyed by (db_path, note_id).
  * TTL: 30 seconds (configurable via RECENT_SAVE_TTL_S). Saves older than
    this are evicted on every record.
  * Not persisted across process restarts. That's intentional — the hint
    only matters within a single process, and search_memories
    inside a different process can still go through its normal path.
  * Thread-safe via the module-level threading.Lock.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Optional

# Default 30s. If a search lands later than this, the hint is too stale to
# trust; the search is probably not a save-then-search but a different
# session.
RECENT_SAVE_TTL_S: float = 30.0

# (db_path, note_id) -> saved_at (time.time())
_state: "OrderedDict[tuple[str, str], float]" = OrderedDict()
_lock = threading.Lock()


def note_saved(note_id: str, db_path: str) -> None:
    """Mark ``note_id`` as just saved against ``db_path``. Idempotent."""
    if not note_id or not db_path:
        return
    key = (db_path, note_id)
    now = time.time()
    with _lock:
        if key in _state:
            _state.move_to_end(key)
        _state[key] = now
        # Evict expired entries.
        cutoff = now - RECENT_SAVE_TTL_S
        while _state:
            oldest = next(iter(_state))
            if _state[oldest] < cutoff:
                _state.popitem(last=False)
            else:
                break


def recent_save_for(db_path: str) -> Optional[tuple[str, float]]:
    """Return the most recent (note_id, saved_at) for ``db_path`` if any
    save fired within the TTL window. Used by ``search_memories`` as a
    post-save visibility check.
    """
    if not db_path:
        return None
    now = time.time()
    with _lock:
        # Walk in insertion order; entries are within TTL for a small window
        # after the most recent save. Iterate from newest to oldest by
        # reversing the dict view; the first in-window match wins.
        items = list(reversed(_state.items()))
        for (path, note_id), ts in items:
            if path != db_path:
                continue
            if now - ts > RECENT_SAVE_TTL_S:
                return None
            return (note_id, ts)
    return None


def clear() -> None:
    """Drop all hint state. Useful for tests / process resets."""
    with _lock:
        _state.clear()
