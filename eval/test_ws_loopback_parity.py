"""Test WS auth loopback parity with REST."""

import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from infra.api_server import APIServer
from infra.db import open_db
from infra.db_migrations import run_schema_setup


class TestWSLoopbackParity(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "memory.db"
        with open_db(self.db_path) as db:
            run_schema_setup(db)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_ws_loopback_allowed_with_insecure_loopback(self):
        port = 19876
        server = APIServer(
            db_path=str(self.db_path),
            agent_id="test",
            host="127.0.0.1",
            port=port,
            token="secret_token",
            insecure_loopback=True,
        )
        server.start()
        try:
            # Connect via raw socket to send HTTP GET upgrade
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect(("127.0.0.1", port))
            handshake = (
                f"GET /ws HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
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
            server.stop()


if __name__ == "__main__":
    unittest.main()
