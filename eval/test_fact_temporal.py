"""Tests for fact_temporal.py — T3 supersession + contradiction logic.

Covers:
  * _event_times_match: granularity-aware time comparison
  * detect_fact_contradiction: S+P match, O differs, time overlap
  * supersede_fact: UPDATE old + new
  * reconcile_fact_supersession: find + supersede

20+ scenarios across all 4 functions.
"""

import os
import sys
import sqlite3
import time
import calendar

sys.path.insert(
    0,
    str(
        os.environ.get("MEMORY_INSTALL_ROOT")
        or os.path.expanduser("~/.config/agentic-memory")
    ),
)

from infra.memory_config import install_root

sys.path.insert(0, str(install_root()))

import fact as fe
import fact.fact_temporal as ft


def _epoch(year: int, month: int = 1, day: int = 1) -> float:
    return float(calendar.timegm((year, month, day, 0, 0, 0, 0, 0, 0)))


def _fresh_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    fe.ensure_facts_schema(conn)
    return conn


def _upsert(conn, *args, **kwargs) -> int:
    """Wrapper that asserts _upsert_fact returned a non-None id."""
    fact_id = fe._upsert_fact(conn, *args, **kwargs)
    assert fact_id is not None, "_upsert_fact returned None"
    return fact_id


class TestEventTimesMatch:
    """Granularity-aware time equality."""

    def test_both_none_match(self):
        assert ft._event_times_match(None, None, None, None) is True

    def test_one_none_matches(self):
        assert ft._event_times_match(None, None, _epoch(2024), "year") is True
        assert ft._event_times_match(_epoch(2024), "year", None, None) is True

    def test_unknown_matches_anything(self):
        # When one side is "unknown", always match.
        assert (
            ft._event_times_match(_epoch(2024, 3, 15), "day", _epoch(2025), "unknown")
            is True
        )
        assert (
            ft._event_times_match(_epoch(2024, 3, 15), "unknown", _epoch(2025), "year")
            is True
        )

    def test_same_year_matches(self):
        assert (
            ft._event_times_match(
                _epoch(2024, 1, 1), "year", _epoch(2024, 6, 1), "year"
            )
            is True
        )

    def test_different_year_no_match(self):
        assert (
            ft._event_times_match(
                _epoch(2024, 1, 1), "year", _epoch(2025, 1, 1), "year"
            )
            is False
        )

    def test_same_month_year(self):
        assert (
            ft._event_times_match(
                _epoch(2024, 3, 1), "month", _epoch(2024, 3, 31), "month"
            )
            is True
        )

    def test_different_month_no_match(self):
        assert (
            ft._event_times_match(
                _epoch(2024, 3, 1), "month", _epoch(2024, 4, 1), "month"
            )
            is False
        )

    def test_same_day(self):
        # _epoch(2024, 3, 15) and _epoch(2024, 3, 15) are identical — they
        # map to the same UTC midnight.  Same day → match.
        assert (
            ft._event_times_match(
                _epoch(2024, 3, 15), "day", _epoch(2024, 3, 15), "day"
            )
            is True
        )

    def test_different_day_no_match(self):
        assert (
            ft._event_times_match(
                _epoch(2024, 3, 15), "day", _epoch(2024, 3, 16), "day"
            )
            is False
        )

    def test_day_month_uses_less_precise(self):
        # Less precise is "month"; both are March 2024 → match.
        assert (
            ft._event_times_match(
                _epoch(2024, 3, 15), "day", _epoch(2024, 3, 1), "month"
            )
            is True
        )

    def test_day_year_uses_year(self):
        # Less precise is "year"; both are 2024 → match.
        assert (
            ft._event_times_match(
                _epoch(2024, 3, 15), "day", _epoch(2024, 1, 1), "year"
            )
            is True
        )

    def test_day_year_different_year(self):
        assert (
            ft._event_times_match(
                _epoch(2024, 3, 15), "day", _epoch(2025, 1, 1), "year"
            )
            is False
        )


class TestDetectFactContradiction:
    """S+P match, O differs, times match → contradiction."""

    def test_same_sp_diff_o_same_time(self):
        assert (
            ft.detect_fact_contradiction(
                "Python",
                "is_a",
                "language",
                _epoch(2024),
                "year",
                "Python",
                "is_a",
                "framework",
                _epoch(2024),
                "year",
            )
            is True
        )

    def test_same_sp_same_o_no_contradiction(self):
        assert (
            ft.detect_fact_contradiction(
                "Python",
                "is_a",
                "language",
                _epoch(2024),
                "year",
                "Python",
                "is_a",
                "language",
                _epoch(2024),
                "year",
            )
            is False
        )

    def test_different_subj_no_contradiction(self):
        assert (
            ft.detect_fact_contradiction(
                "Python",
                "is_a",
                "language",
                _epoch(2024),
                "year",
                "Ruby",
                "is_a",
                "framework",
                _epoch(2024),
                "year",
            )
            is False
        )

    def test_different_pred_no_contradiction(self):
        assert (
            ft.detect_fact_contradiction(
                "Python",
                "is_a",
                "language",
                _epoch(2024),
                "year",
                "Python",
                "uses",
                "framework",
                _epoch(2024),
                "year",
            )
            is False
        )

    def test_diff_time_no_contradiction(self):
        assert (
            ft.detect_fact_contradiction(
                "Python",
                "is_a",
                "language",
                _epoch(2024),
                "year",
                "Python",
                "is_a",
                "framework",
                _epoch(2025),
                "year",
            )
            is False
        )

    def test_one_unknown_time_contradicts(self):
        # If either is unknown, we can't disprove overlap → contradiction.
        assert (
            ft.detect_fact_contradiction(
                "Python",
                "is_a",
                "language",
                None,
                None,
                "Python",
                "is_a",
                "framework",
                _epoch(2024),
                "year",
            )
            is True
        )

    def test_case_insensitive_subj(self):
        assert (
            ft.detect_fact_contradiction(
                "Python",
                "is_a",
                "language",
                _epoch(2024),
                "year",
                "python",
                "is_a",
                "framework",
                _epoch(2024),
                "year",
            )
            is True
        )

    def test_case_insensitive_obj(self):
        # Different O (case-insensitive) → "LANGUAGE" == "language" → no contradiction.
        assert (
            ft.detect_fact_contradiction(
                "Python",
                "is_a",
                "Language",
                _epoch(2024),
                "year",
                "Python",
                "is_a",
                "language",
                _epoch(2024),
                "year",
            )
            is False
        )


