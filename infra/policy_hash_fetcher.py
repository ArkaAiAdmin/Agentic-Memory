"""Fetch policy_hash from peer nodes.

Uses urllib.request (existing dep) to call <peer_url>/crdt/policy_hash —
the REST route the sync server exposes (mirrors memory_admin_policy_hash).
Auth via MEMORY_SYNC_TOKEN Bearer header (same pattern as sync_client.py).
"""
from __future__ import annotations

import json
import logging
import socket
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed


logger = logging.getLogger(__name__)


def _peer_policy_url(peer_url: str) -> str:
    base = peer_url.rstrip("/")
    return f"{base}/crdt/policy_hash"

def fetch_peer_policy_hash(
    peer_url: str,
    *,
    timeout_s: float = 5.0,
    sync_token: str = "",
) -> tuple[str, float, dict]:
    url = _peer_policy_url(peer_url)
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(url, method="GET")
        if sync_token:
            req.add_header("Authorization", f"Bearer {sync_token}")
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return "ok", time.monotonic() - t0, payload
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return "auth_failed", time.monotonic() - t0, {}
        return "bad_response", time.monotonic() - t0, {"code": e.code}
    except (urllib.error.URLError, socket.timeout, TimeoutError):
        return "unreachable", time.monotonic() - t0, {}


def fetch_all_peer_hashes(
    peers: list[dict],
    *,
    timeout_s: float = 5.0,
    max_concurrent: int = 4,
    sync_token: str = "",
) -> dict[str, tuple[str, float, dict]]:
    results: dict[str, tuple[str, float, dict]] = {}
    workers = min(max_concurrent, len(peers) or 1)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(fetch_peer_policy_hash, p["url"],
                      timeout_s=timeout_s, sync_token=sync_token): p
            for p in peers
        }
        for fut in as_completed(futures):
            p = futures[fut]
            try:
                status, latency, body = fut.result()
                results[p.get("name", p.get("agent_id", "?"))] = (status, latency, body)
            except Exception:
                name = p.get("name", p.get("agent_id", "?"))
                results[name] = ("unreachable", timeout_s, {})
    return results
