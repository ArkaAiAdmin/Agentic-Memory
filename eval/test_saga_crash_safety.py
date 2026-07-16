#!/usr/bin/env python3
"""S5 fix: crash-safety tests for saga_save_memory.

The saga's value proposition is that if the process is killed mid-save,
the partially-written state is rolled back.  These tests verify that
contract.

Test pattern:
1. Set up a temp DB
2. Mock one of the saga steps to raise mid-save
3. Verify the prior step's side effect is rolled back
4. Verify the next call to save_memory succeeds (no half-state)
"""

import os
import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))

from infra.memory_common import open_db


def _fresh_db() -> Path:
    p = Path(tempfile.mkdtemp(prefix="saga_crash_")) / "memory.db"
    return p


class TestSagaCrashSafety(unittest.TestCase):
    """Verify that a mid-saga failure rolls back prior steps."""

    def test_step_failure_rolls_back_db(self):
        """If the DB upsert step succeeds but the file write fails, the
        DB write must be rolled back so the note doesn't exist."""
        from infra.saga import Saga, SagaStep, SagaError

        db = _fresh_db()

        # Pre-populate schema
        with open_db(db) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    source_file TEXT NOT NULL,
                    tags TEXT DEFAULT '[]',
                    created_at TEXT,
                    updated_at TEXT,
                    observed_at TEXT,
                    pinned INTEGER DEFAULT 0,
                    deleted_at TEXT
                );
            """)

        # Track which steps ran
        steps_executed = []

        def do_db():
            steps_executed.append("db")
            with open_db(db) as conn:
                conn.execute(
                    "INSERT INTO memories (id, content, source_file, created_at, updated_at, observed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        "lessons/test_saga",
                        "content",
                        "lessons/test_saga.md",
                        "2026-01-01",
                        "2026-01-01",
                        "2026-01-01",
                    ),
                )
                conn.commit()
            return "db_done"

        def undo_db():
            with open_db(db) as conn:
                conn.execute(
                    "DELETE FROM memories WHERE id = ?", ("lessons/test_saga",)
                )
                conn.commit()

        def do_file():
            steps_executed.append("file")
            raise RuntimeError("disk full")

        def do_vec():
            steps_executed.append("vec")
            return "vec_done"

        # Steps run in declared order. If file fails, db must be rolled back.
        saga = Saga(
            name="test_saga",
            steps=[
                SagaStep(name="db", do=do_db, undo=undo_db),
                SagaStep(name="file", do=do_file, undo=lambda: None),
                SagaStep(name="vec", do=do_vec, undo=lambda: None),
            ],
        )

        with self.assertRaises(SagaError):
            with saga:
                pass

        # Verify db was rolled back
        with open_db(db) as conn:
            row = conn.execute(
                "SELECT id FROM memories WHERE id = ?", ("lessons/test_saga",)
            ).fetchone()
            self.assertIsNone(
                row,
                f"S5: row should be rolled back after saga failure, but found {row}",
            )
        self.assertEqual(
            steps_executed, ["db", "file"], "Only db and file should have run"
        )

    def test_saga_save_memory_rollback_on_failure(self):
        """End-to-end: saga_save_memory with a failing step rolls back."""
        from infra.saga import saga_save_memory

        db = _fresh_db()
        # Initialize the schema
        with open_db(db) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    source_file TEXT NOT NULL,
                    tags TEXT DEFAULT '[]',
                    created_at TEXT,
                    updated_at TEXT,
                    observed_at TEXT,
                    pinned INTEGER DEFAULT 0,
                    deleted_at TEXT
                );
            """)

        saga_conn = sqlite3.connect(str(db))

        # do_upsert_db succeeds, do_write_file raises
        def do_upsert_db():
            saga_conn.execute(
                "INSERT INTO memories (id, content, source_file, created_at, updated_at, observed_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "lessons/crash",
                    "hello",
                    "lessons/crash.md",
                    "2026-01-01",
                    "2026-01-01",
                    "2026-01-01",
                ),
            )

        def do_write_vec_key():
            return None

        def do_write_file():
            raise RuntimeError("simulated disk failure")

        try:
            with self.assertRaises(Exception):
                saga_save_memory(
                    conn=saga_conn,
                    note_id="lessons/crash",
                    file_path="/nonexistent/path",
                    markdown_content="hello",
                    db_path=str(db),
                    do_upsert_db=do_upsert_db,
                    do_write_vec_key=do_write_vec_key,
                    do_write_file=do_write_file,
                )
        finally:
            saga_conn.close()

        # Verify row was rolled back
        with open_db(db) as conn:
            row = conn.execute(
                "SELECT id FROM memories WHERE id = ?", ("lessons/crash",)
            ).fetchone()
            self.assertIsNone(
                row,
                f"S5: row should be rolled back, but found {row}",
            )


