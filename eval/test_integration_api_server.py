#!/usr/bin/env python3
"""Integration tests for infr/api_server.py — HTTP REST contract.

Validates the exact HTTP endpoints, request/response shapes, and error
handling that the TypeScript MemoryBridgeClient in
``ide/packages/memory-bridge/`` relies on.

Endpoints tested:
    GET  /health
    POST /api/v1/tools/call
    POST /api/v1/memories
    POST /api/v1/memories/search
    POST /api/v1/memories/session/start

Run:
    .venv/bin/python -m pytest eval/test_integration_api_server.py -v
"""

import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))

from infra.api_server import APIServer


# ── Helpers ────────────────────────────────────────────────────────────────

def _bootstrap_db(db_path: Path) -> None:
    from eval._fixtures import bootstrap_temp_db_clean
    bootstrap_temp_db_clean(db_path)
    for cat in ("lessons", "decisions", "sessions", "projects", "architecture"):
        (db_path.parent / "memory" / cat).mkdir(parents=True, exist_ok=True)


def _url(host: str, port: int, path: str) -> str:
    return f"http://{host}:{port}{path}"


def _fetch(
        method: str,
        url: str,
        body: dict | None = None,
        expect_status: int = 200,
    ) -> dict:
        data = json.dumps(body).encode("utf-8") if body else None
        req = Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        try:
            with urlopen(req, timeout=30.0) as resp:
                body_bytes = resp.read()
                result = json.loads(body_bytes) if body_bytes else {}
                if resp.status != expect_status:
                    raise AssertionError(
                        f"Expected status {expect_status}, got {resp.status} for {method} {url}: {result}"
                    )
                return result
        except HTTPError as e:
            body_bytes = e.read()
            result = json.loads(body_bytes) if body_bytes else {}
            if e.code != expect_status:
                raise AssertionError(
                    f"Expected status {expect_status}, got {e.code} for {method} {url}: {result}"
                ) from e
            return result
        except URLError as e:
            raise AssertionError(f"Request to {url} failed: {e}") from e


def _wait_for_health(url_base: str, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            req = Request(f"{url_base}/health", method="GET")
            with urlopen(req, timeout=1.0) as resp:
                if resp.status == 200:
                    return
        except (URLError, OSError) as e:
            last_err = e
            time.sleep(0.2)
    raise RuntimeError(f"Health check never succeeded: {last_err}")


# ── Tests ──────────────────────────────────────────────────────────────────

class TestAPIServerContract(unittest.TestCase):
    """Validates the HTTP API contract from the TS MemoryBridgeClient perspective."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._saved_env = {}
        for k, v in {
            "MEMORY_EMBEDDING_BACKEND": "none",
            "MEMORY_RERANKER_DISABLED": "true",
            "MEMORY_WRITE_JOURNAL_ENABLED": "0",
            "MEMORY_LLM_EXTRACTION": "0",
            "MEMORY_FAIL_ON_INTEGRITY_DRIFT": "0",
        }.items():
            cls._saved_env[k] = os.environ.get(k)
            os.environ[k] = v

        cls._tmp = Path("/tmp") / f"am_api_test_{os.getpid()}"
        cls._db_path = cls._tmp / "memory.db"

        # Point the coordination save-lock hook (and any other env-resolved
        # DB access) at the test DB — the CLI sets this at startup (cli.py:81);
        # without it the fenced lock targets the live production DB and blocks
        # on the running daemon's write lock.
        cls._saved_env["MEMORY_DB_PATH"] = os.environ.get("MEMORY_DB_PATH")
        os.environ["MEMORY_DB_PATH"] = str(cls._db_path)

        _bootstrap_db(cls._db_path)

        cls._host = "127.0.0.1"
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            cls._port = s.getsockname()[1]

        cls._server = APIServer(
            db_path=cls._db_path,
            agent_id="test-agent",
            host=cls._host,
            port=cls._port,
            insecure_loopback=True,
        )
        cls._server.start()
        cls._url_base = f"http://{cls._host}:{cls._port}"

        _wait_for_health(cls._url_base)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._server.stop()
        for k, v in cls._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import shutil
        shutil.rmtree(cls._tmp, ignore_errors=True)

    # ── /health ────────────────────────────────────────────────────────────

    def test_health_returns_200(self) -> None:
        result = _fetch("GET", f"{self._url_base}/health")
        self.assertIn("status", result)

    # ── POST /api/v1/memories (save) ───────────────────────────────────────

    def test_save_memory(self) -> None:
        result = _fetch("POST", f"{self._url_base}/api/v1/memories", {
            "content": "Integration test memory",
            "category": "lessons",
            "tags": ["test", "integration"],
        }, expect_status=201)
        self.assertIn("id", result)
        self.assertTrue(result["id"].startswith("lessons/"))

    def test_save_empty_content_returns_error(self) -> None:
        result = _fetch("POST", f"{self._url_base}/api/v1/memories", {
            "content": "",
            "category": "lessons",
        }, expect_status=400)
        self.assertIn("error", result)

    # ── POST /api/v1/tools/call ────────────────────────────────────────────

    def test_call_tool_memory_save(self) -> None:
        result = _fetch("POST", f"{self._url_base}/api/v1/tools/call", {
            "tool": "memory_save",
            "args": {"content": "Tool call test", "category": "lessons"},
        })
        self.assertEqual(result.get("tool"), "memory_save")
        self.assertIn("result", result)

    def test_call_tool_memory_search(self) -> None:
        result = _fetch("POST", f"{self._url_base}/api/v1/tools/call", {
            "tool": "memory_search",
            "args": {"query": "integration test", "limit": 5},
        })
        self.assertEqual(result.get("tool"), "memory_search")
        self.assertIn("result", result)

    def test_call_tool_unknown_returns_error(self) -> None:
        result = _fetch("POST", f"{self._url_base}/api/v1/tools/call", {
            "tool": "memory_nonexistent",
            "args": {},
        }, expect_status=404)
        self.assertIn("error", result)

    # ── POST /api/v1/memories/search ───────────────────────────────────────

    def test_search_memories(self) -> None:
        _fetch("POST", f"{self._url_base}/api/v1/memories", {
            "content": "database migration guide",
            "category": "lessons",
        }, expect_status=201)
        result = _fetch("POST", f"{self._url_base}/api/v1/memories/search", {
            "query": "database",
            "limit": 10,
        })
        self.assertIn("results", result)
        self.assertIsInstance(result["results"], list)
        self.assertGreater(len(result["results"]), 0)

    def test_search_empty_query_returns_error(self) -> None:
        result = _fetch("POST", f"{self._url_base}/api/v1/memories/search", {
            "query": "",
            "limit": 10,
        }, expect_status=400)
        self.assertIn("error", result)

    # ── POST /api/v1/memories/session/start ────────────────────────────────

    def test_session_start(self) -> None:
        result = _fetch("POST", f"{self._url_base}/api/v1/memories/session/start", {
            "query": "what am I working on",
        })
        self.assertIn("result", result)

    # ── POST /api/v1/tools/call with memory_recall_context ─────────────────
 
    def test_call_tool_recall_context(self) -> None:
        result = _fetch("POST", f"{self._url_base}/api/v1/tools/call", {
            "tool": "memory_recall_context",
            "args": {"query": "test"},
        })
        self.assertEqual(result.get("tool"), "memory_recall_context")
        self.assertIn("result", result)


if __name__ == "__main__":
    unittest.main()
