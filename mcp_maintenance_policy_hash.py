"""Peer-facing admin endpoints for drift-policy hash — MCP tool + router handler."""
from __future__ import annotations

import json
import logging
import os
import socket
import time

from mcp_instance import mcp  # noqa: F401
from infra.memory_common import configure_logging  # noqa: F401
from infra.infrastructure import (  # noqa: F401
    resolve_active_memory_dir,
)
from mcp_common import with_audit
from infra.config_drift_policy import resolve_policy
from infra.policy_hash_fetcher import fetch_all_peer_hashes
from infra.policy_hash_cache import (
    load_peer_cache, persist_peer_cache, filter_stale_entries,
)
from infra.policy_hash_diff import dict_diff

logger = logging.getLogger(__name__)


def policy_hash_status(
    *,
    peer_timeout_s: float = 5.0,
    max_concurrent: int = 4,
    cache_ttl_s: float = 60.0,
    force_refresh: bool = False,
    include_full_policy: bool = False,
    since_ts: float | None = None,
) -> str:
    try:
        local_policy = resolve_policy()
        local_hash = local_policy.policy_hash()

        try:
            from sync import SyncManager
            sync_status = SyncManager().status()
            peers = sync_status.get("peers", [])
        except Exception:
            peers = []

        now = time.time()
        cache = ({} if force_refresh else load_peer_cache())

        if force_refresh:
            peers_to_query = peers
            cached_for_peers = {}
        else:
            fresh, stale = filter_stale_entries(cache, cache_ttl_s)
            peers_to_query = [
                p for p in peers
                if p.get("name") in stale or p.get("agent_id") in stale
            ]
            cached_for_peers = fresh

        sync_token = os.environ.get("MEMORY_SYNC_TOKEN", "")
        fresh_results = (
            fetch_all_peer_hashes(
                peers_to_query,
                timeout_s=peer_timeout_s,
                max_concurrent=max_concurrent,
                sync_token=sync_token,
            )
            if peers_to_query
            else {}
        )

        merged = {}
        for p in peers:
            name = p.get("name", p.get("agent_id", "?"))
            if name in fresh_results:
                status, latency, body = fresh_results[name]
                entry = {
                    "status": status,
                    "fetched_at": now,
                    "fetched_via": "live",
                    "latency_s": round(latency, 4),
                    "peer_url": p.get("url", ""),
                    "agent_id": p.get("agent_id", ""),
                    **(
                        {k: v for k, v in body.items() if k != "full_policy"}
                        if not include_full_policy
                        else body
                    ),
                }
            elif name in cached_for_peers:
                entry = dict(cached_for_peers[name], fetched_via="cache")
            else:
                entry = {
                    "status": "pending",
                    "peer_url": p.get("url", ""),
                    "agent_id": p.get("agent_id", ""),
                }
            merged[name] = entry

        persist_peer_cache(merged)

        aligned, divergent, unreachable, pending = [], [], [], []
        for name, entry in merged.items():
            peer_hash = entry.get("policy_hash", "")
            if entry.get("status") == "unreachable":
                unreachable.append(name)
            elif entry.get("status") == "pending":
                pending.append(name)
            elif peer_hash and peer_hash == local_hash:
                aligned.append(name)
            elif peer_hash:
                local_dict = local_policy.to_dict()
                peer_dict = {
                    k: entry.get(k)
                    for k in entry
                    if k not in ("fetched_at", "fetched_via", "latency_s", "status")
                }
                delta_keys = dict_diff(local_dict, peer_dict) if peer_dict else ["policy_hash"]
                divergent.append({
                    "name": name,
                    "peer_policy_hash": peer_hash,
                    "local_policy_hash": local_hash,
                    "delta_keys": delta_keys[:20],
                })

        out = {
            "schema_version": 1,
            "generated_at": now,
            "local": {
                "host": socket.gethostname(),
                "agent_id": getattr(local_policy, "agent_id", "") or "",
                "policy_hash": local_hash,
                "scope": local_policy.scope,
            },
            "peers": list(merged.values()),
            "summary": {
                "total_peers": len(merged),
                "aligned": len(aligned),
                "divergent": len(divergent),
                "unreachable": len(unreachable),
                "pending": len(pending),
            },
            "divergent_peers": divergent,
            "cache_ttl_s": cache_ttl_s,
        }
        return json.dumps(out, indent=2, default=str)
    except Exception as e:
        logger.warning("Unhandled exception in policy_hash_status: %s", e)
        from mcp_common import _err, classify_exception
        return _err(classify_exception(e), str(e))


@mcp.tool()
@with_audit("memory_admin_policy_hash")
def memory_admin_policy_hash(*, include_full: bool = False) -> str:
    """Return the local process's drift-policy hash. Used by fleet drift diff."""
    p = resolve_policy()
    body = {
        "policy_hash": p.policy_hash(),
        "scope": p.scope,
        "agent_id": "",
        "schema_version": 1,
    }
    if include_full:
        body["full_policy"] = p.to_dict()
    return json.dumps(body, indent=2)
