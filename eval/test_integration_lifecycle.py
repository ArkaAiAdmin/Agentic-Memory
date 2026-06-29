#!/usr/bin/env python3
"""Integration tests for the full note lifecycle.

Covers: save -> search -> soft delete -> restore -> hard delete -> purge expired,
plus FK cascade, rebuild_index coexistence, and edge cases.

Run with:
    ~/.config/agentic-memory/venv/bin/python -m unittest eval.test_integration_lifecycle -v
"""


import sys
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

from memory_common import open_db, connection_pool, safe_close_db
import memory_delete


def _bootstrap_db(db_path: Path) -> None:
    """Create a fully-migrated test DB via connection_pool.get()."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connection_pool.get(str(db_path))
    safe_close_db(conn)


def _insert_note(
    db_path: Path,
    note_id: str,
    content: str = "hello world",
    source_file: str = "lessons/test.md",
    tags: str = "[]",
    created_at: str | None = None,
) -> None:
    if created_at is None:
        created_at = datetime.now(timezone.utc).isoformat()
    now = datetime.now(timezone.utc).isoformat()
    with open_db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO memories
                (id, content, source_file, tags, created_at, updated_at,
                 observed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (note_id, content, source_file, tags, created_at, now, now),
        )
        conn.commit()


def _insert_note_with_extras(
    db_path: Path,
    note_id: str,
    content: str = "hello world",
    source_file: str = "lessons/test.md",
    tags: str = "[]",
) -> None:
    """Insert a note AND populate ancillary tables (backlinks, chunks, embeddings, kg)."""
    _insert_note(db_path, note_id, content=content, source_file=source_file, tags=tags)
    with open_db(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO backlinks (source_id, target_id) VALUES (?, ?)",
            (note_id, "lessons/some-other"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO memory_chunks (parent_id, chunk_idx, start_offset, end_offset, content) "
            "VALUES (?, 0, 0, ?, ?)",
            (note_id, len(content), content),
        )
        conn.execute(
            "INSERT OR IGNORE INTO memory_embeddings (memory_id, content_hash, embedding, model_revision, dim, updated_at) "
            "VALUES (?, 'abc123', x'0001', 'test', 2, 0)",
            (note_id,),
        )
        has_keys = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_vec_keys'"
        ).fetchone()
        if has_keys:
            conn.execute(
                "INSERT OR IGNORE INTO memory_vec_keys (key, memory_id) VALUES (?, ?)",
                (hash(note_id) % (2**63), note_id),
            )
        conn.commit()


def _backdate_deleted_at(db_path: Path, note_id: str, days_ago: float) -> None:
    target = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    with open_db(db_path) as conn:
        conn.execute(
            "UPDATE memories SET deleted_at = ? WHERE id = ?",
            (target, note_id),
        )
        conn.commit()


def _backdate_created_at(db_path: Path, note_id: str, days_ago: float) -> None:
    target = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    with open_db(db_path) as conn:
        conn.execute(
            "UPDATE memories SET created_at = ? WHERE id = ?",
            (target, note_id),
        )
        conn.commit()


def _count_rows(db_path: Path, table: str) -> int:
    with open_db(db_path) as conn:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return row[0] if row else -1


def _row_exists(db_path: Path, table: str, column: str, value: str) -> bool:
    with open_db(db_path) as conn:
        row = conn.execute(
            f"SELECT 1 FROM {table} WHERE {column} = ?", (value,)
        ).fetchone()
        return row is not None


class TestIntegrationLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="lifecycle_test_")
        self.db_path = Path(self.tmpdir) / "memory.db"
        _bootstrap_db(self.db_path)

    def tearDown(self):
        try:
            connection_pool.close(str(self.db_path))
        except Exception:
            pass
        try:
            connection_pool.clear()
        except Exception:
            pass
        try:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Test 1: Full lifecycle
    # ------------------------------------------------------------------
    def test_full_lifecycle(self):
        nid = "lessons/full-lifecycle"
        _insert_note(self.db_path, nid, content="unique content for lifecycle")

        with open_db(self.db_path) as conn:
            row = conn.execute(
                "SELECT content FROM memories WHERE id = ?", (nid,)
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertIn("unique content", row[0])

        memory_delete.ensure_deleted_at_column(self.db_path)
        ok = memory_delete.soft_delete_note(self.db_path, nid)
        self.assertTrue(ok)
        self.assertTrue(memory_delete.is_soft_deleted(self.db_path, nid))

        ok = memory_delete.restore_note(self.db_path, nid)
        self.assertTrue(ok)
        self.assertFalse(memory_delete.is_soft_deleted(self.db_path, nid))

        memory_delete.soft_delete_note(self.db_path, nid)
        ok = memory_delete.hard_delete_note(self.db_path, nid)
        self.assertTrue(ok)

        with open_db(self.db_path) as conn:
            row = conn.execute(
                "SELECT id FROM memories WHERE id = ?", (nid,)
            ).fetchone()
            self.assertIsNone(row)

    # ------------------------------------------------------------------
    # Test 2: FK cascade on hard_delete
    # ------------------------------------------------------------------
    def test_hard_delete_fk_cascade(self):
        nid = "lessons/fk-cascade"
        _insert_note_with_extras(self.db_path, nid)

        memory_delete.ensure_deleted_at_column(self.db_path)
        memory_delete.soft_delete_note(self.db_path, nid)
        ok = memory_delete.hard_delete_note(self.db_path, nid)
        self.assertTrue(ok)

        self.assertEqual(
            _count_rows(self.db_path, "memories"),
            0,
            "memories table should be empty",
        )
        self.assertEqual(
            _count_rows(self.db_path, "backlinks"),
            0,
            "backlinks should be cleaned up",
        )
        self.assertEqual(
            _count_rows(self.db_path, "memory_chunks"),
            0,
            "memory_chunks should be cascade-deleted",
        )
        self.assertEqual(
            _count_rows(self.db_path, "memory_embeddings"),
            0,
            "memory_embeddings should be cascade-deleted",
        )

        has_vec = _count_rows(self.db_path, "memory_vec_keys") >= 0
        if has_vec:
            self.assertEqual(
                _count_rows(self.db_path, "memory_vec_keys"),
                0,
                "memory_vec_keys should be cascade-deleted",
            )

    # ------------------------------------------------------------------
    # Test 3: FK cascade on purge_expired
    # ------------------------------------------------------------------
    def test_purge_expired_fk_cascade(self):
        nid = "lessons/purge-fk"
        _insert_note_with_extras(self.db_path, nid)

        memory_delete.ensure_deleted_at_column(self.db_path)
        memory_delete.soft_delete_note(self.db_path, nid)
        _backdate_deleted_at(self.db_path, nid, days_ago=35)

        purged = memory_delete.purge_expired(self.db_path)
        self.assertEqual(purged, 1)

        self.assertEqual(_count_rows(self.db_path, "memories"), 0, "all memories gone")
        self.assertEqual(_count_rows(self.db_path, "backlinks"), 0, "backlinks cleaned")
        self.assertEqual(
            _count_rows(self.db_path, "memory_chunks"),
            0,
            "memory_chunks cleaned",
        )
        self.assertEqual(
            _count_rows(self.db_path, "memory_embeddings"),
            0,
            "memory_embeddings cleaned",
        )

    # ------------------------------------------------------------------
    # Test 4: Soft delete preserves data
    # ------------------------------------------------------------------
    def test_soft_delete_preserves_data(self):
        nid = "lessons/preserve"
        content = "preserved content"
        _insert_note(self.db_path, nid, content=content)

        memory_delete.ensure_deleted_at_column(self.db_path)
        memory_delete.soft_delete_note(self.db_path, nid)

        with open_db(self.db_path) as conn:
            row = conn.execute(
                "SELECT id, content, source_file FROM memories WHERE id = ?",
                (nid,),
            ).fetchone()
            self.assertIsNotNone(row, "row should still exist")
            self.assertEqual(row[0], nid)
            self.assertEqual(row[1], content)

    # ------------------------------------------------------------------
    # Test 5: Hard delete raises on active note <30 days old
    # ------------------------------------------------------------------
    def test_hard_delete_active_young_raises(self):
        nid = "lessons/young-active"
        _insert_note(self.db_path, nid)
        memory_delete.ensure_deleted_at_column(self.db_path)
        with self.assertRaises(ValueError):
            memory_delete.hard_delete_note(self.db_path, nid)

        self.assertTrue(_row_exists(self.db_path, "memories", "id", nid))

    # ------------------------------------------------------------------
    # Test 6: purge_expired removes old soft-deleted notes only
    # ------------------------------------------------------------------
    def test_purge_expired_only_removes_old(self):
        old_id = "lessons/old-note"
        fresh_id = "lessons/fresh-note"
        _insert_note(self.db_path, old_id, content="old")
        _insert_note(self.db_path, fresh_id, content="fresh")

        memory_delete.ensure_deleted_at_column(self.db_path)
        memory_delete.soft_delete_note(self.db_path, old_id)
        memory_delete.soft_delete_note(self.db_path, fresh_id)

        _backdate_deleted_at(self.db_path, old_id, days_ago=35)
        _backdate_deleted_at(self.db_path, fresh_id, days_ago=1)

        purged = memory_delete.purge_expired(self.db_path)
        self.assertEqual(purged, 1)

        self.assertFalse(
            _row_exists(self.db_path, "memories", "id", old_id),
            "old note should be gone",
        )
        self.assertTrue(
            _row_exists(self.db_path, "memories", "id", fresh_id),
            "fresh note should remain",
        )

    # ------------------------------------------------------------------
    # Test 7: rebuild_index preserves data after lifecycle
    # ------------------------------------------------------------------
    def test_rebuild_index_preserves_data(self):
        from rebuild_index import rebuild_index

        mem_dir = Path(self.tmpdir) / "memory"
        mem_dir.mkdir(parents=True, exist_ok=True)

        note_path = mem_dir / "lessons" / "rebuild-preserve.md"
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(
            "---\ncreated: 2026-01-01\ntags: [test]\n---\n\ncontent preserved"
        )

        db_path = mem_dir / "memory.db"

        connection_pool.close(str(db_path))
        rebuild_index(mem_dir, db_path)

        with open_db(db_path) as conn:
            row = conn.execute(
                "SELECT id FROM memories WHERE id = 'lessons/rebuild-preserve'"
            ).fetchone()
            self.assertIsNotNone(row, "note should exist after rebuild")

        memory_delete.ensure_deleted_at_column(db_path)
        memory_delete.soft_delete_note(db_path, "lessons/rebuild-preserve")
        memory_delete.restore_note(db_path, "lessons/rebuild-preserve")

        connection_pool.close(str(db_path))
        rebuild_index(mem_dir, db_path)

        with open_db(db_path) as conn:
            row = conn.execute(
                "SELECT content FROM memories WHERE id = 'lessons/rebuild-preserve'"
            ).fetchone()
            self.assertIsNotNone(row, "note should survive rebuild")
            self.assertIn("content preserved", row[0])

    # ------------------------------------------------------------------
    # Test 8: rebuild_index after hard delete cleans up
    # ------------------------------------------------------------------
    def test_rebuild_index_after_delete(self):
        from rebuild_index import rebuild_index

        mem_dir = Path(self.tmpdir) / "memory-rebuild"
        mem_dir.mkdir(parents=True, exist_ok=True)

        notes_data = {
            "lessons/kept": "keep this content",
            "lessons/removed": "remove this content",
        }
        for nid, content in notes_data.items():
            rel = nid + ".md"
            fpath = mem_dir / rel
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text(
                f"---\ncreated: 2026-01-01\ntags: [test]\n---\n\n{content}"
            )

        db_path = mem_dir / "memory.db"

        connection_pool.close(str(db_path))
        rebuild_index(mem_dir, db_path)

        for nid in notes_data:
            self.assertTrue(
                _row_exists(db_path, "memories", "id", nid),
                f"{nid} should exist after bootstrap",
            )

        memory_delete.ensure_deleted_at_column(db_path)

        del_path = mem_dir / "lessons" / "removed.md"
        del_path.unlink()

        connection_pool.close(str(db_path))
        rebuild_index(mem_dir, db_path)

        self.assertTrue(
            _row_exists(db_path, "memories", "id", "lessons/kept"),
            "kept note should survive rebuild",
        )
        self.assertFalse(
            _row_exists(db_path, "memories", "id", "lessons/removed"),
            "removed note should be absent after rebuild",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