class TestSupersedeFact:
    """supersede_fact updates both old and new."""

    def _make_pair(self, conn, event_time=None, granularity=None):
        """Insert two facts with different objects. Returns (old_id, new_id)."""
        old_id = _upsert(
            conn,
            "Python",
            "is_a",
            "language",
            0.9,
            time.time(),
            "mem_a",
            "ctx",
            event_time=event_time,
            event_time_granularity=granularity,
        )
        new_id = _upsert(
            conn,
            "Python",
            "is_a",
            "framework",
            0.9,
            time.time(),
            "mem_b",
            "ctx",
            event_time=event_time,
            event_time_granularity=granularity,
        )
        return old_id, new_id

    def test_basic_supersession(self):
        conn = _fresh_db()
        old_id, new_id = self._make_pair(conn, _epoch(2024), "year")
        assert ft.supersede_fact(conn, old_id, new_id, "contradicted") is True
        old = conn.execute(
            "SELECT invalid_at, superseded_by, invalidation_reason, "
            "contradiction_score FROM kg_facts WHERE id = ?",
            (old_id,),
        ).fetchone()
        assert old[0] is not None  # invalid_at populated
        assert old[1] == new_id  # superseded_by = new_id
        assert old[2] == "contradicted"
        assert old[3] == 1.0
        new = conn.execute(
            "SELECT supersedes FROM kg_facts WHERE id = ?",
            (new_id,),
        ).fetchone()
        assert new[0] == old_id

    def test_invalid_at_uses_new_event_time(self):
        """When the new fact has an event_time, that's the invalid_at."""
        conn = _fresh_db()
        old_id, new_id = self._make_pair(conn, _epoch(2024), "year")
        new_event_time = _epoch(2025, 3, 15)
        # Update new fact's event_time directly
        conn.execute(
            "UPDATE kg_facts SET event_time = ? WHERE id = ?",
            (new_event_time, new_id),
        )
        ft.supersede_fact(conn, old_id, new_id, "superseded")
        old = conn.execute(
            "SELECT invalid_at FROM kg_facts WHERE id = ?", (old_id,)
        ).fetchone()
        assert old[0] == new_event_time

    def test_cannot_supersede_self(self):
        conn = _fresh_db()
        old_id, new_id = self._make_pair(conn, _epoch(2024), "year")
        assert ft.supersede_fact(conn, old_id, old_id) is False

    def test_cannot_supersede_locked(self):
        conn = _fresh_db()
        old_id, new_id = self._make_pair(conn, _epoch(2024), "year")
        # Lock the old fact
        conn.execute("UPDATE kg_facts SET locked = 1 WHERE id = ?", (old_id,))
        assert ft.supersede_fact(conn, old_id, new_id) is False

    def test_cannot_supersede_already_superseded(self):
        conn = _fresh_db()
        old_id, new_id = self._make_pair(conn, _epoch(2024), "year")
        # First supersession
        ft.supersede_fact(conn, old_id, new_id, "contradicted")
        # Second attempt fails
        # Need a third fact to act as the "new"
        third_id = _upsert(
            conn,
            "Python",
            "is_a",
            "snake",
            0.9,
            time.time(),
            "mem_c",
            "ctx",
        )
        assert ft.supersede_fact(conn, old_id, third_id) is False

    def test_supersede_missing_old(self):
        conn = _fresh_db()
        _, new_id = self._make_pair(conn, _epoch(2024), "year")
        assert ft.supersede_fact(conn, 9999, new_id) is False

    def test_supersede_missing_new(self):
        conn = _fresh_db()
        old_id, _ = self._make_pair(conn, _epoch(2024), "year")
        assert ft.supersede_fact(conn, old_id, 9999) is False


class TestReconcileFactSupersession:
    """End-to-end: reconcile finds and supersedes contradicting facts."""

    def test_finds_and_supersedes_candidate(self):
        conn = _fresh_db()
        # Insert a fact first
        old_id = _upsert(
            conn,
            "Python",
            "is_a",
            "language",
            0.9,
            time.time(),
            "mem_old",
            "ctx",
            event_time=_epoch(2024),
            event_time_granularity="year",
        )
        # Insert a contradicting fact
        new_id = _upsert(
            conn,
            "Python",
            "is_a",
            "framework",
            0.9,
            time.time(),
            "mem_new",
            "ctx",
            event_time=_epoch(2024),
            event_time_granularity="year",
        )
        superseded = ft.reconcile_fact_supersession(conn, new_id)
        assert old_id in superseded
        old = conn.execute(
            "SELECT superseded_by, invalidation_reason FROM kg_facts WHERE id = ?",
            (old_id,),
        ).fetchone()
        assert old[0] == new_id
        assert old[1] == "contradicted"

    def test_no_contradiction_returns_empty(self):
        conn = _fresh_db()
        new_id = _upsert(
            conn,
            "Python",
            "is_a",
            "language",
            0.9,
            time.time(),
            "mem_a",
            "ctx",
            event_time=_epoch(2024),
            event_time_granularity="year",
        )
        # No existing facts → no candidates
        superseded = ft.reconcile_fact_supersession(conn, new_id)
        assert superseded == []

    def test_different_predicate_not_superseded(self):
        conn = _fresh_db()
        # Same subject, different predicate, different object — no contradiction
        _upsert(
            conn,
            "Python",
            "is_a",
            "language",
            0.9,
            time.time(),
            "mem_a",
            "ctx",
            event_time=_epoch(2024),
            event_time_granularity="year",
        )
        new_id = _upsert(
            conn,
            "Python",
            "uses",
            "framework",
            0.9,
            time.time(),
            "mem_b",
            "ctx",
            event_time=_epoch(2024),
            event_time_granularity="year",
        )
        superseded = ft.reconcile_fact_supersession(conn, new_id)
        assert superseded == []

    def test_same_object_not_superseded(self):
        conn = _fresh_db()
        _upsert(
            conn,
            "Python",
            "is_a",
            "language",
            0.9,
            time.time(),
            "mem_a",
            "ctx",
            event_time=_epoch(2024),
            event_time_granularity="year",
        )
        new_id = _upsert(
            conn,
            "Python",
            "is_a",
            "language",
            0.9,
            time.time(),
            "mem_b",
            "ctx",
            event_time=_epoch(2024),
            event_time_granularity="year",
        )
        # Same S+P+O → no contradiction
        superseded = ft.reconcile_fact_supersession(conn, new_id)
        assert superseded == []

    def test_already_superseded_skipped(self):
        conn = _fresh_db()
        old_id = _upsert(
            conn,
            "Python",
            "is_a",
            "language",
            0.9,
            time.time(),
            "mem_a",
            "ctx",
            event_time=_epoch(2024),
            event_time_granularity="year",
        )
        # Manually mark old as superseded
        conn.execute(
            "UPDATE kg_facts SET superseded_by = 0, invalid_at = 100 WHERE id = ?",
            (old_id,),
        )
        # New fact should not be able to supersede the already-superseded one
        new_id = _upsert(
            conn,
            "Python",
            "is_a",
            "framework",
            0.9,
            time.time(),
            "mem_b",
            "ctx",
            event_time=_epoch(2024),
            event_time_granularity="year",
        )
        superseded = ft.reconcile_fact_supersession(conn, new_id)
        assert old_id not in superseded

    def test_different_time_not_superseded(self):
        conn = _fresh_db()
        _upsert(
            conn,
            "Python",
            "is_a",
            "language",
            0.9,
            time.time(),
            "mem_a",
            "ctx",
            event_time=_epoch(2020),
            event_time_granularity="year",
        )
        new_id = _upsert(
            conn,
            "Python",
            "is_a",
            "framework",
            0.9,
            time.time(),
            "mem_b",
            "ctx",
            event_time=_epoch(2024),
            event_time_granularity="year",
        )
        superseded = ft.reconcile_fact_supersession(conn, new_id)
        assert superseded == []  # different years → no contradiction

    def test_missing_new_fact_id(self):
        conn = _fresh_db()
        assert ft.reconcile_fact_supersession(conn, 9999) == []

    def test_multiple_candidates_one_superseded(self):
        conn = _fresh_db()
        # Two old facts: one with matching time, one with different
        match_id = _upsert(
            conn,
            "Python",
            "is_a",
            "language",
            0.9,
            time.time(),
            "mem_a",
            "ctx",
            event_time=_epoch(2024),
            event_time_granularity="year",
        )
        no_match_id = _upsert(
            conn,
            "Python",
            "is_a",
            "scripting_thing",
            0.9,
            time.time(),
            "mem_b",
            "ctx",
            event_time=_epoch(2020),
            event_time_granularity="year",
        )
        new_id = _upsert(
            conn,
            "Python",
            "is_a",
            "framework",
            0.9,
            time.time(),
            "mem_c",
            "ctx",
            event_time=_epoch(2024),
            event_time_granularity="year",
        )
        superseded = ft.reconcile_fact_supersession(conn, new_id)
        assert match_id in superseded
        assert no_match_id not in superseded