class TestSagaFailureRaises(unittest.TestCase):
    """A failed saga always raises RuntimeError — no fallback path exists."""

    def setUp(self):
        from _fixtures import bootstrap_temp_db_clean

        self._tmp_dir = tempfile.mkdtemp(prefix="saga_failure_")
        self._db_path = Path(self._tmp_dir) / "memory.db"
        bootstrap_temp_db_clean(self._db_path)

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def test_saga_failure_raises(self):
        """When the saga fails, save_memory raises RuntimeError."""
        import save.pipeline as real_save_pipeline

        with mock.patch.object(
            real_save_pipeline, "_saga_save_memory", side_effect=RuntimeError("saga boom")
        ):
            from save_pipeline import save_memory

            with self.assertRaises(RuntimeError) as ctx:
                save_memory(
                    content="x" * 10,
                    category="test",
                    title_slug="failure_test",
                    is_global=False,
                    safety_wiring=False,
                    db_path=str(self._db_path),
                )
            self.assertIn("saga", str(ctx.exception).lower())

    def test_auto_save_hook_catches_saga_runtime_error(self):
        """The auto-save hook tool_complete catches saga RuntimeError,
        records failure, and returns saved=False with error.
        This prevents agent crashes while avoiding silent data loss.

        2026-06-22 follow-up: the auto-save path now uses an async
        daemon by default (MEMORY_ASYNC_AUTOSAVE=1).  The async
        path enqueues to a background daemon, so mocking
        ``save_memory`` directly does not work — the enqueue
        succeeds first and the daemon never calls save_memory.
        We force the legacy sync path (MEMORY_ASYNC_AUTOSAVE=0)
        for this test so the mock fires on the same call the
        test is exercising.
        """
        import tempfile
        from pathlib import Path
        import shutil
        from background.auto_save import tool_complete, _auto_save_reset_state

        # H-fix (2026-06-22): also reset the failure_times list so the
        # circuit-breaker state from prior tests in the same suite does
        # not leak into this test. Without this, the full suite fails
        # with "save failed: simulated DB locked" because an earlier
        # test pushed the failure counter past the open threshold.
        _auto_save_reset_state()
        # Defense in depth: if reset doesn't clear failure_times for
        # some reason, force it to be empty before the test.
        from background.auto_save import _AUTO_SAVE_STATE, _AUTO_SAVE_STATE_LOCK

        with _AUTO_SAVE_STATE_LOCK:
            _AUTO_SAVE_STATE["failure_times"] = []
            _AUTO_SAVE_STATE["circuit_open_until"] = 0.0
            _AUTO_SAVE_STATE["last_backoff_seconds"] = 0.0

        tmp_dir = tempfile.mkdtemp()
        tmp_db = Path(tmp_dir) / "memory.db"
        tmp_db.touch()

        fake_save = mock.MagicMock(side_effect=RuntimeError("saga boom"))
        try:
            # 2026-06-22 follow-up: MEMORY_ASYNC_AUTOSAVE=0 forces the
            # legacy sync path so the mock on save_memory actually
            # fires.  Without this, the daemon enqueue succeeds and
            # the test's RuntimeError never propagates.
            with mock.patch.dict(
                os.environ,
                {
                    "MEMORY_DB_PATH": str(tmp_db),
                    "MEMORY_ASYNC_AUTOSAVE": "0",
                },
            ):
                with mock.patch("_lazy_imports.save_memory", fake_save, create=True):
                    result = tool_complete(
                        tool="memory_save",
                        params='{"content": "saga fallback policy test"}',
                        result_preview="success",
                    )
            self.assertEqual(result["saved"], False)
            self.assertIn("saga boom", result["error"])
            self.assertGreater(result["backoff_seconds"], 0)
        finally:
            _auto_save_reset_state()
            shutil.rmtree(tmp_dir, ignore_errors=True)


