"""Test contradiction merge wiring (G3).

auto_resolve_contradiction_pair with force_strategy='merge' should:
  1. Create a merged note from both inputs
  2. Supersede both originals toward the merged note
  3. Return action='merged' with the merged_note_id
"""
import sqlite3
import time
from unittest.mock import patch

from eval._fixtures import bootstrap_temp_db_clean
from kg.contradiction_resolver import auto_resolve_contradiction_pair, _pick_strategy


def _insert_memories(conn, now):
    """Insert two contradictory notes into the memories table."""
    conn.execute(
        "INSERT INTO memories (id, content, category, created_at, updated_at, "
        "observed_at, source_file, metadata, valid_from, valid_to, superseded_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("note-a", "Alice is a lawyer", "lessons",
         str(now - 100), str(now - 100), str(now - 100), "", "{}", None, None, None),
    )
    conn.execute(
        "INSERT INTO memories (id, content, category, created_at, updated_at, "
        "observed_at, source_file, metadata, valid_from, valid_to, superseded_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("note-b", "Alice is a chef", "lessons",
         str(now), str(now), str(now), "", "{}", None, None, None),
    )
    conn.commit()


class TestContradictionMerge:
    """force_strategy='merge' produces a merged note."""

    def test_explicit_merge_strategy(self, tmp_path):
        """Explicit strategy='merge' produces action='merged'."""
        db_path = tmp_path / "test.db"
        bootstrap_temp_db_clean(db_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            now = time.time()
            _insert_memories(conn, now)

            merged_id = "merged/note-a__note-b"

            def _mock_save_memory_auto(content, category, title_slug, tags,
                                       importance, defer_expensive, db_path,
                                       _conn=None):
                """Simulate save: insert merged note, return its ID."""
                c = _conn or conn
                c.execute(
                    "INSERT INTO memories "
                    "(id, content, category, created_at, updated_at, "
                    " observed_at, source_file, metadata, valid_from, valid_to, superseded_by) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (merged_id, content, category,
                     str(now), str(now), str(now), "", "{}", None, None, None),
                )
                c.commit()
                return merged_id

            with (
                patch("save.pipeline.save_memory_auto", side_effect=_mock_save_memory_auto),
                patch("save.pipeline.memory_supersede_db") as mock_supersede,
            ):
                mock_supersede.return_value = (True, None)
                result = auto_resolve_contradiction_pair(
                    str(db_path), "note-a", "note-b",
                    conn=conn, force_strategy="merge",
                )

            assert result["action"] == "merged", f"Expected 'merged', got {result}"
            assert result["strategy"] == "merge"
            assert result["merged_note_id"] == merged_id
            assert set(result["superseded"]) == {"note-a", "note-b"}

            # memory_supersede_db should have been called twice (once per original)
            assert mock_supersede.call_count == 2
        finally:
            conn.close()

    def test_pick_strategy_newer_wins(self, tmp_path):
        """_pick_strategy: newer note wins (by created_at)."""
        db_path = tmp_path / "test.db"
        bootstrap_temp_db_clean(db_path)
        conn = sqlite3.connect(str(db_path))
        try:
            now = time.time()
            _insert_memories(conn, now)
            # note-b has later created_at → note-b wins → supersede_a_with_b
            row_a = conn.execute(
                "SELECT id, content, source_file, created_at, updated_at, metadata "
                "FROM memories WHERE id = ?", ("note-a",)
            ).fetchone()
            row_b = conn.execute(
                "SELECT id, content, source_file, created_at, updated_at, metadata "
                "FROM memories WHERE id = ?", ("note-b",)
            ).fetchone()
            strategy = _pick_strategy(row_a, row_b)
            assert strategy == "supersede_a_with_b", (
                f"Expected newer note (b) to win, got strategy={strategy}"
            )
        finally:
            conn.close()

    def test_keep_both_strategy(self, tmp_path):
        """force_strategy='keep_both' returns action='kept_both' with no DB mutation."""
        db_path = tmp_path / "test.db"
        bootstrap_temp_db_clean(db_path)
        conn = sqlite3.connect(str(db_path))
        try:
            now = time.time()
            _insert_memories(conn, now)

            result = auto_resolve_contradiction_pair(
                str(db_path), "note-a", "note-b",
                conn=conn, force_strategy="keep_both",
            )

            assert result["action"] == "kept_both"
            assert result["strategy"] == "keep_both"

            # Both notes should still exist unchanged
            a = conn.execute("SELECT content FROM memories WHERE id = ?", ("note-a",)).fetchone()
            b = conn.execute("SELECT content FROM memories WHERE id = ?", ("note-b",)).fetchone()
            assert a is not None and "lawyer" in a[0]
            assert b is not None and "chef" in b[0]
        finally:
            conn.close()

    def test_missing_note_returns_error(self, tmp_path):
        """If a note doesn't exist, action='error' is returned."""
        db_path = tmp_path / "test.db"
        bootstrap_temp_db_clean(db_path)
        conn = sqlite3.connect(str(db_path))
        try:
            result = auto_resolve_contradiction_pair(
                str(db_path), "nonexistent-a", "nonexistent-b",
                conn=conn, force_strategy="merge",
            )
            assert result["action"] == "error"
            assert "not found" in result["error"]
        finally:
            conn.close()