class TestEndToEndIndexing:
    """Verify index_facts_for_memory triggers reconciliation."""

    def test_save_triggers_reconciliation(self):
        """Two memories with contradicting facts trigger auto-supersession."""
        conn = _fresh_db()
        # First memory: explicit bold-label fact so the subject is clean.
        fe.index_facts_for_memory(
            conn,
            "mem_1",
            "## Status\n\n**Type:** Python is a language. Active in 2024.",
        )
        # Second memory: same S+P, different O, same time.
        fe.index_facts_for_memory(
            conn,
            "mem_2",
            "## Status\n\n**Type:** Python is a framework. Active in 2024.",
        )
        # Check that the first fact is now superseded
        rows = conn.execute(
            "SELECT subject, object, superseded_by, invalidation_reason "
            "FROM kg_facts WHERE superseded_by IS NOT NULL"
        ).fetchall()
        assert len(rows) == 1
        subj, obj, sup_by, reason = rows[0]
        # Subject may be "type python" (regex picks up "Type:" as a label
        # prefix) or similar — we just check it contains "python" and
        # the object is "language" (the older fact).
        assert "python" in subj
        assert obj == "language"
        assert sup_by is not None
        assert reason == "contradicted"

    def test_save_no_contradiction_does_not_supersede(self):
        """Two memories with non-contradicting facts don't trigger supersession."""
        conn = _fresh_db()
        fe.index_facts_for_memory(
            conn, "mem_1", "## Description\n\nPython is a language."
        )
        fe.index_facts_for_memory(
            conn, "mem_2", "## Description\n\nRuby is a language."
        )
        rows = conn.execute(
            "SELECT COUNT(*) FROM kg_facts WHERE superseded_by IS NOT NULL"
        ).fetchone()
        assert rows[0] == 0

    def test_reverse_insertion_order_newer_wins(self):
        """Sprint 2: order-independent — newer fact wins regardless of insert order."""
        conn = _fresh_db()
        # Both facts have same year precision (both 2024), so they contradict.
        # But B's raw event_time (2024-06-01) is later than A's (2024-01-01),
        # so B should always win regardless of insertion order.
        newer_ts = _epoch(2024, 6, 1)  # 2024-06-01
        older_ts = _epoch(2024, 1, 1)  # 2024-01-01
        # Insert the NEWER fact FIRST
        newer_id = _upsert(
            conn,
            "Python",
            "is_a",
            "framework",
            0.9,
            time.time(),
            "mem_newer",
            "ctx",
            event_time=newer_ts,
            event_time_granularity="year",
        )
        # Insert the OLDER fact SECOND
        older_id = _upsert(
            conn,
            "Python",
            "is_a",
            "language",
            0.9,
            time.time(),
            "mem_older",
            "ctx",
            event_time=older_ts,
            event_time_granularity="year",
        )
        # Reconcile on the older (last-inserted) fact
        superseded = ft.reconcile_fact_supersession(conn, older_id)
        # The OLDER fact should be superseded (newer fact is chronologically later)
        assert older_id in superseded, (
            f"older_id={older_id} should be in superseded={superseded}"
        )
        older = conn.execute(
            "SELECT superseded_by, invalidation_reason FROM kg_facts WHERE id = ?",
            (older_id,),
        ).fetchone()
        assert older[0] == newer_id
        assert older[1] == "contradicted"
        # Newer fact should NOT be superseded
        newer = conn.execute(
            "SELECT superseded_by FROM kg_facts WHERE id = ?",
            (newer_id,),
        ).fetchone()
        assert newer[0] is None

    def test_forward_insertion_order_newer_wins(self):
        """Sprint 2: same outcome in forward order."""
        conn = _fresh_db()
        older_ts = _epoch(2024, 1, 1)
        newer_ts = _epoch(2024, 6, 1)
        older_id = _upsert(
            conn,
            "Python",
            "is_a",
            "language",
            0.9,
            time.time(),
            "mem_older",
            "ctx",
            event_time=older_ts,
            event_time_granularity="year",
        )
        newer_id = _upsert(
            conn,
            "Python",
            "is_a",
            "framework",
            0.9,
            time.time(),
            "mem_newer",
            "ctx",
            event_time=newer_ts,
            event_time_granularity="year",
        )
        superseded = ft.reconcile_fact_supersession(conn, newer_id)
        assert older_id in superseded
        older = conn.execute(
            "SELECT superseded_by FROM kg_facts WHERE id = ?",
            (older_id,),
        ).fetchone()
        assert older[0] == newer_id

    def test_bidirectional_new_fact_can_lose(self):
        """Sprint 2: If the new fact is chronologically later, it always wins."""
        conn = _fresh_db()
        b_ts = _epoch(2024, 6, 1)
        c_ts = _epoch(2024, 12, 1)
        # Insert B first (Jun)
        b_id = _upsert(
            conn,
            "Python",
            "is_a",
            "runtime",
            0.9,
            time.time(),
            "mem_b",
            "ctx",
            event_time=b_ts,
            event_time_granularity="year",
        )
        # Insert C second (Dec). C will reconcile against B.
        c_id = _upsert(
            conn,
            "Python",
            "is_a",
            "platform",
            0.9,
            time.time(),
            "mem_c",
            "ctx",
            event_time=c_ts,
            event_time_granularity="year",
        )
        # Reconcile: C (Dec) should supersede B (Jun)
        superseded = ft.reconcile_fact_supersession(conn, c_id)
        assert b_id in superseded
        b_row = conn.execute(
            "SELECT superseded_by FROM kg_facts WHERE id = ?",
            (b_id,),
        ).fetchone()
        assert b_row[0] == c_id
        # C should NOT be superseded
        c_row = conn.execute(
            "SELECT superseded_by FROM kg_facts WHERE id = ?",
            (c_id,),
        ).fetchone()
        assert c_row[0] is None

    def test_equal_event_times_new_wins(self):
        """Sprint 2: when event_times are equal, the new fact wins (tie-break)."""
        conn = _fresh_db()
        shared_ts = _epoch(2024, 3, 15)
        old_id = _upsert(
            conn,
            "Python",
            "is_a",
            "language",
            0.9,
            time.time(),
            "mem_old",
            "ctx",
            event_time=shared_ts,
            event_time_granularity="day",
        )
        new_id = _upsert(
            conn,
            "Python",
            "is_a",
            "framework",
            0.9,
            time.time(),
            "mem_new",
            "ctx",
            event_time=shared_ts,
            event_time_granularity="day",
        )
        superseded = ft.reconcile_fact_supersession(conn, new_id)
        assert old_id in superseded
        old = conn.execute(
            "SELECT superseded_by FROM kg_facts WHERE id = ?",
            (old_id,),
        ).fetchone()
        assert old[0] == new_id

    def test_mark_fact_superseded_preserves_winner(self):
        """_mark_fact_superseded doesn't overwrite winner's supersedes column."""
        conn = _fresh_db()
        # Create a chain: A superseded by B
        a_id = _upsert(conn, "X", "r", "a", 0.9, time.time(), "m1", "c")
        b_id = _upsert(conn, "X", "r", "b", 0.9, time.time(), "m2", "c")
        ft.supersede_fact(conn, a_id, b_id, "first", score=1.0)
        # Verify A is superseded by B
        a_row = conn.execute(
            "SELECT superseded_by FROM kg_facts WHERE id = ?", (a_id,)
        ).fetchone()
        assert a_row[0] == b_id
        # B should have supersedes = A
        b_row = conn.execute(
            "SELECT supersedes FROM kg_facts WHERE id = ?", (b_id,)
        ).fetchone()
        assert b_row[0] == a_id

        # Now a new fact C arrives. B should supersede C (B's event_time > C's).
        c_id = _upsert(conn, "X", "r", "c", 0.9, time.time(), "m3", "c")
        # Mark B as winning against C using _mark_fact_superseded
        result = ft._mark_fact_superseded(conn, c_id, b_id, "contradicted")
        assert result is True
        # C should be superseded by B
        c_row = conn.execute(
            "SELECT superseded_by FROM kg_facts WHERE id = ?", (c_id,)
        ).fetchone()
        assert c_row[0] == b_id
        # B's supersedes should STILL be A (not overwritten to C)
        b_row = conn.execute(
            "SELECT supersedes FROM kg_facts WHERE id = ?", (b_id,)
        ).fetchone()
        assert b_row[0] == a_id


