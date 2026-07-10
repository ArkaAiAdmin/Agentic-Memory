"""Test temporal query axis: valid vs transaction time (G2).

query_facts_at_time supports two time_axis modes:
  - "valid":  filters on valid_at/invalid_at  (when the fact was true in the world)
  - "transaction": filters on transaction_time (when we learned the fact)
"""
import sqlite3

from eval._fixtures import bootstrap_temp_db_clean
from fact.fact_temporal import query_facts_at_time


def _insert_fact(conn, **overrides):
    """Insert a kg_facts row with sensible defaults."""
    defaults = {
        "subject": "alice",
        "predicate": "is_a",
        "object": "lawyer",
        "confidence": 1.0,
        "first_seen": 50.0,
        "last_seen": 100.0,
        "valid_at": 100.0,
        "invalid_at": 200.0,
        "transaction_time": 50.0,
    }
    defaults.update(overrides)
    conn.execute(
        "INSERT INTO kg_facts "
        "(subject, predicate, object, confidence, first_seen, last_seen, "
        " valid_at, invalid_at, transaction_time) "
        "VALUES (:subject, :predicate, :object, :confidence, "
        "        :first_seen, :last_seen, :valid_at, :invalid_at, "
        "        :transaction_time)",
        defaults,
    )
    conn.commit()


class TestTemporalQueryAxes:
    """time_axis parameter controls which timestamps are filtered."""

    def test_valid_time_axis_filters_by_valid_at(self, tmp_path):
        """valid_at=100, invalid_at=200: visible at as_of=150, hidden at as_of=250."""
        db_path = tmp_path / "test.db"
        bootstrap_temp_db_clean(db_path)
        conn = sqlite3.connect(str(db_path))
        try:
            _insert_fact(conn)
            # Within validity window
            rows = query_facts_at_time(conn, 150.0, time_axis="valid")
            assert len(rows) == 1, f"Expected 1 fact at as_of=150, got {len(rows)}"
            # After invalid_at
            rows = query_facts_at_time(conn, 250.0, time_axis="valid")
            assert len(rows) == 0, f"Expected 0 facts at as_of=250, got {len(rows)}"
        finally:
            conn.close()

    def test_transaction_time_axis_filters_by_transaction_time(self, tmp_path):
        """transaction_time=50: visible at as_of=150, hidden at as_of=10."""
        db_path = tmp_path / "test.db"
        bootstrap_temp_db_clean(db_path)
        conn = sqlite3.connect(str(db_path))
        try:
            _insert_fact(conn)
            # After transaction_time
            rows = query_facts_at_time(conn, 150.0, time_axis="transaction")
            assert len(rows) == 1, f"Expected 1 fact at as_of=150, got {len(rows)}"
            # Before transaction_time
            rows = query_facts_at_time(conn, 10.0, time_axis="transaction")
            assert len(rows) == 0, f"Expected 0 facts at as_of=10, got {len(rows)}"
        finally:
            conn.close()

    def test_boundary_inclusivity(self, tmp_path):
        """invalid_at=200 included at as_of=200 (>= clause), excluded at 201."""
        db_path = tmp_path / "test.db"
        bootstrap_temp_db_clean(db_path)
        conn = sqlite3.connect(str(db_path))
        try:
            _insert_fact(conn)
            # Boundary: invalid_at=200 >= 200 → included
            rows = query_facts_at_time(conn, 200.0, time_axis="valid")
            assert len(rows) == 1, (
                "Boundary inclusive: invalid_at=200 should be visible at as_of=200"
            )
            # Just past boundary
            rows = query_facts_at_time(conn, 201.0, time_axis="valid")
            assert len(rows) == 0, (
                "Boundary exclusive: invalid_at=200 should NOT be visible at as_of=201"
            )
        finally:
            conn.close()

    def test_null_invalid_at_still_valid(self, tmp_path):
        """A fact with invalid_at=NULL is always visible (still valid)."""
        db_path = tmp_path / "test.db"
        bootstrap_temp_db_clean(db_path)
        conn = sqlite3.connect(str(db_path))
        try:
            _insert_fact(conn, invalid_at=None)
            rows = query_facts_at_time(conn, 999999.0, time_axis="valid")
            assert len(rows) == 1, "NULL invalid_at should be visible at any time"
        finally:
            conn.close()

    def test_null_valid_at_always_valid(self, tmp_path):
        """A fact with valid_at=NULL is treated as 'always valid'."""
        db_path = tmp_path / "test.db"
        bootstrap_temp_db_clean(db_path)
        conn = sqlite3.connect(str(db_path))
        try:
            _insert_fact(conn, valid_at=None, invalid_at=200.0)
            # valid_at=NULL → always valid; invalid_at=200 → visible at 150
            rows = query_facts_at_time(conn, 150.0, time_axis="valid")
            assert len(rows) == 1, "NULL valid_at should be visible before invalid_at"
        finally:
            conn.close()

    def test_superseded_fact_excluded(self, tmp_path):
        """Facts with superseded_by set should be excluded from results."""
        db_path = tmp_path / "test.db"
        bootstrap_temp_db_clean(db_path)
        conn = sqlite3.connect(str(db_path))
        try:
            # Insert two facts; the second supersedes the first.
            # Both need valid_at <= 150 to be visible at query time.
            _insert_fact(conn, subject="alice", object="lawyer")
            _insert_fact(
                conn, subject="alice", object="chef",
                valid_at=50.0, invalid_at=None, transaction_time=60.0,
            )
            # Manually mark the first as superseded
            newer_id = conn.execute(
                "SELECT id FROM kg_facts WHERE object='chef'"
            ).fetchone()[0]
            conn.execute(
                "UPDATE kg_facts SET superseded_by = ? WHERE object='lawyer'",
                (newer_id,),
            )
            conn.commit()

            rows = query_facts_at_time(conn, 150.0, time_axis="valid")
            subjects = [r["subject"] for r in rows]
            assert "alice" in subjects
            # The superseded fact should NOT appear
            objects = [r["object"] for r in rows]
            assert "lawyer" not in objects, "Superseded fact should be excluded"
        finally:
            conn.close()

    def test_query_filter(self, tmp_path):
        """The query parameter filters by subject/predicate/object substring."""
        db_path = tmp_path / "test.db"
        bootstrap_temp_db_clean(db_path)
        conn = sqlite3.connect(str(db_path))
        try:
            _insert_fact(conn, subject="alice", object="lawyer")
            _insert_fact(conn, subject="bob", object="engineer",
                         valid_at=100.0, invalid_at=200.0, transaction_time=60.0)
            # Filter for "alice"
            rows = query_facts_at_time(conn, 150.0, time_axis="valid", query="alice")
            assert len(rows) == 1
            assert rows[0]["subject"] == "alice"
            # Filter for "bob"
            rows = query_facts_at_time(conn, 150.0, time_axis="valid", query="bob")
            assert len(rows) == 1
            assert rows[0]["subject"] == "bob"
        finally:
            conn.close()
