#!/usr/bin/env python3
"""Regression test: contradiction detector must be tenant-scoped.

In a multi-agent deployment several agents share one physical memory.db
(scoped via the tenant_memories view). The detector previously read the raw
`memories` base table, so it compared notes across tenants and produced
cross-tenant false-positive contradictions. The resolver reads tenant_memories,
so the two disagreed on scope.

Run:
    ~/.config/agentic-memory/venv/bin/python -m unittest eval.test_contradiction_tenant_scope -v
"""
import sys
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

from infra.memory_common import open_db, connection_pool  # noqa: E402
from kg.contradiction_detector import detect_contradictions  # noqa: E402


class TestContradictionTenantScope(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="contradiction_tenant_test_")
        self.db_path = Path(self.tmpdir) / "memory.db"
        self._bootstrap_db()

    def tearDown(self):
        connection_pool.clear()
        try:
            for p in Path(self.tmpdir).glob("*"):
                p.unlink()
            Path(self.tmpdir).rmdir()
        except Exception:
            pass

    def _bootstrap_db(self):
        with open_db(self.db_path) as conn:
            conn.executescript(
                """
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
                    score REAL DEFAULT 1.0,
                    valid_from TEXT,
                    valid_to TEXT,
                    superseded_by TEXT,
                    deleted_at TEXT,
                    deleted_by TEXT,
                    tenant_id TEXT DEFAULT 'default'
                );
                """
            )
            conn.commit()

    def _insert(self, note_id: str, content: str, tenant: str):
        now = datetime.now(timezone.utc).isoformat()
        with open_db(self.db_path) as conn:
            conn.execute(
                "INSERT INTO memories (id, content, source_file, created_at, "
                "updated_at, observed_at, tenant_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (note_id, content, f"{note_id}.md", now, now, now, tenant),
            )
            conn.commit()

    def test_cross_tenant_pairs_excluded_when_scoped(self):
        """Two contradictory notes in DIFFERENT tenants must not be flagged
        when detection is scoped to one tenant."""
        self._insert(
            "lessons/a1", "The cache is enabled and working.", "tenant-a"
        )
        self._insert(
            "lessons/b1", "The cache is disabled and not working.", "tenant-b"
        )

        # Scoped to tenant-a: only tenant-a notes considered.
        a = detect_contradictions(self.tmpdir, tenant_id="tenant-a")
        self.assertEqual(a, [], "cross-tenant pair leaked into tenant-a scope")

        # Scoped to tenant-b.
        b = detect_contradictions(self.tmpdir, tenant_id="tenant-b")
        self.assertEqual(b, [], "cross-tenant pair leaked into tenant-b scope")

    def test_same_tenant_contradiction_still_detected(self):
        """A real contradiction within one tenant must still be found."""
        self._insert(
            "lessons/a1", "The cache is enabled and working.", "tenant-a"
        )
        self._insert(
            "lessons/a2", "The cache is disabled and not working.", "tenant-a"
        )
        a = detect_contradictions(self.tmpdir, tenant_id="tenant-a")
        self.assertEqual(len(a), 1, "same-tenant contradiction not detected")

    def test_unscoped_sees_all_tenants(self):
        """Backward-compat: omitting tenant_id scans everything (old behavior)."""
        self._insert(
            "lessons/a1", "The cache is enabled and working.", "tenant-a"
        )
        self._insert(
            "lessons/b1", "The cache is disabled and not working.", "tenant-b"
        )
        all_rows = detect_contradictions(self.tmpdir)
        self.assertEqual(len(all_rows), 1, "unscoped scan should see the pair")


if __name__ == "__main__":
    unittest.main()