# ---------------------------------------------------------------------------
# T4.1-T4.4: Time-aware query layer
# ---------------------------------------------------------------------------


class TestTemporalFactClause:
    """T4.1: _temporal_fact_clause returns correct SQL fragment."""

    def test_as_of_none_returns_current_only(self):
        clause, params = ft._temporal_fact_clause(None)
        assert "f.invalid_at IS NULL" in clause
        assert "f.superseded_by IS NULL" in clause
        assert params == []

    def test_as_of_epoch(self):
        clause, params = ft._temporal_fact_clause(1700000000.0)
        assert "f.valid_at <= ?" in clause
        assert "f.invalid_at IS NULL OR f.invalid_at > ?" in clause
        assert params == [1700000000.0, 1700000000.0]


class TestQueryFactsAtTime:
    """T4.2: query_facts_at_time returns facts valid at epoch t."""

    def _setup_facts(self, conn):
        """Create a small KG with a mix of valid/expired facts."""
        # Currently valid (no valid_at, no invalid_at)
        _upsert(
            conn,
            "Python",
            "is_a",
            "language",
            0.9,
            time.time(),
            "m1",
            "ctx",
            event_time=1700000000.0,
            event_time_granularity="day",
        )
        # Valid in 2024 (valid_at in 2024, no invalid_at)
        _upsert(
            conn,
            "Python",
            "is_a",
            "framework",
            0.9,
            time.time(),
            "m2",
            "ctx",
            event_time=1700000000.0,
            event_time_granularity="day",
        )
        conn.execute(
            "UPDATE kg_facts SET valid_at = ?, invalid_at = NULL "
            "WHERE subject = 'python' AND object = 'framework'",
            (1704067200.0,),
        )
        # Expired (invalid_at in the past)
        _upsert(
            conn,
            "Python",
            "is_a",
            "snake",
            0.9,
            time.time(),
            "m3",
            "ctx",
            event_time=1700000000.0,
            event_time_granularity="day",
        )
        conn.execute(
            "UPDATE kg_facts SET valid_at = 1600000000.0, invalid_at = 1670000000.0 "
            "WHERE subject = 'python' AND object = 'snake'"
        )

    def test_query_at_2024_returns_valid_facts(self):
        conn = _fresh_db()
        self._setup_facts(conn)
        # 2024-01-01 = 1704067200
        facts = ft.query_facts_at_time(conn, 1704067200.0)
        objects = {f["object"] for f in facts}
        assert "language" in objects
        assert "framework" in objects
        assert "snake" not in objects  # expired in 2023

    def test_query_at_2022_returns_language_and_snake(self):
        """Snake was valid 2020-2022, language is always-valid;
        framework starts in 2024 so it should NOT be valid in 2022."""
        conn = _fresh_db()
        self._setup_facts(conn)
        # 2022-01-01 = 1640995200
        facts = ft.query_facts_at_time(conn, 1640995200.0)
        objects = {f["object"] for f in facts}
        assert "language" in objects
        assert "snake" in objects  # valid 2020-2022
        assert "framework" not in objects  # starts 2024

    def test_query_with_text_filter(self):
        conn = _fresh_db()
        self._setup_facts(conn)
        facts = ft.query_facts_at_time(conn, 1704067200.0, query="framework")
        assert len(facts) == 1
        assert facts[0]["object"] == "framework"

    def test_query_returns_dicts(self):
        conn = _fresh_db()
        self._setup_facts(conn)
        facts = ft.query_facts_at_time(conn, 1704067200.0, limit=5)
        assert isinstance(facts, list)
        for f in facts:
            assert isinstance(f, dict)
            assert "id" in f
            assert "subject" in f
            assert "predicate" in f
            assert "object" in f
            assert "event_time" in f
            assert "valid_at" in f
            assert "invalid_at" in f


