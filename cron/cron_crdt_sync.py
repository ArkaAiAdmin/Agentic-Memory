#!/usr/bin/env python3
"""Cron wrapper: auto multi-agent CRDT sync.

Reads configured peers from ``memory.toml`` and runs a two-way sync
(push local changes, pull remote changes) with each peer.

Designed to be scheduled via cron every N minutes (set via
``sync.schedule.interval_minutes`` in memory.toml, but the cron
entry itself must be set up separately — this script runs one
cycle and exits).
"""

from __future__ import annotations

from _flock import acquire_lock_or_exit
import os
import sys
import logging
from pathlib import Path

os.environ.setdefault("MEMORY_MULTI_AGENT", "1")
os.environ.setdefault("MEMORY_CRDT_ENABLED", "1")

# Anchor at the package root so imports work regardless of cwd.
_PACKAGE_ROOT = Path(__file__).resolve().parent
if _PACKAGE_ROOT.name == "cron":
    _PACKAGE_ROOT = _PACKAGE_ROOT.parent
sys.path.insert(0, str(_PACKAGE_ROOT))
os.chdir(str(_PACKAGE_ROOT))

from infra.memory_common import configure_logging
from config import get_config

configure_logging()
logger = logging.getLogger(__name__)


def main() -> int:
    # argparse handles --help and exits cleanly. The pipeline itself
    # takes no flags.
    if "--help" in sys.argv[1:] or "-h" in sys.argv[1:]:
        print("usage: %s [-h|--help]" % sys.argv[0], file=sys.stderr)
        print("Cron job — runs the scheduled operation; no flags required.", file=sys.stderr)
        sys.exit(0)

    cfg = get_config()
    acquire_lock_or_exit('cron_crdt_sync')

    # Resolve DB path.
    env_path = os.environ.get("MEMORY_DB_PATH")
    if env_path:
        db_path = Path(env_path)
    else:
        db_path = Path(cfg.db_path)

    if not db_path.exists():
        print(f"ERROR: memory.db not found at {db_path}")
        return 1

    peers = cfg.sync_peers
    if not peers:
        print("No sync peers configured. Add [[sync.peers]] to memory.toml.")
        return 0

    from infra.sync_client import sync_with_peer
    from save.crdt_helpers import _crdt_agent_id

    local_agent_id = _crdt_agent_id()
    results = []

    for peer in peers:
        peer_name = peer.get("name", peer.get("agent_id", "unknown"))
        peer_url = peer.get("url", "")
        peer_agent_id = peer.get("agent_id", "")

        if not peer_url or not peer_agent_id:
            logger.warning("cron_crdt_sync: skipping incomplete peer config: %s", peer)
            continue

        print(f"Syncing with {peer_name} ({peer_url})...")
        try:
            result = sync_with_peer(
                db_path=str(db_path),
                peer_url=peer_url,
                peer_name=peer_name,
                peer_agent_id=peer_agent_id,
                local_agent_id=local_agent_id,
            )
            results.append((peer_name, result))
            if result.get("success"):
                push = result.get("push", {})
                pull = result.get("pull", {})
                print(
                    f"  OK: pushed {push.get('total', 0)}, "
                    f"pulled {pull.get('total', 0)} "
                    f"({result.get('duration_ms', 0)}ms)"
                )
            else:
                err = result.get("push", {}).get("error", "") or result.get(
                    "pull", {}
                ).get("error", "")
                print(f"  FAILED: {err}")
        except Exception as e:
            logger.error("cron_crdt_sync: sync with %s failed: %s", peer_name, e)
            print(f"  ERROR: {e}")

    print(f"Sync cycle complete: {len(results)}/{len(peers)} peers synced.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
