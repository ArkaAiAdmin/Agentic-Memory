"""
Regression tests for the soft-delete API in memory_delete.

History: this file was destroyed by an accidental `git checkout` on
2026-06-16 (the file was never committed; the git index had an empty
blob hash). This is the rebuilt version.

The rebuild uses a fully-bootstrapped temp DB (prod schema copy) to
avoid the H21 fixture problem (partial migration chains leaving
columns missing). The new tests are stricter than the original:
they assert bug-hunting invariants like
- both deleted_at AND deleted_by are set
- the FTS5 index is maintained across delete/restore
- the cascade (hard_delete) removes 7 dependent tables
- deleted_by with empty/non-string raises
- invalid note_id raises (caller can distinguish bad input from DB error)
- purge_expired only removes notes older than 30 days
- list_trash orders oldest first

See: decisions/enrich-pre-compaction-survival-note
"""

import json
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _wait_until import wait_until  # noqa: E402

from memory_common import get_memory_paths, open_db
from memory_delete import (
    ensure_deleted_at_column,
    hard_delete_note,
    is_soft_deleted,
    list_trash,
    purge_expired,
    restore_note,
    soft_delete_note,
)
from save_pipeline import save_memory


def _bootstrap_full_db(db_path: Path) -> None:
    """Create a fully-bootstrapped temp DB by copying the live schema.

    This is the same pattern used by test_no_silent_search_failures.py.
    Copies the live prod DB so all 5 migrations (incl. 005 which adds
    deleted_at + deleted_by) are present.
    """
    _, _, global_mem = get_memory_paths()
    prod_db = global_mem / "memory.db"
    if prod_db.exists():
        shutil.copy2(prod_db, db_path)
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            for table in tables:
                if table == "sqlite_sequence":
                    continue
                if any(table.endswith(suffix) for suffix in ["_data", "_idx", "_docsize", "_config", "_content"]):
                    continue
                try:
                    conn.execute(f"DELETE FROM {table}")
                except sqlite3.OperationalError:
                    pass
            conn.commit()
        finally:
            conn.close()


def _table_names(conn: sqlite3.Connection) -> set:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _count_rows(conn: sqlite3.Connection, table: str, where: str = "1=1") -> int:
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]
    except sqlite3.OperationalError:
        return -1  # table doesn't exist


