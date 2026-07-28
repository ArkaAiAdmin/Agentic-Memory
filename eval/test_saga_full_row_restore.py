#!/usr/bin/env python3
"""Regression tests for Sprint 1.1 hardening of the save-memory saga.

Two gaps are locked here:

1. ``_restore_full_row`` must restore *every* column of a pre-existing
   memories row on UPDATE rollback — not just content/tags/tier/scores.
   The prior _restore_memory_row path left columns like ``supersedes``,
   ``version_vector``, ``logical_clock`` and ``importance`` inconsistent
   after a rolled-back UPDATE.

2. The non-proxy DEFERRED commit-failure path (infra/saga.py __exit__) must
   raise a well-formed ``SagaError`` (with saga_name/failed_step/
   original_error), not a ``TypeError`` from a malformed constructor call.
   A malformed raise would mask the real commit failure.
"""

import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path

INSTALL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(INSTALL_DIR))

from eval._fixtures import bootstrap_temp_db_clean


def _fresh_db() -> Path:
    p = Path(tempfile.mkdtemp(prefix="saga_full_row_")) / "memory.db"
    bootstrap_temp_db_clean(p)
    return p


class TestFullRowRestore(unittest.TestCase):
    """UPDATE rollback restores the complete pre-save row."""

    def test_restore_full_row_restores_all_columns(self):
        from infra.saga import _restore_full_row, _capture_full_row

        db = _fresh_db()
        note_id = "lessons/full_row"
        with sqlite3.connect(str(db)) as conn:
            conn.execute(
                "INSERT INTO memories "
                "(id, content, source_file, tags, created_at, updated_at, observed_at, "
                " importance, score, supersedes, version_vector, logical_clock) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    note_id,
                    "original content",
                    "lessons/full_row.md",
                    "['a','b']",
                    "2026-01-01",
                    "2026-01-01",
                    "2026-01-01",
                    2,
                    0.5,
                    "old-supersedes",
                    '{"x":1}',
                    7,
                ),
            )
            conn.commit()

            # Capture the pre-image, then mutate every column.
            snapshot = _capture_full_row(conn, note_id)
            assert snapshot is not None, "pre-image must be captured for an existing row"
            conn.execute(
                "UPDATE memories SET content=?, importance=?, score=?, "
                "supersedes=?, version_vector=?, logical_clock=? WHERE id=?",
                ("mutated", 5, 0.9, "new-supersedes", '{"y":2}', 99, note_id),
            )
            conn.commit()

            # Restore from the snapshot — should bring back every column.
            _restore_full_row(conn, note_id, snapshot)
            conn.commit()

            row = conn.execute(
                "SELECT content, importance, score, supersedes, version_vector, logical_clock "
                "FROM memories WHERE id=?",
                (note_id,),
            ).fetchone()
            self.assertEqual(row[0], "original content")
            self.assertEqual(row[1], 2)
            self.assertEqual(row[2], 0.5)
            self.assertEqual(row[3], "old-supersedes")
            self.assertEqual(row[4], '{"x":1}')
            self.assertEqual(row[5], 7)

    def test_saga_update_rollback_uses_full_row(self):
        """End-to-end: a failing UPDATE save restores the full pre-image."""
        from infra.saga import saga_save_memory

        db = _fresh_db()
        note_id = "lessons/rollback_e2e"
        with sqlite3.connect(str(db)) as conn:
            conn.execute(
                "INSERT INTO memories "
                "(id, content, source_file, tags, created_at, updated_at, observed_at, "
                " importance, supersedes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    note_id,
                    "before",
                    "lessons/rollback_e2e.md",
                    "['keep']",
                    "2026-01-01",
                    "2026-01-01",
                    "2026-01-01",
                    3,
                    "prev-supersedes",
                ),
            )
            conn.commit()

        saga_conn = sqlite3.connect(str(db))

        def do_upsert_db():
            saga_conn.execute(
                "UPDATE memories SET content=?, importance=?, supersedes=? WHERE id=?",
                ("after", 1, "injected-supersedes", note_id),
            )

        def do_write_vec_key():
            # Simulate a downstream failure after the DB row was mutated.
            # Phase 3B moved file-write to a non-fatal post-commit hook,
            # so we fail in vec_key step to test the UPDATE rollback path.
            raise RuntimeError("downstream failure")

        def do_write_file():
            pass

        with self.assertRaises(Exception):
            saga_save_memory(
                conn=saga_conn,
                note_id=note_id,
                file_path="/nonexistent/path",
                markdown_content="after",
                db_path=str(db),
                do_upsert_db=do_upsert_db,
                do_write_vec_key=do_write_vec_key,
                do_write_file=do_write_file,
            )
        saga_conn.close()

        # The rolled-back UPDATE must restore content AND supersedes AND importance.
        with sqlite3.connect(str(db)) as conn:
            row = conn.execute(
                "SELECT content, importance, supersedes FROM memories WHERE id=?",
                (note_id,),
            ).fetchone()
            self.assertEqual(row[0], "before", "content must be restored")
            self.assertEqual(row[1], 3, "importance must be restored")
            self.assertEqual(row[2], "prev-supersedes", "supersedes must be restored")


