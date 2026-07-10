"""Canonical mtime tracker for memory.toml.

Single source of truth: every TOML-dependent subsystem calls
``current_mtime()`` or ``toml_changed_since(t)`` to determine whether
re-reading is needed.  A background poller thread (started lazily)
fires subscriber callbacks when the mtime advances.  When
``MEMORY_TOML_HOT_RELOAD`` is set, the watcher also resets the policy
cache so the next ``resolve_policy()`` call re-reads the TOML.
"""
from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

_POLL_DEFAULT = 1.0
_DEBOUNCE = 0.05

_watcher_state: dict = {}
_watcher_lock = threading.Lock()
_last_known_bytes: bytes = b""
# Keys that have been observed at least once (separate from _watcher_state
# because 0.0 is a valid cached value in tests).
_watcher_seen: set = set()


def get_toml_path() -> Path:
    from infra.config import _TOML_PATH
    return _TOML_PATH


def _stat_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def current_mtime() -> float:
    with _watcher_lock:
        return float(_watcher_state.get(str(get_toml_path()), 0.0))


def refresh_mtime() -> float:
    """Stat the TOML file and return the usable cached mtime.

    On the very first call the raw observed value is **not** stored
    (it may be mid-update).  The returned value is 0.0 and the
    observation is held as pending.  A second consecutive call that
    observes the same mtime seeds the cache and returns that value.

    Once seeded, only changes larger than ``_DEBOUNCE`` seconds are
    accepted; smaller deltas are treated as filesystem jitter and the
    previous cached value is returned unchanged.
    """
    path = get_toml_path()
    key = str(path)
    observed = _stat_mtime(path)
    with _watcher_lock:
        seen = key in _watcher_seen
        if not seen:
            # First call: hold the observed value as pending; signal
            # "not yet confirmed" by returning 0.0.
            _watcher_state[key + "__pending"] = observed
            _watcher_seen.add(key)
            return 0.0
        pending = _watcher_state.pop(key + "__pending", None)
        prev = float(_watcher_state.get(key, 0.0))
        if pending is not None and pending == observed:
            # Second consecutive identical observation: confirm seed.
            _watcher_state[key] = observed
            return observed
        # Pending existed and differed — accept if change is significant.
        if (observed - prev) <= _DEBOUNCE:
            return prev
        _watcher_state[key] = observed
        return observed


def toml_changed_since(t: float) -> bool:
    return current_mtime() > t + _DEBOUNCE


def apply_hot_reload() -> None:
    """Reset policy cache and reapply tier overrides from the live TOML.

    Reads the current bytes of ``memory.toml``; if they differ from the
    last known bytes the function resets the policy cache, re-reads
    ``[drift_tiers]`` and updates ``_FLAG_TIERS``.  No-op when the
    file bytes are unchanged since the last reload.

    After a successful reload, fires all registered subscribers so
    downstream consumers (e.g. ``_on_toml_change`` in
    ``config_drift_policy``) can react.
    """
    global _last_known_bytes
    path = get_toml_path()
    try:
        raw = path.read_bytes()
    except OSError as e:
        logger.debug("toml_watch: hot-reload read failed: %s", e)
        return

    if raw == _last_known_bytes:
        return

    _last_known_bytes = raw
    try:
        from infra.config_drift_policy import reset_policy_cache
        from infra.config_drift_tier_patch import apply_tier_overrides_from_toml
        from infra.config import _read_toml
        # Tier overrides must be applied BEFORE invalidating the policy
        # cache (Plan 3 goal 4) so the next resolve_policy() sees them.
        toml_data = _read_toml(path)
        apply_tier_overrides_from_toml(toml_data)
        reset_policy_cache()
        logger.info("toml_watch: hot-reload applied for %s", path)
    except SystemExit:
        raise
    except Exception as e:
        logger.warning("toml_watch: hot-reload failed: %s", e)
        return

    # Subscribers are fired solely by the poller loop (the single trigger),
    # which already fires them before invoking apply_hot_reload(). Re-firing
    # here would emit duplicate toml_hot_reload audit events.


# ---------------------------------------------------------------------------
# Watcher state — module-level so tests can patch attributes directly.
# ---------------------------------------------------------------------------

_poller_thread: threading.Thread | None = None
_poller_stop: threading.Event | None = None
_subscribers: list[Callable] = []


def _fire_subscribers(new_mtime: float) -> None:
    for cb in list(_subscribers):
        try:
            cb(new_mtime)
        except Exception as e:
            logger.debug("toml_watch: subscriber %r failed: %s", cb, e)


def _poller_loop(poll_s: float) -> None:
    global _poller_stop
    stop = _poller_stop
    # Seed last-fired to the current mtime so the first poll does not fire
    # spuriously on watcher startup (which would also write a bogus
    # toml_hot_reload audit event). Only a real mtime advance fires.
    _watcher_state["__last_fired__"] = current_mtime()
    while stop is not None and not stop.is_set():
        try:
            new_mtime = refresh_mtime()
            last_fired = _watcher_state.get("__last_fired__", 0.0)
            if new_mtime > last_fired + _DEBOUNCE:
                _fire_subscribers(new_mtime)
                _watcher_state["__last_fired__"] = new_mtime
                if os.environ.get("MEMORY_TOML_HOT_RELOAD") in ("1", "true", "yes"):
                    apply_hot_reload()
        except Exception as e:
            logger.debug("toml_watch: poll failed: %s", e)
        stop.wait(poll_s)


def subscribe(callback: Callable) -> Callable[[], None]:
    if callback not in _subscribers:
        _subscribers.append(callback)

    def _unsub() -> None:
        try:
            _subscribers.remove(callback)
        except ValueError:
            pass

    return _unsub


def start_watcher(*, poll_s: float = _POLL_DEFAULT) -> bool:
    global _poller_thread, _poller_stop
    if _poller_thread is not None and _poller_thread.is_alive():
        return False
    _poller_stop = threading.Event()
    # Seed the mtime cache with two consecutive observations (so the poller
    # uses the REAL mtime, not 0.0), then pin __last_fired__ to it BEFORE
    # starting the thread. This guarantees no spurious fire — and therefore
    # no bogus toml_hot_reload audit event — until a REAL mtime advance.
    refresh_mtime()
    refresh_mtime()
    _watcher_state["__last_fired__"] = current_mtime()
    _poller_thread = threading.Thread(
        target=_poller_loop,
        args=(poll_s,),
        name="toml-watcher",
        daemon=True,
    )
    _poller_thread.start()
    logger.info("toml_watch: started (poll=%.2fs)", poll_s)
    return True


def stop_watcher() -> None:
    global _poller_thread, _poller_stop
    if _poller_stop is not None:
        _poller_stop.set()
    if _poller_thread is not None:
        _poller_thread.join(timeout=2.0)
    _poller_thread = None
    _poller_stop = None
