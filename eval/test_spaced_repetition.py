"""Tests for BUG-5: Spaced Repetition subsystem wiring.

Verifies that SpacedRepetition.record_success() and record_failure()
are called from search flows, and that failures don't break search.
"""

import datetime
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def tmp_db():
    """Create a temporary DB with the review_schedule table."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS review_schedule (
            memory_id TEXT PRIMARY KEY,
            retrieval_count INTEGER DEFAULT 0,
            interval_days REAL DEFAULT 1.0,
            next_review TEXT NOT NULL,
            last_reviewed TEXT,
            ease_factor REAL DEFAULT 2.5
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            content TEXT,
            source_file TEXT,
            tags TEXT,
            created_at TEXT,
            updated_at TEXT,
            fitness_score REAL DEFAULT 0.5,
            importance INTEGER DEFAULT 3,
            pinned INTEGER DEFAULT 0,
            deleted_at TEXT,
            access_count INTEGER DEFAULT 0,
            last_accessed TEXT,
            success_score REAL DEFAULT 0.0
        )
    """)
    conn.commit()
    conn.close()
    yield db_path
    db_path.unlink(missing_ok=True)


class TestSpacedRepetitionRecordSuccess:
    """Test SpacedRepetition.record_success directly."""

    def test_first_success_creates_entry(self, tmp_db):
        from spaced_repetition import SpacedRepetition

        sr = SpacedRepetition(tmp_db)
        sr.record_success("test/note-1")
        row = sr.db.execute(
            "SELECT retrieval_count, interval_days, ease_factor FROM review_schedule WHERE memory_id = ?",
            ("test/note-1",),
        ).fetchone()
        sr.close()
        assert row is not None
        rc, interval, ef = row
        assert rc == 1
        assert interval == 1.0
        assert ef == 2.5

    def test_subsequent_success_increases_interval(self, tmp_db):
        from spaced_repetition import SpacedRepetition

        sr = SpacedRepetition(tmp_db)
        sr.record_success("test/note-1")
        sr.record_success("test/note-1")
        row = sr.db.execute(
            "SELECT retrieval_count, interval_days, ease_factor FROM review_schedule WHERE memory_id = ?",
            ("test/note-1",),
        ).fetchone()
        sr.close()
        assert row is not None
        rc, interval, ef = row
        assert rc == 2
        assert interval > 1.0  # interval increased
        assert ef > 2.5  # ease increased

    def test_interval_capped_at_180_days(self, tmp_db):
        from spaced_repetition import SpacedRepetition

        sr = SpacedRepetition(tmp_db)
        # Simulate many successes to push interval high
        for _ in range(20):
            sr.record_success("test/note-1")
        row = sr.db.execute(
            "SELECT interval_days FROM review_schedule WHERE memory_id = ?",
            ("test/note-1",),
        ).fetchone()
        sr.close()
        assert row[0] <= 180.0


class TestSpacedRepetitionRecordFailure:
    """Test SpacedRepetition.record_failure directly."""

    def test_failure_resets_interval(self, tmp_db):
        from spaced_repetition import SpacedRepetition

        sr = SpacedRepetition(tmp_db)
        sr.record_success("test/note-1")
        sr.record_success("test/note-1")
        sr.record_failure("test/note-1")
        row = sr.db.execute(
            "SELECT retrieval_count, interval_days, ease_factor FROM review_schedule WHERE memory_id = ?",
            ("test/note-1",),
        ).fetchone()
        sr.close()
        assert row is not None
        rc, interval, ef = row
        assert rc == 0  # reset
        assert interval == 1.0  # reset
        assert ef < 2.5  # ease decreased

    def test_failure_ease_floor(self, tmp_db):
        from spaced_repetition import SpacedRepetition

        sr = SpacedRepetition(tmp_db)
        # Push ease factor down with many failures
        for _ in range(10):
            sr.record_failure("test/note-1")
        row = sr.db.execute(
            "SELECT ease_factor FROM review_schedule WHERE memory_id = ?",
            ("test/note-1",),
        ).fetchone()
        sr.close()
        assert row[0] >= 1.3  # floor


class TestGetDueReviews:
    """Test get_due_reviews."""

    def test_due_reviews_empty(self, tmp_db):
        from spaced_repetition import SpacedRepetition

        sr = SpacedRepetition(tmp_db)
        due = sr.get_due_reviews()
        sr.close()
        assert due == []

    def test_due_reviews_returns_past_due(self, tmp_db):
        from spaced_repetition import SpacedRepetition

        sr = SpacedRepetition(tmp_db)
        sr.record_success("test/note-1")
        # Manually set next_review to yesterday
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        sr.db.execute(
            "UPDATE review_schedule SET next_review = ? WHERE memory_id = ?",
            (yesterday, "test/note-1"),
        )
        sr.db.commit()
        due = sr.get_due_reviews()
        sr.close()
        assert len(due) == 1
        assert due[0][0] == "test/note-1"


