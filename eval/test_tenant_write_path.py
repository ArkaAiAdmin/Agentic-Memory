#!/usr/bin/env python3
"""CHANGE 5 — tenant write-path validation.

The authenticated principal's tenant must be authoritative on writes and
deletes. We assert:
  * save_memory with an explicit tenant_id writes a row scoped to that tenant
    (not silently re-derived to "default").
  * soft_delete_note scoped to a different tenant refuses to delete the note
    (returns False) — preventing cross-tenant deletion.

Run:
    ~/.config/agentic-memory/venv/bin/python -m unittest eval.test_tenant_write_path -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

os.environ.setdefault("MEMORY_AGENT_ID", "test-agent")
os.environ.setdefault("MEMORY_AUTH_MODE", "open")

from save.pipeline import save_memory  # noqa: E402
from memory_delete import soft_delete_note  # noqa: E402
from infra.memory_common import open_db  # noqa: E402


class TestTenantWritePath(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="tenant_write_")
        self.db_path = Path(self.tmpdir) / "memory.db"
        with open_db(self.db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    source_file TEXT NOT NULL,
                    tags TEXT DEFAULT '[]',
                    created_at TEXT, updated_at TEXT, observed_at TEXT,
                    pinned INTEGER DEFAULT 0, importance INTEGER DEFAULT 3,
                    score REAL DEFAULT 1.0, valid_from TEXT, valid_to TEXT,
                    superseded_by TEXT, deleted_at TEXT, deleted_by TEXT,
                    tenant_id TEXT DEFAULT 'default'
                );
                """
            )
            conn.commit()

    def tearDown(self):
        try:
            for p in Path(self.tmpdir).glob("*"):
                p.unlink()
            Path(self.tmpdir).rmdir()
        except Exception:
            pass

    def test_explicit_tenant_id_scopes_row(self):
        note_id = save_memory(
            content="tenant scoped note",
            category="lessons",
            title_slug="scoped",
            tenant_id="tenant-a",
            defer_expensive=True,
            db_path=str(self.db_path),
        )
        with open_db(self.db_path) as conn:
            row = conn.execute(
                "SELECT tenant_id FROM memories WHERE id = ?", (note_id,)
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "tenant-a")

    def test_cross_tenant_soft_delete_refused(self):
        note_id = save_memory(
            content="note in tenant-a",
            category="lessons",
            title_slug="tenant-a-note",
            tenant_id="tenant-a",
            defer_expensive=True,
            db_path=str(self.db_path),
        )
        # A call scoped to tenant-b must NOT delete tenant-a's note.
        ok = soft_delete_note(
            self.db_path, note_id, deleted_by="test", tenant_id="tenant-b"
        )
        self.assertFalse(ok, "cross-tenant soft delete must be refused")
        with open_db(self.db_path) as conn:
            row = conn.execute(
                "SELECT deleted_at FROM memories WHERE id = ?", (note_id,)
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertIsNone(row[0], "note must remain active")

    def test_same_tenant_soft_delete_allowed(self):
        note_id = save_memory(
            content="note in tenant-a",
            category="lessons",
            title_slug="tenant-a-note2",
            tenant_id="tenant-a",
            defer_expensive=True,
            db_path=str(self.db_path),
        )
        ok = soft_delete_note(
            self.db_path, note_id, deleted_by="test", tenant_id="tenant-a"
        )
        self.assertTrue(ok, "same-tenant soft delete must succeed")


if __name__ == "__main__":
    unittest.main()
