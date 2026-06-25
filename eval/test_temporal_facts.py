"""Integration tests for T2.5: _upsert_fact stores event_time/granularity.

Verifies the end-to-end flow:
  index_facts_for_memory -> extract_event_time(content) -> _upsert_fact

The memory's date is extracted once via `extract_event_time` and
applied to every fact extracted from that memory.  For both the LLM
and regex paths, this gives a memory-level event_time on every fact.
"""

import os, sys, sqlite3, tempfile, time

os.environ["MEMORY_KNOWLEDGE_GRAPH"] = "1"
os.environ["MEMORY_LLM_HYBRID"] = "0"  # Force regex path for deterministic tests
sys.path.insert(
    0,
    str(
        os.environ.get("MEMORY_INSTALL_ROOT")
        or os.path.expanduser("~/.config/agentic-memory")
    ),
)

from memory_config import install_root

sys.path.insert(0, str(install_root()))

import fact_extraction as fe


def _fresh_db() -> sqlite3.Connection:
    """Create an in-memory DB with kg_facts schema."""
    conn = sqlite3.connect(":memory:")
    fe.ensure_facts_schema(conn)
    return conn


class TestUpsertFactEventTime:
    """Verify _upsert_fact stores event_time on INSERT."""

    def test_insert_stores_event_time(self):
        conn = _fresh_db()
        now = time.time()
        fe._upsert_fact(
            conn,
            "Python",
            "is_a",
            "language",
            0.9,
            now,
            "mem_test_1",
            "context",
            event_time=1704067200.0,  # 2024-01-01
            event_time_granularity="day",
        )
        row = conn.execute(
            "SELECT event_time, event_time_granularity, transaction_time "
            "FROM kg_facts WHERE subject = 'python'"
        ).fetchone()
        assert row is not None
        assert row[0] == 1704067200.0
        assert row[1] == "day"
        # transaction_time should be ~now (within 1 second)
        assert abs(row[2] - now) < 1.0
        conn.close()

    def test_insert_without_event_time(self):
        """Backward compat: callers can omit event_time."""
        conn = _fresh_db()
        now = time.time()
        fe._upsert_fact(
            conn,
            "Python",
            "is_a",
            "language",
            0.9,
            now,
            "mem_test_2",
            "context",
        )
        row = conn.execute(
            "SELECT event_time, event_time_granularity, transaction_time "
            "FROM kg_facts WHERE subject = 'python'"
        ).fetchone()
        assert row is not None
        assert row[0] is None  # event_time not set
        assert row[1] is None  # granularity not set
        assert row[2] is not None  # transaction_time still populated
        conn.close()

    def test_insert_with_granularity_only(self):
        """Granularity without explicit epoch is not useful, but should not error."""
        conn = _fresh_db()
        now = time.time()
        fe._upsert_fact(
            conn,
            "Python",
            "is_a",
            "language",
            0.9,
            now,
            "mem_test_3",
            "context",
            event_time=None,
            event_time_granularity="year",
        )
        row = conn.execute(
            "SELECT event_time, event_time_granularity FROM kg_facts"
        ).fetchone()
        assert row[0] is None
        assert row[1] == "year"
        conn.close()

    def test_duplicate_does_not_overwrite_event_time(self):
        """On duplicate, the original event_time is preserved."""
        conn = _fresh_db()
        now = time.time()
        # First insert with event_time
        fe._upsert_fact(
            conn,
            "Python",
            "is_a",
            "language",
            0.9,
            now,
            "mem_orig",
            "context",
            event_time=1704067200.0,
            event_time_granularity="day",
        )
        # Re-insert (same SPO) with a different event_time
        fe._upsert_fact(
            conn,
            "Python",
            "is_a",
            "language",
            0.95,
            now + 10,
            "mem_dup",
            "context",
            event_time=1735689600.0,  # 2025-01-01
            event_time_granularity="day",
        )
        # event_time should still be the original
        row = conn.execute(
            "SELECT event_time, event_time_granularity, mention_count, "
            "source_memory FROM kg_facts"
        ).fetchone()
        assert row[0] == 1704067200.0  # preserved
        assert row[1] == "day"
        # mention_count and source_memory should have updated
        assert row[2] == 2
        assert row[3] == "mem_dup"  # last source wins
        conn.close()