class TestSagaErrorCommitFailure(unittest.TestCase):
    """The commit-failure path must raise a well-formed SagaError."""

    def test_commit_failure_raises_saga_error_not_type_error(self):
        from infra.saga import Saga, SagaStep, SagaError, SagaMode

        db = _fresh_db()
        with sqlite3.connect(str(db)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS t (id TEXT PRIMARY KEY, v TEXT)"
            )
            conn.commit()

        # Use a real DB connection so the non-proxy DEFERRED path runs,
        # then wrap it so commit() raises to simulate a commit failure.
        # (sqlite3.Connection.commit is read-only and can't be mocked
        # directly, so we delegate via a thin wrapper.)
        real_conn = sqlite3.connect(str(db))
        captured = {"commit_called": False}

        class _FailingCommitConn:
            def __getattr__(self, name):
                return getattr(real_conn, name)

            def commit(self):
                captured["commit_called"] = True
                raise sqlite3.OperationalError("disk write failed at commit")

        def do_step():
            real_conn.execute("INSERT INTO t (id, v) VALUES (?, ?)", ("k", "v"))
            return "ok"

        saga = Saga(
            name="commit_test",
            steps=[SagaStep(name="step1", do=do_step, undo=lambda: None)],
            conn=_FailingCommitConn(),
            mode=SagaMode.DEFERRED,
        )
        # The __exit__ commit failure must surface as SagaError, not TypeError.
        with self.assertRaises(SagaError) as ctx:
            with saga:
                pass
        self.assertTrue(captured["commit_called"])
        # The exception must be a well-formed SagaError (not a TypeError
        # from a malformed constructor call), carrying the commit failure.
        self.assertIsInstance(ctx.exception, SagaError)
        self.assertEqual(ctx.exception.saga_name, "commit_test")
        self.assertEqual(ctx.exception.failed_step, "<commit>")
        self.assertIsInstance(ctx.exception.original_error, sqlite3.OperationalError)
        self.assertIn("commit", str(ctx.exception.original_error).lower())
        real_conn.close()