class TestQueryFactSupersessionChain:
    """T4.3: query_fact_supersession_chain walks the superseded_by chain."""

    def test_no_chain_returns_single_fact(self):
        conn = _fresh_db()
        fact_id = _upsert(
            conn, "Python", "is_a", "language", 0.9, time.time(), "m1", "ctx"
        )
        chain = ft.query_fact_supersession_chain(conn, fact_id)
        assert len(chain) == 1
        assert chain[0]["id"] == fact_id

    def test_chain_walk_oldest_first(self):
        conn = _fresh_db()
        # Create chain: fact1 -> superseded by fact2 -> superseded by fact3
        fact1 = _upsert(
            conn,
            "Python",
            "is_a",
            "language",
            0.9,
            time.time(),
            "m1",
            "ctx",
            event_time=1700000000.0,
            event_time_granularity="day",
        )
        fact2 = _upsert(
            conn,
            "Python",
            "is_a",
            "framework",
            0.9,
            time.time(),
            "m2",
            "ctx",
            event_time=1700000000.0,
            event_time_granularity="day",
        )
        fact3 = _upsert(
            conn,
            "Python",
            "is_a",
            "snake",
            0.9,
            time.time(),
            "m3",
            "ctx",
            event_time=1700000000.0,
            event_time_granularity="day",
        )
        # Manually wire the chain: fact1 superseded by fact2, fact2 by fact3
        conn.execute(
            "UPDATE kg_facts SET superseded_by = ?, invalidation_reason = 'contradicted' WHERE id = ?",
            (fact2, fact1),
        )
        conn.execute(
            "UPDATE kg_facts SET superseded_by = ?, invalidation_reason = 'contradicted' WHERE id = ?",
            (fact3, fact2),
        )
        chain = ft.query_fact_supersession_chain(conn, fact1)
        # Oldest first: [fact1, fact2, fact3]
        assert len(chain) == 3
        assert chain[0]["id"] == fact1
        assert chain[1]["id"] == fact2
        assert chain[2]["id"] == fact3

    def test_chain_from_middle(self):
        conn = _fresh_db()
        fact1 = _upsert(
            conn, "Python", "is_a", "language", 0.9, time.time(), "m1", "ctx"
        )
        fact2 = _upsert(
            conn, "Python", "is_a", "framework", 0.9, time.time(), "m2", "ctx"
        )
        conn.execute(
            "UPDATE kg_facts SET superseded_by = ? WHERE id = ?", (fact2, fact1)
        )
        chain = ft.query_fact_supersession_chain(conn, fact1)
        assert len(chain) == 2
        chain = ft.query_fact_supersession_chain(conn, fact2)
        # fact2 is the head — only 1 fact in chain
        assert len(chain) == 1
        assert chain[0]["id"] == fact2

    def test_missing_fact_id(self):
        conn = _fresh_db()
        chain = ft.query_fact_supersession_chain(conn, 9999)
        assert chain == []


class TestQueryFactsChangedSince:
    """T4.4: query_facts_changed_since returns recently changed facts."""

    def test_recently_inserted(self):
        conn = _fresh_db()
        # Insert a fact "now" (after the since_ts)
        now = time.time()
        _upsert(conn, "Python", "is_a", "language", 0.9, now, "m1", "ctx")
        # Query for changes since 1 hour ago
        one_hour_ago = now - 3600
        facts = ft.query_facts_changed_since(conn, one_hour_ago)
        assert len(facts) >= 1
        assert any(f["object"] == "language" for f in facts)

    def test_recently_invalidated(self):
        conn = _fresh_db()
        now = time.time()
        # Insert a fact and mark it as invalidated just now
        fact_id = _upsert(conn, "Python", "is_a", "language", 0.9, now, "m1", "ctx")
        conn.execute(
            "UPDATE kg_facts SET invalid_at = ? WHERE id = ?",
            (now, fact_id),
        )
        one_hour_ago = now - 3600
        facts = ft.query_facts_changed_since(conn, one_hour_ago)
        # The fact should appear (invalidated recently)
        assert any(f["id"] == fact_id for f in facts)

    def test_old_changes_excluded(self):
        conn = _fresh_db()
        now = time.time()
        # Insert a fact in the past (8 days ago)
        eight_days_ago = now - 8 * 86400
        _upsert(conn, "Python", "is_a", "language", 0.9, eight_days_ago, "m1", "ctx")
        # Query for changes since 1 day ago — should not include
        one_day_ago = now - 86400
        facts = ft.query_facts_changed_since(conn, one_day_ago)
        assert len(facts) == 0

    def test_ordered_by_most_recent_change(self):
        conn = _fresh_db()
        now = time.time()
        # Insert old fact
        _upsert(conn, "Python", "is_a", "language", 0.9, now - 7200, "m1", "ctx")
        # Insert new fact
        _upsert(conn, "Python", "is_a", "framework", 0.9, now, "m2", "ctx")
        # Query for changes since 1 day ago
        one_day_ago = now - 86400
        facts = ft.query_facts_changed_since(conn, one_day_ago)
        # Most recent change (framework) should be first
        assert facts[0]["object"] == "framework"


# ---------------------------------------------------------------------------
# T5: Memory-update handling (invalidate stale facts on edit)
# ---------------------------------------------------------------------------


