"""Tests for sync_server TLS configuration.

Generates self-signed certs at test time using the ``cryptography``
library, then verifies that ``_build_tls_context`` correctly produces
an ``ssl.SSLContext`` (or raises the right errors for misconfiguration).

These tests do NOT start the server (would need to bind to a port,
accept connections, etc.) — they verify the configuration helper, which
is the unit-testable surface. End-to-end HTTPS smoke is covered by
manual deployment tests.
"""

from __future__ import annotations

import datetime
import os
import socket
import ssl
import sys
import tempfile
import threading
import time
import unittest
from http.client import HTTPSConnection
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _wait_until import wait_until  # noqa: E402

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def _make_self_signed_cert(cert_path: Path, key_path: Path) -> None:
    """Generate a self-signed RSA cert + key for testing.

    Not safe for production. Cert is valid for 1 day.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "test-sync-server.local")]
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.DNSName("127.0.0.1")]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )


def _make_ca(cert_path: Path, key_path: Path) -> None:
    """Generate a self-signed CA cert (same shape as _make_self_signed_cert)."""
    _make_self_signed_cert(cert_path, key_path)


class TestBuildTlsContext(unittest.TestCase):
    """Unit tests for sync_server._build_tls_context."""

    def setUp(self):
        self._env_backup = {k: os.environ.get(k) for k in (sync_server_env_names())}
        self.tmpdir = tempfile.mkdtemp(prefix="tls_test_")

    def tearDown(self):
        import shutil

        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_returns_none_when_no_env_vars_set(self):
        # Clear all TLS env vars
        for k in self._env_backup:
            os.environ.pop(k, None)
        ctx = sync_server._build_tls_context()
        self.assertIsNone(ctx)

    def test_raises_when_only_cert_set(self):
        cert = Path(self.tmpdir) / "test.crt"
        _make_self_signed_cert(cert, Path(self.tmpdir) / "unused.key")
        os.environ["MEMORY_SYNC_TLS_CERT"] = str(cert)
        # No key set
        with self.assertRaises(ValueError) as cm:
            sync_server._build_tls_context()
        self.assertIn("must both be set", str(cm.exception))

    def test_raises_when_only_key_set(self):
        key = Path(self.tmpdir) / "test.key"
        cert = Path(self.tmpdir) / "unused.crt"
        _make_self_signed_cert(cert, key)
        os.environ["MEMORY_SYNC_TLS_KEY"] = str(key)
        # No cert set
        with self.assertRaises(ValueError) as cm:
            sync_server._build_tls_context()
        self.assertIn("must both be set", str(cm.exception))

    def test_raises_when_cert_file_missing(self):
        os.environ["MEMORY_SYNC_TLS_CERT"] = "/nonexistent/cert.pem"
        os.environ["MEMORY_SYNC_TLS_KEY"] = "/nonexistent/key.pem"
        with self.assertRaises(FileNotFoundError) as cm:
            sync_server._build_tls_context()
        self.assertIn("cert not found", str(cm.exception))

    def test_returns_context_when_both_set(self):
        cert = Path(self.tmpdir) / "server.crt"
        key = Path(self.tmpdir) / "server.key"
        _make_self_signed_cert(cert, key)
        os.environ["MEMORY_SYNC_TLS_CERT"] = str(cert)
        os.environ["MEMORY_SYNC_TLS_KEY"] = str(key)
        ctx = sync_server._build_tls_context()
        self.assertIsNotNone(ctx)
        self.assertIsInstance(ctx, ssl.SSLContext)
        # Default: client cert NOT required (TLS without mTLS)
        self.assertEqual(ctx.verify_mode, ssl.CERT_NONE)

    def test_mtls_when_client_ca_set(self):
        cert = Path(self.tmpdir) / "server.crt"
        key = Path(self.tmpdir) / "server.key"
        ca_cert = Path(self.tmpdir) / "ca.crt"
        ca_key = Path(self.tmpdir) / "ca.key"
        _make_self_signed_cert(cert, key)
        _make_ca(ca_cert, ca_key)
        os.environ["MEMORY_SYNC_TLS_CERT"] = str(cert)
        os.environ["MEMORY_SYNC_TLS_KEY"] = str(key)
        os.environ["MEMORY_SYNC_TLS_CLIENT_CA"] = str(ca_cert)
        ctx = sync_server._build_tls_context()
        self.assertIsNotNone(ctx)
        # mTLS: client cert IS required
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)

    def test_raises_when_client_ca_missing(self):
        cert = Path(self.tmpdir) / "server.crt"
        key = Path(self.tmpdir) / "server.key"
        _make_self_signed_cert(cert, key)
        os.environ["MEMORY_SYNC_TLS_CERT"] = str(cert)
        os.environ["MEMORY_SYNC_TLS_KEY"] = str(key)
        os.environ["MEMORY_SYNC_TLS_CLIENT_CA"] = "/nonexistent/ca.crt"
        with self.assertRaises(FileNotFoundError) as cm:
            sync_server._build_tls_context()
        self.assertIn("client CA not found", str(cm.exception))


def sync_server_env_names() -> tuple:
    """Tuple of env var names that _build_tls_context reads."""
    return (
        "MEMORY_SYNC_TLS_CERT",
        "MEMORY_SYNC_TLS_KEY",
        "MEMORY_SYNC_TLS_CLIENT_CA",
    )


# Import after helpers are defined so the import order is stable.
import sync_server  # noqa: E402


class TestTlsServerEndToEnd(unittest.TestCase):
    """End-to-end test: start the sync server with TLS, connect over HTTPS."""

    def setUp(self):
        self._env_backup = {k: os.environ.get(k) for k in sync_server_env_names()}
        self.tmpdir = tempfile.mkdtemp(prefix="tls_e2e_")
        self.db_path = Path(self.tmpdir) / "memory.db"
        self.cert_path = Path(self.tmpdir) / "server.crt"
        self.key_path = Path(self.tmpdir) / "server.key"
        _make_self_signed_cert(self.cert_path, self.key_path)

        os.environ["MEMORY_SYNC_TLS_CERT"] = str(self.cert_path)
        os.environ["MEMORY_SYNC_TLS_KEY"] = str(self.key_path)

        # Pick a random-ish free port
        import socket

        self.port = 19877

    def tearDown(self):
        if hasattr(self, "server") and self.server is not None:
            try:
                self.server.stop()
            except Exception:
                pass
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_https_health_endpoint(self):
        """Connect to /health over HTTPS and verify response."""
        # Initialize the DB schema so /health works
        from db_migrations import run_schema_setup
        import sqlite3

        conn = sqlite3.connect(str(self.db_path))
        run_schema_setup(conn)
        conn.close()

        self.server = sync_server.SyncServer(
            db_path=str(self.db_path),
            agent_id="test-tls-agent",
            host="127.0.0.1",
            port=self.port,
        )
        self.assertTrue(self.server.start())

        # Wait for server to be listening. The original for-loop tried to
        # create an HTTPSConnection (which doesn't actually connect), so it
        # always broke on the first iteration — a real wait-for-server-start
        # predicate uses a raw socket probe instead.
        def _port_listening() -> bool:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return True
            except OSError:
                return False

        wait_until(
            _port_listening,
            timeout=5.0,
            interval=0.05,
            message=f"server on port {self.port} did not start listening",
        )

        # Build an SSL context that accepts self-signed certs
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        conn_ctx = HTTPSConnection("127.0.0.1", self.port, context=ctx, timeout=5)
        conn_ctx.request("GET", "/health")
        resp = conn_ctx.getresponse()
        self.assertEqual(resp.status, 200)
        body = resp.read().decode("utf-8")
        self.assertIn("test-tls-agent", body)
        conn_ctx.close()


# ===========================================================================
# SEC-1 regression: empty CORS allowlist must not send "*"
# ===========================================================================


class TestCorsAllowlist(unittest.TestCase):
    """Regression test for SEC-1 (2026-06-22).

    Before the fix, an empty MEMORY_SYNC_CORS_ORIGINS caused the
    server to send ``Access-Control-Allow-Origin: *`` — a broad
    attack surface for a server that already exposes a Bearer
    token API.  The fix: empty allowlist means "no CORS" (no
    Access-Control-Allow-Origin header at all).  Browser-based
    cross-origin clients are blocked, but same-origin and
    direct-curl clients are unaffected.

    These tests exercise the option-handling code directly
    without spinning up a full sync server.
    """

    def test_empty_allowlist_does_not_emit_wildcard(self) -> None:
        """An empty allowlist must not produce a wildcard response."""
        import importlib

        # Reload sync_server to pick up a fresh SYNC_CORS_ORIGINS.
        os.environ.pop("MEMORY_SYNC_CORS_ORIGINS", None)
        if "sync_server" in sys.modules:
            del sys.modules["sync_server"]
        from sync_server import _SyncHandler, SYNC_CORS_ORIGINS

        self.assertEqual(SYNC_CORS_ORIGINS, frozenset())

        # Simulate an OPTIONS request from a browser.  The response
        # must NOT include Access-Control-Allow-Origin: *.
        captured_headers: list[tuple[str, str]] = []

        class _FakeRequest(_SyncHandler):
            def __init__(self) -> None:
                self.headers = {"Origin": "https://attacker.example"}
                self._captured = captured_headers

            def send_response(self, code) -> None:
                pass

            def send_header(self, key, value) -> None:
                self._captured.append((key, value))

            def end_headers(self) -> None:
                pass

        req = _FakeRequest()
        req.do_OPTIONS()
        acao_headers = [
            v for k, v in captured_headers if k == "Access-Control-Allow-Origin"
        ]
        self.assertEqual(
            acao_headers,
            [],
            f"SEC-1 fix: empty CORS allowlist must not send "
            f"Access-Control-Allow-Origin.  Got: {acao_headers}",
        )

    def test_loopback_detection(self) -> None:
        """_is_loopback correctly identifies loopback addresses."""
        if "sync_server" in sys.modules:
            del sys.modules["sync_server"]
        from sync_server import _is_loopback

        for loopback in ("127.0.0.1", "localhost", "::1"):
            self.assertTrue(_is_loopback(loopback), f"{loopback} should be loopback")
        # 0.0.0.0 is NOT loopback — it means "all interfaces" and
        # is treated as non-loopback for security-warning purposes
        # (SEC-4 fix).
        self.assertFalse(
            _is_loopback("0.0.0.0"),
            "0.0.0.0 means 'all interfaces' and is NOT a safe loopback-only bind",
        )
        # Non-loopback — these tests run on a real network, so
        # we just check that a clearly-non-loopback name is not
        # detected as loopback.  (A "definitely-not-loopback" name
        # would need a guarantee; here we just check a public IP.)
        self.assertFalse(_is_loopback("8.8.8.8"))


# ===========================================================================
# SEC-4 regression: plaintext HTTP warning on non-loopback
# ===========================================================================


class TestPlaintextWarning(unittest.TestCase):
    """SEC-4 regression (2026-06-22).

    When the sync server is bound to a non-loopback address and
    TLS is not configured, the Bearer token and HMAC body are
    sent in clear text on the wire.  The fix logs a loud warning
    at startup so operators can see it in their logs.
    """

    def test_warning_logged_for_non_loopback_no_tls(self) -> None:
        """Starting the server on a non-loopback address without TLS
        must log a warning."""
        import logging
        import socket as _socket

        # Pick a free port — we don't actually want to bind it.
        s = _socket.socket()
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]
        s.close()

        if "sync_server" in sys.modules:
            del sys.modules["sync_server"]
        import sync_server
        from sync_server import SyncServer

        # Capture log messages.
        with self.assertLogs("sync_server", level="WARNING") as cm:
            server = SyncServer(
                db_path="/tmp/does_not_matter.db",
                agent_id="sec4-test",
                host="0.0.0.0",  # non-loopback
                port=free_port,
            )
            try:
                server.start()
            except Exception:
                pass  # We only care about the log output
            try:
                server.stop()
            except Exception:
                pass
        # The warning text mentions TLS.
        self.assertTrue(
            any("TLS" in msg or "clear text" in msg for msg in cm.output),
            f"Expected TLS warning in log output, got: {cm.output}",
        )

    def test_no_warning_for_loopback(self) -> None:
        """A loopback bind must NOT trigger the plaintext warning."""
        import socket as _socket

        s = _socket.socket()
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]
        s.close()

        if "sync_server" in sys.modules:
            del sys.modules["sync_server"]
        import sync_server
        from sync_server import SyncServer

        # Capture log messages at INFO level.  We expect no WARNING
        # for the plaintext path.
        import logging

        logger = logging.getLogger("sync_server")
        # We can't use assertLogs(level=INFO) because it would
        # include the regular "listening on" INFO log.  Instead,
        # use a custom handler.
        records = []
        handler = logging.Handler()
        handler.emit = lambda r: records.append(r)
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            server = SyncServer(
                db_path="/tmp/does_not_matter.db",
                agent_id="sec4-test",
                host="127.0.0.1",  # loopback
                port=free_port,
            )
            try:
                server.start()
            except Exception:
                pass
            try:
                server.stop()
            except Exception:
                pass
        finally:
            logger.removeHandler(handler)
        # No WARNING about TLS / clear text.
        tls_warnings = [
            r
            for r in records
            if r.levelno >= logging.WARNING
            and ("TLS" in r.getMessage() or "clear text" in r.getMessage())
        ]
        self.assertEqual(
            tls_warnings,
            [],
            f"Loopback bind should not trigger plaintext TLS warning, "
            f"got: {[r.getMessage() for r in tls_warnings]}",
        )


if __name__ == "__main__":
    unittest.main()