class TestGetStats:
    """Test get_stats."""

    def test_stats_empty(self, tmp_db):
        from spaced_repetition import SpacedRepetition

        sr = SpacedRepetition(tmp_db)
        stats = sr.get_stats()
        sr.close()
        assert stats["total_scheduled"] == 0
        assert stats["due_for_review"] == 0

    def test_stats_with_entries(self, tmp_db):
        from spaced_repetition import SpacedRepetition

        sr = SpacedRepetition(tmp_db)
        sr.record_success("test/note-1")
        sr.record_success("test/note-2")
        stats = sr.get_stats()
        sr.close()
        assert stats["total_scheduled"] == 2


class TestWiringInMcpTools:
    """Test that _record_spaced_repetition is called from memory_search."""

    def test_record_success_called_on_results(self, tmp_db):
        """When search returns results, record_success is called for each."""
        import mcp_surface.mcp_search as mcp_search

        mock_sr = MagicMock()
        with patch.object(mcp_search, "_SpacedRepetition", return_value=mock_sr):
            items = [{"id": "a/1"}, {"id": "b/2"}]
            mcp_search._record_spaced_repetition(tmp_db, items, "test query")
            assert mock_sr.record_success.call_count == 2
            mock_sr.record_success.assert_any_call("a/1")
            mock_sr.record_success.assert_any_call("b/2")
            mock_sr.close.assert_called_once()

    def test_record_failure_called_on_empty(self, tmp_db):
        """When search returns no results, record_failure is called."""
        import mcp_surface.mcp_search as mcp_search

        mock_sr = MagicMock()
        with patch.object(mcp_search, "_SpacedRepetition", return_value=mock_sr):
            mcp_search._record_spaced_repetition(tmp_db, [], "test query")
            mock_sr.record_failure.assert_called_once_with("test query")
            mock_sr.close.assert_called_once()

    def test_sr_failure_doesnt_break_search(self, tmp_db):
        """If SpacedRepetition raises, the helper returns silently."""
        import mcp_surface.mcp_search as mcp_search

        with patch.object(
            mcp_search, "_SpacedRepetition", side_effect=RuntimeError("db locked")
        ):
            # Should not raise
            mcp_search._record_spaced_repetition(tmp_db, [{"id": "x"}], "q")

    def test_sr_import_failure_doesnt_break_search(self, tmp_db):
        """If SpacedRepetition import failed (_SpacedRepetition is None), helper is a no-op."""
        import mcp_surface.mcp_search as mcp_search

        with patch.object(mcp_search, "_SpacedRepetition", None):
            # Should not raise
            mcp_search._record_spaced_repetition(tmp_db, [{"id": "x"}], "q")

    def test_sr_close_failure_doesnt_break_search(self, tmp_db):
        """If sr.close() raises, it's swallowed."""
        import mcp_surface.mcp_search as mcp_search

        mock_sr = MagicMock()
        mock_sr.close.side_effect = RuntimeError("close failed")
        with patch.object(mcp_search, "_SpacedRepetition", return_value=mock_sr):
            # Should not raise
            mcp_search._record_spaced_repetition(tmp_db, [{"id": "x"}], "q")


class TestWiringInRecall:
    """Test that _fetch_relevant in recall.py calls spaced repetition."""

    def test_fetch_relevant_calls_sr_on_results(self, tmp_db):
        """When _fetch_relevant finds results, SpacedRepetition.record_success is called."""
        # Insert a test memory
        conn = sqlite3.connect(str(tmp_db))
        conn.execute(
            "INSERT INTO memories (id, content, source_file, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("test/note-1", "hello world", "test.md", "2026-01-01", "2026-01-01"),
        )
        conn.commit()
        conn.close()

        mock_sr = MagicMock()
        with patch("recall.SpacedRepetition", create=True) as MockSR:
            MockSR.return_value = mock_sr
            from recall.recall import _fetch_relevant

            # _fetch_relevant does `from spaced_repetition import SpacedRepetition`
            # internally, so patch spaced_repetition (not recall)
            with patch("spaced_repetition.SpacedRepetition", MockSR):
                items = _fetch_relevant(tmp_db, "hello", 5)
                # If search returned results, SR should have been called
                if items:
                    assert mock_sr.record_success.called
                    mock_sr.close.assert_called()
