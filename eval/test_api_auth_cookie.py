"""Phase 2 tests: dashboard auth cookie + rate limiting on the REST API.

Covers:
  - POST /api/v1/auth/login with a valid token mints an HttpOnly JWT cookie.
  - The cookie authenticates a protected route (no Authorization header).
  - An invalid token is rejected (403).
  - POST /api/v1/auth/logout clears the cookie.
  - Per-IP rate limiting returns 429 when the window is exceeded.
"""

from __future__ import annotations

import json
import os
import socket as _socket
import sys
import tempfile
import time
import urllib.error
import urllib.request
import unittest
from pathlib import Path

INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))

_TEST_DB_DIR = tempfile.mkdtemp(prefix="api_auth_test_")
_TEST_DB_PATH = Path(_TEST_DB_DIR) / "memory.db"
os.environ["MEMORY_DB_PATH"] = str(_TEST_DB_PATH)

from infra.db_migrations import run_schema_setup  # noqa: E402
from infra.api_server import APIServer  # noqa: E402


# Rate-limit env vars are read by APIServer.__init__; set them only for the
# duration of this test class and restore afterwards so they don't leak into
# other test modules (env vars are process-global under pytest).
_RATE_ENV = {
    "MEMORY_API_RATE_LIMIT": "3",
    "MEMORY_API_RATE_WINDOW": "30",
}
_RATE_ENV_SAVED: dict = {}


def _free_port() -> int:
    s = _socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait(host: str, port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            c = _socket.create_connection((host, port), timeout=0.5)
            c.close()
            return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("server did not start")


class TestApiAuthCookie(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import sqlite3 as _sqlite3

        conn = _sqlite3.connect(str(_TEST_DB_PATH))
        conn.execute("PRAGMA foreign_keys=ON")
        run_schema_setup(conn)
        conn.close()

        # Set rate-limit env vars only for this server instance, saving the
        # prior values to restore in tearDownClass (containment).
        for _k, _v in _RATE_ENV.items():
            _RATE_ENV_SAVED[_k] = os.environ.get(_k)
            os.environ[_k] = _v

        cls.host = "127.0.0.1"
        cls.port = _free_port()
        cls.token = "phase2-test-token-abcdef0123456789"
        cls.server = APIServer(
            db_path=_TEST_DB_PATH, agent_id="test-agent",
            host=cls.host, port=cls.port, token=cls.token,
        )
        cls.server.start()
        _wait(cls.host, cls.port)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.stop()
        except Exception:
            pass
        # Restore rate-limit env so other test modules are unaffected.
        for _k, _v in _RATE_ENV_SAVED.items():
            if _v is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _v

    def _url(self, path: str) -> str:
        return f"http://{self.host}:{self.port}{path}"

    def setUp(self):
        # Reset the shared per-IP rate-limit buckets so tests don't interfere
        # with each other (the server is shared across the class).
        self.server._rate_buckets.clear()

    def _post(self, path: str, payload: dict, headers=None):
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            self._url(path), data=data, method="POST",
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        return urllib.request.urlopen(req, timeout=5)

    def test_login_valid_token_sets_cookie(self):
        resp = self._post("/api/v1/auth/login", {"token": self.token})
        self.assertEqual(resp.status, 200)
        cookie = resp.headers.get("Set-Cookie", "")
        self.assertIn("am_token=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)

    def test_invalid_token_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/v1/auth/login", {"token": "wrong-token"})
        self.assertEqual(ctx.exception.code, 403)

    def test_cookie_authenticates_protected_route(self):
        resp = self._post("/api/v1/auth/login", {"token": self.token})
        cookie = resp.headers.get("Set-Cookie", "").split(";")[0]
        req = urllib.request.Request(
            self._url("/api/v1/memories/stats"),
            headers={"Cookie": cookie},
        )
        protected = urllib.request.urlopen(req, timeout=5)
        self.assertEqual(protected.status, 200)

    def test_protected_route_requires_auth_without_cookie(self):
        req = urllib.request.Request(self._url("/api/v1/memories/stats"))
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 401)

    def test_logout_clears_cookie(self):
        resp = self._post("/api/v1/auth/logout", {})
        self.assertEqual(resp.status, 200)
        cookie = resp.headers.get("Set-Cookie", "")
        self.assertIn("am_token=", cookie)
        self.assertIn("Max-Age=0", cookie)

    def test_rate_limit_returns_429(self):
        # rate_limit=3, so 3 quick POSTs succeed then the 4th is throttled.
        ok = 0
        limited = False
        for _ in range(5):
            try:
                self._post("/api/v1/auth/login", {"token": self.token})
                ok += 1
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    limited = True
                    break
        self.assertGreaterEqual(ok, 1)
        self.assertTrue(limited, "expected a 429 after exceeding the rate limit")


if __name__ == "__main__":
    import unittest

    unittest.main()