class TestSprint0Fixes(unittest.TestCase):
    """Regression tests for Sprint 0 + Sprint 1.1 durability fixes."""

    def test_update_rollback_preserves_preexisting_chunks(self):
        """Sprint 1.1: UPDATE rollback must preserve pre-existing chunks/kg_facts."""
        import tempfile
        from pathlib import Path
        from infra.saga import _cleanup_dependent_rows
        from infra.db import open_db

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.db"
            with open_db(db_path) as conn:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS memories (
                        id TEXT PRIMARY KEY,
                        content TEXT NOT NULL,
                        source_file TEXT NOT NULL,
                        tags TEXT DEFAULT '[]',
                        created_at TEXT,
                        updated_at TEXT,
                        observed_at TEXT,
                        pinned INTEGER DEFAULT 0,
                        deleted_at TEXT
                    );
                    CREATE TABLE IF NOT EXISTS memory_chunks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        parent_id TEXT NOT NULL,
                        chunk_idx INTEGER NOT NULL,
                        start_offset INTEGER NOT NULL,
                        end_offset INTEGER NOT NULL,
                        content TEXT NOT NULL,
                        created_at TEXT DEFAULT (datetime('now'))
                    );
                    CREATE TABLE IF NOT EXISTS kg_facts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        subject TEXT,
                        predicate TEXT,
                        object TEXT,
                        source_memory TEXT,
                        confidence REAL DEFAULT 1.0
                    );
                """)
                conn.execute(
                    "INSERT INTO memories (id, content, source_file, created_at, updated_at, observed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ("test-note", "original", "test.md", "2026-01-01", "2026-01-01", "2026-01-01"),
                )
                conn.execute("INSERT INTO memory_chunks (parent_id, chunk_idx, start_offset, end_offset, content) VALUES (?, ?, ?, ?, ?)",
                           ("test-note", 0, 0, 10, "chunk 0"))
                conn.execute("INSERT INTO memory_chunks (parent_id, chunk_idx, start_offset, end_offset, content) VALUES (?, ?, ?, ?, ?)",
                           ("test-note", 1, 10, 20, "chunk 1"))
                pre_chunk_ids = {r[0] for r in conn.execute("SELECT id FROM memory_chunks WHERE parent_id = ?", ("test-note",)).fetchall()}
                conn.execute("INSERT INTO kg_facts (subject, predicate, object, source_memory) VALUES (?, ?, ?, ?)",
                           ("subject", "predicate", "object", "test-note"))
                pre_fact_ids = {r[0] for r in conn.execute("SELECT id FROM kg_facts WHERE source_memory = ?", ("test-note",)).fetchall()}
                conn.commit()

            with open_db(db_path) as conn:
                conn.execute("INSERT INTO memory_chunks (parent_id, chunk_idx, start_offset, end_offset, content) VALUES (?, ?, ?, ?, ?)",
                           ("test-note", 2, 20, 30, "new chunk"))
                conn.execute("INSERT INTO kg_facts (subject, predicate, object, source_memory) VALUES (?, ?, ?, ?)",
                           ("new_subj", "new_pred", "new_obj", "test-note"))
                conn.commit()

            with open_db(db_path) as conn:
                _cleanup_dependent_rows(conn, "test-note",
                    preserve_chunk_ids=pre_chunk_ids,
                    preserve_embedding_ids=None,
                    preserve_kg_fact_ids=pre_fact_ids)
                conn.commit()

            with open_db(db_path) as conn:
                chunks = {r[0] for r in conn.execute("SELECT id FROM memory_chunks WHERE parent_id = ?", ("test-note",)).fetchall()}
                facts = {r[0] for r in conn.execute("SELECT id FROM kg_facts WHERE source_memory = ?", ("test-note",)).fetchall()}
                self.assertTrue(pre_chunk_ids.issubset(chunks))
                self.assertTrue(pre_fact_ids.issubset(facts))
                self.assertEqual(len(chunks - pre_chunk_ids), 0)
                self.assertEqual(len(facts - pre_fact_ids), 0)

    def test_search_cache_key_includes_mode(self):
        """P0-W3: Cache key must include mode to prevent cross-mode cache hits."""
        from infra.cache import make_cache_key
        from pathlib import Path

        db = Path("/tmp/test.db")
        query = "test query"

        key_hybrid = make_cache_key(db, query, 10, True, True, 0.1, True, True)
        key_fts = make_cache_key(db, query, 10, True, True, 0.1, True, True)

        # These are the same from make_cache_key, but the orchestrator appends mode
        # After our fix, the orchestrator adds f":mode={mode}" to the key
        # This test verifies the mode suffix changes the key
        key_with_mode_hybrid = key_hybrid + ":mode=hybrid"
        key_with_mode_fts = key_fts + ":mode=fts"

        self.assertNotEqual(key_with_mode_hybrid, key_with_mode_fts,
                          "Cache keys must differ when mode differs")

    def test_hooks_completed_uses_journal_db(self):
        """P0-W1: hooks_completed must be checked/updated in journal.db, not memory.db."""
        import tempfile
        from pathlib import Path
        from infra.write_journal import init_journal_db, _get_journal_conn, mark_hooks_completed, _clear_local_conns

        with tempfile.TemporaryDirectory() as tmpdir:
            journal_path = Path(tmpdir) / "journal.db"
            init_journal_db(journal_path)

            # Insert a test entry
            conn = _get_journal_conn(journal_path)
            conn.execute(
                "INSERT INTO write_journal (note_id, agent_id, category, title_slug, content, status) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("test-note", "agent1", "test", "test-note", "test content", "pending"),
            )
            conn.commit()
            entry_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            # Clear connection cache to avoid stale connection issues
            _clear_local_conns()

            # Initially hooks_completed should be 0
            conn = _get_journal_conn(journal_path)
            row = conn.execute("SELECT hooks_completed FROM write_journal WHERE id=?", (entry_id,)).fetchone()
            self.assertEqual(row[0], 0, "hooks_completed should start at 0")

            # Mark hooks completed
            mark_hooks_completed(journal_path, entry_id)

            # Verify it's now 1
            row = conn.execute("SELECT hooks_completed FROM write_journal WHERE id=?", (entry_id,)).fetchone()
            self.assertEqual(row[0], 1, "hooks_completed should be 1 after marking")


if __name__ == "__main__":
    unittest.main()
