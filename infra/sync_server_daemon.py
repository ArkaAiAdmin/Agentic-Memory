#!/usr/bin/env python3
"""Standalone entry point for the local CRDT sync server daemon.

Launched by ``cron/cron_crdt_sync.py`` when the configured peer is the local
sync server (``sync.enable_server = true`` in memory.toml) and no server is
already listening. Exits on SIGTERM/SIGINT.

Usage:
    python -m infra.sync_server_daemon --host 127.0.0.1 --port 9877 \
        --db-path /path/to/memory.db
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PACKAGE_ROOT))
os.chdir(str(_PACKAGE_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="Local CRDT sync server daemon")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9877)
    parser.add_argument("--db-path", required=True)
    args = parser.parse_args()

    from infra.sync_server import SyncServer
    from infra.log import setup_logging

    logger = setup_logging("sync_server_daemon")

    server = SyncServer(
        db_path=args.db_path,
        agent_id=os.environ.get("MEMORY_AGENT_ID", "local"),
        host=args.host,
        port=args.port,
    )
    server.port = args.port

    _stop = {"requested": False}

    def _handle(signum, _frame):
        _stop["requested"] = True
        try:
            server.stop()
        except Exception:
            pass

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    logger.info("sync_server_daemon: starting on %s:%d", args.host, args.port)
    server.start()
    try:
        while not _stop["requested"]:
            signal.pause() if hasattr(signal, "pause") else sys.exit(0)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
