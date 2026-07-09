"""Runtime drift progression tracker.

Drift detected mid-process accumulates hits in a rolling window. Each tier
has its own counter; hitting ``progression_max_hits`` within
``progression_window_secs`` escalates the effective mode by one step
(WARN -> SOFT_BLOCK -> HARD_FAIL).

State persists in process memory only -- counter resets on process restart.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

_history: dict[str, _DriftHistory] = {}
_history_lock = threading.Lock()


class _DriftHistory:
    __slots__ = ("tier", "hits", "escalated", "last_check_at")

    def __init__(self, tier: str) -> None:
        self.tier = tier
        self.hits: tuple[float, ...] = ()
        self.escalated = False
        self.last_check_at = 0.0


def record_drift(tier: str, *, window_secs: int = 600) -> None:
    """Record a drift detection for the tier."""
    now = time.time()
    with _history_lock:
        h = _history.get(tier)
        if h is None:
            h = _DriftHistory(tier=tier)
            _history[tier] = h
        cutoff = now - window_secs
        filtered = tuple(t for t in h.hits if t >= cutoff) + (now,)
        h.hits = filtered
        h.last_check_at = now


def get_hits(tier: str) -> int:
    """Number of drift hits for the tier within the configured window."""
    with _history_lock:
        h = _history.get(tier)
        if h is None:
            return 0
        return len(h.hits)


def should_escalate(tier: str, policy: Any) -> bool:
    """True iff hits in window >= max, and we haven't already escalated."""
    with _history_lock:
        h = _history.get(tier)
        if h is None or h.escalated:
            return False
        now = time.time()
        cutoff = now - policy.progression_window_secs
        recent = sum(1 for t in h.hits if t >= cutoff)
        return bool(recent >= policy.progression_max_hits)


def mark_escalated(tier: str) -> None:
    """Mark the tier as escalated (no double-escalation)."""
    with _history_lock:
        h = _history.get(tier)
        if h is not None:
            h.escalated = True


def reset_history(tier: Optional[str] = None) -> None:
    """Reset the in-process history (testing / reload convenience)."""
    with _history_lock:
        if tier is None:
            _history.clear()
        else:
            _history.pop(tier, None)