class TestTenantScopedUndo(unittest.TestCase):
    """Undo SQL must filter by tenant_id to avoid cross-tenant leaks."""

    def test_restore_full_row_does_not_touch_other_tenant(self):
        from infra.saga import _restore_full_row, _capture_full_row

        db = _fresh_db()
        with sqlite3.connect(str(db)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS memories ("
                "  id TEXT PRIMARY KEY, content TEXT, tenant_id TEXT DEFAULT 'default')"
            )
            conn.execute(
                "INSERT INTO memories (id, content, tenant_id) VALUES (?, ?, ?)",
                ("test/note_a", "tenant-a content", "tenant-a"),
            )
            conn.execute(
                "INSERT INTO memories (id, content, tenant_id) VALUES (?, ?, ?)",
                ("test/note_b", "tenant-b content", "tenant-b"),
            )
            conn.commit()

            snapshot_a = _capture_full_row(conn, "test/note_a")
            assert snapshot_a is not None
            # Mutate tenant-b's row using wrong-tenant WHERE...
            conn.execute(
                "UPDATE memories SET content=? WHERE id=? AND tenant_id=?",
                ("mutated", "test/note_b", "tenant-b"),
            )
            conn.commit()

            # Undo with tenant_id="tenant-a" must NOT touch tenant-b's row.
            _restore_full_row(conn, "test/note_a", snapshot_a, tenant_id="tenant-a")
            conn.commit()

            row_a = conn.execute(
                "SELECT content FROM memories WHERE id=? AND tenant_id=?",
                ("test/note_a", "tenant-a"),
            ).fetchone()
            row_b = conn.execute(
                "SELECT content FROM memories WHERE id=? AND tenant_id=?",
                ("test/note_b", "tenant-b"),
            ).fetchone()
            self.assertEqual(row_a[0], "tenant-a content")
            self.assertEqual(row_b[0], "mutated")

    def test_delete_memory_row_does_not_touch_other_tenant(self):
        from infra.saga import _delete_memory_row

        db = _fresh_db()
        with sqlite3.connect(str(db)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS memories ("
                "  id TEXT PRIMARY KEY, content TEXT, tenant_id TEXT DEFAULT 'default')"
            )
            conn.execute(
                "INSERT INTO memories (id, content, tenant_id) VALUES (?, ?, ?)",
                ("undotest/a", "keep-tenant-a", "tenant-a"),
            )
            conn.execute(
                "INSERT INTO memories (id, content, tenant_id) VALUES (?, ?, ?)",
                ("undotest/b", "keep-tenant-b", "tenant-b"),
            )
            conn.commit()

            # Delete by wrong-tenant id must NOT affect the other tenant
            _delete_memory_row(conn, "undotest/a", tenant_id="tenant-b")
            conn.commit()

            row_a = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE id=? AND tenant_id=?",
                ("undotest/a", "tenant-a"),
            ).fetchone()[0]
            row_b = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE id=? AND tenant_id=?",
                ("undotest/b", "tenant-b"),
            ).fetchone()[0]
            self.assertEqual(row_a, 1, "tenant-a row must be preserved")
            self.assertEqual(row_b, 1, "tenant-b row must be preserved")


class TestRollbackAuditLog(unittest.TestCase):
    """Saga._rollback must persist a record when undos fail."""

    def test_rollback_errors_are_written_to_audit_log(self):
        from infra.saga import Saga, SagaStep

        db = _fresh_db()
        with sqlite3.connect(str(db)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS t (id TEXT PRIMARY KEY)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS saga_audit_log "
                "(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, "
                " saga_name TEXT, failed_step TEXT, original_error TEXT, "
                " rollback_count INTEGER, rollback_errors TEXT)"
            )
            conn.commit()

        real_conn = sqlite3.connect(str(db))

        class _ProxyConn:
            def __getattr__(self, name):
                return getattr(real_conn, name)

        conn = _ProxyConn()

        def do_step():
            conn.execute("INSERT INTO t (id) VALUES (?)", ("ok",))

        def undo_step():
            raise RuntimeError("undo exploded")

        saga = Saga(
            name="audit_test",
            steps=[SagaStep(name="step1", do=do_step, undo=undo_step)],
            conn=conn,
        )
        # Force a step failure so __exit__ triggers rollback.
        with self.assertRaises(Exception):
            with saga:
                raise RuntimeError("injected step failure")

        # saga_audit_log must have one row documenting the partial rollback.
        row = conn.execute(
            "SELECT saga_name, rollback_count, rollback_errors FROM saga_audit_log"
        ).fetchone()
        self.assertIsNotNone(row, "saga_audit_log must record the failed rollback")
        self.assertEqual(row[0], "audit_test")
        self.assertGreaterEqual(row[1], 1, "at least one undo error must be recorded")
        self.assertIn("undo exploded", row[2])
        real_conn.close()


if __name__ == "__main__":
    unittest.main()
