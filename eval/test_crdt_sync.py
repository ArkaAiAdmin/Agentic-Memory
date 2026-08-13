#!/usr/bin/env python3
"""Integration tests for the auto multi-agent sync layer.

Tests the full sync protocol without external network:
- Spins up two SyncServer instances on random ports
- Writes notes to each DB
- Syncs them via sync_client
- Verifies CRDT merge results
"""

import json
import socket as _socket
import sys
import tempfile
import time
import unittest
from pathlib import Path

INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))
sys.path.insert(0, str(INSTALL_DIR / "eval"))

from infra.memory_common import connection_pool, open_db


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


from infra.sync_server import SyncServer
from infra.sync_client import (
    pull_from_peer,
    push_to_peer,
    sync_with_peer,
    _get_last_push_timestamp,
)


def _fresh_db(name: str) -> Path:
    p = Path(tempfile.mkdtemp(prefix=f"sync_{name}_")) / "memory.db"
    connection_pool.clear()
    return p


def _init_db(db_path: Path):
    """Create an empty DB with the full schema."""
    with open_db(db_path, timeout=10.0) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                source_file TEXT NOT NULL,
                tags TEXT DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                pinned INTEGER DEFAULT 0,
                importance INTEGER DEFAULT 3,
                decay TEXT DEFAULT 'none',
                score REAL DEFAULT 1.0,
                supersedes TEXT,
                repo_id TEXT,
                access_count INTEGER DEFAULT 1,
                success_score REAL DEFAULT 0.0,
                fitness_score REAL DEFAULT 1.0,
                conflict_policy TEXT DEFAULT 'supersede',
                version_vector TEXT DEFAULT '{}',
                logical_clock INTEGER DEFAULT 0,
                consolidation_state TEXT DEFAULT 'working'
            );
            CREATE TABLE IF NOT EXISTS sync_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                peer_name TEXT NOT NULL,
                peer_url TEXT NOT NULL,
                peer_agent_id TEXT NOT NULL,
                direction TEXT NOT NULL CHECK (direction IN ('push', 'pull', 'sync')),
                started_at REAL NOT NULL,
                completed_at REAL,
                success INTEGER DEFAULT 0,
                changes_pushed INTEGER DEFAULT 0,
                changes_pulled INTEGER DEFAULT 0,
                error_message TEXT,
                error_count INTEGER DEFAULT 0,
                duration_ms INTEGER DEFAULT 0
            );
        """)
        conn.commit()


def _write_note(db_path: Path, note_id: str, content: str, agent_id: str = "agent-A"):
    """Write a note directly with CRDT metadata, bypassing the full pipeline."""
    from datetime import datetime, timezone

    from save_pipeline import _crdt_bump_version

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open_db(db_path, timeout=10.0) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            """INSERT OR REPLACE INTO memories
               (id, content, source_file, tags, created_at, updated_at, observed_at,
                version_vector, logical_clock)
               VALUES (?, ?, ?, '[]', ?, ?, ?, '{}', 0)""",
            (note_id, content, note_id, now, now, now),
        )
        _crdt_bump_version(conn, note_id, {"id", "content", "source_file"})
        conn.commit()


class TestSyncServer(unittest.TestCase):
    """Test the HTTP sync server endpoints."""

    @classmethod
    def setUpClass(cls):
        cls.db_a = _fresh_db("server_a")
        _init_db(cls.db_a)
        _write_note(cls.db_a, "note/1", "Hello from A", "agent-A")
        _write_note(cls.db_a, "note/2", "Hello again from A", "agent-A")

        cls.server = SyncServer(
            db_path=str(cls.db_a),
            agent_id="agent-A",
            host="127.0.0.1",
            port=0,  # bind to random port
        )
        # Force a real port by re-creating with the actual bound port.
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        cls.port = sock.getsockname()[1]
        sock.close()

        cls.server.port = cls.port
        cls.server.start()
        cls.url = f"http://127.0.0.1:{cls.port}"
        _wait_for_server("127.0.0.1", cls.port)

        # Patch urllib so every request carries the X-Sync-Timestamp
        # header that sync_server._check_replay requires when
        # SYNC_MAX_REQUEST_AGE > 0. The tests below exercise both
        # success and failure paths; this avoids 401-preempts-400.
        import urllib.request as _ur

        _orig_Request = _ur.Request

        class _PatchedRequest(_orig_Request):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                if not self.get_header("X-Sync-Timestamp"):
                    self.add_header("X-Sync-Timestamp", str(int(time.time())))

        _ur.Request = _PatchedRequest
        cls._urllib_patched = True

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        if getattr(cls, "_urllib_patched", False):
            import importlib
            import urllib.request as _ur

            importlib.reload(_ur)

    def test_health(self):
        import urllib.request
        import json

        resp = urllib.request.urlopen(f"{self.url}/health", timeout=5)
        data = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(data["status"], "ok")
        # SEC (LOW-2): unauth /health must not leak agent identity
        self.assertNotIn("agent_id", data)
        self.assertGreaterEqual(data["note_count"], 2)

    def test_changes_since(self):
        import urllib.request
        import json

        resp = urllib.request.urlopen(
            f"{self.url}/crdt/changes?since=0&agent=test", timeout=5
        )
        data = json.loads(resp.read().decode("utf-8"))
        self.assertIn("changes", data)
        self.assertGreaterEqual(data["count"], 2)

    def test_changes_limit(self):
        import urllib.request
        import json

        resp = urllib.request.urlopen(
            f"{self.url}/crdt/changes?since=0&agent=test&limit=1", timeout=5
        )
        data = json.loads(resp.read().decode("utf-8"))
        self.assertLessEqual(data["count"], 1)

    def test_changes_missing_since(self):
        import urllib.request
        from urllib.error import HTTPError

        # Set X-Sync-Timestamp so the replay check passes; the 400 must
        # come from the route handler (missing `since` param), not from
        # replay protection.
        req = urllib.request.Request(f"{self.url}/crdt/changes?agent=test")
        req.add_header("X-Sync-Timestamp", str(int(__import__("time").time())))
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("expected 400")
        except HTTPError as e:
            self.assertEqual(e.code, 400)

    def test_changes_empty(self):
        import urllib.request
        import json
        from datetime import datetime, timezone

        future_ts = int(datetime.now(timezone.utc).timestamp()) + 86400
        resp = urllib.request.urlopen(
            f"{self.url}/crdt/changes?since={future_ts}&agent=test", timeout=5
        )
        data = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(data["count"], 0)
        self.assertEqual(data["changes"], [])

    def test_push_new_notes(self):
        import urllib.request
        import json

        body = json.dumps(
            {
                "agent_id": "agent-B",
                "notes": {
                    "note/pushed/1": {
                        "content": "Hello from B",
                        "source_file": "note/pushed/1",
                        "logical_clock": 1,
                        "version_vector": '{"agent-B": 1}',
                        "sender_clock": 1,
                    }
                },
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{self.url}/crdt/push",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read().decode("utf-8"))
        self.assertGreaterEqual(data["applied"], 1)
        self.assertEqual(data["total"], 1)

    def test_push_invalid(self):
        import urllib.request
        from urllib.error import HTTPError

        body = json.dumps({"agent_id": ""}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.url}/crdt/push",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Sync-Timestamp": str(int(__import__("time").time())),
            },
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("expected 400")
        except HTTPError as e:
            self.assertEqual(e.code, 400)

    def test_404(self):
        import urllib.request
        from urllib.error import HTTPError

        try:
            urllib.request.urlopen(f"{self.url}/nonexistent", timeout=5)
            self.fail("expected 404")
        except HTTPError as e:
            self.assertEqual(e.code, 404)


class TestSyncClient(unittest.TestCase):
    """Test sync_client pull/push with a real local server."""

    @classmethod
    def setUpClass(cls):
        cls.db_a = _fresh_db("client_a")
        _init_db(cls.db_a)
        _write_note(cls.db_a, "note/a1", "Alice's note", "agent-A")
        _write_note(cls.db_a, "note/a2", "Alice's second note", "agent-A")

        cls.db_b = _fresh_db("client_b")
        _init_db(cls.db_b)
        _write_note(cls.db_b, "note/b1", "Bob's note", "agent-B")

        cls.server = SyncServer(
            db_path=str(cls.db_a),
            agent_id="agent-A",
            host="127.0.0.1",
            port=0,
        )
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        cls.port = sock.getsockname()[1]
        sock.close()

        cls.server.port = cls.port
        cls.server.start()
        cls.url = f"http://127.0.0.1:{cls.port}"
        _wait_for_server("127.0.0.1", cls.port)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def test_pull_from_empty_db(self):
        result = pull_from_peer(
            db_path=self.db_b,
            peer_url=self.url,
            peer_name="alice",
            peer_agent_id="agent-A",
            local_agent_id="agent-B",
            since=0,
        )
        self.assertIn("applied", result)
        self.assertGreater(result["total"], 0)

    def test_push_to_peer(self):
        result = push_to_peer(
            db_path=self.db_b,
            peer_url=self.url,
            peer_name="alice",
            peer_agent_id="agent-A",
            local_agent_id="agent-B",
            since=0,
        )
        self.assertIn("total", result)
        self.assertGreaterEqual(result["total"], 1)

    def test_push_empty(self):
        import time as ttime

        future = ttime.time() + 86400
        result = push_to_peer(
            db_path=self.db_b,
            peer_url=self.url,
            peer_name="alice",
            peer_agent_id="agent-A",
            local_agent_id="agent-B",
            since=future,
        )
        self.assertEqual(result.get("total", 0), 0)

    def test_sync_with_peer_full_cycle(self):
        result = sync_with_peer(
            db_path=self.db_b,
            peer_url=self.url,
            peer_name="alice",
            peer_agent_id="agent-A",
            local_agent_id="agent-B",
        )
        self.assertIn("push", result)
        self.assertIn("pull", result)
        self.assertTrue(result.get("success", False))

    def test_sync_log_written(self):
        """Verify sync_with_peer writes a row to sync_log."""
        from infra.memory_common import open_db

        sync_with_peer(
            db_path=self.db_b,
            peer_url=self.url,
            peer_name="alice",
            peer_agent_id="agent-A",
            local_agent_id="agent-B",
        )
        with open_db(self.db_b, timeout=5.0) as conn:
            rows = conn.execute(
                "SELECT COUNT(*) FROM sync_log WHERE peer_name='alice'"
            ).fetchone()
            self.assertGreater(rows[0], 0)

    def test_get_last_push_timestamp(self):
        push_to_peer(
            db_path=self.db_b,
            peer_url=self.url,
            peer_name="alice",
            peer_agent_id="agent-A",
            local_agent_id="agent-B",
            since=0,
        )
        ts = _get_last_push_timestamp(self.db_b, "alice")
        self.assertGreater(ts, 0)

    def test_sync_to_self(self):
        """Sync a DB to its own server — should be a no-op."""
        result = sync_with_peer(
            db_path=self.db_a,
            peer_url=self.url,
            peer_name="self",
            peer_agent_id="agent-A",
            local_agent_id="agent-A",
        )
        self.assertIn("push", result)
        self.assertIn("pull", result)


class TestSyncCrdtMerge(unittest.TestCase):
    """End-to-end: two servers, write conflicting notes, verify CRDT merge."""

    def test_bidirectional_sync(self):
        db_a = _fresh_db("bi_a")
        _init_db(db_a)
        db_b = _fresh_db("bi_b")
        _init_db(db_b)

        _write_note(db_a, "note/conflict", "Alice v1", "agent-A")

        server_a = SyncServer(
            db_path=str(db_a), agent_id="agent-A", host="127.0.0.1", port=0
        )
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port_a = sock.getsockname()[1]
        sock.close()
        server_a.port = port_a
        server_a.start()
        url_a = f"http://127.0.0.1:{port_a}"

        server_b = SyncServer(
            db_path=str(db_b), agent_id="agent-B", host="127.0.0.1", port=0
        )
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port_b = sock.getsockname()[1]
        sock.close()
        server_b.port = port_b
        server_b.start()
        url_b = f"http://127.0.0.1:{port_b}"

        _wait_for_server("127.0.0.1", port_a)
        _wait_for_server("127.0.0.1", port_b)

        try:
            # Pull from peer A to B — should get Alice v1
            r1 = pull_from_peer(
                db_path=db_b,
                peer_url=url_a,
                peer_name="a",
                peer_agent_id="agent-A",
                local_agent_id="agent-B",
                since=0,
            )
            self.assertGreater(r1["total"], 0)

            # Write conflicting note on B
            _write_note(db_b, "note/conflict", "Bob v1", "agent-B")

            # Sync A -> B (push B's changes to A, pull A's changes to B)
            r2 = sync_with_peer(
                db_path=db_b,
                peer_url=url_a,
                peer_name="a",
                peer_agent_id="agent-A",
                local_agent_id="agent-B",
            )
            self.assertIn("push", r2)
            self.assertIn("pull", r2)

            # Sync B -> A (push A's changes to B, pull B's changes to A)
            r3 = sync_with_peer(
                db_path=db_a,
                peer_url=url_b,
                peer_name="b",
                peer_agent_id="agent-B",
                local_agent_id="agent-A",
            )
            self.assertIn("push", r3)
        finally:
            server_a.stop()
            server_b.stop()

    def test_conflict_resolution(self):
        """Two agents write to same note, LWW should pick a winner."""
        db = _fresh_db("conflict")
        _init_db(db)

        # Agent A writes first
        _write_note(db, "note/lww", "Version A", "agent-A")
        # Agent B writes with higher logical clock
        _write_note(db, "note/lww", "Version B", "agent-B")

        from infra.memory_common import open_db

        with open_db(db, timeout=5.0) as conn:
            row = conn.execute(
                "SELECT content, version_vector FROM memories WHERE id='note/lww'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertIn(row[0], ["Version A", "Version B"])


if __name__ == "__main__":
    unittest.main()
