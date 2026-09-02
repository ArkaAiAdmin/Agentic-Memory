"""Tests for honest health reporting and structured error codes in api_server.py."""

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

from infra.api_server import APIServer
from infra.db import open_db
from infra.db_migrations import run_schema_setup


class TestKernelHealthAndErrors(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "memory.db"
        with open_db(self.db_path) as db:
            run_schema_setup(db)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_health_reports_healthy_on_valid_db(self):
        port = 19877
        server = APIServer(
            db_path=str(self.db_path),
            agent_id="test",
            host="127.0.0.1",
            port=port,
            token="test_tok",
            insecure_loopback=True,
        )
        server.start()
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
            with urllib.request.urlopen(req) as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode())
                self.assertEqual(data["status"], "healthy")
                self.assertIn("package_version", data)
        finally:
            server.stop()

    def test_tool_call_invalid_args_returns_400(self):
        port = 19878
        server = APIServer(
            db_path=str(self.db_path),
            agent_id="test",
            host="127.0.0.1",
            port=port,
            token="test_tok",
            insecure_loopback=True,
        )
        server.start()
        try:
            payload = json.dumps({
                "tool": "memory_search",
                "arguments": "invalid_non_dict_args",
            }).encode("utf-8")
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/v1/tools/call",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            try:
                urllib.request.urlopen(req)
                self.fail("Expected HTTP 400")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 400)
        finally:
            server.stop()


if __name__ == "__main__":
    unittest.main()
