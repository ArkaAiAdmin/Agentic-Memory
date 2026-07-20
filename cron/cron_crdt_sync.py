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
import socket
import subprocess
import sys
import time
from pathlib import Path

os.environ.setdefault("MEMORY_MULTI_AGENT", "1")
os.environ.setdefault("MEMORY_CRDT_ENABLED", "1")

# Anchor at the package root so imports work regardless of cwd.
_PACKAGE_ROOT = Path(__file__).resolve().parent
if _PACKAGE_ROOT.name == "cron":
    _PACKAGE_ROOT = _PACKAGE_ROOT.parent
sys.path.insert(0, str(_PACKAGE_ROOT))
os.chdir(str(_PACKAGE_ROOT))

from config import get_config
from infra.log import setup_logging

logger = setup_logging(__name__)


def _server_is_listening(host: str, port: int, timeout: float = 1.0) -> bool:
    """Return True if a TCP server accepts connections on host:port."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _start_local_sync_server(cfg) -> subprocess.Popen | None:
    """Start the local CRDT sync server as a child process if configured.

    The sync server (``SyncServer``) is the peer endpoint configured in
    ``memory.toml`` (``sync.peers[].url`` typically points at the local
    ``listen_host:listen_port``). Previously nothing started this server, so
    every sync cycle failed with "Failed to push to <url>" — the peer was
    never listening. We start it here (when ``sync.enable_server`` is on) so
    the loopback peer actually converges.

    Returns the Popen if we started it (caller must terminate it), else None.
    """
    if not getattr(cfg, "sync_enable_server", False):
        return None
    host = getattr(cfg, "sync_listen_host", "127.0.0.1")
    port = int(getattr(cfg, "sync_listen_port", 9877))
    if _server_is_listening(host, port):
        # Already running (e.g. launched by a separate daemon) — don't manage it.
        return None
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "infra.sync_server_daemon",
             "--host", host, "--port", str(port),
             "--db-path", str(cfg.db_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("cron_crdt_sync: could not start local sync server: %s", exc)
        return None
    # Wait briefly for it to bind.
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if _server_is_listening(host, port):
            logger.info("cron_crdt_sync: started local sync server on %s:%d", host, port)
            return proc
        time.sleep(0.2)
    logger.warning("cron_crdt_sync: local sync server did not start in time")
    try:
        proc.terminate()
    except Exception:
        pass
    return None


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

    # Sprint 2.4 fix: the configured peer is typically the local sync server
    # (loopback). Nothing else starts it, so ensure it is listening before we
    # try to push/pull — otherwise every cycle fails with "Failed to push".
    server_proc = _start_local_sync_server(cfg)
    if server_proc is not None:
        # Give the server a moment to fully warm up the schema/migrations.
        time.sleep(0.5)

    try:
        for peer in peers:
            peer_name = peer.get("name", peer.get("agent_id", "unknown"))
            peer_url = peer.get("url", "")
            peer_agent_id = peer.get("agent_id", "")

            if not peer_url or not peer_agent_id:
                logger.warning("cron_crdt_sync: skipping incomplete peer config: %s", peer)
                continue

            # Skip self — don't sync with ourselves
            if peer_agent_id == local_agent_id:
                logger.debug("cron_crdt_sync: skipping self (%s)", peer_agent_id)
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

                # Sprint 2.4: Also sync KG with this peer
                try:
                    from infra.sync_client import sync_kg_with_peer
                    kg_result = sync_kg_with_peer(
                        db_path=str(db_path),
                        peer_url=peer_url,
                        peer_name=peer_name,
                        local_agent_id=local_agent_id,
                    )
                    if kg_result.get("pulled", 0) > 0 or kg_result.get("pushed", 0) > 0:
                        print(
                            f"  KG: pulled {kg_result.get('pulled', 0)}, "
                            f"pushed {kg_result.get('pushed', 0)}"
                        )
                    if kg_result.get("errors"):
                        print(f"  KG errors: {kg_result['errors']}")
                except Exception as kg_exc:
                    logger.debug("cron_crdt_sync: KG sync with %s failed: %s", peer_name, kg_exc)
            except Exception as e:
                logger.error("cron_crdt_sync: sync with %s failed: %s", peer_name, e)
                print(f"  ERROR: {e}")
    finally:
        # Stop the local sync server we started for this cycle (if any).
        if server_proc is not None:
            try:
                server_proc.terminate()
                server_proc.wait(timeout=5)
            except Exception as sp_exc:
                logger.warning("cron_crdt_sync: stop local sync server: %s", sp_exc)

    print(f"Sync cycle complete: {len(results)}/{len(peers)} peers synced.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
