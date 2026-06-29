"""Thread-safe error counter for tracking per-phase exception swallows.

This module provides a simple counter that can be used to track how often
each phase of the search pipeline (and other subsystems) swallows exceptions.
The goal is to make silent failures visible for observability and debugging.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PhaseErrorEntry:
    """A single error entry for a phase."""
    phase: str
    timestamp: float
    error_type: str
    error_message: str


class ErrorCounter:
    """Thread-safe counter for tracking phase errors."""

    def __init__(self, max_entries: int = 10000):
        self._lock = threading.Lock()
        self._counts: dict[str, int] = defaultdict(int)
        self._entries: list[PhaseErrorEntry] = []
        self._max_entries = max_entries

    def increment(self, phase: str, error: Optional[BaseException] = None) -> None:
        """Increment the counter for a phase.

        Args:
            phase: The phase name (e.g., "search.quality_gates", "auto_save.daemon")
            error: Optional exception that was caught and swallowed
        """
        with self._lock:
            self._counts[phase] += 1
            if error is not None:
                self._entries.append(
                    PhaseErrorEntry(
                        phase=phase,
                        timestamp=time.time(),
                        error_type=type(error).__name__,
                        error_message=str(error)[:500],
                    )
                )
                # Trim entries if we exceed max
                if len(self._entries) > self._max_entries:
                    self._entries = self._entries[-self._max_entries :]

    def get_count(self, phase: str) -> int:
        """Get the count for a specific phase."""
        with self._lock:
            return self._counts.get(phase, 0)

    def get_all(self) -> dict[str, int]:
        """Get all counts."""
        with self._lock:
            return dict(self._counts)

    def get_counts(
        self,
        since_ts: Optional[float] = None,
        until_ts: Optional[float] = None,
        limit: int = 50,
    ) -> dict:
        """Get error counts with optional time filtering.

        Returns:
            Dict with 'total_count', 'phase_counts', and 'recent_entries'
        """
        with self._lock:
            # Filter entries by time if requested
            filtered_entries = self._entries
            if since_ts is not None:
                filtered_entries = [e for e in filtered_entries if e.timestamp >= since_ts]
            if until_ts is not None:
                filtered_entries = [e for e in filtered_entries if e.timestamp <= until_ts]

            # Count by phase
            phase_counts: dict[str, int] = defaultdict(int)
            for entry in filtered_entries:
                phase_counts[entry.phase] += 1

            # Sort by count descending and limit
            sorted_phases = sorted(phase_counts.items(), key=lambda x: x[1], reverse=True)
            limited_phases = dict(sorted_phases[:limit])

            # Get recent entries
            recent = [
                {
                    "phase": e.phase,
                    "timestamp": e.timestamp,
                    "error_type": e.error_type,
                    "error_message": e.error_message,
                }
                for e in filtered_entries[-limit:]
            ]

            return {
                "total_count": sum(phase_counts.values()),
                "phase_counts": limited_phases,
                "recent_entries": recent,
            }

    def reset(self) -> None:
        """Reset all counters."""
        with self._lock:
            self._counts.clear()
            self._entries.clear()


# Global singleton instance
_global_counter: Optional[ErrorCounter] = None
_counter_lock = threading.Lock()


def get_counter() -> ErrorCounter:
    """Get the global error counter singleton."""
    global _global_counter
    if _global_counter is None:
        with _counter_lock:
            if _global_counter is None:
                _global_counter = ErrorCounter()
    return _global_counter


def increment(phase: str, error: Optional[BaseException] = None) -> None:
    """Convenience function to increment the global counter."""
    get_counter().increment(phase, error)


def get_counts(
    since_ts: Optional[float] = None,
    until_ts: Optional[float] = None,
    limit: int = 50,
) -> dict:
    """Convenience function to get counts from the global counter."""
    return get_counter().get_counts(since_ts, until_ts, limit)


def get_all() -> dict[str, int]:
    """Convenience function to get all counts from the global counter."""
    return get_counter().get_all()


def reset() -> None:
    """Convenience function to reset the global counter."""
    get_counter().reset()


__all__ = [
    "ErrorCounter",
    "PhaseErrorEntry",
    "get_counter",
    "increment",
    "get_counts",
    "get_all",
    "reset",
]