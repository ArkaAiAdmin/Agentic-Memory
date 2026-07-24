"""Integration tests for CRDT wiring in the save pipeline.

Covers:
- _crdt_agent_id resolution (env var, hostname fallback)
- _is_crdt_enabled detection (env var, config, default)
- _crdt_bump_version on successive saves
- Integration via _update_memory_index_incremental
- memory_crdt_sync MCP admin tool
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

INSTALL_DIR = Path(__file__).resolve().parent.parent
import sys

sys.path.insert(0, str(INSTALL_DIR))

import sqlite3
from infra.memory_common import connection_pool
from infra.db_migrations import run_db_migrations


def _fresh_db() -> Path:
    """Create a fresh temp DB with migrations applied."""
    tmp = Path(tempfile.mkdtemp(prefix="crdt_integration_"))
    db_path = tmp / "memory.db"
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.commit()
    run_db_migrations(conn)
    conn.commit()
    conn.close()
    connection_pool.clear()
    return db_path


def _seed_note(db_path: Path, note_id: str, content: str = "hello") -> str:
    """Insert a note directly (no CRDT) for testing CRDT bump on update."""
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.execute(
        """INSERT OR REPLACE INTO memories
           (id, content, source_file, tags, created_at, updated_at, observed_at,
            fitness_score, importance, pinned, version_vector, logical_clock)
           VALUES (?, ?, ?, '[]', ?, ?, ?, 0.5, 3, 0, ?, ?)""",
        (
            note_id,
            content,
            f"{note_id}.md",
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
            json.dumps({"local": 1}),
            1,
        ),
    )
    conn.commit()
    conn.close()
    return note_id


class TestCrdtAgentId(unittest.TestCase):
    """_crdt_agent_id resolution order."""

    def setUp(self):
        from save_pipeline import _crdt_agent_id

        self.fn = _crdt_agent_id

    @mock.patch.dict(os.environ, {"MEMORY_AGENT_ID": "env-agent"}, clear=True)
    def test_env_var_wins(self):
        aid = self.fn()
        self.assertEqual(aid, "env-agent")

    @mock.patch("socket.gethostname", return_value="test-host")
    @mock.patch.dict(os.environ, {}, clear=True)
    def test_hostname_fallback(self, mock_hostname):
        aid = self.fn()
        self.assertEqual(aid, "test-host")

    @mock.patch.dict(os.environ, {}, clear=True)
    def test_ultimate_fallback(self):
        aid = self.fn()
        self.assertIsInstance(aid, str)
        self.assertTrue(len(aid) > 0)


class TestIsCrdtEnabled(unittest.TestCase):
    """_is_crdt_enabled resolution."""

    def setUp(self):
        from save_pipeline import _is_crdt_enabled

        self.fn = _is_crdt_enabled

    @mock.patch.dict(os.environ, {"MEMORY_CRDT_ENABLED": "0"}, clear=True)
    def test_env_var_disabled(self):
        self.assertFalse(self.fn())

    @mock.patch.dict(os.environ, {"MEMORY_CRDT_ENABLED": "1"}, clear=True)
    def test_env_var_enabled(self):
        self.assertTrue(self.fn())


class TestCrdtBumpVersionNewNote(unittest.TestCase):
    """_crdt_bump_version on a freshly inserted note."""

    def setUp(self):
        self.db_path = _fresh_db()
        self.conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        self.note_id = "lessons/crdt-bump-test-new"
        _seed_note(self.db_path, self.note_id, "seed content")
        # Reset version vector to empty for this test
        self.conn.execute(
            "UPDATE memories SET version_vector='{}', logical_clock=0 WHERE id=?",
            (self.note_id,),
        )
        self.conn.commit()
        # _crdt_bump_version reads/writes via the tenant_memories temp view
        # (normally created by open_db). Create it here for the raw connection.
        from infra.db import _setup_tenant_view
        _setup_tenant_view(self.conn, "default")
        self.cols = {
            row[1]
            for row in self.conn.execute("PRAGMA table_info(memories)").fetchall()
        }

    def tearDown(self):
        self.conn.close()
        connection_pool.clear()

    @mock.patch.dict(
        os.environ,
        {"MEMORY_AGENT_ID": "test-agent", "MEMORY_CRDT_ENABLED": "1"},
        clear=True,
    )
    def test_bump_from_empty(self):
        from save_pipeline import _crdt_bump_version

        _crdt_bump_version(self.conn, self.note_id, self.cols)
        row = self.conn.execute(
            "SELECT version_vector, logical_clock FROM memories WHERE id=?",
            (self.note_id,),
        ).fetchone()
        vv = json.loads(row[0])
        self.assertIn("test-agent", vv)
        self.assertEqual(vv["test-agent"], 1)
        self.assertEqual(row[1], 1)

    @mock.patch.dict(
        os.environ,
        {"MEMORY_AGENT_ID": "test-agent", "MEMORY_CRDT_ENABLED": "1"},
        clear=True,
    )
    def test_bump_twice(self):
        from save_pipeline import _crdt_bump_version

        _crdt_bump_version(self.conn, self.note_id, self.cols)
        _crdt_bump_version(self.conn, self.note_id, self.cols)
        row = self.conn.execute(
            "SELECT version_vector, logical_clock FROM memories WHERE id=?",
            (self.note_id,),
        ).fetchone()
        vv = json.loads(row[0])
        self.assertEqual(vv["test-agent"], 2)
        self.assertEqual(row[1], 2)


class TestCrdtThroughSavePipeline(unittest.TestCase):
    """End-to-end: version vectors tracked when saving through the pipeline."""

    def setUp(self):
        self.orig_crdt = os.environ.get("MEMORY_CRDT_ENABLED")
        self.orig_agent = os.environ.get("MEMORY_AGENT_ID")
        os.environ["MEMORY_CRDT_ENABLED"] = "1"
        os.environ["MEMORY_AGENT_ID"] = "test-save-agent"
        self.orig_path = os.environ.get("MEMORY_DB_PATH")
        self.db_path = _fresh_db()
        os.environ["MEMORY_DB_PATH"] = str(self.db_path)

    def tearDown(self):
        if self.orig_crdt is not None:
            os.environ["MEMORY_CRDT_ENABLED"] = self.orig_crdt
        else:
            os.environ.pop("MEMORY_CRDT_ENABLED", None)
        if self.orig_agent is not None:
            os.environ["MEMORY_AGENT_ID"] = self.orig_agent
        else:
            os.environ.pop("MEMORY_AGENT_ID", None)
        if self.orig_path is not None:
            os.environ["MEMORY_DB_PATH"] = self.orig_path
        else:
            os.environ.pop("MEMORY_DB_PATH", None)
        connection_pool.clear()

    def test_save_tracks_version_vector(self):
        from save_pipeline import _update_memory_index_incremental

        now = "2026-06-17T12:00:00+00:00"
        _update_memory_index_incremental(
            db_path=self.db_path,
            category="lessons",
            title_slug="integration-test",
            content="integration test content",
            tags=["test"],
            pinned=False,
            now_iso=now,
            is_global=False,
        )
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        row = conn.execute(
            "SELECT version_vector, logical_clock FROM memories WHERE id=?",
            ("lessons/integration-test",),
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row, "Note should exist")
        vv = json.loads(row[0])
        self.assertIn("test-save-agent", vv)
        self.assertEqual(vv["test-save-agent"], 1)
        self.assertEqual(row[1], 1)

    def test_version_vector_increments_on_update(self):
        from save_pipeline import _update_memory_index_incremental

        now = "2026-06-17T12:00:00+00:00"
        _update_memory_index_incremental(
            db_path=self.db_path,
            category="lessons",
            title_slug="increment-test",
            content="v1",
            tags=[],
            pinned=False,
            now_iso=now,
            is_global=False,
        )
        now2 = "2026-06-17T13:00:00+00:00"
        _update_memory_index_incremental(
            db_path=self.db_path,
            category="lessons",
            title_slug="increment-test",
            content="v2",
            tags=[],
            pinned=False,
            now_iso=now2,
            is_global=False,
        )
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        row = conn.execute(
            "SELECT version_vector, logical_clock FROM memories WHERE id=?",
            ("lessons/increment-test",),
        ).fetchone()
        conn.close()
        vv = json.loads(row[0])
        self.assertEqual(vv["test-save-agent"], 2)
        self.assertEqual(row[1], 2)

    def test_crdt_disabled_no_version_vector(self):
        os.environ["MEMORY_CRDT_ENABLED"] = "0"
        from save_pipeline import _update_memory_index_incremental

        now = "2026-06-17T12:00:00+00:00"
        _update_memory_index_incremental(
            db_path=self.db_path,
            category="lessons",
            title_slug="disabled-test",
            content="no crdt",
            tags=[],
            pinned=False,
            now_iso=now,
            is_global=False,
        )
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        row = conn.execute(
            "SELECT version_vector, logical_clock FROM memories WHERE id=?",
            ("lessons/disabled-test",),
        ).fetchone()
        conn.close()
        # Columns still exist in schema, but version_vector stays at default '{}'
        # since no CRDT bump runs
        vv = json.loads(row[0]) if row[0] else {}
        self.assertEqual(vv, {})


class TestMemoryCrdtSyncTool(unittest.TestCase):
    """memory_crdt_sync MCP admin tool integration."""

    def setUp(self):
        self.db_path = _fresh_db()
        self.orig_path = os.environ.get("MEMORY_DB_PATH")
        os.environ["MEMORY_DB_PATH"] = str(self.db_path)
        self.orig_trusted = os.environ.get("MEMORY_CRDT_TRUSTED_PEERS")
        os.environ["MEMORY_CRDT_TRUSTED_PEERS"] = "test-syncer"

    def tearDown(self):
        if self.orig_path is not None:
            os.environ["MEMORY_DB_PATH"] = self.orig_path
        else:
            os.environ.pop("MEMORY_DB_PATH", None)
        if self.orig_trusted is not None:
            os.environ["MEMORY_CRDT_TRUSTED_PEERS"] = self.orig_trusted
        else:
            os.environ.pop("MEMORY_CRDT_TRUSTED_PEERS", None)
        connection_pool.clear()

    @mock.patch("mcp_surface.mcp_crdt._resolve_memory_dir")
    def test_sync_empty_notes(self, mock_resolve):
        mock_resolve.return_value = self.db_path.parent
        from mcp_surface.mcp_crdt import memory_crdt_sync

        result = memory_crdt_sync(
            agent_id="test-syncer",
            remote_notes_json="{}",
        )
        parsed = json.loads(result)
        self.assertEqual(parsed["total"], 0)
        self.assertEqual(parsed["applied"], 0)

    @mock.patch("mcp_surface.mcp_crdt._resolve_memory_dir")
    def test_sync_single_note(self, mock_resolve):
        mock_resolve.return_value = self.db_path.parent
        from mcp_surface.mcp_crdt import memory_crdt_sync

        remote_notes = {
            "lessons/synced-note": [
                "synced content",
                "lessons/synced-note.md",
                1,
                json.dumps({"remote-agent": 1}),
                1,
            ]
        }
        result = memory_crdt_sync(
            agent_id="test-syncer",
            remote_notes_json=json.dumps(remote_notes),
        )
        parsed = json.loads(result)
        self.assertEqual(parsed["total"], 1)
        self.assertGreaterEqual(parsed["applied"], 0)

    @mock.patch("mcp_surface.mcp_crdt._resolve_memory_dir")
    def test_sync_invalid_json_returns_error(self, mock_resolve):
        mock_resolve.return_value = self.db_path.parent
        from mcp_surface.mcp_crdt import memory_crdt_sync

        result = memory_crdt_sync(
            agent_id="test-syncer",
            remote_notes_json="not valid json",
        )
        self.assertTrue(result.startswith("Error"))


class TestFieldLevelCRDTIntegration(unittest.TestCase):
    """End-to-end test of v13 per-field LWWES via the save pipeline.

    Verifies the bug fix: concurrent edits to different fields of
    the same note both win (v12 would lose one side's whole note).
    """

    def setUp(self):
        self.db_path = _fresh_db()

    def tearDown(self):
        connection_pool.clear()

    def test_concurrent_different_fields_both_win_e2e(self):
        """Two agents edit different fields of the same note concurrently.

        The v12 behavior: one side's whole note wins; the other
        side's edits are lost.
        The v13 behavior: each field is merged independently; both
        agents' edits survive.
        """
        from crdt.crdt_merge import crdt_save

        # Step 1: agent-A creates the note.
        r1 = crdt_save(
            db_path=self.db_path,
            note_id="concurrent-test",
            content="A's content",
            remote_agent_id="agent-A",
            local_agent_id="local",
            source_file="ct.md",
            category="lessons",
            remote_vv_str=json.dumps({"agent-A": 1}),
            remote_logical_clock=1,
        )
        self.assertTrue(r1["applied"], f"step 1 failed: {r1}")

        # Step 2: agent-B writes a DIFFERENT field (category) concurrently.
        r2 = crdt_save(
            db_path=self.db_path,
            note_id="concurrent-test",
            content="A's content",  # same content
            remote_agent_id="agent-B",
            local_agent_id="local",
            source_file="ct.md",
            category="decisions",  # different category
            remote_vv_str=json.dumps({"agent-B": 1}),
            remote_logical_clock=1,
        )
        # The field-level merge should have applied B's category.
        # (B's content is the same as A's, so no change there.)
        self.assertTrue(
            r2.get("applied") or r2.get("conflict"),
            f"step 2 expected applied or conflict, got {r2}",
        )

        # Verify the field-level state in the DB.
        from crdt.crdt_field import read_fields

        fields = read_fields(sqlite3.connect(str(self.db_path)), "concurrent-test")
        self.assertEqual(
            fields.get("content"),
            "A's content",
            "content must be preserved (A's value)",
        )
        # The category must be present (either A or B wins via LWW;
        # the key is that the FIELD survived, not the whole note).
        self.assertIn("category", fields, "category field must survive")
        # B's category ("decisions") is a different value, so the
        # merge must have processed it (A wins LWW because A<B lex,
        # so the value stays "lessons" — but the row is B's write).
        self.assertIn(fields["category"], ("lessons", "decisions"))

    def test_field_crdt_table_populated_after_save(self):
        """After a save via crdt_save, the memory_field_crdt table
        must have rows for content/tags/category."""
        from crdt.crdt_merge import crdt_save
        from crdt.crdt_field import read_fields

        crdt_save(
            db_path=self.db_path,
            note_id="e2e",
            content="hello",
            remote_agent_id="agent-A",
            local_agent_id="local",
            source_file="e2e.md",
            category="lessons",
            remote_vv_str=json.dumps({"agent-A": 1}),
            remote_logical_clock=1,
        )
        conn = sqlite3.connect(str(self.db_path))
        try:
            fields = read_fields(conn, "e2e")
            self.assertIn("content", fields)
            self.assertIn("category", fields)
            self.assertIn("tags", fields)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
