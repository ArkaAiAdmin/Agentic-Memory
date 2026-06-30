#!/usr/bin/env python3
"""Circuit breaker for auto-save subsystem.

Owns:
- _AutoSaveState TypedDict + module-level state
- _update_shared_memory_state
- _auto_save_circuit_open
- _check_circuit_timeout_expiry
- _auto_save_record_failure_and_maybe_trip
- _auto_save_record_success
- _record_circuit_skip
- _persist_circuit_state
- _auto_save_get_state
- _auto_save_reset_state
- _load_circuit_state_from_audit

Imported by: auto_save.py (backward-compat shim), background daemon, CLI hooks.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, TypedDict

logger = logging.getLogger(__name__)


class _AutoSaveState(TypedDict):
    failure_times: list[float]
    circuit_open_until: float
    last_backoff_seconds: float


_AUTO_SAVE_STATE: _AutoSaveState = {
    "failure_times": [],
    "circuit_open_until": 0.0,
    "last_backoff_seconds": 0.0,
}
_AUTO_SAVE_STATE_LOCK = threading.Lock()

# Module-level keep-alive for the daemon's flock FD.
_DAEMON_LOCKS: dict[str, Any] = {}

# Module-level stop flag for graceful shutdown.
_DAEMON_STOP_REQUESTED = False


def _update_shared_memory_state() -> None:
    """Mirror in-process circuit breaker state into shared memory segment.

    Best-effort: failures logged at debug level, never raised.
    """
    import os as _os
    import time as _t

    if _os.environ.get("MEMORY_USE_SHARED_MEMORY", "0") != "1":
        return
    try:
        import shared_memory_state as _sms

        state = _sms.SharedMemoryState()
        if not state.attach():
            return
        try:
            with _AUTO_SAVE_STATE_LOCK:
                state.write_state(
                    circuit_open_until=_AUTO_SAVE_STATE["circuit_open_until"],
                    failure_count=len(_AUTO_SAVE_STATE["failure_times"]),
                    last_backoff_seconds=_AUTO_SAVE_STATE["last_backoff_seconds"],
                    daemon_pid=_os.getpid(),
                    daemon_started_at=_t.time(),
                    is_daemon_alive=True,
                )
        finally:
            state.detach()
    except Exception as _e:
        logger.debug("auto-save: shared memory update failed: %s", _e)


def _auto_save_circuit_open() -> bool:
    """True if the circuit breaker is currently open (skipping saves)."""
    import os as _os
    import time as _t

    if _os.environ.get("MEMORY_USE_SHARED_MEMORY", "0") == "1":
        try:
            import shared_memory_state as _sms

            state = _sms.SharedMemoryState()
            if state.attach():
                try:
                    result = state.is_circuit_open()
                    if result is not None:
                        return bool(result)
                finally:
                    state.detach()
        except Exception:
            pass

    with _AUTO_SAVE_STATE_LOCK:
        return _t.time() < _AUTO_SAVE_STATE["circuit_open_until"]


def _check_circuit_timeout_expiry() -> None:
    """Check if the circuit breaker timeout has expired and persist close."""
    import time as _t

    with _AUTO_SAVE_STATE_LOCK:
        if (
            _AUTO_SAVE_STATE["circuit_open_until"] > 0
            and _t.time() >= _AUTO_SAVE_STATE["circuit_open_until"]
        ):
            _AUTO_SAVE_STATE["circuit_open_until"] = 0.0
            _AUTO_SAVE_STATE["failure_times"] = []
            should_persist = True
        else:
            should_persist = False
    if should_persist:
        _persist_circuit_state(
            "close",
            details={
                "reason": "timeout_expired",
                "recovered_at": _t.time(),
            },
        )
        _update_shared_memory_state()


def _auto_save_record_failure_and_maybe_trip() -> dict:
    """Record a save failure. Returns the resolved backoff config."""
    import time as _t

    try:
        from _lazy_imports import get_config

        cfg = get_config()
        max_retries = int(getattr(cfg, "auto_save_max_retries", 3))
        base = float(getattr(cfg, "auto_save_backoff_base_seconds", 1.0))
        cap = float(getattr(cfg, "auto_save_backoff_cap_seconds", 30.0))
        cb_seconds = float(getattr(cfg, "auto_save_circuit_breaker_seconds", 300.0))
        window = float(getattr(cfg, "auto_save_failure_window_seconds", 60.0))
    except Exception:
        max_retries, base, cap, cb_seconds, window = 3, 1.0, 30.0, 300.0, 60.0

    now = _t.time()
    cutoff = now - window
    transitioned_to_open = False
    open_until = 0.0
    with _AUTO_SAVE_STATE_LOCK:
        _AUTO_SAVE_STATE["failure_times"] = [
            t for t in _AUTO_SAVE_STATE["failure_times"] if t >= cutoff
        ]
        _AUTO_SAVE_STATE["failure_times"].append(now)

        n_failures = len(_AUTO_SAVE_STATE["failure_times"])
        next_backoff = min(cap, base * (2 ** max(0, n_failures - 1)))
        _AUTO_SAVE_STATE["last_backoff_seconds"] = next_backoff

        if n_failures > max_retries:
            prior_open = _AUTO_SAVE_STATE["circuit_open_until"]
            _AUTO_SAVE_STATE["circuit_open_until"] = now + cb_seconds
            if prior_open <= now:
                transitioned_to_open = True
                open_until = float(_AUTO_SAVE_STATE["circuit_open_until"])
        logger.error(
            "auto-save circuit breaker OPEN: %d failures in %.0fs window; "
            "skipping saves for %.0fs",
            n_failures,
            window,
            cb_seconds,
        )
    if transitioned_to_open:
        _persist_circuit_state(
            "open",
            details={
                "n_failures": n_failures,
                "window_s": window,
                "cb_seconds": cb_seconds,
                "open_until": open_until,
            },
        )
        _update_shared_memory_state()

    return {
        "max_retries": max_retries,
        "next_backoff": next_backoff,
        "n_failures": n_failures,
        "circuit_breaker_seconds": cb_seconds,
    }


def _auto_save_record_success() -> None:
    """Reset backoff state on a successful save."""
    import time as _t

    was_open = False
    open_until_before = 0.0
    with _AUTO_SAVE_STATE_LOCK:
        was_open = _AUTO_SAVE_STATE["circuit_open_until"] > 0
        open_until_before = float(_AUTO_SAVE_STATE["circuit_open_until"])
        _AUTO_SAVE_STATE["failure_times"] = []
        _AUTO_SAVE_STATE["circuit_open_until"] = 0.0
        _AUTO_SAVE_STATE["last_backoff_seconds"] = 0.0
    if was_open:
        _persist_circuit_state(
            "close",
            details={
                "open_until_was": open_until_before,
                "recovered_at": _t.time(),
            },
        )


def _record_circuit_skip(entry: dict) -> None:
    """Record a skipped entry due to circuit breaker being open."""
    try:
        from db import connection_pool

        db_path = _get_db_path()
        conn = connection_pool.get(str(db_path), timeout=5.0)
        try:
            conn.execute(
                "INSERT INTO memory_audit_log ("
                "  ts, tool, args, results_count, latency_ms"
                ") VALUES (?, ?, ?, ?, ?)",
                (
                    time.time(),
                    "auto_save_circuit_skip",
                    json.dumps(entry, default=str),
                    0,
                    0.0,
                ),
            )
            conn.commit()
        finally:
            try:
                from memory_common import safe_close_db

                safe_close_db(conn, should_commit=False)
            except Exception as e:
                logger.debug("Failed to close db connection in audit: %s", e)
    except Exception:
        pass


def _persist_circuit_state(event: str, *, details: dict) -> None:
    """Append a circuit-breaker event to memory_audit_log."""
    try:
        from db import connection_pool

        db_path = _get_db_path()
        conn = connection_pool.get(str(db_path), timeout=5.0)
        try:
            conn.execute(
                "INSERT INTO memory_audit_log ("
                "  ts, tool, args, results_count, latency_ms"
                ") VALUES (?, ?, ?, ?, ?)",
                (
                    time.time(),
                    f"auto_save_circuit_{event}",
                    json.dumps(details, default=str),
                    1,
                    0.0,
                ),
            )
            conn.commit()
        finally:
            try:
                from memory_common import safe_close_db

                safe_close_db(conn, should_commit=False)
            except Exception as exc:
                logger.debug(
                    "auto_save: safe_close_db during circuit-state persist failed (non-fatal): %s",
                    exc,
                )
    except Exception as exc:
        logger.debug(
            "auto_save: circuit-state persistence failed (non-fatal): %s",
            exc,
        )


def _auto_save_get_state() -> dict:
    """Return current backoff/breaker state (read-only copy)."""
    with _AUTO_SAVE_STATE_LOCK:
        state = {
            "failure_times": list(_AUTO_SAVE_STATE["failure_times"]),
            "circuit_open_until": _AUTO_SAVE_STATE["circuit_open_until"],
            "last_backoff_seconds": _AUTO_SAVE_STATE["last_backoff_seconds"],
        }
    state["circuit_open"] = _auto_save_circuit_open()
    return state


def _auto_save_reset_state() -> None:
    """Test helper: fully reset the backoff/breaker state."""
    with _AUTO_SAVE_STATE_LOCK:
        _AUTO_SAVE_STATE["failure_times"] = []
        _AUTO_SAVE_STATE["circuit_open_until"] = 0.0
        _AUTO_SAVE_STATE["last_backoff_seconds"] = 0.0


def _load_circuit_state_from_audit() -> None:
    """Load circuit breaker state from memory_audit_log on startup."""
    try:
        from db import connection_pool

        db_path = _get_db_path()
        conn = connection_pool.get(str(db_path), timeout=5.0)
        try:
            rows = conn.execute(
                """
                SELECT tool, args, ts
                FROM memory_audit_log
                WHERE tool IN ('auto_save_circuit_open', 'auto_save_circuit_close')
                ORDER BY ts DESC
                LIMIT 2
                """
            ).fetchall()

            if rows:
                latest_tool = rows[0][0]
                latest_args = json.loads(rows[0][1]) if rows[0][1] else {}
                now = time.time()

                with _AUTO_SAVE_STATE_LOCK:
                    if latest_tool == "auto_save_circuit_open":
                        open_until = latest_args.get("open_until", 0)
                        if open_until > now:
                            _AUTO_SAVE_STATE["circuit_open_until"] = open_until
                            n_failures = latest_args.get("n_failures", 3)
                            window = latest_args.get("window_s", 60.0)
                            _AUTO_SAVE_STATE["failure_times"] = [
                                now - window + i * (window / n_failures)
                                for i in range(n_failures)
                            ]
                        else:
                            _AUTO_SAVE_STATE["circuit_open_until"] = 0.0
                            _AUTO_SAVE_STATE["failure_times"] = []
                            _persist_circuit_state(
                                "close",
                                details={
                                    "reason": "timeout_expired_during_reload",
                                    "open_until_was": latest_args.get("open_until", 0),
                                    "recovered_at": time.time(),
                                },
                            )
                    elif latest_tool == "auto_save_circuit_close":
                        _AUTO_SAVE_STATE["circuit_open_until"] = 0.0
                        _AUTO_SAVE_STATE["failure_times"] = []
        finally:
            try:
                from memory_common import safe_close_db

                safe_close_db(conn, should_commit=False)
            except Exception as e:
                logger.debug("Failed to close db connection in load circuit: %s", e)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Helpers that live here because circuit_breaker.py owns the DB path.
# auto_save.py path helpers are also being migrated; this one is needed
# by circuit_breaker.py itself (and thus must be defined before the
# module-level _load_circuit_state_from_audit() call below).
# ---------------------------------------------------------------------------

def _get_db_path() -> "Path":
    """Resolve the memory database path."""
    from pathlib import Path
    import os
    env = os.environ.get("MEMORY_DB_PATH")
    if env:
        return Path(env)
    from infra.infrastructure import resolve_active_memory_dir
    return resolve_active_memory_dir() / "memory.db"


# Load circuit breaker state on module import (mirrors the auto_save.py behavior)
_load_circuit_state_from_audit()
