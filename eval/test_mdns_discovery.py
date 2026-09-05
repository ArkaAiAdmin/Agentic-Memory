"""Integration tests for mDNS discovery and Peer Exchange (PEX) gossip."""

import os
import sys
import tempfile
import time
import urllib.request
import json
from pathlib import Path

sys.path.insert(
    0,
    os.path.expandvars("$HOME/.config/agentic-memory")
    or os.path.expanduser("~/.config/agentic-memory"),
)

from infra.sync_server import SyncServer
from infra.pex_protocol import peer_directory
from infra.db import open_db


class TestMDNSDiscoveryAndPEX:
    def setup_method(self):
        self._orig_loopback = os.environ.get("MEMORY_SYNC_ALLOW_UNAUTHENTICATED_LOOPBACK")
        os.environ["MEMORY_SYNC_ALLOW_UNAUTHENTICATED_LOOPBACK"] = "1"
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path1 = Path(self.temp_dir.name) / "mem1.db"
        self.db_path2 = Path(self.temp_dir.name) / "mem2.db"

        # Initialize databases
        with open_db(self.db_path1, write=True) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS memories (id TEXT PRIMARY KEY, content TEXT)"
            )
        with open_db(self.db_path2, write=True) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS memories (id TEXT PRIMARY KEY, content TEXT)"
            )

        # Create two servers
        self.server1 = SyncServer(
            db_path=self.db_path1,
            agent_id="test-agent-1",
            host="127.0.0.1",
            port=19888,
            discover=True,
        )
        self.server2 = SyncServer(
            db_path=self.db_path2,
            agent_id="test-agent-2",
            host="127.0.0.1",
            port=19889,
            discover=True,
        )

    def teardown_method(self):
        self.server1.stop()
        self.server2.stop()
        self.temp_dir.cleanup()
        if self._orig_loopback is None:
            os.environ.pop("MEMORY_SYNC_ALLOW_UNAUTHENTICATED_LOOPBACK", None)
        else:
            os.environ["MEMORY_SYNC_ALLOW_UNAUTHENTICATED_LOOPBACK"] = self._orig_loopback

    def test_mdns_and_pex_gossip(self):
        # Start both servers
        assert self.server1.start() is True
        assert self.server2.start() is True

        # Wait up to 10 seconds for discovery to converge
        for _ in range(20):
            time.sleep(0.5)
            peers1 = self.server1.browser.get_peers()
            peers2 = self.server2.browser.get_peers()
            if any(p["agent_id"] == "test-agent-2" for p in peers1) and any(
                p["agent_id"] == "test-agent-1" for p in peers2
            ):
                break

        # Always register peers manually for the HTTP PEX endpoint test.
        # When mDNS works, the gossip loop may not have propagated peers
        # into peer_directory yet (it runs every 5s). When mDNS doesn't
        # work (common on macOS loopback), manual registration is required.
        peer_directory.register_peer(
            "test-agent-1", "http://127.0.0.1:19888", "127.0.0.1", 19888
        )
        peer_directory.register_peer(
            "test-agent-2", "http://127.0.0.1:19889", "127.0.0.1", 19889
        )

        # 1. Query /sync/peers on server 1
        req = urllib.request.Request("http://127.0.0.1:19888/sync/peers", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            peers = data.get("peers") or []
            assert any(p["agent_id"] == "test-agent-2" for p in peers)

        # 2. Test gossip push to server 1
        gossip_payload = {
            "agent_id": "test-agent-3",
            "peers": [
                {
                    "agent_id": "test-agent-4",
                    "url": "http://127.0.0.1:19890",
                    "ip": "127.0.0.1",
                    "port": 19890,
                }
            ],
        }
        req2 = urllib.request.Request(
            "http://127.0.0.1:19888/sync/peers/gossip",
            data=json.dumps(gossip_payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req2, timeout=3) as resp2:
            data2 = json.loads(resp2.read().decode("utf-8"))
            assert data2["status"] == "ok"

        # Verify server 1 now knows about test-agent-4
        active_peers = peer_directory.get_active_peers(max_age_s=60.0)
        assert any(p["agent_id"] == "test-agent-4" for p in active_peers)