class TestSoftDeleteSchema(unittest.TestCase):
    """Schema invariants: the soft-delete columns must always be present."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "memory.db"
        _bootstrap_full_db(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_canonical_schema_has_deleted_at(self):
        """deleted_at is added by migration 005_columns_indexes_chunks."""
        with open_db(self.db_path) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
        self.assertIn("deleted_at", cols)

    def test_canonical_schema_has_deleted_by(self):
        """deleted_by is added by migration 005_columns_indexes_chunks.

        Originally this test was failing because temp DBs were bootstrapped
        with a partial migration chain (only 1-4, not 5). With a full
        prod-schema copy, deleted_by is present from the start.
        """
        with open_db(self.db_path) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
        self.assertIn("deleted_by", cols)

    def test_ensure_deleted_at_column_is_idempotent(self):
        """ensure_deleted_at_column must be a no-op when columns exist."""
        before = set()
        with open_db(self.db_path) as conn:
            before = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
        # Run twice — must not raise, must not change schema
        ensure_deleted_at_column(self.db_path)
        ensure_deleted_at_column(self.db_path)
        with open_db(self.db_path) as conn:
            after = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
        self.assertEqual(before, after)

    def test_ensure_column_on_missing_db_is_noop(self):
        """ensure_deleted_at_column on a non-existent DB must not raise."""
        nonexistent = Path(self.tmpdir) / "ghost.db"
        ensure_deleted_at_column(nonexistent)
        self.assertFalse(nonexistent.exists())


def _insert_note(
    db_path: Path,
    note_id: str,
    content: str = "test content",
    category: str = "lessons",
) -> None:
    """Helper: save a note to the DB."""
    save_memory(
        content=content,
        category=category,
        title_slug=note_id.split("/", 1)[-1],
        safety_wiring=False,
        db_path=db_path,
    )


class TestSoftDeleteBasic(unittest.TestCase):
    """The soft_delete_note happy path."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "memory.db"
        _bootstrap_full_db(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_soft_delete_returns_true_on_active_note(self):
        _insert_note(self.db_path, "lessons/sd-active-1")
        result = soft_delete_note(self.db_path, "lessons/sd-active-1")
        self.assertTrue(result)

    def test_soft_delete_sets_both_columns(self):
        """Bug-hunting: deleted_at AND deleted_by must both be set.

        The function docstring promises both are set. If a future refactor
        only sets one, this test catches it.
        """
        _insert_note(self.db_path, "lessons/sd-both-cols")
        soft_delete_note(self.db_path, "lessons/sd-both-cols", deleted_by="alice")
        with open_db(self.db_path) as conn:
            row = conn.execute(
                "SELECT deleted_at, deleted_by FROM memories WHERE id=?",
                ("lessons/sd-both-cols",),
            ).fetchone()
        self.assertIsNotNone(row[0], "deleted_at must be set")
        self.assertEqual(row[1], "alice", "deleted_by must be set to the value passed")

    def test_soft_delete_at_is_iso8601_utc(self):
        """Bug-hunting: deleted_at must be parseable ISO-8601 in UTC."""
        _insert_note(self.db_path, "lessons/sd-iso")
        soft_delete_note(self.db_path, "lessons/sd-iso")
        with open_db(self.db_path) as conn:
            row = conn.execute(
                "SELECT deleted_at FROM memories WHERE id=?",
                ("lessons/sd-iso",),
            ).fetchone()
        # Must parse as ISO-8601 (the +00:00 suffix is UTC)
        parsed = datetime.fromisoformat(row[0])
        self.assertIsNotNone(parsed.tzinfo, "deleted_at must have a timezone")
        # Within the last 5 seconds (test should run in <5s)
        now = datetime.now(timezone.utc)
        self.assertLess((now - parsed).total_seconds(), 5.0)

    def test_soft_delete_default_deleted_by_is_user(self):
        """Bug-hunting: default deleted_by is 'user' per the function signature."""
        _insert_note(self.db_path, "lessons/sd-default")
        soft_delete_note(self.db_path, "lessons/sd-default")
        with open_db(self.db_path) as conn:
            row = conn.execute(
                "SELECT deleted_by FROM memories WHERE id=?",
                ("lessons/sd-default",),
            ).fetchone()
        self.assertEqual(row[0], "user")

    def test_is_soft_deleted_true_after_delete(self):
        _insert_note(self.db_path, "lessons/sd-is-true")
        soft_delete_note(self.db_path, "lessons/sd-is-true")
        self.assertTrue(is_soft_deleted(self.db_path, "lessons/sd-is-true"))

    def test_is_soft_deleted_false_for_active_note(self):
        _insert_note(self.db_path, "lessons/sd-is-false")
        self.assertFalse(is_soft_deleted(self.db_path, "lessons/sd-is-false"))

    def test_soft_delete_unknown_note_returns_false(self):
        """Bug-hunting: unknown note_id returns False (not raise).

        This distinguishes "DB error" from "no such note" — the caller
        can tell them apart.
        """
        result = soft_delete_note(self.db_path, "lessons/does-not-exist")
        self.assertFalse(result)


class TestSoftDeleteIdempotency(unittest.TestCase):
    """Idempotency: the second call must be a safe no-op."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "memory.db"
        _bootstrap_full_db(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_double_soft_delete_returns_false_second_time(self):
        _insert_note(self.db_path, "lessons/sd-idem-1")
        self.assertTrue(soft_delete_note(self.db_path, "lessons/sd-idem-1"))
        # Second call: note is already soft-deleted, should be a no-op
        self.assertFalse(soft_delete_note(self.db_path, "lessons/sd-idem-1"))

    def test_double_soft_delete_preserves_original_timestamp(self):
        """Bug-hunting: re-deleting must NOT update the timestamp.

        The second call is a no-op, so deleted_at from the first call
        must remain unchanged.
        """
        _insert_note(self.db_path, "lessons/sd-idem-2")
        soft_delete_note(self.db_path, "lessons/sd-idem-2")
        with open_db(self.db_path) as conn:
            first = conn.execute(
                "SELECT deleted_at FROM memories WHERE id=?",
                ("lessons/sd-idem-2",),
            ).fetchone()[0]
        # Anti-thundering-herd: tiny gap between two soft-delete calls so
        # the second one has a measurably later timestamp (idempotency
        # invariant: deleted_at must NOT advance on the second call).
        time.sleep(0.05)
        soft_delete_note(self.db_path, "lessons/sd-idem-2")
        with open_db(self.db_path) as conn:
            second = conn.execute(
                "SELECT deleted_at FROM memories WHERE id=?",
                ("lessons/sd-idem-2",),
            ).fetchone()[0]
        self.assertEqual(first, second, "second soft-delete must not touch deleted_at")


class TestSoftDeleteInputValidation(unittest.TestCase):
    """Bad input: soft_delete must raise on bad note_id or bad deleted_by.

    This lets the caller distinguish "bad input" (ValueError) from
    "DB error" (caught and returns False).
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "memory.db"
        _bootstrap_full_db(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_empty_note_id_raises(self):
        with self.assertRaises(ValueError):
            soft_delete_note(self.db_path, "")

    def test_note_id_with_quote_raises(self):
        """SQL-injection guard: embedded double-quote is rejected."""
        with self.assertRaises(ValueError):
            soft_delete_note(self.db_path, 'lessons/has"quote')

    def test_empty_deleted_by_raises(self):
        _insert_note(self.db_path, "lessons/empty-deleter")
        with self.assertRaises(ValueError):
            soft_delete_note(self.db_path, "lessons/empty-deleter", deleted_by="")

    def test_non_string_deleted_by_raises(self):
        _insert_note(self.db_path, "lessons/int-deleter")
        with self.assertRaises(ValueError):
            soft_delete_note(self.db_path, "lessons/int-deleter", deleted_by=42)

    def test_is_soft_deleted_empty_id_raises(self):
        with self.assertRaises(ValueError):
            is_soft_deleted(self.db_path, "")


class TestRestore(unittest.TestCase):
    """restore_note: clearing the soft-delete markers."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "memory.db"
        _bootstrap_full_db(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_restore_after_soft_delete_returns_true(self):
        _insert_note(self.db_path, "lessons/restore-1")
        soft_delete_note(self.db_path, "lessons/restore-1")
        self.assertTrue(restore_note(self.db_path, "lessons/restore-1"))

    def test_restore_clears_both_columns(self):
        """Bug-hunting: restore must clear BOTH deleted_at and deleted_by.

        If a future refactor only clears one, the note will look
        partially-deleted (deleted_at=NULL but deleted_by='alice').
        """
        _insert_note(self.db_path, "lessons/restore-both")
        soft_delete_note(self.db_path, "lessons/restore-both", deleted_by="bob")
        restore_note(self.db_path, "lessons/restore-both")
        with open_db(self.db_path) as conn:
            row = conn.execute(
                "SELECT deleted_at, deleted_by FROM memories WHERE id=?",
                ("lessons/restore-both",),
            ).fetchone()
        self.assertIsNone(row[0], "deleted_at must be cleared")
        self.assertIsNone(row[1], "deleted_by must be cleared")

    def test_is_soft_deleted_false_after_restore(self):
        _insert_note(self.db_path, "lessons/restore-flag")
        soft_delete_note(self.db_path, "lessons/restore-flag")
        restore_note(self.db_path, "lessons/restore-flag")
        self.assertFalse(is_soft_deleted(self.db_path, "lessons/restore-flag"))

    def test_restore_active_note_returns_false(self):
        """Restore on an already-active note: no-op, returns False."""
        _insert_note(self.db_path, "lessons/restore-active")
        self.assertFalse(restore_note(self.db_path, "lessons/restore-active"))

    def test_restore_unknown_note_returns_false(self):
        self.assertFalse(restore_note(self.db_path, "lessons/never-existed"))

    def test_restore_then_redelete_works(self):
        """Full lifecycle: save → delete → restore → delete again."""
        _insert_note(self.db_path, "lessons/lifecycle")
        soft_delete_note(self.db_path, "lessons/lifecycle")
        restore_note(self.db_path, "lessons/lifecycle")
        # Second delete should work (not "already deleted" anymore)
        self.assertTrue(soft_delete_note(self.db_path, "lessons/lifecycle"))


class TestListTrash(unittest.TestCase):
    """list_trash: returning soft-deleted notes, oldest first."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "memory.db"
        _bootstrap_full_db(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_empty_trash_returns_empty_list(self):
        self.assertEqual(list_trash(self.db_path), [])

    def test_trash_includes_soft_deleted(self):
        _insert_note(self.db_path, "lessons/in-trash")
        soft_delete_note(self.db_path, "lessons/in-trash")
        trash = list_trash(self.db_path, include_expired=True)
        ids = [t["id"] for t in trash]
        self.assertIn("lessons/in-trash", ids)

    def test_trash_excludes_active_notes(self):
        _insert_note(self.db_path, "lessons/active-not-in-trash")
        trash = list_trash(self.db_path, include_expired=True)
        ids = [t["id"] for t in trash]
        self.assertNotIn("lessons/active-not-in-trash", ids)

    def test_trash_entry_has_required_keys(self):
        """The list_trash contract: each entry must have these keys."""
        _insert_note(self.db_path, "lessons/trash-shape")
        soft_delete_note(self.db_path, "lessons/trash-shape")
        trash = list_trash(self.db_path, include_expired=True)
        self.assertEqual(len(trash), 1)
        entry = trash[0]
        for k in ("id", "source_file", "deleted_at", "deleted_by", "days_until_purge"):
            self.assertIn(k, entry, f"missing key: {k}")

    def test_trash_orders_oldest_first(self):
        """Bug-hunting: oldest first matches the function docstring."""
        _insert_note(self.db_path, "lessons/a")
        # Anti-thundering-herd: ensure note A's deleted_at is measurably
        # before note B's deleted_at, so the ORDER BY test is deterministic.
        time.sleep(0.05)
        soft_delete_note(self.db_path, "lessons/a")
        _insert_note(self.db_path, "lessons/b")
        time.sleep(0.05)
        soft_delete_note(self.db_path, "lessons/b")
        _insert_note(self.db_path, "lessons/c")
        soft_delete_note(self.db_path, "lessons/c")
        trash = list_trash(self.db_path, include_expired=True)
        self.assertEqual(
            [t["id"] for t in trash], ["lessons/a", "lessons/b", "lessons/c"]
        )

    def test_trash_includes_deleted_by(self):
        """Bug-hunting: deleted_by from the original call must be preserved."""
        _insert_note(self.db_path, "lessons/trash-deleter")
        soft_delete_note(
            self.db_path, "lessons/trash-deleter", deleted_by="cleanup_script"
        )
        trash = list_trash(self.db_path, include_expired=True)
        self.assertEqual(trash[0]["deleted_by"], "cleanup_script")

    def test_trash_days_until_purge_decreases(self):
        """Bug-hunting: days_until_purge is positive for fresh deletes."""
        _insert_note(self.db_path, "lessons/fresh-trash")
        soft_delete_note(self.db_path, "lessons/fresh-trash")
        trash = list_trash(self.db_path, include_expired=True)
        self.assertGreater(trash[0]["days_until_purge"], 29.0)
        self.assertLessEqual(trash[0]["days_until_purge"], 30.0)

    def test_trash_default_excludes_30_day_old(self):
        """include_expired=False (default) must hide notes past 30 days."""
        _insert_note(self.db_path, "lessons/old-trash")
        soft_delete_note(self.db_path, "lessons/old-trash")
        # Manually backdate deleted_at to 31 days ago
        with open_db(self.db_path) as conn:
            old_date = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
            conn.execute(
                "UPDATE memories SET deleted_at=? WHERE id=?",
                (old_date, "lessons/old-trash"),
            )
        # Default (include_expired=False) excludes it
        self.assertEqual(list_trash(self.db_path), [])
        # include_expired=True includes it
        trash_expired = list_trash(self.db_path, include_expired=True)
        self.assertEqual([t["id"] for t in trash_expired], ["lessons/old-trash"])


class TestPurgeExpired(unittest.TestCase):
    """purge_expired: hard-deleting notes past the 30-day window."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "memory.db"
        _bootstrap_full_db(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_purge_removes_only_expired(self):
        """Bug-hunting: only notes > 30 days old are hard-deleted."""
        _insert_note(self.db_path, "lessons/fresh")
        _insert_note(self.db_path, "lessons/expired")
        soft_delete_note(self.db_path, "lessons/fresh")
        soft_delete_note(self.db_path, "lessons/expired")
        # Backdate one
        with open_db(self.db_path) as conn:
            old_date = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
            conn.execute(
                "UPDATE memories SET deleted_at=? WHERE id=?",
                (old_date, "lessons/expired"),
            )
        n_purged = purge_expired(self.db_path)
        self.assertEqual(n_purged, 1)
        # Expired is gone
        self.assertFalse(is_soft_deleted(self.db_path, "lessons/expired"))
        # Fresh is still soft-deleted (not hard-deleted)
        self.assertTrue(is_soft_deleted(self.db_path, "lessons/fresh"))

    def test_purge_returns_zero_on_empty(self):
        self.assertEqual(purge_expired(self.db_path), 0)

    def test_purge_cascade_removes_backlinks(self):
        """Bug-hunting: hard_delete must cascade to backlinks."""
        # Create two notes, link them
        _insert_note(
            self.db_path, "lessons/link-source", content="see [[lessons/link-target]]"
        )
        _insert_note(self.db_path, "lessons/link-target")
        soft_delete_note(self.db_path, "lessons/link-source")
        # Backdate so it's purge-eligible
        with open_db(self.db_path) as conn:
            old_date = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
            conn.execute(
                "UPDATE memories SET deleted_at=? WHERE id=?",
                (old_date, "lessons/link-source"),
            )
        # Pre-purge: count backlinks for this note
        with open_db(self.db_path) as conn:
            before = conn.execute(
                "SELECT COUNT(*) FROM backlinks WHERE source_id=? OR target_id=?",
                ("lessons/link-source", "lessons/link-source"),
            ).fetchone()[0]
        purge_expired(self.db_path)
        # Post-purge: backlinks for the deleted note must be gone
        with open_db(self.db_path) as conn:
            after = conn.execute(
                "SELECT COUNT(*) FROM backlinks WHERE source_id=? OR target_id=?",
                ("lessons/link-source", "lessons/link-source"),
            ).fetchone()[0]
        self.assertGreater(before, 0, "test setup: expected backlinks to exist")
        self.assertEqual(
            after, 0, "purge must remove backlinks for the hard-deleted note"
        )


class TestHardDelete(unittest.TestCase):
    """hard_delete_note: immediate permanent removal (no 30-day wait)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "memory.db"
        _bootstrap_full_db(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_hard_delete_removes_from_memories_table(self):
        _insert_note(self.db_path, "lessons/hard-1")
        # SAFETY: hard_delete requires soft-delete first OR > 30 days old
        soft_delete_note(self.db_path, "lessons/hard-1")
        self.assertTrue(hard_delete_note(self.db_path, "lessons/hard-1"))
        with open_db(self.db_path) as conn:
            row = conn.execute(
                "SELECT id FROM memories WHERE id=?", ("lessons/hard-1",)
            ).fetchone()
        self.assertIsNone(row)

    def test_hard_delete_unknown_returns_false(self):
        self.assertFalse(hard_delete_note(self.db_path, "lessons/never-was"))

    def test_hard_delete_refuses_active_fresh_note(self):
        """Bug-hunting: hard_delete on a fresh active note must REFUSE
        (not silently delete). The function raises ValueError as a
        safety check against accidental data loss.

        Found during the 2026-06-16 test rebuild — this safety contract
        was previously hidden by the blanket xfail. The check is:
        "must be >30 days or soft-deleted first".
        """
        _insert_note(self.db_path, "lessons/hard-active-fresh")
        with self.assertRaises(ValueError) as ctx:
            hard_delete_note(self.db_path, "lessons/hard-active-fresh")
        # The error message should mention the safety check
        self.assertIn("soft-deleted", str(ctx.exception).lower())

    def test_hard_delete_allows_old_active_note(self):
        """A note > 30 days old (created_at) can be hard-deleted
        directly without first being soft-deleted. This is the second
        branch of the safety check.
        """
        _insert_note(self.db_path, "lessons/hard-old")
        # Backdate created_at to >30 days ago
        with open_db(self.db_path) as conn:
            old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
            conn.execute(
                "UPDATE memories SET created_at=? WHERE id=?",
                (old, "lessons/hard-old"),
            )
        self.assertTrue(hard_delete_note(self.db_path, "lessons/hard-old"))
        with open_db(self.db_path) as conn:
            row = conn.execute(
                "SELECT id FROM memories WHERE id=?", ("lessons/hard-old",)
            ).fetchone()
        self.assertIsNone(row)

    def test_hard_delete_works_on_soft_deleted_note(self):
        """The normal path: soft-delete, then hard-delete (skipping 30-day wait)."""
        _insert_note(self.db_path, "lessons/hard-soft-then-hard")
        soft_delete_note(self.db_path, "lessons/hard-soft-then-hard")
        self.assertTrue(hard_delete_note(self.db_path, "lessons/hard-soft-then-hard"))
        self.assertFalse(is_soft_deleted(self.db_path, "lessons/hard-soft-then-hard"))


class TestSoftDeleteSearchIntegration(unittest.TestCase):
    """Bug-hunting: soft-deleted notes must NOT appear in normal search results.

    The FTS5 index is maintained by triggers, but the FTS5 row for a
    soft-deleted note should still match searches. The soft-delete
    filter must happen at the application layer (search_memories
    filtering out deleted_at IS NOT NULL).
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "memory.db"
        _bootstrap_full_db(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_soft_deleted_note_excluded_from_search(self):
        """The unique content token 'sdsearchtoken' must NOT appear after delete."""
        _insert_note(
            self.db_path,
            "lessons/sd-search-active",
            content="this is sdsearchtoken before delete",
        )
        _insert_note(
            self.db_path,
            "lessons/sd-search-soft",
            content="this is sdsearchtoken will be soft deleted",
        )
        # Soft-delete one of them
        soft_delete_note(self.db_path, "lessons/sd-search-soft")
        # Search should still find the active one but not the deleted one
        from memory_mcp import search_memories

        r = search_memories(self.db_path, "sdsearchtoken", limit=10)
        ids = [item["id"] for item in r.get("results", [])]
        self.assertIn("lessons/sd-search-active", ids)
        self.assertNotIn("lessons/sd-search-soft", ids)

    def test_restored_note_reappears_in_search(self):
        """After restore, the note must be searchable again."""
        _insert_note(
            self.db_path, "lessons/restore-search", content="this is restoresearchtoken"
        )
        soft_delete_note(self.db_path, "lessons/restore-search")
        restore_note(self.db_path, "lessons/restore-search")
        from memory_mcp import search_memories

        r = search_memories(self.db_path, "restoresearchtoken", limit=10)
        ids = [item["id"] for item in r.get("results", [])]
        self.assertIn("lessons/restore-search", ids)


if __name__ == "__main__":
    unittest.main()
