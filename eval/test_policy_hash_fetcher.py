"""Tests for infra.policy_hash_fetcher."""
import json
import socketserver
import threading
import time
import unittest
from http.server import HTTPServer, BaseHTTPRequestHandler
import sys
import os
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from infra.policy_hash_fetcher import (
    fetch_peer_policy_hash,
    fetch_all_peer_hashes,
)


class _ProbeHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"policy_hash": "abc123", "scope": "test"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args, **kwargs):
        pass


class _TimeoutHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        time.sleep(5)
        self.send_response(200)
        self.end_headers()


class _AuthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(401)
        self.end_headers()


def _free_port():
    with socketserver.TCPServer(("", 0), _ProbeHandler) as s:
        return s.server_address[1]


class TestPolicyHashFetcher(unittest.TestCase):
    def test_fetch_ok_returns_payload(self):
        port = _free_port()
        server = HTTPServer(("127.0.0.1", port), _ProbeHandler)
        t = threading.Thread(target=server.serve_forever)
        t.start()
        try:
            status, latency, body = fetch_peer_policy_hash(f"http://127.0.0.1:{port}")
            self.assertEqual(status, "ok")
            self.assertEqual(body.get("policy_hash"), "abc123")
            self.assertGreater(latency, 0)
        finally:
            server.shutdown()
            t.join(timeout=3)

    def test_fetch_timeout_returns_unreachable(self):
        port = _free_port()
        server = HTTPServer(("127.0.0.1", port), _TimeoutHandler)
        t = threading.Thread(target=server.serve_forever)
        t.start()
        try:
            status, latency, body = fetch_peer_policy_hash(
                f"http://127.0.0.1:{port}", timeout_s=0.5,
            )
            self.assertEqual(status, "unreachable")
        finally:
            server.shutdown()
            t.join(timeout=3)

    def test_fetch_auth_failed_401(self):
        port = _free_port()
        server = HTTPServer(("127.0.0.1", port), _AuthHandler)
        t = threading.Thread(target=server.serve_forever)
        t.start()
        try:
            status, latency, body = fetch_peer_policy_hash(
                f"http://127.0.0.1:{port}",
            )
            self.assertEqual(status, "auth_failed")
        finally:
            server.shutdown()
            t.join(timeout=3)

    def test_fetch_all_concurrent_bounded(self):
        responses = [{"url": f"http://127.0.0.1:{_free_port()}"} for _ in range(6)]
        result = fetch_all_peer_hashes(responses, max_concurrent=2, timeout_s=1.0)
        for name, (status, _, _) in result.items():
            self.assertEqual(status, "unreachable")

    def test_fetch_with_sync_token_header(self):
        import urllib.request as _urllib_request
        from unittest.mock import patch, MagicMock
        captured: dict = {}

        def _fake_urlopen(req, timeout=None):
            captured["auth_header"] = req.get_header("Authorization")
            mock_resp = MagicMock()
            mock_resp.read.return_value = b'{"policy_hash": "x"}'
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            return mock_resp

        with patch.object(_urllib_request, "urlopen", side_effect=_fake_urlopen):
            fetch_peer_policy_hash("http://example.com", sync_token="mytoken")
        self.assertEqual(captured.get("auth_header"), "Bearer mytoken")

    def test_fetch_bad_response_non_200(self):
        from unittest.mock import patch
        import email.message
        hdrs = email.message.Message()
        with patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError(
            "url", 500, "err", hdrs, None,
        )):
            status, _, _ = fetch_peer_policy_hash("http://example.com")
        self.assertEqual(status, "bad_response")

    def test_fetch_socket_timeout_unreachable(self):
        from unittest.mock import patch
        import socket as sock
        with patch("urllib.request.urlopen", side_effect=sock.timeout("timed out")):
            status, _, _ = fetch_peer_policy_hash("http://example.com", timeout_s=0.1)
        self.assertEqual(status, "unreachable")

    def test_fetch_empty_peers_returns_empty(self):
        result = fetch_all_peer_hashes([], max_concurrent=2)
        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
