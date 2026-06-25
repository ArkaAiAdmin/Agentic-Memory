#!/usr/bin/env python3
"""Cron wrapper: auto-share high-importance memories.

P2 #1 wire-up. Scans the local ``memories`` table for share-worthy
notes (high importance, high fitness, not already in the shared pool)
and copies them into ``shared_memories`` so the cross-agent sharing
feature has real content to operate on.

The cron is intentionally conservative: defaults require
``importance >= 4`` and ``fitness_score >= 0.6`` and cap each cycle
at 25 notes, so a runaway cron cannot flood the pool.

Configuration:

* ``MEMORY_MULTI_AGENT`` env var (or ``[features].multi_agent`` in
  ``memory.toml``) gates the whole feature — if disabled, this cron
  is a no-op.
* ``MEMORY_AUTO_SHARE_MIN_IMPORTANCE`` overrides the importance cutoff.
* ``MEMORY_AUTO_SHARE_MIN_FITNESS`` overrides the fitness cutoff.
* ``MEMORY_AUTO_SHARE_MAX_PER_CYCLE`` overrides the per-cycle cap.
"""

from __future__ import annotations

from _flock import acquire_lock_or_exit
import os
import sys
import logging
from pathlib import Path

os.environ.setdefault("MEMORY_MULTI_AGENT", "1")

_PACKAGE_ROOT = Path(__file__).resolve().parent
if _PACKAGE_ROOT.name == "cron":
    _PACKAGE_ROOT = _PACKAGE_ROOT.parent
sys.path.insert(0, str(_PACKAGE_ROOT))
os.chdir(str(_PACKAGE_ROOT))

from memory_common import configure_logging

configure_logging()
logger = logging.getLogger(__name__)


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r is not an int; using default %d", name, raw, default)
        return default


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s=%r is not a float; using default %f", name, raw, default)
        return default


def main() -> int:
    if "--help" in sys.argv[1:] or "-h" in sys.argv[1:]:
        print("usage: %s [-h|--help]" % sys.argv[0], file=sys.stderr)
        print("Cron job — runs the scheduled operation; no flags required.", file=sys.stderr)
        sys.exit(0)

    import memory_sharing as ma
    acquire_lock_or_exit('cron_auto_share')

    if not ma.MULTI_AGENT_ENABLED:
        print("MEMORY_MULTI_AGENT not enabled, skipping auto-share.")
        return 0

    min_importance = _int_env(
        "MEMORY_AUTO_SHARE_MIN_IMPORTANCE", ma._AUTO_SHARE_MIN_IMPORTANCE
    )
    min_fitness = _float_env(
        "MEMORY_AUTO_SHARE_MIN_FITNESS", ma._AUTO_SHARE_MIN_FITNESS
    )
    max_per_cycle = _int_env(
        "MEMORY_AUTO_SHARE_MAX_PER_CYCLE", ma._AUTO_SHARE_MAX_PER_CYCLE
    )

    try:
        from save.crdt_helpers import _crdt_agent_id

        agent_id = _crdt_agent_id()
    except Exception:
        agent_id = "auto-share"

    print(
        f"auto_share: scanning importance>={min_importance} "
        f"fitness>={min_fitness:.2f} cap={max_per_cycle} agent={agent_id}"
    )

    try:
        result = ma.auto_share_high_value(
            agent_id=agent_id,
            min_importance=min_importance,
            min_fitness=min_fitness,
            limit=max_per_cycle,
        )
    except Exception as e:
        logger.error("auto_share cycle failed: %s", e)
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if not result.get("enabled"):
        print("multi-agent sharing disabled — no-op")
        return 0

    if "error" in result:
        print(f"ERROR: {result['error']}", file=sys.stderr)
        return 1

    print(
        f"auto_share: scanned={result.get('scanned', 0)} "
        f"shared={result.get('shared', 0)} "
        f"skipped={result.get('skipped', 0)}"
    )
    for sid in result.get("shared_ids", []):
        print(f"  + {sid}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
