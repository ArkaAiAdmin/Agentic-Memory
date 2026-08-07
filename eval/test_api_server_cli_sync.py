"""Tests for the CLI ``api`` entry point sync/heartbeat wiring and the
sync-server-from-config helpers.

Covers:
- ``cli.api_server_main`` embeds a CRDT ``SyncServer`` (from env/config)
  as a daemon thread and the API server stays healthy.
- The API server still starts (health 200) even if the embedded sync
  server fails to bind (non-fatal path).
- The ``api`` process writes an ``agent_heartbeats`` row via the
  heartbeat loop.
- ``infra.sync_server.start_server_from_config`` returns ``None`` when
  sync is disabled and starts a server otherwise.
- ``infra.sync_server._resolve_agent_sync_port`` honours the
  ``MEMORY_SYNC_LISTEN_PORT`` env override before the peer entry, and
  falls back to the global default when no peer matches.

Run:
    .venv/bin/python -m pytest eval/test_api_server_cli_sync.py -v
"""

import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen

INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _fetch(url: str, timeout: float = 3.0):
    req = Request(url, method="GET")
    with urlopen(req, timeout=timeout) as resp:
        return resp.status


def _wait_for_health(url_base: str, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            _fetch(f"{url_base}/health")
            return True
        except Exception:
            time.sleep(0.25)
    return False


def _port_listening(port: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.25)
    return False


class _ApiProcess:
    """Boots ``cli.py api`` as a subprocess against an isolated temp DB."""

    def __init__(self, tmpdir: Path, db_name: str = "memory.db", sync_port: int | None = None):
        self.tmpdir = Path(tmpdir)
        self.db_path = self.tmpdir / db_name
        self.api_port = _free_port()
        self.sync_port = sync_port if sync_port is not None else _free_port()
        while self.sync_port == self.api_port:
            self.sync_port = _free_port()

        env = dict(os.environ)
        env.pop("MEMORY_DB_PATH", None)
        env["MEMORY_AGENT_ID"] = "TESTAGENT"
        env["MEMORY_API_TOKEN"] = "test-token-000"
        env["MEMORY_SYNC_ENABLE_SERVER"] = "true"
        env["MEMORY_SYNC_LISTEN_HOST"] = "127.0.0.1"
        env["MEMORY_SYNC_LISTEN_PORT"] = str(self.sync_port)

        self.proc = subprocess.Popen(
            [
                sys.executable,
                str(INSTALL_DIR / "cli.py"),
                "api",
                "--db",
                str(self.db_path),
                "--port",
                str(self.api_port),
                "--host",
                "127.0.0.1",
            ],
            cwd=str(INSTALL_DIR),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    @property
    def api_url(self) -> str:
        return f"http://127.0.0.1:{self.api_port}"

    def wait_api_health(self, timeout: float = 15.0) -> bool:
        return _wait_for_health(self.api_url, timeout)

    def wait_sync_listening(self, timeout: float = 10.0) -> bool:
        return _port_listening(self.sync_port, timeout)

    def read_heartbeat(self, agent_id: str, timeout: float = 15.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                conn = sqlite3.connect(str(self.db_path), timeout=5)
                try:
                    row = conn.execute(
                        "SELECT agent_id, last_heartbeat, session_id, project_id "
                        "FROM agent_heartbeats WHERE agent_id=?",
                        (agent_id,),
                    ).fetchone()
                finally:
                    conn.close()
                if row:
                    return {
                        "agent_id": row[0],
                        "last_heartbeat": row[1],
                        "session_id": row[2],
                        "project_id": row[3],
                    }
            except Exception:
                pass
            time.sleep(0.5)
        return None

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class TestApiServerMainEmbedsSyncAndHeartbeat(unittest.TestCase):
    """Subprocess-level: the real CLI path starts sync + heartbeat."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="api_cli_sync_"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_health_returns_200_and_heartbeat_written(self):
        srv = _ApiProcess(self.tmpdir)
        try:
            self.assertTrue(
                srv.wait_api_health(),
                "API server should be reachable on /health",
            )
            self.assertTrue(
                srv.wait_sync_listening(),
                "embedded sync server should be listening on its port",
            )
            row = srv.read_heartbeat(agent_id="TESTAGENT")
            self.assertIsNotNone(
                row,
                "heartbeat loop should write an agent_heartbeats row",
            )
            assert row is not None
            self.assertEqual(row["project_id"], "default")
        finally:
            srv.stop()

    def test_health_returns_200_when_sync_port_occupied(self):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        try:
            srv = _ApiProcess(self.tmpdir, sync_port=blocker.getsockname()[1])
            try:
                self.assertTrue(
                    srv.wait_api_health(),
                    "API server must still start even when the sync port is taken",
                )
                self.assertTrue(
                    _port_listening(srv.sync_port, timeout=5.0),
                    "the blocker socket keeps the port bound",
                )
            finally:
                srv.stop()
        finally:
            blocker.close()


class _SyncCfg:
    """Minimal stand-in for the memory config's sync section."""

    def __init__(self, peers=(), listen_port=9877, enable_server=False, listen_host="127.0.0.1"):
        self.sync_peers = list(peers)
        self.sync_listen_port = listen_port
        self.sync_enable_server = enable_server
        self.sync_listen_host = listen_host


def _peer(name: str, url: str, agent_id: str) -> dict:
    return {"name": name, "url": url, "agent_id": agent_id}


class TestResolveAgentSyncPort(unittest.TestCase):
    def test_env_var_overrides_peer_entry(self):
        from infra.sync_server import _resolve_agent_sync_port

        cfg = _SyncCfg(
            peers=[_peer("P", "http://127.0.0.1:9878", "P")],
            listen_port=9999,
        )
        with patch.dict(os.environ, {"MEMORY_SYNC_LISTEN_PORT": "5555"}):
            self.assertEqual(_resolve_agent_sync_port(cfg, "ANY"), 9999)

    def test_peer_entry_match_is_case_insensitive(self):
        from infra.sync_server import _resolve_agent_sync_port

        cfg = _SyncCfg(
            peers=[_peer("Opencode", "http://127.0.0.1:9878", "OPENCODE")],
            listen_port=9877,
        )
        self.assertEqual(_resolve_agent_sync_port(cfg, "opencode"), 9878)

    def test_no_peer_match_falls_back_to_default(self):
        from infra.sync_server import _resolve_agent_sync_port

        cfg = _SyncCfg(
            peers=[_peer("B", "http://127.0.0.1:9880", "AMI")],
            listen_port=1234,
        )
        self.assertEqual(_resolve_agent_sync_port(cfg, "UNKNOWN"), 1234)

    def test_peer_without_port_falls_back(self):
        from infra.sync_server import _resolve_agent_sync_port

        cfg = _SyncCfg(
            peers=[_peer("NoPort", "http://127.0.0.1", "NOPORT")],
            listen_port=4321,
        )
        self.assertEqual(_resolve_agent_sync_port(cfg, "NOPORT"), 4321)

    def test_peer_entry_may_be_object(self):
        from infra.sync_server import _resolve_agent_sync_port

        class PeerObj:
            agent_id = "OBJ"
            url = "http://127.0.0.1:9880"

        cfg = _SyncCfg(peers=[PeerObj()], listen_port=9877)
        self.assertEqual(_resolve_agent_sync_port(cfg, "obj"), 9880)


class TestStartServerFromConfig(unittest.TestCase):
    def test_returns_none_when_sync_disabled(self):
        from infra import sync_server as mod

        cfg = _SyncCfg(enable_server=False)
        with patch("infra._lazy_imports.get_config", return_value=cfg):
            self.assertIsNone(mod.start_server_from_config("/tmp/x.db"))

    def test_starts_server_with_resolved_port(self):
        from infra import sync_server as mod

        started = []

        class FakeServer:
            def __init__(self, db_path, agent_id, host, port):
                started.append({"db": db_path, "agent_id": agent_id, "host": host, "port": port})

            def start(self):
                pass

        cfg = _SyncCfg(
            peers=[_peer("T", "http://127.0.0.1:9881", "TESTAGENT")],
            listen_port=9877,
            enable_server=True,
            listen_host="0.0.0.0",
        )
        with patch("infra._lazy_imports.get_config", return_value=cfg), \
             patch("infra.sync_server.SyncServer", FakeServer), \
             patch("save_pipeline._crdt_agent_id", return_value="TESTAGENT"):
            result = mod.start_server_from_config("/tmp/x.db")

        self.assertIsNotNone(result)
        self.assertEqual(
            started,
            [{"db": "/tmp/x.db", "agent_id": "TESTAGENT", "host": "0.0.0.0", "port": 9881}],
        )
        self.assertIsInstance(result, FakeServer)

    def test_agent_without_peer_uses_global_port(self):
        from infra import sync_server as mod

        started = []

        class FakeServer:
            def __init__(self, db_path, agent_id, host, port):
                started.append(port)

            def start(self):
                pass

        cfg = _SyncCfg(
            peers=[_peer("B", "http://127.0.0.1:9880", "AMI")],
            listen_port=4567,
            enable_server=True,
            listen_host="127.0.0.1",
        )
        with patch("infra._lazy_imports.get_config", return_value=cfg), \
             patch("infra.sync_server.SyncServer", FakeServer), \
             patch("save_pipeline._crdt_agent_id", return_value="GHOST"):
            result = mod.start_server_from_config("/tmp/y.db")

        self.assertIsInstance(result, FakeServer)
        self.assertEqual(started, [4567])


if __name__ == "__main__":
    unittest.main()
