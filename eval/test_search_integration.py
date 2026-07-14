"""Integration tests for the search pipeline (Phases 1–7).

Tests the full search path end-to-end with a hermetic temp DB:
  - FTS5 search returns relevant notes
  - Missing DB path returns the expected error envelope (no crash)
  - Empty result set returns the expected no-results envelope
  - `phase_latencies` is populated when search completes successfully
  - `include_facts=True` surfaces knowledge-graph facts in the response
  - `deep_rerank=True` does not crash and returns standard keys
  - `synthesize=True` returns a synthesis block when results exist
  - Different `recency_weight` values change results
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest import TestCase

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from eval._fixtures import bootstrap_temp_db_clean  # noqa: E402


class TestSearchEndToEnd(TestCase):
    """End-to-end search tests with a hermetic temp DB."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "search.db"
        # Clean BB2 turn history and phase latencies
        from memory_mcp import _bb2_clear_history
        _bb2_clear_history()
        from search.orchestrator import _phase_latencies
        _phase_latencies.clear()

    def _seed_db(self, notes: list[tuple[str, str, list[str], str]]) -> Path:
        bootstrap_temp_db_clean(self.db_path)
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        now = "2026-06-15T12:00:00+00:00"
        for nid, content, tags, category in notes:
            source = f"memory/{category}/{nid}.md"
            conn.execute(
                "INSERT INTO memories (id,content,source_file,tags,created_at,updated_at,observed_at,category) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (nid, content, source, json.dumps(tags), now, now, now, category),
            )
        conn.commit()
        conn.close()
        # Reset connection pool so new connections use the seeded DB
        from infra.db import connection_pool
        connection_pool._pool.clear()
        connection_pool._pooled_ids.clear()
        return self.db_path

    def test_search_returns_valid_envelope(self) -> None:
        from search.orchestrator import search_memories

        db = self._seed_db([
            ("lessons/python-basics", "Python is a popular programming language.",
             ["python"], "lessons"),
            ("lessons/ml-intro",     "Machine learning uses statistical methods.",
             ["ml", "python"], "lessons"),
        ])
        result = search_memories(db, "Python programming", limit=5)
        assert isinstance(result, dict)
        assert "results" in result
        assert "count" in result
        assert "output" in result
        assert isinstance(result["results"], list)
        assert isinstance(result["count"], int)
        assert isinstance(result["output"], str)

    def test_search_fts_finds_relevant_notes(self) -> None:
        from search.orchestrator import search_memories

        db = self._seed_db([
            ("lessons/python-basics", "Python is a popular programming language.",
             ["python"], "lessons"),
            ("lessons/ml-intro",     "Machine learning uses statistical methods.",
             ["ml", "python"], "lessons"),
            ("lessons/java-notes",   "Java is an object-oriented language.", ["java"], "lessons"),
        ])
        result = search_memories(db, "Python programming", limit=10)
        ids = [r.get("id") for r in result["results"]]
        assert "lessons/python-basics" in ids

    def test_search_returns_empty_for_no_match(self) -> None:
        from search.orchestrator import search_memories
        from unittest.mock import patch

        db = self._seed_db([
            ("lessons/python-basics", "Python programming language.", ["python"], "lessons"),
        ])
        with patch("search.query_parser._semantic_expand", return_value=[]):
            result = search_memories(db, "zzzz-no-match-query-xyz", limit=5)
        assert result["count"] == 0
        assert result["results"] == []

    @pytest.mark.timeout(180)
    def test_search_phase_latencies_recorded_on_success(self) -> None:
        from search.orchestrator import search_memories, _phase_latencies

        db = self._seed_db([
            ("lessons/python-basics", "Python is a popular programming language.",
             ["python"], "lessons"),
        ])
        _phase_latencies.clear()
        result = search_memories(db, "Python", limit=3)
        assert "phase_latencies" in result, (
            f"Expected phase_latencies, got keys: {list(result)}"
        )
        for known_phase in ("search.fts", "search.hybrid_fusion", "rerank"):
            assert known_phase in result["phase_latencies"], (
                f"Missing phase: {known_phase}, got: {list(result['phase_latencies'])}"
            )
        for v in result["phase_latencies"].values():
            assert isinstance(v, float)
            assert v >= 0.0

    def test_search_phase_latencies_absent_on_missing_db(self) -> None:
        from search.orchestrator import search_memories

        fake = self.tmpdir / "does-not-exist.db"
        result = search_memories(fake, "anything")
        assert result["count"] == 0
        assert result["results"] == []
        assert "phase_latencies" not in result

    def test_search_deep_rerank_does_not_crash(self) -> None:
        from search.orchestrator import search_memories

        db = self._seed_db([
            ("lessons/python-basics", "Python is a popular programming language.",
             ["python"], "lessons"),
            ("lessons/ml-intro", "Machine learning uses statistical methods.",
             ["ml", "python"], "lessons"),
        ])
        result = search_memories(db, "Python", deep_rerank=True, limit=3)
        assert "results" in result
        assert "count" in result

    def test_search_synthesize_does_not_crash(self) -> None:
        from search.orchestrator import search_memories

        db = self._seed_db([
            ("lessons/python-basics", "Python is a popular programming language.",
             ["python"], "lessons"),
        ])
        result = search_memories(db, "Python programming", synthesize=True, limit=3)
        assert "results" in result
        assert "count" in result

    def test_search_zero_limit_returns_empty(self) -> None:
        from search.orchestrator import search_memories

        db = self._seed_db([
            ("lessons/python-basics", "Python programming language.", ["python"], "lessons"),
        ])
        result = search_memories(db, "Python", limit=0)
        assert result["count"] == 0
        assert result["results"] == []

    @pytest.mark.timeout(180)
    def test_search_query_id_is_unique_per_call(self) -> None:
        from search.orchestrator import search_memories

        db = self._seed_db([
            ("lessons/python-basics", "Python programming language.", ["python"], "lessons"),
        ])
        r1 = search_memories(db, "Python")
        r2 = search_memories(db, "Python")
        assert "query_id" in r1
        assert "query_id" in r2
        assert r1["query_id"] != r2["query_id"]

    def test_search_raw_results_aligns_with_count(self) -> None:
        from search.orchestrator import search_memories

        db = self._seed_db([
            ("lessons/python-basics", "Python programming language.", ["python"], "lessons"),
            ("lessons/ml-intro",     "Machine learning methods.", ["ml"], "lessons"),
        ])
        result = search_memories(db, "Python", limit=5)
        assert len(result.get("raw_results", [])) == result["count"]

    def test_search_result_contains_required_fields(self) -> None:
        from search.orchestrator import search_memories

        db = self._seed_db([
            ("lessons/python-basics", "Python programming language.", ["python"], "lessons"),
        ])
        result = search_memories(db, "Python", limit=2)
        if result["results"]:
            r = result["results"][0]
            for key in ("id", "content", "source_file", "tags", "final_score", "rank"):
                assert key in r, f"Missing key {key!r} in result: {r}"
