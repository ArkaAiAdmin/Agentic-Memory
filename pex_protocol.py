"""Peer Exchange (PEX) Gossip Protocol for Agentic Memory.

Maintains a thread-safe registry of known sync peers and implements gossip routing.
"""

from __future__ import annotations

import json
import logging
import urllib.request
import urllib.error
import time
from typing import Any, Dict, List, Set, Tuple

logger = logging.getLogger(__name__)


class PeerDirectory:
    """Thread-safe directory of discovered and gossiped peers."""

    def __init__(self) -> None:
        self._lock = threading.Lock() if "threading" in globals() else None
        # We'll import threading lazily if needed, but it's safe to use stdlib threading
        import threading
        self._lock = threading.Lock()
        # map: agent_id -> {url, ip, port, last_seen, source}
        self.peers: Dict[str, Dict[str, Any]] = {}

    def register_peer(
        self,
        agent_id: str,
        url: str,
        ip: str,
        port: int,
        source: str = "mdns",
    ) -> bool:
        """Register or update a peer. Returns True if it is a new peer."""
        with self._lock:
            is_new = agent_id not in self.peers
            self.peers[agent_id] = {
                "agent_id": agent_id,
                "url": url.rstrip("/"),
                "ip": ip,
                "port": port,
                "last_seen": time.time(),
                "source": source,
            }
            return is_new

    def merge_peers(self, peer_list: List[Dict[str, Any]], source: str = "gossip") -> int:
        """Merge a list of peers. Returns number of newly registered peers."""
        added = 0
        for p in peer_list:
            aid = p.get("agent_id")
            url = p.get("url")
            ip = p.get("ip") or "127.0.0.1"
            port = p.get("port")
            if aid and url and port:
                if self.register_peer(aid, url, ip, int(port), source=source):
                    added += 1
        return added

    def get_active_peers(self, max_age_s: float = 60.0) -> List[Dict[str, Any]]:
        """Return list of peers seen within the max_age threshold."""
        now = time.time()
        active = []
        with self._lock:
            for aid, p in list(self.peers.items()):
                if now - p["last_seen"] < max_age_s:
                    active.append({
                        "agent_id": aid,
                        "url": p["url"],
                        "ip": p["ip"],
                        "port": p["port"],
                    })
                else:
                    self.peers.pop(aid, None)
        return active


# Global singleton peer directory
peer_directory = PeerDirectory()


def send_gossip(target_url: str, local_agent_id: str, peers: List[Dict[str, Any]]) -> Optional[dict]:
    """POST local peer list to target peer's gossip endpoint."""
    url = f"{target_url.rstrip('/')}/sync/peers/gossip"
    payload = {
        "agent_id": local_agent_id,
        "peers": peers,
    }
    try:
        body_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body_bytes,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body)
    except Exception as e:
        logger.debug("Failed to send PEX gossip to %s: %s", url, e)
        return None
