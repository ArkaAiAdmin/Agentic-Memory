#!/usr/bin/env python3
"""Cross-repo kernel v1 contract verification suite.

Validates the full v1 API contract expected by agentic-memory-ide harness:
1. /health contract keys, types, and degraded state on dead_letter > 0
2. /api/v1/memories/search envelope parity ({results, count, query}) across GET and POST
3. WebSocket authentication loopback matrix (insecure, Bearer/subprotocol, query-param rejection)
"""

import json
import os
import socket as _socket
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.request
import unittest
from pathlib import Path

INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))

from infra.memory_common import connection_pool
from infra.db import open_db
from infra.db_migrations import run_schema_setup
from infra.api_server import APIServer


def _get_free_port() -> int:
    s = _socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_for_server(host: str, port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            s = _socket.create_connection((host, port), timeout=0.5)
            s.close()
            return
        except (OSError, ConnectionRefusedError):
            time.sleep(0.05)
    raise RuntimeError(f"Server {host}:{port} did not start within {timeout}s")


class TestContractKernelV1(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "memory.db"
        self.journal_db_path = Path(self.tmpdir) / "journal.db"
        with open_db(self.db_path) as db:
            run_schema_setup(db)

        # Initialize journal.db
        conn = sqlite3.connect(str(self.journal_db_path))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS write_journal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                note_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                category TEXT NOT NULL,
                title_slug TEXT NOT NULL,
                content TEXT NOT NULL,
                tags TEXT,
                pinned INTEGER DEFAULT 0,
                is_global INTEGER DEFAULT 0,
                importance INTEGER DEFAULT 3,
                tenant_id TEXT DEFAULT 'default',
                epistemic_source TEXT DEFAULT 'agent',
                belief_status TEXT DEFAULT 'active',
                asserting_agent_id TEXT DEFAULT '',
                fact_type TEXT DEFAULT 'observation',
                defer_expensive INTEGER DEFAULT 1,
                context TEXT DEFAULT 'generic',
                status TEXT NOT NULL DEFAULT 'pending',
                error TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                processed_at TEXT,
                retry_count INTEGER DEFAULT 0,
                content_hash TEXT,
                started_at TEXT,
                hooks_completed INTEGER DEFAULT 0,
                data_subject_sub TEXT
            )
        """)
        conn.commit()
        conn.close()

        self.port = _get_free_port()
        self.token = os.urandom(16).hex()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_health_contract(self):
        """GET /health must return all required contract keys and reflect degraded status."""
        server = APIServer(
            db_path=str(self.db_path),
            agent_id="test",
            host="127.0.0.1",
            port=self.port,
            token=self.token,
            insecure_loopback=True,
        )
        server.start()
        _wait_for_server("127.0.0.1", self.port)

        try:
            # 1. Healthy baseline
            url = f"http://127.0.0.1:{self.port}/health"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode("utf-8"))

            required_keys = {
                "status",
                "db_ok",
                "db_path",
                "journal_pending",
                "dead_letter",
                "note_count",
                "package_version",
                "schema_version",
            }
            for key in required_keys:
                self.assertIn(key, data, f"Missing required health key: {key}")

            self.assertEqual(data["status"], "healthy")
            self.assertTrue(data["db_ok"])
            self.assertEqual(data["dead_letter"], 0)
            self.assertIsInstance(data["journal_pending"], int)
            self.assertIsInstance(data["note_count"], int)

            # 2. Inject failed row into journal.db -> status must transition to degraded
            conn = sqlite3.connect(str(self.journal_db_path))
            conn.execute("""
                INSERT INTO write_journal (note_id, agent_id, category, title_slug, content, status, error)
                VALUES ('test_note_failed', 'ami', 'lessons', 'failed-test', 'content', 'failed', 'forced_error')
            """)
            conn.commit()
            conn.close()

            with urllib.request.urlopen(req, timeout=3.0) as resp:
                self.assertEqual(resp.status, 200)
                degraded_data = json.loads(resp.read().decode("utf-8"))

            self.assertEqual(degraded_data["status"], "degraded")
            self.assertTrue(degraded_data["db_ok"])
            self.assertGreater(degraded_data["dead_letter"], 0)

        finally:
            server.stop()

    def test_search_envelope_parity(self):
        """GET /search and POST /search must return identical {results, count, query} envelopes."""
        # Seed a test memory in SQLite
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("""
            INSERT INTO memories (id, category, content, source_file, created_at, updated_at)
            VALUES ('mem_1', 'decisions', 'Use SQLite WAL mode and FTS5 indexing', 'decisions/arch.md', datetime('now'), datetime('now'))
        """)
        conn.commit()
        conn.close()

        server = APIServer(
            db_path=str(self.db_path),
            agent_id="test",
            host="127.0.0.1",
            port=self.port,
            token=self.token,
            insecure_loopback=True,
        )
        server.start()
        _wait_for_server("127.0.0.1", self.port)

        try:
            # 1. GET /api/v1/memories/search?query=SQLite&limit=2
            get_url = f"http://127.0.0.1:{self.port}/api/v1/memories/search?query=SQLite&limit=2"
            with urllib.request.urlopen(urllib.request.Request(get_url), timeout=3.0) as resp:
                self.assertEqual(resp.status, 200)
                get_data = json.loads(resp.read().decode("utf-8"))

            self.assertIn("results", get_data)
            self.assertIn("count", get_data)
            self.assertIn("query", get_data)
            self.assertEqual(get_data["query"], "SQLite")
            self.assertEqual(get_data["count"], len(get_data["results"]))

            # 2. POST /api/v1/memories/search {"query": "SQLite", "limit": 2}
            post_url = f"http://127.0.0.1:{self.port}/api/v1/memories/search"
            post_body = json.dumps({"query": "SQLite", "limit": 2}).encode("utf-8")
            post_req = urllib.request.Request(
                post_url,
                data=post_body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(post_req, timeout=3.0) as resp:
                self.assertEqual(resp.status, 200)
                post_data = json.loads(resp.read().decode("utf-8"))

            self.assertIn("results", post_data)
            self.assertIn("count", post_data)
            self.assertIn("query", post_data)
            self.assertEqual(post_data["query"], "SQLite")
            self.assertEqual(post_data["count"], len(post_data["results"]))

            # 3. Invalid limit query param -> 400 validation on both GET and POST
            invalid_get = f"http://127.0.0.1:{self.port}/api/v1/memories/search?query=test&limit=invalid"
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(urllib.request.Request(invalid_get), timeout=3.0)
            self.assertEqual(ctx.exception.code, 400)

            invalid_post_req = urllib.request.Request(
                post_url,
                data=json.dumps({"query": "test", "limit": "invalid"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(invalid_post_req, timeout=3.0)
            self.assertEqual(ctx.exception.code, 400)

        finally:
            server.stop()

    def test_ws_loopback_matrix(self):
        """Matrix testing for WS auth: insecure loopback, token subprotocol, and query rejection."""
        # Test Case A: insecure_loopback=True allows connection without auth
        server_insecure = APIServer(
            db_path=str(self.db_path),
            agent_id="test",
            host="127.0.0.1",
            port=self.port,
            token=self.token,
            insecure_loopback=True,
        )
        server_insecure.start()
        _wait_for_server("127.0.0.1", self.port)
        try:
            sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            sock.connect(("127.0.0.1", self.port))
            handshake = (
                f"GET /ws HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.port}\r\n"
                f"Upgrade: websocket\r\n"
                f"Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                f"Sec-WebSocket-Version: 13\r\n\r\n"
            )
            sock.sendall(handshake.encode("utf-8"))
            resp = sock.recv(1024).decode("utf-8")
            self.assertIn("101 Switching Protocols", resp)
            sock.close()
        finally:
            server_insecure.stop()

        # Test Case B, C, D: insecure_loopback=False (strict auth)
        port_secure = _get_free_port()
        server_secure = APIServer(
            db_path=str(self.db_path),
            agent_id="test",
            host="127.0.0.1",
            port=port_secure,
            token=self.token,
            insecure_loopback=False,
        )
        server_secure.start()
        _wait_for_server("127.0.0.1", port_secure)
        try:
            # B: Missing auth -> 401 Unauthorized
            sock_noauth = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            sock_noauth.connect(("127.0.0.1", port_secure))
            handshake_noauth = (
                f"GET /ws HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port_secure}\r\n"
                f"Upgrade: websocket\r\n"
                f"Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                f"Sec-WebSocket-Version: 13\r\n\r\n"
            )
            sock_noauth.sendall(handshake_noauth.encode("utf-8"))
            resp_noauth = sock_noauth.recv(1024).decode("utf-8")
            self.assertIn("401", resp_noauth)
            sock_noauth.close()

            # C: Valid Sec-WebSocket-Protocol token handshake -> 101 Switching Protocols
            sock_auth = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            sock_auth.connect(("127.0.0.1", port_secure))
            handshake_auth = (
                f"GET /ws HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port_secure}\r\n"
                f"Upgrade: websocket\r\n"
                f"Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                f"Sec-WebSocket-Version: 13\r\n"
                f"Sec-WebSocket-Protocol: token, {self.token}\r\n\r\n"
            )
            sock_auth.sendall(handshake_auth.encode("utf-8"))
            resp_auth = sock_auth.recv(1024).decode("utf-8")
            self.assertIn("101 Switching Protocols", resp_auth)
            sock_auth.close()

            # D: Query-param token rejection (/ws?token=...) -> 401 Unauthorized
            sock_query = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            sock_query.connect(("127.0.0.1", port_secure))
            handshake_query = (
                f"GET /ws?token={self.token} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port_secure}\r\n"
                f"Upgrade: websocket\r\n"
                f"Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                f"Sec-WebSocket-Version: 13\r\n\r\n"
            )
            sock_query.sendall(handshake_query.encode("utf-8"))
            resp_query = sock_query.recv(1024).decode("utf-8")
            self.assertIn("401", resp_query)
            sock_query.close()

        finally:
            server_secure.stop()


if __name__ == "__main__":
    unittest.main()
