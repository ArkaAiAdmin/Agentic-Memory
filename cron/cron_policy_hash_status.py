#!/usr/bin/env python3
"""Fleet policy-posture divergence surveillance cron.

Periodically compares this node's enforcement posture (the resolved
config-drift ``policy_hash``) against every configured sync peer.  Emits a
single-line divergence summary that operators can grep from the cron log:

    FLEET-POLICY-STATUS: aligned=N divergent=M unreachable=K pending=P

and, when any peer diverges from the local posture:

    FLEET-DRIFT-ALERT: N peer(s) diverge from local posture

The full JSON result is always written to ``memory/fleet-policy-hash.log``
(convention: the per-cron log lives under the active memory dir, same place
``cron_check_config_drift.py`` writes its ``.drift_cron_*.json`` artifacts).

Zero configured peers is a normal state (single-node deployments): the
summary reports ``aligned=0 divergent=0 unreachable=0 pending=0`` and the
script exits 0 — it never crashes on an empty sync config.
"""
import argparse
import json
import logging
import os
import sys
from typing import Optional

from pathlib import Path

os.environ.setdefault("MEMORY_CONFIG_DRIFT_SKIP_ENFORCEMENT", "1")
os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger(__name__)

# Lock — don't overlap with other fleet-policy runs
try:
    from _flock import acquire_lock_or_exit
except ImportError:
    def acquire_lock_or_exit(name: str, max_attempts: int = 5) -> None:
        logger.error("cron_policy_hash_status: _flock module not available, cannot acquire lock")
        sys.exit(1)


def _log_path() -> Optional[Path]:
    try:
        from infra.infrastructure import resolve_active_memory_dir
        return resolve_active_memory_dir() / "fleet-policy-hash.log"
    except Exception as e:  # pragma: no cover - best-effort fallback
        logger.warning("cron_policy_hash_status: could not resolve memory dir: %s", e)
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Fleet policy-posture divergence surveillance.")
    parser.add_argument("--peer-timeout-s", type=float, default=5.0,
                        help="Per-peer HTTP timeout in seconds. Default: 5.")
    parser.add_argument("--max-concurrent", type=int, default=4,
                        help="Max peers queried concurrently. Default: 4.")
    parser.add_argument("--cache-ttl-s", type=float, default=60.0,
                        help="Peer-policy cache TTL in seconds. Default: 60.")
    parser.add_argument("--force-refresh", action="store_true",
                        help="Bypass the peer-policy cache and requery all peers.")
    parser.add_argument("--alert-stdout", action="store_true",
                        help="Print the divergence summary + alert to stdout "
                             "(the cron log captures this via >> redirect).")
    args = parser.parse_args()

    acquire_lock_or_exit("cron_policy_hash_status")

    from mcp_maintenance import memory_maintenance

    raw = memory_maintenance(
        "policy_hash_status",
        peer_timeout_s=args.peer_timeout_s,
        max_concurrent=args.max_concurrent,
        cache_ttl_s=args.cache_ttl_s,
        force_refresh=args.force_refresh,
        include_full_policy=False,
    )

    try:
        result = json.loads(raw)
    except (ValueError, TypeError) as e:
        logger.error("cron_policy_hash_status: failed to parse result: %s", e)
        return 1

    # The handler returns an error envelope on hard failure; surface it.
    if isinstance(result, dict) and result.get("error"):
        logger.error("cron_policy_hash_status: %s", result.get("error"))
        return 1

    summary = result.get("summary", {})
    aligned = int(summary.get("aligned", 0))
    divergent = int(summary.get("divergent", 0))
    unreachable = int(summary.get("unreachable", 0))
    pending = int(summary.get("pending", 0))

    status_line = (
        f"FLEET-POLICY-STATUS: aligned={aligned} divergent={divergent} "
        f"unreachable={unreachable} pending={pending}"
    )

    # Always persist the full JSON to the fleet log so on-call tooling can
    # read it later, independent of the cron stdout redirect.
    log_file = _log_path()
    if log_file is not None:
        try:
            with open(log_file, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(result, indent=2, default=str))
                fh.write("\n")
        except OSError as e:
            logger.warning("cron_policy_hash_status: failed to write log: %s", e)

    # stdout (captured by the cron log via >> redirect)
    print(status_line, file=sys.stdout)
    if divergent > 0:
        print(
            f"FLEET-DRIFT-ALERT: {divergent} peer(s) diverge from local posture",
            file=sys.stdout,
        )

    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    sys.exit(main())
