#!/usr/bin/env python3
"""Cron wrapper: one-shot peer sync (P2 #2 wire-up).

Runs a single two-way sync to the peer URL configured via:

* ``MEMORY_SYNC_PEER`` env var, or
* ``[sync].peers[0].url`` from ``memory.toml`` (the first peer wins
  for the one-shot case — use ``cron/cron_crdt_sync.py`` if you need
  to sync with multiple peers in one cycle).

Each invocation writes one row to ``sync_log`` (via
``sync_client.sync_with_peer``), so this cron is what populates the
``sync_log`` table over time. The table being empty is the P2 #2
signal that this cron has never run.

The cron is *self-disabling*: if no peer is configured and no peer
URL is provided, it exits 0 with a one-line notice.
"""

from __future__ import annotations

from _flock import acquire_lock_or_exit
import os
import sys
import logging
from pathlib import Path

os.environ.setdefault("MEMORY_CRDT_ENABLED", "1")

_PACKAGE_ROOT = Path(__file__).resolve().parent
if _PACKAGE_ROOT.name == "cron":
    _PACKAGE_ROOT = _PACKAGE_ROOT.parent
sys.path.insert(0, str(_PACKAGE_ROOT))
os.chdir(str(_PACKAGE_ROOT))

from memory_common import configure_logging

configure_logging()
logger = logging.getLogger(__name__)


def _resolve_peer_url_from_config() -> str:
    """Return the first configured peer's URL, or '' if none."""
    try:
        from _lazy_imports import get_config

        cfg = get_config()
    except Exception:
        return ""
    peers = getattr(cfg, "sync_peers", None) or []
    for peer in peers:
        url = (peer.get("url") if isinstance(peer, dict) else None) or ""
        if url:
            return url
    return ""


def main() -> int:
    if "--help" in sys.argv[1:] or "-h" in sys.argv[1:]:
        print("usage: %s [-h|--help]" % sys.argv[0], file=sys.stderr)
        print("Cron job — runs the scheduled operation; no flags required.", file=sys.stderr)
        sys.exit(0)

    from sync_client import sync_once
    acquire_lock_or_exit('cron_sync')

    peer_url = (
        os.environ.get("MEMORY_SYNC_PEER", "").strip()
        or _resolve_peer_url_from_config()
    )
    if not peer_url:
        print(
            "cron_sync: no peer configured (set MEMORY_SYNC_PEER or [[sync.peers]]); skipping."
        )
        return 0

    peer_name = os.environ.get("MEMORY_SYNC_PEER_NAME", "").strip() or None
    peer_agent_id = os.environ.get("MEMORY_SYNC_PEER_AGENT_ID", "").strip() or None
    db_path = os.environ.get("MEMORY_DB_PATH", "").strip() or None
    try:
        limit = int(os.environ.get("MEMORY_SYNC_LIMIT", "200"))
    except ValueError:
        limit = 200

    print(f"cron_sync: peer={peer_url} db={db_path or '(default)'}")
    try:
        result = sync_once(
            peer_url=peer_url,
            db_path=db_path,
            peer_name=peer_name,
            peer_agent_id=peer_agent_id,
            limit=limit,
        )
    except Exception as e:
        logger.error("cron_sync: sync_once failed: %s", e)
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if "error" in result and not result.get("push"):
        print(f"ERROR: {result['error']}", file=sys.stderr)
        return 1

    push = result.get("push", {})
    pull = result.get("pull", {})
    print(
        f"cron_sync: pushed={push.get('total', 0)} "
        f"pulled={pull.get('total', 0)} "
        f"success={result.get('success', False)} "
        f"duration={result.get('duration_ms', 0)}ms"
    )
    if not result.get("success"):
        err = push.get("error", "") or pull.get("error", "")
        print(f"  WARN: {err}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