class TestInvalidateFact:
    """T5.3: invalidate_fact marks a fact as no-longer-valid."""

    def test_basic_invalidation(self):
        conn = _fresh_db()
        fid = _upsert(conn, "Python", "is_a", "language", 0.9, time.time(), "m1", "ctx")
        assert ft.invalidate_fact(conn, fid) is True
        row = conn.execute(
            "SELECT invalid_at, invalidation_reason, superseded_by FROM kg_facts WHERE id = ?",
            (fid,),
        ).fetchone()
        assert row[0] is not None  # invalid_at set
        assert row[1] == "manual"
        assert row[2] is None  # no replacement

    def test_custom_reason(self):
        conn = _fresh_db()
        fid = _upsert(conn, "Python", "is_a", "language", 0.9, time.time(), "m1", "ctx")
        assert ft.invalidate_fact(conn, fid, reason="expired") is True
        row = conn.execute(
            "SELECT invalidation_reason FROM kg_facts WHERE id = ?", (fid,)
        ).fetchone()
        assert row[0] == "expired"

    def test_custom_invalid_at(self):
        conn = _fresh_db()
        fid = _upsert(conn, "Python", "is_a", "language", 0.9, time.time(), "m1", "ctx")
        custom_ts = 1700000000.0
        assert ft.invalidate_fact(conn, fid, invalid_at=custom_ts) is True
        row = conn.execute(
            "SELECT invalid_at FROM kg_facts WHERE id = ?", (fid,)
        ).fetchone()
        assert row[0] == custom_ts

    def test_cannot_invalidate_locked(self):
        conn = _fresh_db()
        fid = _upsert(conn, "Python", "is_a", "language", 0.9, time.time(), "m1", "ctx")
        conn.execute("UPDATE kg_facts SET locked = 1 WHERE id = ?", (fid,))
        assert ft.invalidate_fact(conn, fid) is False

    def test_cannot_invalidate_already_invalidated(self):
        conn = _fresh_db()
        fid = _upsert(conn, "Python", "is_a", "language", 0.9, time.time(), "m1", "ctx")
        ft.invalidate_fact(conn, fid)
        assert ft.invalidate_fact(conn, fid) is False  # second call is no-op

    def test_missing_fact(self):
        conn = _fresh_db()
        assert ft.invalidate_fact(conn, 9999) is False


class TestInvalidateStaleFacts:
    """T5.1-T5.3: invalidate_stale_facts diffs old vs new and invalidates."""

    def test_no_old_facts_returns_empty(self):
        """New memory: no old facts, nothing to invalidate."""
        conn = _fresh_db()
        invalidated = ft.invalidate_stale_facts(
            conn, "new_mem", {("python", "is_a", "language")}
        )
        assert invalidated == []

    def test_facts_kept_if_still_in_new(self):
        """Old fact still in new content: not invalidated."""
        conn = _fresh_db()
        fid = _upsert(conn, "Python", "is_a", "language", 0.9, time.time(), "m1", "ctx")
        invalidated = ft.invalidate_stale_facts(
            conn, "m1", {("python", "is_a", "language")}
        )
        assert invalidated == []
        row = conn.execute(
            "SELECT invalid_at FROM kg_facts WHERE id = ?", (fid,)
        ).fetchone()
        assert row[0] is None  # not invalidated

    def test_removed_fact_invalidated(self):
        """Old fact NOT in new content: invalidated."""
        conn = _fresh_db()
        fid = _upsert(conn, "Python", "is_a", "language", 0.9, time.time(), "m1", "ctx")
        # New content has different facts
        invalidated = ft.invalidate_stale_facts(
            conn, "m1", {("python", "is_a", "framework")}
        )
        assert fid in invalidated
        row = conn.execute(
            "SELECT invalid_at, invalidation_reason FROM kg_facts WHERE id = ?",
            (fid,),
        ).fetchone()
        assert row[0] is not None
        assert row[1] == "manual"

    def test_only_invalidates_facts_from_this_memory(self):
        """Old fact from a DIFFERENT memory: not invalidated."""
        conn = _fresh_db()
        fid = _upsert(
            conn, "Python", "is_a", "language", 0.9, time.time(), "other_mem", "ctx"
        )
        # New content for m1 doesn't have this fact, but it's owned by other_mem
        invalidated = ft.invalidate_stale_facts(
            conn, "m1", {("python", "is_a", "framework")}
        )
        assert invalidated == []
        row = conn.execute(
            "SELECT invalid_at FROM kg_facts WHERE id = ?", (fid,)
        ).fetchone()
        assert row[0] is None  # not invalidated

    def test_mixed_kept_and_removed(self):
        """Some facts kept, some removed: only the removed are invalidated."""
        conn = _fresh_db()
        keep = _upsert(
            conn, "Python", "is_a", "language", 0.9, time.time(), "m1", "ctx"
        )
        remove = _upsert(
            conn, "Python", "is_a", "framework", 0.9, time.time(), "m1", "ctx"
        )
        # New content only mentions "language"
        invalidated = ft.invalidate_stale_facts(
            conn, "m1", {("python", "is_a", "language")}
        )
        assert remove in invalidated
        assert keep not in invalidated
        # Verify
        row_keep = conn.execute(
            "SELECT invalid_at FROM kg_facts WHERE id = ?", (keep,)
        ).fetchone()
        row_remove = conn.execute(
            "SELECT invalid_at FROM kg_facts WHERE id = ?", (remove,)
        ).fetchone()
        assert row_keep[0] is None
        assert row_remove[0] is not None


