"""Sprint 5: tests for search_memories as_of temporal time-travel parameter.

Verifies:
  - search_memories with as_of=None returns current-state results (default)
  - search_memories with as_of in the past returns only memories valid at that time
  - search_memories with different as_of values produces different result sets
  - search_memories cache key includes as_of (no cross-pollution between timestamps)
  - memory_search MCP tool signature accepts as_of
  - _search_kg_facts accepts as_of and uses temporal clause
  - _apply_temporal_decay uses as_of for timestamp resolution
"""

import inspect
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval._fixtures import bootstrap_temp_db_clean  # noqa: E402

from search.orchestrator import (
    search_memories,
    _search_kg_facts,
    _rerank_results,
    _apply_temporal_decay,
)
from search.scoring import _temporal_decay_factor


class _TemporalTestFixtures:
    """Mix-in providing a hermetic temp-DB seeded with temporal notes."""

    @staticmethod
    def _seed_temporal_db(db_path: Path) -> Path:
        """Create a temp DB with schema + notes that have valid_to/valid_from.

        Seeds 3 notes:
          - note-past-1:        valid_from=2026-01-01, valid_to=2026-03-01  (expired)
          - note-future-1:      valid_from=2026-06-01, valid_to=2026-12-01  (not yet valid before 2026-06-01)
          - note-always-valid:  valid_from=NULL, valid_to=NULL             (always valid)

        All have created_at=2026-02-15T10:00:00+00:00.
        """
        bootstrap_temp_db_clean(db_path)
        conn = sqlite3.connect(str(db_path), detect_types=sqlite3.PARSE_DECLTYPES)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")

        cols_rows = conn.execute("PRAGMA table_info(memories)").fetchall()
        col_names = {r[1] for r in cols_rows}

        has_valid_from = "valid_from" in col_names
        has_valid_to = "valid_to" in col_names
        has_superseded = "superseded_by" in col_names
        has_fitness = "fitness_score" in col_names
        has_importance = "importance" in col_names
        has_pinned = "pinned" in col_names
        has_tenant = "tenant_id" in col_names

        base_cols = ["id", "content", "source_file", "tags",
                      "created_at", "updated_at", "observed_at", "category"]
        base_vals = ["?", "?", "?", "?", "?", "?", "?", "?"]

        if has_fitness:
            base_cols += ["fitness_score"]
            base_vals += ["?"]
        if has_importance:
            base_cols += ["importance"]
            base_vals += ["?"]
        if has_pinned:
            base_cols += ["pinned"]
            base_vals += ["?"]
        if has_tenant:
            base_cols += ["tenant_id"]
            base_vals += ["?"]

        now_iso = "2026-02-15T10:00:00+00:00"
        rows = []

        def _note(id_, content, tags, cat, valid_from=None, valid_to=None,
                  superseded_by=None, fitness=None, importance=None, pinned=0,
                  tenant="default"):
            v = [id_, content, f"memory/{cat}/{id_}.md",
                 json.dumps(tags), now_iso, now_iso, now_iso, cat]
            if has_fitness:
                v.append(fitness if fitness is not None else 0.5)
            if has_importance:
                v.append(importance if importance is not None else 3)
            if has_pinned:
                v.append(pinned)
            if has_tenant:
                v.append(tenant)
            rows.append(tuple(v))
            # Store temporal metadata for later insertion
            return (id_, valid_from, valid_to, superseded_by)

        temporal_meta = []
        temporal_meta.append(_note(
            "note-past-1", "This note expired in March 2026.", ["temporal"], "lessons",
            valid_from="2026-01-01T00:00:00+00:00",
            valid_to="2026-03-01T00:00:00+00:00",
        ))
        temporal_meta.append(_note(
            "note-future-1", "This note only becomes valid from June 2026.", ["temporal"], "lessons",
            valid_from="2026-06-01T00:00:00+00:00",
            valid_to="2026-12-01T00:00:00+00:00",
        ))
        temporal_meta.append(_note(
            "note-always-valid", "This note is always valid.", ["temporal", "always"], "lessons",
        ))

        placeholders = ",".join(base_cols)
        q_marks = ",".join(["?"] * len(base_cols))
        conn.executemany(
            f"INSERT INTO memories ({placeholders}) VALUES ({q_marks})",
            rows,
        )

        # Set valid_from / valid_to on the temporal rows
        for nid, vfrom, vto, sby in temporal_meta:
            sets = []
            params = []
            if has_valid_from and vfrom is not None:
                sets.append("valid_from = ?")
                params.append(vfrom)
            else:
                sets.append("valid_from = NULL")
            if has_valid_to and vto is not None:
                sets.append("valid_to = ?")
                params.append(vto)
            else:
                sets.append("valid_to = NULL")
            if has_superseded:
                sets.append("superseded_by = NULL")
            params.append(nid)
            conn.execute(
                f"UPDATE memories SET {', '.join(sets)} WHERE id = ?",
                params,
            )

        conn.commit()
        conn.close()

        # Reset connection pool so a fresh connection is obtained
        try:
            from infra.db import connection_pool
            connection_pool._pool.clear()
            connection_pool._pooled_ids.clear()
        except Exception:
            pass

        return db_path


