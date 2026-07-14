#!/usr/bin/env python3
"""Integration tests for the REST and WebSocket API server.
"""

import base64
import hashlib
import json
import os
import socket as _socket
import sqlite3
import sys
import tempfile
import time
import urllib.request
import urllib.error
import unittest
from pathlib import Path
from typing import Tuple

INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))

# Ensure MEMORY_DB_PATH is set before any config imports are initialized
_TEST_DB_DIR = tempfile.mkdtemp(prefix="api_test_setup_")
_TEST_DB_PATH = Path(_TEST_DB_DIR) / "memory.db"
_OLD_DB_PATH = os.environ.get("MEMORY_DB_PATH")
os.environ["MEMORY_DB_PATH"] = str(_TEST_DB_PATH)

from infra.memory_common import connection_pool
from infra.db_migrations import run_schema_setup
from infra.api_server import APIServer
from agentic_memory.client import MemoryClient


def _get_free_port() -> int:
    """Find an available port on localhost."""
    s = _socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_for_server(host: str, port: int, timeout: float = 5.0) -> None:
    """Poll the server port until it accepts connections or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            s = _socket.create_connection((host, port), timeout=0.5)
            s.close()
            return
        except (OSError, ConnectionRefusedError):
            time.sleep(0.05)
    raise RuntimeError(f"Server {host}:{port} did not start within {timeout}s")


class TestAPIServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_path = _TEST_DB_PATH
        
        # Initialize database schema including migration 031 outbox events
        connection_pool.clear()
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(str(cls.db_path))
        conn.execute("PRAGMA foreign_keys=ON")
        run_schema_setup(conn)
        conn.close()

        cls.port = _get_free_port()
        cls.host = "127.0.0.1"
        cls.token = "test-api-token-0123456789abcdef"
        cls.server = APIServer(
            db_path=cls.db_path,
            agent_id="test-agent",
            host=cls.host,
            port=cls.port,
            token=cls.token,
        )
        cls.server.start()
        _wait_for_server(cls.host, cls.port)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        connection_pool.clear()
        if _OLD_DB_PATH is not None:
            os.environ["MEMORY_DB_PATH"] = _OLD_DB_PATH
        else:
            os.environ.pop("MEMORY_DB_PATH", None)

    def setUp(self):
        # Clear SDK memories before each test
        client = MemoryClient(db_path=self.db_path)
        client.clear()
        
        # Clear event outbox table to isolate tests
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("DELETE FROM memory_events")
        conn.commit()
        conn.close()

    def _http_request(self, path: str, method: str = "GET", body: dict | None = None, timeout: float = 30.0) -> Tuple[int, dict]:
        url = f"http://{self.host}:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                return res.status, json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                err_data = json.loads(e.read().decode("utf-8"))
            except Exception:
                err_data = {"error": e.reason}
            return e.code, err_data

    def test_health_check(self):
        status, data = self._http_request("/health")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "healthy")
        self.assertEqual(data["agent_id"], "test-agent")

    def test_add_and_get_memory(self):
        # 1. Add memory
        status, data = self._http_request("/api/v1/memories", "POST", {
            "content": "User prefers spacing of 4 spaces in indentation",
            "tags": ["formatting", "python"],
            "category": "sdk",
            "is_global": False
        })
        self.assertEqual(status, 201)
        self.assertEqual(data["status"], "success")
        note_id = data["id"]
        self.assertTrue(note_id)

        # 2. Get details
        status, get_data = self._http_request(f"/api/v1/memories/{note_id}")
        self.assertEqual(status, 200)
        self.assertEqual(get_data["id"], note_id)
        self.assertEqual(get_data["content"], "User prefers spacing of 4 spaces in indentation")
        self.assertIn("formatting", get_data["tags"])
        self.assertEqual(get_data["category"], "sdk")

    def test_is_global_defaults_to_false(self):
        # G6: omitting is_global from the request body must default to False.
        # The REST server must NOT share writes into the global pool by
        # default (the pre-hardening default was True, a cross-tenant hole).
        # We capture the kwarg passed to MemoryClient.save rather than
        # asserting on a DB column, because is_global is not stored as a
        # standalone column on the memories table.
        captured = {}
        original_save = MemoryClient.save

        def fake_save(self, **kwargs):
            captured["is_global"] = kwargs.get("is_global")
            return "fake-note-id-" + str(kwargs.get("category", "x"))

        MemoryClient.save = fake_save
        try:
            status, data = self._http_request("/api/v1/memories", "POST", {
                "content": "default is_global regression check",
                "category": "sdk",
            })
        finally:
            MemoryClient.save = original_save
        self.assertEqual(status, 201)
        self.assertEqual(captured.get("is_global"), False,
                         "is_global must default to False when omitted from the body")

    def test_search_memories(self):
        # Add a record
        status, data = self._http_request("/api/v1/memories", "POST", {
            "content": "Rust memory safety is guaranteed by borrow checker",
            "tags": ["rust"],
            "is_global": False
        })
        self.assertEqual(status, 201)

        # Search via GET — use longer timeout (embedding model cold-start)
        status, data = self._http_request("/api/v1/memories/search?query=borrow+checker", timeout=30.0)
        self.assertEqual(status, 200)
        self.assertGreater(len(data["results"]), 0)
        self.assertIn("Rust memory safety", data["results"][0]["content"])

        # Search via POST
        status, post_data = self._http_request("/api/v1/memories/search", "POST", {
            "query": "borrow checker",
            "limit": 5
        }, timeout=30.0)
        self.assertEqual(status, 200)
        self.assertGreater(len(post_data["results"]), 0)
        self.assertIn("Rust memory safety", post_data["results"][0]["content"])

    def test_delete_memory(self):
        # Add memory
        status, data = self._http_request("/api/v1/memories", "POST", {
            "content": "Temporary scratchpad reminder",
            "is_global": False
        })
        note_id = data["id"]

        # Delete memory
        status, del_data = self._http_request(f"/api/v1/memories/{note_id}", "DELETE")
        self.assertEqual(status, 200)
        self.assertTrue(del_data["success"])

        # Getting deleted memory should return 410 (or 404 depending on trash state)
        status, get_data = self._http_request(f"/api/v1/memories/{note_id}")
        self.assertIn(status, (404, 410))

    def test_stats(self):
        status, data = self._http_request("/api/v1/memories/stats")
        self.assertEqual(status, 200)
        self.assertIn("memories", data)
        self.assertIn("vector_keys", data)

    def test_websocket_ping_pong_and_broadcast(self):
        # Connect raw socket for WS handshake
        sock = _socket.create_connection((self.host, self.port), timeout=2.0)
        
        # Handshake
        key = "dGhlIHNhbXBsZSBub25jZQ=="
        handshake = (
            "GET /ws HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Authorization: Bearer {self.token}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(handshake.encode("utf-8"))
        
        res = sock.recv(1024).decode("utf-8")
        self.assertIn("101 Switching Protocols", res)
        self.assertIn("Upgrade: websocket", res)

        # Helper to send masked text frame
        def send_ws_text(payload_str: str):
            payload = payload_str.encode("utf-8")
            length = len(payload)
            header = bytearray([0x81])
            if length <= 125:
                header.append(length | 0x80)
            elif length <= 65535:
                header.append(126 | 0x80)
                header.extend(length.to_bytes(2, byteorder="big"))
            else:
                header.append(127 | 0x80)
                header.extend(length.to_bytes(8, byteorder="big"))
            masking_key = b"\x01\x02\x03\x04"
            header.extend(masking_key)
            masked = bytearray(payload[i] ^ masking_key[i % 4] for i in range(len(payload)))
            sock.sendall(bytes(header) + masked)

        # Helper to receive unmasked text frame
        def recv_ws_text() -> str:
            hdr = sock.recv(2)
            if not hdr or len(hdr) < 2:
                return ""
            payload_len = hdr[1] & 0x7F
            if payload_len == 126:
                payload_len = int.from_bytes(sock.recv(2), byteorder="big")
            elif payload_len == 127:
                payload_len = int.from_bytes(sock.recv(8), byteorder="big")
            
            data = b""
            while len(data) < payload_len:
                chunk = sock.recv(payload_len - len(data))
                if not chunk:
                    break
                data += chunk
            return data.decode("utf-8")

        # 1. Test ping-pong
        send_ws_text(json.dumps({"action": "ping"}))
        response = recv_ws_text()
        resp_obj = json.loads(response)
        self.assertEqual(resp_obj["event"], "pong")

        # 2. Test event broadcast on database change (insert)
        # Make a write to the database via MemoryClient (or REST API)
        status, add_data = self._http_request("/api/v1/memories", "POST", {
            "content": "Broadcast test memory item",
            "is_global": False
        })
        self.assertEqual(status, 201)
        
        # The outbox broadaster should query the outbox table and send to socket
        broadcast_response = recv_ws_text()
        event_obj = json.loads(broadcast_response)
        self.assertEqual(event_obj["event"], "memory_event")
        self.assertIn(event_obj["data"]["event_type"], ("memory_added", "memory_updated"))
        self.assertEqual(event_obj["data"]["payload"]["content"], "Broadcast test memory item")

        # Clean up socket
        sock.close()


if __name__ == "__main__":
    unittest.main()