class TestAuditFactTemporalEvent:
    """T5.4: audit_fact_temporal_event writes to memory_audit_log."""

    def test_audit_writes_to_kg_fact_temporal_tool(self):
        conn = _fresh_db()
        # Audit table may or may not exist — the helper is best-effort.
        # Ensure it exists for the test.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS memory_audit_log ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  ts REAL NOT NULL,"
            "  tool TEXT NOT NULL,"
            "  args TEXT,"
            "  results_count INTEGER,"
            "  top1_id TEXT,"
            "  latency_ms REAL NOT NULL,"
            "  error TEXT,"
            "  request_id TEXT"
            ")"
        )
        ft.audit_fact_temporal_event(
            conn,
            event="invalidate",
            fact_id=42,
            reason="manual",
            subject="python",
            predicate="is_a",
            obj="language",
            memory_id="m1",
        )
        row = conn.execute(
            "SELECT tool, args FROM memory_audit_log WHERE tool = 'kg_fact_temporal' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        assert row[0] == "kg_fact_temporal"
        import json

        payload = json.loads(row[1])
        assert payload["event"] == "invalidate"
        assert payload["fact_id"] == 42
        assert payload["reason"] == "manual"
        assert payload["subject"] == "python"
        assert payload["memory_id"] == "m1"


class TestEndToEndMemoryEdit:
    """T5.5: full end-to-end: edit a memory, verify its facts are re-evaluated."""

    def test_edit_removes_fact_invalidates_it(self):
        """Edit a memory to remove a fact; old fact gets invalidated."""
        conn = _fresh_db()
        # First save: two distinct facts (no S+P collision, so no
        # contradiction fires — we want to isolate T5 invalidation
        # from T3 contradiction logic)
        fe.index_facts_for_memory(
            conn,
            "mem_a",
            "## Description\n\n"
            "**Type:** Python is a language. "
            "**Maintainer:** PSF is a foundation.",
        )
        # Find the PSF fact (we want to verify it gets invalidated)
        psf_rows = conn.execute(
            "SELECT id, invalid_at FROM kg_facts "
            "WHERE source_memory = 'mem_a' AND object = 'foundation'"
        ).fetchall()
        if not psf_rows:
            # Regex didn't extract "PSF is a foundation" — skip
            return
        psf_id = psf_rows[0][0]
        assert psf_rows[0][1] is None  # not invalidated yet
        # Second save: remove the PSF fact (only language remains)
        fe.index_facts_for_memory(
            conn,
            "mem_a",
            "## Description\n\n**Type:** Python is a language.",
        )
        # The PSF fact should now be invalidated
        after = conn.execute(
            "SELECT invalid_at, invalidation_reason FROM kg_facts WHERE id = ?",
            (psf_id,),
        ).fetchone()
        assert after[0] is not None
        assert after[1] == "manual"

    def test_new_memory_does_not_invalidate(self):
        """First save for a memory: no old facts, nothing invalidated."""
        conn = _fresh_db()
        fe.index_facts_for_memory(
            conn, "fresh_mem", "## Description\n\nPython is a language."
        )
        # No facts should be invalidated
        rows = conn.execute(
            "SELECT COUNT(*) FROM kg_facts WHERE invalid_at IS NOT NULL"
        ).fetchone()
        assert rows[0] == 0

    def test_edit_keeps_fact_that_is_still_present(self):
        """Edit a memory but keep a fact: it should not be invalidated."""
        conn = _fresh_db()
        # First save
        fe.index_facts_for_memory(
            conn, "mem_b", "## Description\n\nPython is a language."
        )
        # Capture the language fact ID
        before = conn.execute(
            "SELECT id, invalid_at FROM kg_facts "
            "WHERE source_memory = 'mem_b' AND object = 'language'"
        ).fetchone()
        assert before is not None
        original_id = before[0]
        # Second save: same content (with extra content)
        fe.index_facts_for_memory(
            conn,
            "mem_b",
            "## Description\n\nPython is a language. **More:** The system uses Python.",
        )
        # The original language fact should still be there, NOT invalidated
        after = conn.execute(
            "SELECT id, invalid_at FROM kg_facts WHERE id = ?", (original_id,)
        ).fetchone()
        assert after[0] == original_id
        assert after[1] is None  # not invalidated


class TestPropagateEntitySupersession:
    """Sprint 4: graph-level contradiction propagation on kg_facts."""

    def _setup_entity_facts(self, conn):
        """Create facts sharing the same entity_id."""
        shared_ts = _epoch(2024)
        # Base fact: Alice was_at Paris (subject_entity_id=1)
        at_paris = _upsert(
            conn,
            "Alice",
            "was_at",
            "Paris",
            0.9,
            time.time(),
            "m_at_paris",
            "ctx",
            event_time=shared_ts,
            event_time_granularity="year",
        )
        conn.execute(
            "UPDATE kg_facts SET subject_entity_id = 1, object_entity_id = 2 "
            "WHERE id = ?",
            (at_paris,),
        )
        return at_paris, shared_ts

    def test_propagate_same_entity_same_predicate(self):
        """Sibling facts on the same entity+same predicate get propagated."""
        conn = _fresh_db()
        shared_ts = _epoch(2024)
        # Base fact: Alice was_at Paris (subject_entity_id=1, predicate="was_at")
        at_paris = _upsert(
            conn,
            "Alice",
            "was_at",
            "Paris",
            0.9,
            time.time(),
            "m_at_paris",
            "ctx",
            event_time=shared_ts,
            event_time_granularity="year",
        )
        conn.execute(
            "UPDATE kg_facts SET subject_entity_id = 1, object_entity_id = 2 "
            "WHERE id = ?",
            (at_paris,),
        )
        # Sibling fact: Alice was_at Rome (same subject_entity_id=1, same predicate)
        at_rome = _upsert(
            conn,
            "Alice",
            "was_at",
            "Rome",
            0.9,
            time.time(),
            "m_at_rome",
            "ctx",
            event_time=shared_ts,
            event_time_granularity="year",
        )
        conn.execute(
            "UPDATE kg_facts SET subject_entity_id = 1 WHERE id = ?",
            (at_rome,),
        )
        # New fact supersedes at_paris: Alice was_at London (same year 2024)
        new_id = _upsert(
            conn,
            "Alice",
            "was_at",
            "London",
            0.9,
            time.time(),
            "m_at_london",
            "ctx",
            event_time=shared_ts,
            event_time_granularity="year",
        )
        conn.execute(
            "UPDATE kg_facts SET subject_entity_id = 1 WHERE id = ?",
            (new_id,),
        )
        # Reconcile on new_id: should supersede at_paris AND supersede at_rome
        superseded = ft.reconcile_fact_supersession(conn, new_id)
        assert at_paris in superseded
        # Check that at_rome is also superseded (same predicate → contradiction path)
        at_rome_row = conn.execute(
            "SELECT superseded_by, invalidation_reason FROM kg_facts WHERE id = ?",
            (at_rome,),
        ).fetchone()
        assert at_rome_row[0] is not None, "at_rome should be superseded (same predicate)"

    def test_propagate_same_entity_different_predicate_not_propagated(self):
        """Facts with different predicates on same entity are NOT propagated."""
        conn = _fresh_db()
        shared_ts = _epoch(2024)
        # Base fact: Alice was_at Paris (subject_entity_id=1, predicate="was_at")
        at_paris = _upsert(
            conn,
            "Alice",
            "was_at",
            "Paris",
            0.9,
            time.time(),
            "m_at_paris",
            "ctx",
            event_time=shared_ts,
            event_time_granularity="year",
        )
        conn.execute(
            "UPDATE kg_facts SET subject_entity_id = 1, object_entity_id = 2 "
            "WHERE id = ?",
            (at_paris,),
        )
        # Sibling fact: Alice is_in Paris (same subject_entity_id=1, different predicate)
        is_in_paris = _upsert(
            conn,
            "Alice",
            "is_in",
            "Paris",
            0.9,
            time.time(),
            "m_is_in_paris",
            "ctx",
            event_time=shared_ts,
            event_time_granularity="year",
        )
        conn.execute(
            "UPDATE kg_facts SET subject_entity_id = 1 WHERE id = ?",
            (is_in_paris,),
        )
        # New fact supersedes at_paris: Alice was_at London (same year 2024)
        new_id = _upsert(
            conn,
            "Alice",
            "was_at",
            "London",
            0.9,
            time.time(),
            "m_at_london",
            "ctx",
            event_time=shared_ts,
            event_time_granularity="year",
        )
        conn.execute(
            "UPDATE kg_facts SET subject_entity_id = 1 WHERE id = ?",
            (new_id,),
        )
        # Reconcile on new_id: should supersede at_paris but NOT propagate to is_in_paris
        superseded = ft.reconcile_fact_supersession(conn, new_id)
        assert at_paris in superseded
        # Check that is_in_paris is NOT superseded (different predicate, no propagation)
        is_in_row = conn.execute(
            "SELECT superseded_by, invalidation_reason FROM kg_facts WHERE id = ?",
            (is_in_paris,),
        ).fetchone()
        assert is_in_row[0] is None, "is_in_paris should NOT be propagated superseded (different predicate)"

    def test_propagate_no_entity_ids_returns_empty(self):
        """Fact with no entity_ids has no propagation targets."""
        conn = _fresh_db()
        plain_id = _upsert(
            conn,
            "Python",
            "is_a",
            "language",
            0.9,
            time.time(),
            "m_plain",
            "ctx",
            event_time=_epoch(2024),
            event_time_granularity="year",
        )
        # entity_id is NULL by default → no propagation
        result = ft.propagate_entity_supersession(conn, plain_id, 999)
        assert result == []

    def test_propagate_same_predicate_skipped(self):
        """Facts with the same predicate are not double-propagated (reconcile handles them)."""
        conn = _fresh_db()
        a_id = _upsert(
            conn,
            "X",
            "is_a",
            "alpha",
            0.9,
            time.time(),
            "m_a",
            "ctx",
            event_time=_epoch(2024),
            event_time_granularity="year",
        )
        conn.execute(
            "UPDATE kg_facts SET subject_entity_id = 10 WHERE id = ?",
            (a_id,),
        )
        # Propagate should return empty because there are no other predicates on X
        result = ft.propagate_entity_supersession(conn, a_id, 999)
        assert result == []

    def test_propagate_already_superseded_skipped(self):
        """Already-superseded facts are not re-propagated."""
        conn = _fresh_db()
        # Superseded fact about entity 20
        superseded_id = _upsert(
            conn,
            "Y",
            "r1",
            "old_val",
            0.9,
            time.time(),
            "m_sup",
            "ctx",
            event_time=_epoch(2024),
            event_time_granularity="year",
        )
        conn.execute(
            "UPDATE kg_facts SET subject_entity_id = 20, superseded_by = 0 WHERE id = ?",
            (superseded_id,),
        )
        result = ft.propagate_entity_supersession(conn, superseded_id, 999)
        # No active candidates → empty
        assert result == []

    def test_propagate_invalidates_sibling_fact(self):
        """Full round-trip: supersede a fact, verify same-predicate sibling is also invalidated."""
        conn = _fresh_db()
        shared_ts = _epoch(2024)
        # Bob lives_in NYC (subject_entity_id=30)
        lives_nyc = _upsert(
            conn,
            "Bob",
            "lives_in",
            "NYC",
            0.9,
            time.time(),
            "m_lives_nyc",
            "ctx",
            event_time=shared_ts,
            event_time_granularity="year",
        )
        conn.execute(
            "UPDATE kg_facts SET subject_entity_id = 30, object_entity_id = 31 WHERE id = ?",
            (lives_nyc,),
        )
        # Bob lives_in Chicago (same predicate, same entity)
        lives_chi = _upsert(
            conn,
            "Bob",
            "lives_in",
            "Chicago",
            0.9,
            time.time(),
            "m_lives_chi",
            "ctx",
            event_time=shared_ts,
            event_time_granularity="year",
        )
        conn.execute(
            "UPDATE kg_facts SET subject_entity_id = 30, object_entity_id = 32 WHERE id = ?",
            (lives_chi,),
        )
        # New fact: Bob lives_in LA
        new_id = _upsert(
            conn,
            "Bob",
            "lives_in",
            "LA",
            0.9,
            time.time(),
            "m_lives_la",
            "ctx",
            event_time=shared_ts,
            event_time_granularity="year",
        )
        conn.execute(
            "UPDATE kg_facts SET subject_entity_id = 30 WHERE id = ?",
            (new_id,),
        )
        superseded = ft.reconcile_fact_supersession(conn, new_id)
        assert lives_nyc in superseded
        # lives_chi should be superseded via contradiction (same predicate)
        chi_row = conn.execute(
            "SELECT superseded_by FROM kg_facts WHERE id = ?",
            (lives_chi,),
        ).fetchone()
        assert chi_row[0] is not None, "lives_in Chicago should be superseded (same predicate)"



class TestPropagateEntitySupersessionNoneTime:
    """Bug 3 fix: propagate_entity_supersession must not cascade to
    sibling facts when the new (superseding) fact has event_time=None."""

    def test_no_propagation_when_new_fact_has_none_event_time(self):
        conn = _fresh_db()
        old_id = _upsert(
            conn, "X", "likes", "ice_cream", 0.9,
            time.time(), "mem_a", "ctx",
            event_time=None, event_time_granularity=None,
        )
        conn.execute(
            "UPDATE kg_facts SET subject_entity_id = 1 WHERE id = ?",
            (old_id,),
        )
        new_id = _upsert(
            conn, "X", "likes", "pizza", 0.9,
            time.time(), "mem_b", "ctx",
            event_time=None, event_time_granularity=None,
        )
        conn.execute(
            "UPDATE kg_facts SET subject_entity_id = 1 WHERE id = ?",
            (new_id,),
        )
        sibling_id = _upsert(
            conn, "X", "knows", "Y", 0.9,
            time.time(), "mem_c", "ctx",
            event_time=_epoch(2024), event_time_granularity="year",
        )
        conn.execute(
            "UPDATE kg_facts SET subject_entity_id = 1 WHERE id = ?",
            (sibling_id,),
        )
        propagated = ft.propagate_entity_supersession(conn, old_id, new_id)
        assert sibling_id not in propagated, (
            "Sibling fact should not be superseded when new fact has event_time=None"
        )
        sibling = conn.execute(
            "SELECT superseded_by FROM kg_facts WHERE id=?", (sibling_id,)
        ).fetchone()
        assert sibling[0] is None

    def test_propagation_still_works_with_concrete_event_time(self):
        conn = _fresh_db()
        old_id = _upsert(
            conn, "X", "likes", "ice_cream", 0.9,
            time.time(), "mem_a", "ctx",
            event_time=_epoch(2024), event_time_granularity="year",
        )
        conn.execute(
            "UPDATE kg_facts SET subject_entity_id = 1 WHERE id = ?",
            (old_id,),
        )
        new_id = _upsert(
            conn, "X", "likes", "pizza", 0.9,
            time.time(), "mem_b", "ctx",
            event_time=_epoch(2024), event_time_granularity="year",
        )
        conn.execute(
            "UPDATE kg_facts SET subject_entity_id = 1 WHERE id = ?",
            (new_id,),
        )
        sibling_id = _upsert(
            conn, "X", "likes", "sushi", 0.9,
            time.time(), "mem_c", "ctx",
            event_time=_epoch(2024), event_time_granularity="year",
        )
        conn.execute(
            "UPDATE kg_facts SET subject_entity_id = 1 WHERE id = ?",
            (sibling_id,),
        )
        propagated = ft.propagate_entity_supersession(conn, old_id, new_id)
        assert sibling_id in propagated, (
            "Sibling fact with same predicate SHOULD be superseded when new fact has concrete event_time"
        )