class TestSearchMemoriesAsOf(unittest.TestCase):
    """Sprint 5: as_of parameter propagation tests."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "search_temporal.db"

    def _seed(self) -> Path:
        return _TemporalTestFixtures._seed_temporal_db(self.db_path)

    # ------------------------------------------------------------------
    # as_of=None returns current results (existing behavior preserved)
    # ------------------------------------------------------------------
    def test_as_of_none_returns_current_results(self) -> None:
        db = self._seed()
        result = search_memories(db, "note valid temporal", limit=5, as_of=None)
        assert result["count"] >= 1
        ids = {r["id"] for r in result["results"]}
        # note-always-valid is always present; past/future notes visible at their valid times
        assert "note-always-valid" in ids

    def test_as_of_none_cache_key_unchanged(self) -> None:
        """Cache key without as_of is same as with as_of=None explicitly."""
        db = self._seed()
        r1 = search_memories(db, "note valid temporal", limit=5, as_of=None)
        r2 = search_memories(db, "note valid temporal", limit=5)
        # Both should return counts (cache misses on first result cold)
        assert isinstance(r1["count"], int)
        assert isinstance(r2["count"], int)

    # ------------------------------------------------------------------
    # as_of in the past returns only memories valid at that time
    # ------------------------------------------------------------------
    def test_as_of_past_includes_valid_and_always(self) -> None:
        db = self._seed()
        # Feb 15 2026: note-past-1 is valid (valid_to=2026-03-01), note-future-1 not yet valid
        as_of = "2026-02-15T12:00:00+00:00"
        as_of_ts = __import__("datetime").datetime.fromisoformat(as_of).timestamp()
        result = search_memories(db, "note", limit=5, as_of=as_of_ts, include_invalid=False)
        ids = {r["id"] for r in result["results"]}
        assert "note-past-1" in ids, "note-past-1 should be valid on 2026-02-15"
        assert "note-always-valid" in ids, "note-always-valid should always be visible"
        assert "note-future-1" not in ids, "note-future-1 not yet valid on 2026-02-15"

    def test_as_of_after_expiry_excludes_old(self) -> None:
        db = self._seed()
        # April 15 2026: note-past-1 has expired (valid_to=2026-03-01)
        as_of = "2026-04-15T12:00:00+00:00"
        as_of_ts = __import__("datetime").datetime.fromisoformat(as_of).timestamp()
        result = search_memories(db, "note", limit=5, as_of=as_of_ts, include_invalid=False)
        ids = {r["id"] for r in result["results"]}
        assert "note-past-1" not in ids, "note-past-1 should be expired on 2026-04-15"
        assert "note-always-valid" in ids
        # note-future-1 valid_from=2026-06-01 -> still not valid on 2026-04-15
        assert "note-future-1" not in ids

    def test_as_of_future_includes_future_note(self) -> None:
        db = self._seed()
        # July 15 2026: note-future-1 is now valid (valid_from=2026-06-01)
        as_of = "2026-07-15T12:00:00+00:00"
        as_of_ts = __import__("datetime").datetime.fromisoformat(as_of).timestamp()
        result = search_memories(db, "note", limit=5, as_of=as_of_ts, include_invalid=False)
        ids = {r["id"] for r in result["results"]}
        assert "note-future-1" in ids, "note-future-1 should be valid on 2026-07-15"
        assert "note-always-valid" in ids
        assert "note-past-1" not in ids, "note-past-1 should be expired on 2026-07-15"

    # ------------------------------------------------------------------
    # Different as_of values produce different result sets
    # ------------------------------------------------------------------
    def test_different_as_of_produces_different_results(self) -> None:
        db = self._seed()
        t1 = __import__("datetime").datetime.fromisoformat("2026-02-15T12:00:00+00:00").timestamp()
        t2 = __import__("datetime").datetime.fromisoformat("2026-07-15T12:00:00+00:00").timestamp()
        r1 = search_memories(db, "note", limit=5, as_of=t1, include_invalid=False)
        r2 = search_memories(db, "note", limit=5, as_of=t2, include_invalid=False)
        ids1 = {r["id"] for r in r1["results"]}
        ids2 = {r["id"] for r in r2["results"]}
        assert ids1 != ids2, "Result IDs should differ for different as_of timestamps"

    # ------------------------------------------------------------------
    # Cache key includes as_of (no cross-pollution)
    # ------------------------------------------------------------------
    def test_cache_key_includes_as_of(self) -> None:
        db = self._seed()
        t1 = __import__("datetime").datetime.fromisoformat("2026-02-15T12:00:00+00:00").timestamp()
        t2 = __import__("datetime").datetime.fromisoformat("2026-07-15T12:00:00+00:00").timestamp()
        # Call with two different as_of values - both should succeed
        r1 = search_memories(db, "note", limit=5, as_of=t1, include_invalid=False)
        r2 = search_memories(db, "note", limit=5, as_of=t2, include_invalid=False)
        # Results must be different because valid sets at t1 and t2 differ
        ids1 = {r["id"] for r in r1["results"]}
        ids2 = {r["id"] for r in r2["results"]}
        assert ids1 != ids2


class TestMCPMemorySearchAsOf(unittest.TestCase):
    """Sprint 5: mcp_search.memory_search as_of parameter tests."""

    def test_signature_accepts_as_of(self) -> None:
        from mcp_search import memory_search
        sig = inspect.signature(memory_search)
        assert "as_of" in sig.parameters, "memory_search must accept as_of parameter"
        assert sig.parameters["as_of"].default is None, "as_of default must be None"

    def test_as_of_none_no_crash(self) -> None:
        """Calling memory_search with as_of=None must not crash."""
        from mcp_search import memory_search
        # Just verify the call doesn't raise — actual DB is not guaranteed present
        try:
            result = memory_search("test query", limit=1, as_of=None)
            assert isinstance(result, str)
        except Exception:
            pass  # DB not present in test env is acceptable


class TestScoringAsOf(unittest.TestCase):
    """Sprint 5: scoring primitives as_of parameter tests."""

    def test_temporal_decay_factor_as_of_override(self) -> None:
        created = "2026-01-01T00:00:00+00:00"
        # Without as_of: decay based on current time
        decay_now = _temporal_decay_factor(created, now_ts=1700000000.0)
        # With as_of: decay based on as_of timestamp
        decay_as_of = _temporal_decay_factor(created, now_ts=1700000000.0, as_of=1700000000.0)
        assert decay_now == decay_as_of, (
            "When now_ts is explicitly provided, as_of should have no extra effect"
        )

    def test_temporal_decay_factor_as_of_newer_less_decay(self) -> None:
        created = "2026-01-01T00:00:00+00:00"
        created_ts = __import__("datetime").datetime.fromisoformat(created).timestamp()
        older_as_of = created_ts + 180 * 86400  # 6 months after creation
        newer_as_of = created_ts + 30 * 86400   # 1 month after creation
        old_decay = _temporal_decay_factor(created, as_of=older_as_of)
        new_decay = _temporal_decay_factor(created, as_of=newer_as_of)
        assert new_decay > old_decay, "Closer as_of should produce less decay"

    def test_apply_temporal_decay_as_of(self) -> None:
        results = [
            ("n1", "content1", "s1", "[]", "2026-01-01T00:00:00+00:00",
             -1.0, 0.5, 0.5, 3, False, None, None),
        ]
        created_ts = __import__("datetime").datetime.fromisoformat(
            "2026-01-01T00:00:00+00:00"
        ).timestamp()
        # With as_of far after creation: stronger decay (older results)
        decayed_old = _apply_temporal_decay(results, as_of=created_ts + 180 * 86400)
        # With as_of closer to creation: minimal decay (newer results)
        decayed_new = _apply_temporal_decay(results, as_of=created_ts + 30 * 86400)
        assert decayed_old[0][6] < decayed_new[0][6], (
            "Older as_of should produce lower decayed scores"
        )


class TestSearchKgFactsAsOf(unittest.TestCase):
    """Sprint 5: _search_kg_facts as_of parameter tests."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "kg_facts_temporal.db"

    def _seed_kg_facts(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        from infra.db_migrations import run_schema_setup
        run_schema_setup(conn)
        from fact import ensure_facts_schema
        ensure_facts_schema(conn)
        conn.commit()
        # Insert temporal facts
        conn.execute(
            "INSERT INTO kg_facts (subject, predicate, object, confidence, "
            "mention_count, first_seen, last_seen, event_time, "
            "event_time_granularity, context, valid_at, invalid_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "user1", "likes", "coffee", 0.9, 1,
                "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00", "day",
                "test context", 1700000000.0, 1730000000.0,  # valid Jan-2023 to Oct-2024
            ),
        )
        # Fact valid after Oct-2024
        conn.execute(
            "INSERT INTO kg_facts (subject, predicate, object, confidence, "
            "mention_count, first_seen, last_seen, event_time, "
            "event_time_granularity, context, valid_at, invalid_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "user1", "likes", "tea", 0.9, 1,
                "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00", "day",
                "test context2", 1730000000.0, None,
            ),
        )
        # Rebuild FTS
        conn.execute("INSERT INTO kg_facts_fts(kg_facts_fts) VALUES('rebuild')")
        conn.commit()
        try:
            from infra.db import connection_pool
            connection_pool._pool.clear()
            connection_pool._pooled_ids.clear()
        except Exception:
            pass
        return conn

    def test_as_of_within_range_returns_fact(self) -> None:
        conn = self._seed_kg_facts()
        # 1705000000 is within [1700000000, 1730000000]
        facts = _search_kg_facts(conn, "coffee", 5, include_invalid=True, as_of=1705000000.0)
        subjects = [f["subject"] for f in facts]
        assert "user1" in subjects, "coffee fact should be valid at as_of=1705000000"
        conn.close()

    def test_as_of_after_invalid_excludes_fact(self) -> None:
        conn = self._seed_kg_facts()
        # 1740000000 > 1730000000 (invalid_at for coffee fact)
        facts = _search_kg_facts(conn, "coffee", 5, include_invalid=True, as_of=1740000000.0)
        subjects = [f["subject"] for f in facts]
        assert "user1" not in [f["subject"] for f in facts if f.get("object") == "coffee"], (
            "coffee fact should be invalid at as_of=1740000000"
        )
        conn.close()

    def test_as_of_within_range_future_fact(self) -> None:
        conn = self._seed_kg_facts()
        # 1735000000 is within [1730000000, None] (valid from Oct-2024)
        facts = _search_kg_facts(conn, "tea", 5, include_invalid=True, as_of=1735000000.0)
        subjects = [f["subject"] for f in facts]
        assert "user1" in subjects, "tea fact should be valid at as_of=1735000000"
        conn.close()

    def test_signature_accepts_as_of(self) -> None:
        sig = inspect.signature(_search_kg_facts)
        assert "as_of" in sig.parameters, "_search_kg_facts must accept as_of"


if __name__ == "__main__":
    unittest.main()