class TestIndexFactsEventTimeWiring:
    """Verify index_facts_for_memory extracts event_time and applies to all facts."""

    def test_memory_with_iso_date(self):
        """A memory with an ISO date has its date applied to all facts."""
        conn = _fresh_db()
        content = (
            "## Status\n\n"
            "**Description:** Python is a programming language. "
            "Python supports multiple paradigms. Active since 2024-03-15."
        )
        result = fe.index_facts_for_memory(conn, "mem_iso", content)
        assert result["facts"] > 0, "expected at least one fact"
        rows = conn.execute(
            "SELECT DISTINCT event_time, event_time_granularity FROM kg_facts"
        ).fetchall()
        assert len(rows) == 1  # all facts share the same memory-level time
        epoch, gran = rows[0]
        assert gran == "day"
        import calendar

        assert epoch == calendar.timegm((2024, 3, 15, 0, 0, 0, 0, 0, 0))
        conn.close()

    def test_memory_with_year_precision(self):
        """A memory with 'in 2024' applies year-precision time to all facts."""
        conn = _fresh_db()
        content = (
            "## Status\n\n"
            "**Description:** Python is a programming language. "
            "Python supports multiple paradigms. Active in 2024."
        )
        result = fe.index_facts_for_memory(conn, "mem_year", content)
        assert result["facts"] > 0, "expected at least one fact"
        rows = conn.execute(
            "SELECT DISTINCT event_time, event_time_granularity FROM kg_facts"
        ).fetchall()
        assert len(rows) == 1
        epoch, gran = rows[0]
        assert gran == "year"
        import calendar

        assert epoch == calendar.timegm((2024, 1, 1, 0, 0, 0, 0, 0, 0))
        conn.close()

    def test_memory_with_no_date(self):
        """A memory with no date has event_time = NULL for all facts."""
        conn = _fresh_db()
        content = "## Notes\n\nThe system processes requests from users."
        result = fe.index_facts_for_memory(conn, "mem_nodate", content)
        # May or may not extract facts — if it does, event_time should be NULL.
        if result["facts"] > 0:
            rows = conn.execute(
                "SELECT DISTINCT event_time, event_time_granularity FROM kg_facts"
            ).fetchall()
            for epoch, gran in rows:
                assert epoch is None
                assert gran is None or gran == "unknown"
        conn.close()

    def test_memory_with_quarter(self):
        """A memory with 'Q1 2026' applies month-precision time."""
        conn = _fresh_db()
        content = "## Notes\n\nThe redesign shipped. In Q1 2026 we launched."
        result = fe.index_facts_for_memory(conn, "mem_q1", content)
        if result["facts"] > 0:
            rows = conn.execute(
                "SELECT DISTINCT event_time, event_time_granularity FROM kg_facts"
            ).fetchall()
            assert len(rows) == 1
            epoch, gran = rows[0]
            assert gran == "month"
            import calendar

            assert epoch == calendar.timegm((2026, 1, 1, 0, 0, 0, 0, 0, 0))
        conn.close()


class TestIndexFactsBackwardCompat:
    """Verify index_facts_for_memory signature didn't change."""

    def test_returns_facts_count(self):
        """The function still returns the same shape: {"facts": int}."""
        conn = _fresh_db()
        result = fe.index_facts_for_memory(
            conn, "mem_bc", "## Hello\n\nThe world is round."
        )
        assert "facts" in result
        assert isinstance(result["facts"], int)
        conn.close()

    def test_operational_skip_still_works(self):
        """Operational categories are still skipped (B24 behavior)."""
        conn = _fresh_db()
        result = fe.index_facts_for_memory(
            conn, "sessions/auto-test", "as of 2024-01-01 something happened"
        )
        assert result["facts"] == 0
        conn.close()


# ---------------------------------------------------------------------------
# T8: feature_temporal_kg flag
# ---------------------------------------------------------------------------


