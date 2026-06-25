"""CRDT version vector helpers for the save pipeline.

Extracted from save_pipeline.py (2026-06-20) as part of the god-module
decomposition. Behavior is identical; this module exists so callers
that only need the CRDT bookkeeping primitives don't have to import
the full save pipeline.

The three functions here are:
- _crdt_agent_id(): resolve the local agent identifier
- _is_crdt_enabled(): check the CRDT feature flag
- _crdt_bump_version(db, note_id, cols): increment the legacy note-level VV

Note: in v13, the per-field CRDT (crdt_field.py) is the source of truth.
These functions are kept for backward compat with the legacy note-level
LWW path and for tests.
"""

from __future__ import annotations

import json
import logging
import os
import socket

logger = logging.getLogger(__name__)

try:
    from crdt_merge import parse_version_vector
except ImportError:  # FLAVOR_A: optional dependency guard
    parse_version_vector = None  # type: ignore[assignment]

try:
    from _lazy_imports import get_config as _get_config
except ImportError:  # FLAVOR_A: optional dependency guard
    _get_config = None


def _crdt_agent_id() -> str:
    """Return the local agent identifier for CRDT version tracking.

    Resolution order: MEMORY_AGENT_ID env var > config.get().agent_id >
    socket.gethostname() > "local".
    """
    env_id = os.environ.get("MEMORY_AGENT_ID")
    if env_id:
        return env_id
    if _get_config is not None:
        try:
            cfg = _get_config()
            if cfg.agent_id:
                return str(cfg.agent_id)
        except Exception:
            logger.warning("Failed to fetch agent_id from config")
            pass
    try:
        return socket.gethostname()
    except Exception:  # FLAVOR_A: hostname fallback for containerized envs
        return "local"


def _is_crdt_enabled() -> bool:
    """Check if CRDT version tracking is enabled.

    Resolution: MEMORY_CRDT_ENABLED env var > config > True (default).
    """
    env_val = os.environ.get("MEMORY_CRDT_ENABLED")
    if env_val is not None:
        return env_val.strip().lower() in ("1", "true", "yes", "on")
    if _get_config is not None:
        try:
            cfg = _get_config()
            return bool(cfg.crdt_enabled)
        except Exception:
            logger.warning("Failed to fetch crdt_enabled from config")
            pass
    return True


def _crdt_bump_version(db, note_id: str, cols: set) -> None:
    """Bump the CRDT version vector for ``note_id``.

    Reads the current ``version_vector`` and ``logical_clock`` columns,
    increments the local agent's counter, and writes back the new values.
    No-op if the CRDT columns don't exist in the schema.
    Best-effort: failures are logged, never propagated.
    """
    if not {"version_vector", "logical_clock"}.issubset(cols):
        return
    if parse_version_vector is None:
        return
    try:
        row = db.execute(
            "SELECT version_vector, logical_clock FROM memories WHERE id=?",
            (note_id,),
        ).fetchone()
        if row is None:
            return
        existing_vv_str = row[0]
        vv = parse_version_vector(existing_vv_str)
        agent = _crdt_agent_id()
        vv[agent] = vv.get(agent, 0) + 1
        new_vv_str = json.dumps(vv, sort_keys=True)
        new_clock = vv[agent]
        db.execute(
            "UPDATE memories SET version_vector=?, logical_clock=? WHERE id=?",
            (new_vv_str, new_clock, note_id),
        )
    except Exception:
        logger.debug("CRDT bump_version failed for %s (benign)", note_id)
