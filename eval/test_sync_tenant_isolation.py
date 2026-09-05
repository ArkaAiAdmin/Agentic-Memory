#!/usr/bin/env python3
"""Tests for tenant isolation in the sync server and client.

Verifies that the sync server only returns memories for its configured
tenant, and that the client only pushes memories for the specified tenant.
"""

import json
import os
import socket as _socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from datetime import datetime, timezone

INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))
sys.path.insert(0, str(INSTALL_DIR / "eval"))


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


def _fresh_db(name: str) -> Path:
    p = Path(tempfile.mkdtemp(prefix=f"tenant_iso_{name}_")) / "memory.db"
    return p


def _init_db_with_tenants(db_path: Path) -> None:
    """Create a DB with the memories table (including tenant_id) and sync_log."""
    import sqlite3

    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL DEFAULT '',
            source_file TEXT NOT NULL DEFAULT '',
            tags TEXT DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            observed_at TEXT NOT NULL DEFAULT (datetime('now')),
            deleted_at TEXT,
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
            consolidation_state TEXT DEFAULT 'working',
            tenant_id TEXT NOT NULL DEFAULT 'default'
        );
        CREATE INDEX IF NOT EXISTS idx_memories_tenant_id ON memories(tenant_id);
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
        CREATE TABLE IF NOT EXISTS memory_field_crdt (
            memory_id TEXT NOT NULL,
            field_name TEXT NOT NULL,
            value TEXT,
            version_vector TEXT DEFAULT '{}',
            logical_clock INTEGER DEFAULT 0,
            last_writer_agent TEXT DEFAULT '',
            is_deleted INTEGER DEFAULT 0,
            updated_at TEXT DEFAULT (datetime('now')),
            tenant_id TEXT NOT NULL DEFAULT 'default',
            PRIMARY KEY (memory_id, field_name, tenant_id)
        );
    """)
    conn.commit()
    conn.close()


def _insert_note(
    db_path: Path,
    note_id: str,
    content: str,
    tenant_id: str = "default",
    agent_id: str = "test-agent",
) -> None:
    """Insert a note directly into the memories table with tenant_id."""
    import sqlite3

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        """INSERT OR REPLACE INTO memories
           (id, content, source_file, tags, created_at, updated_at, observed_at,
            version_vector, logical_clock, tenant_id)
           VALUES (?, ?, ?, '[]', ?, ?, ?, '{}', 0, ?)""",
        (note_id, content, note_id, now, now, now, tenant_id),
    )
    conn.commit()
    conn.close()


def _count_notes(db_path: Path, tenant_id: str | None = None) -> int:
    """Count non-deleted notes, optionally filtered by tenant_id."""
    import sqlite3

    conn = sqlite3.connect(str(db_path), timeout=5)
    if tenant_id:
        row = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL AND tenant_id = ?",
            (tenant_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL"
        ).fetchone()
    conn.close()
    return row[0] if row else 0


class TestSyncTenantIsolation(unittest.TestCase):
    """Verify that the sync server scopes queries to its tenant_id."""

    @classmethod
    def setUpClass(cls):
        cls.db = _fresh_db("tenant_iso")
        _init_db_with_tenants(cls.db)

        # Insert notes for two tenants
        _insert_note(cls.db, "note/a1", "Alice tenant-A note", tenant_id="agent-a")
        _insert_note(cls.db, "note/a2", "Alice tenant-A note 2", tenant_id="agent-a")
        _insert_note(cls.db, "note/b1", "Bob tenant-B note", tenant_id="agent-b")
        _insert_note(cls.db, "note/b2", "Bob tenant-B note 2", tenant_id="agent-b")
        _insert_note(cls.db, "note/b3", "Bob tenant-B note 3", tenant_id="agent-b")

        # Start server scoped to tenant agent-a
        from infra.sync_server import SyncServer

        cls._orig_loopback = os.environ.get("MEMORY_SYNC_ALLOW_UNAUTHENTICATED_LOOPBACK")
        os.environ["MEMORY_SYNC_ALLOW_UNAUTHENTICATED_LOOPBACK"] = "1"

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        cls.port = sock.getsockname()[1]
        sock.close()

        cls.server = SyncServer(
            db_path=str(cls.db),
            agent_id="agent-a",
            host="127.0.0.1",
            port=cls.port,
            tenant_id="agent-a",
        )
        cls.server.start()
        cls.url = f"http://127.0.0.1:{cls.port}"
        _wait_for_server("127.0.0.1", cls.port)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        if cls._orig_loopback is None:
            os.environ.pop("MEMORY_SYNC_ALLOW_UNAUTHENTICATED_LOOPBACK", None)
        else:
            os.environ["MEMORY_SYNC_ALLOW_UNAUTHENTICATED_LOOPBACK"] = cls._orig_loopback

    def test_health_counts_only_own_tenant(self):
        """The /health endpoint should only count notes for the server's tenant."""
        import urllib.request

        resp = urllib.request.urlopen(f"{self.url}/health", timeout=5)
        data = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(data["status"], "ok")
        # Server is scoped to agent-a which has 2 notes
        self.assertEqual(data["note_count"], 2)
        # Verify the total across all tenants is higher
        total = _count_notes(self.db)
        self.assertGreater(total, data["note_count"])

    def _get_with_timestamp(self, url: str):
        """Make a GET request with X-Sync-Timestamp header (required by replay protection)."""
        import urllib.request

        req = urllib.request.Request(url)
        req.add_header("X-Sync-Timestamp", str(int(time.time())))
        return urllib.request.urlopen(req, timeout=5)

    def test_changes_returns_only_own_tenant(self):
        """The /crdt/changes endpoint should only return notes for the server's tenant."""
        resp = self._get_with_timestamp(
            f"{self.url}/crdt/changes?since=0&agent=test"
        )
        data = json.loads(resp.read().decode("utf-8"))
        changes = data.get("changes", [])

        # Server is scoped to agent-a, should only see 2 notes
        self.assertEqual(len(changes), 2)

        # Verify all returned notes belong to agent-a
        returned_ids = {c["id"] for c in changes}
        self.assertEqual(returned_ids, {"note/a1", "note/a2"})

    def test_changes_does_not_leak_other_tenant(self):
        """Verify that tenant-B notes are NOT returned by the server."""
        resp = self._get_with_timestamp(
            f"{self.url}/crdt/changes?since=0&agent=test&limit=1000"
        )
        data = json.loads(resp.read().decode("utf-8"))
        changes = data.get("changes", [])

        returned_ids = {c["id"] for c in changes}
        # None of Bob's notes should appear
        self.assertNotIn("note/b1", returned_ids)
        self.assertNotIn("note/b2", returned_ids)
        self.assertNotIn("note/b3", returned_ids)

    def test_push_to_peer_scopes_by_tenant(self):
        """The client push_to_peer should only push notes for the specified tenant."""
        from infra.sync_client import push_to_peer

        # Create a fresh server DB with both tenants
        server_db = _fresh_db("push_target")
        _init_db_with_tenants(server_db)

        from infra.sync_server import SyncServer

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        server_port = sock.getsockname()[1]
        sock.close()

        server = SyncServer(
            db_path=str(server_db),
            agent_id="target",
            host="127.0.0.1",
            port=server_port,
            tenant_id="default",
        )
        server.start()
        server_url = f"http://127.0.0.1:{server_port}"
        _wait_for_server("127.0.0.1", server_port)

        try:
            # Create a client DB with notes from two tenants
            client_db = _fresh_db("push_client")
            _init_db_with_tenants(client_db)
            _insert_note(client_db, "c/a1", "client tenant-A", tenant_id="tenant-a")
            _insert_note(client_db, "c/b1", "client tenant-B", tenant_id="tenant-b")

            # Push only tenant-a notes
            result = push_to_peer(
                db_path=client_db,
                peer_url=server_url,
                peer_name="target",
                peer_agent_id="target",
                local_agent_id="client",
                since=0,
                tenant_id="tenant-a",
            )
            self.assertIn("total", result)
            # Only 1 note should be pushed (tenant-a only)
            self.assertEqual(result["total"], 1)

            # Verify the server only received tenant-a note
            import sqlite3

            conn = sqlite3.connect(str(server_db), timeout=5)
            rows = conn.execute("SELECT id FROM memories").fetchall()
            conn.close()
            server_ids = {r[0] for r in rows}
            self.assertIn("c/a1", server_ids)
            self.assertNotIn("c/b1", server_ids)
        finally:
            server.stop()


import socket


if __name__ == "__main__":
    unittest.main()
