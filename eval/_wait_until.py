"""Deterministic wait-until polling helper for flaky test mitigation.

Replaces `time.sleep(N)` with a `wait_until(predicate, ...)` call that
returns as soon as the predicate is true (or raises TimeoutError). This
removes race conditions where a test sleeps for a fixed duration and
either waits too long (CI slowdown) or too little (CI flake).
"""

from __future__ import annotations
import time
from typing import Callable, TypeVar

T = TypeVar("T")


def wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 5.0,
    interval: float = 0.01,
    message: str = "predicate did not become true within timeout",
) -> None:
    """Poll `predicate()` until it returns truthy, or raise TimeoutError.

    Args:
        predicate: zero-arg callable returning bool (or truthy/falsy)
        timeout: max seconds to wait (default 5.0)
        interval: poll interval in seconds (default 0.01)
        message: error message on timeout

    Raises:
        TimeoutError if predicate doesn't return truthy within `timeout` seconds
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise TimeoutError(f"{message} (waited {timeout}s)")


def wait_until_value(
    get_value: Callable[[], T],
    expected: T,
    *,
    timeout: float = 5.0,
    interval: float = 0.01,
    message: str = "value did not equal expected within timeout",
) -> T:
    """Poll get_value() until it returns `expected`, return that value.

    Args:
        get_value: zero-arg callable returning a value
        expected: value to wait for
        timeout: max seconds to wait
        interval: poll interval
        message: error message

    Returns:
        The value when it matches `expected` (== comparison)

    Raises:
        TimeoutError if the value never matches
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = get_value()
        if current == expected:
            return current
        time.sleep(interval)
    raise TimeoutError(
        f"{message}: last value was {current!r}, expected {expected!r} (waited {timeout}s)"
    )


__all__ = ["wait_until", "wait_until_value"]