class TestFeatureFlag:
    """T8: the feature_temporal_kg flag gates the temporal subsystem."""

    def test_default_on_no_event_time_extraction(self):
        """When flag is ON (default), event_time is populated."""
        # Default is ON; no env var set
        os.environ.pop("MEMORY_TEMPORAL_KG", None)
        conn = _fresh_db()
        result = fe.index_facts_for_memory(
            conn,
            "mem_t8_on",
            "## Description\n\nPython is a language. Active in 2024.",
        )
        assert result["facts"] > 0
        rows = conn.execute(
            "SELECT DISTINCT event_time, event_time_granularity FROM kg_facts"
        ).fetchall()
        # At least one fact has event_time
        assert any(r[0] is not None for r in rows)
        conn.close()

    def test_flag_off_no_event_time(self):
        """When flag is OFF, event_time is NOT populated."""
        os.environ["MEMORY_TEMPORAL_KG"] = "0"
        try:
            conn = _fresh_db()
            result = fe.index_facts_for_memory(
                conn,
                "mem_t8_off",
                "## Description\n\nPython is a language. Active in 2024.",
            )
            assert result["facts"] > 0
            rows = conn.execute(
                "SELECT DISTINCT event_time, event_time_granularity FROM kg_facts"
            ).fetchall()
            # No fact should have event_time when the flag is off
            assert all(r[0] is None for r in rows)
            conn.close()
        finally:
            del os.environ["MEMORY_TEMPORAL_KG"]

    def test_flag_off_no_supersession(self):
        """When flag is OFF, supersession reconciliation does NOT run."""
        os.environ["MEMORY_TEMPORAL_KG"] = "0"
        try:
            conn = _fresh_db()
            # First save: Python is a language
            fe.index_facts_for_memory(
                conn,
                "m_off_1",
                "## Description\n\nPython is a language. Active in 2024.",
            )
            # Second save: Python is a framework — should NOT supersede
            fe.index_facts_for_memory(
                conn,
                "m_off_2",
                "## Description\n\nPython is a framework. Active in 2024.",
            )
            # No supersession should have happened
            superseded = conn.execute(
                "SELECT COUNT(*) FROM kg_facts WHERE superseded_by IS NOT NULL"
            ).fetchone()[0]
            assert superseded == 0
            conn.close()
        finally:
            del os.environ["MEMORY_TEMPORAL_KG"]

    def test_flag_off_no_invalidation_on_edit(self):
        """When flag is OFF, edit invalidation does NOT run."""
        os.environ["MEMORY_TEMPORAL_KG"] = "0"
        try:
            conn = _fresh_db()
            # First save
            fe.index_facts_for_memory(
                conn,
                "m_off_edit",
                "## Description\n\nPython is a language. PSF is a foundation.",
            )
            # Capture any PSF fact
            psf = conn.execute(
                "SELECT id FROM kg_facts "
                "WHERE source_memory = 'm_off_edit' AND object = 'foundation'"
            ).fetchone()
            if psf:
                # Edit to remove PSF
                fe.index_facts_for_memory(
                    conn,
                    "m_off_edit",
                    "## Description\n\nPython is a language.",
                )
                # PSF should NOT be invalidated (flag off)
                row = conn.execute(
                    "SELECT invalid_at FROM kg_facts WHERE id = ?", (psf[0],)
                ).fetchone()
                assert row[0] is None
            conn.close()
        finally:
            del os.environ["MEMORY_TEMPORAL_KG"]

    def test_flag_off_still_extracts_facts(self):
        """When flag is OFF, basic fact extraction still works (no regression)."""
        os.environ["MEMORY_TEMPORAL_KG"] = "0"
        try:
            conn = _fresh_db()
            result = fe.index_facts_for_memory(
                conn, "m_off_basic", "## Description\n\nPython is a language."
            )
            assert result["facts"] > 0  # extraction still works
            rows = conn.execute(
                "SELECT COUNT(*) FROM kg_facts WHERE source_memory = 'm_off_basic'"
            ).fetchone()[0]
            assert rows > 0
            conn.close()
        finally:
            del os.environ["MEMORY_TEMPORAL_KG"]
