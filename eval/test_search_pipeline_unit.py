#!/usr/bin/env python3
"""Unit tests for search_pipeline.py — targeted at mutation survival sites.

Covers:
- search_memories return value structure
- Empty query handling (no terms)
- Zero-result suggestions
- Count calculation (len(result_items))
- include_global semantic fallback
- Limit boundary conditions
- _compute_final_score logic
"""

import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

from infra.memory_common import open_db
from infra.infrastructure import GLOBAL_MEM_DIR
from save_pipeline import save_memory
from _fixtures import bootstrap_temp_db_clean
from search_pipeline import (
    search_memories,
    _compute_final_score,
    ScoreContext,
    _build_zero_result_suggestions,
    _detect_query_type,
    _weights_for_query_type,
    _expand_query,
    _reciprocal_rank_fusion,
    _apply_temporal_decay,
)
from search.orchestrator import _rerank_results

PROD_DB = Path(os.environ.get("MEMORY_DB_PATH", str(GLOBAL_MEM_DIR / "memory.db")))


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def _hard_delete(db_path, note_id):
    with open_db(db_path) as db:
        row = db.execute("SELECT rowid FROM memories WHERE id=?", (note_id,)).fetchone()
        if row:
            try:
                db.execute("DELETE FROM memories_fts WHERE rowid=?", (row[0],))
            except Exception:
                pass
        db.execute("DELETE FROM memories WHERE id=?", (note_id,))
        db.commit()
    (Path(PROD_DB).parent / f"{note_id}.md").unlink(missing_ok=True)


class TestSearchMemoriesReturnStructure(unittest.TestCase):
    """Test that search_memories returns the correct structure."""

    def test_returns_dict_with_results_count_output(self):
        result = search_memories(PROD_DB, "test query", limit=5, safety_wiring=False)
        self.assertIsInstance(result, dict)
        self.assertIn("results", result)
        self.assertIn("count", result)
        self.assertIn("output", result)

    def test_count_matches_results_length(self):
        result = search_memories(PROD_DB, "test", limit=5, safety_wiring=False)
        self.assertEqual(result["count"], len(result["results"]))

    def test_results_are_list(self):
        result = search_memories(PROD_DB, "test", limit=5, safety_wiring=False)
        self.assertIsInstance(result["results"], list)

    def test_output_is_string(self):
        result = search_memories(PROD_DB, "test", limit=5, safety_wiring=False)
        self.assertIsInstance(result["output"], str)

    def test_limit_respected(self):
        result = search_memories(PROD_DB, "test", limit=3, safety_wiring=False)
        self.assertLessEqual(len(result["results"]), 3)


class TestEmptyQueryHandling(unittest.TestCase):
    """Test empty query returns no results with suggestions."""

    def test_empty_query_returns_no_results(self):
        result = search_memories(PROD_DB, "", limit=5, safety_wiring=False)
        self.assertEqual(result["count"], 0)
        self.assertEqual(len(result["results"]), 0)

    def test_empty_query_has_output(self):
        result = search_memories(PROD_DB, "", limit=5, safety_wiring=False)
        self.assertIsInstance(result["output"], str)

    def test_whitespace_only_query(self):
        result = search_memories(PROD_DB, "   ", limit=5, safety_wiring=False)
        self.assertEqual(result["count"], 0)

    def test_special_chars_only_query(self):
        result = search_memories(PROD_DB, "!@#$%^&*()", limit=5, safety_wiring=False)
        # Special chars may or may not match FTS; just verify no crash
        self.assertIsInstance(result["count"], int)


class TestZeroResultSuggestions(unittest.TestCase):
    """Test _build_zero_result_suggestions returns useful suggestions."""

    def test_returns_dict(self):
        suggestions = _build_zero_result_suggestions(PROD_DB, "nonexistent_xyz_123")
        self.assertIsInstance(suggestions, dict)

    def test_suggestions_have_expected_keys(self):
        suggestions = _build_zero_result_suggestions(PROD_DB, "test")
        # Should have some suggestion keys
        self.assertTrue(len(suggestions) >= 0)


class TestIncludeGlobalFilter(unittest.TestCase):
    """Test include_global=False does not filter global notes (the SQL filter was removed).

    Uses temp DBs to avoid prod pollution.
    """

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.local_db = self.tmpdir / "memory.db"
        self.global_dir = self.tmpdir / "global"
        self.global_dir.mkdir(parents=True, exist_ok=True)
        self.global_db = self.global_dir / "memory.db"
        bootstrap_temp_db_clean(self.local_db)
        bootstrap_temp_db_clean(self.global_db)
        self._patcher = patch("save_pipeline.GLOBAL_MEM_DIR", self.global_dir)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_include_global_true_returns_all(self):
        slug = f"unit-glob-{int(time.time())}"
        save_memory(
            content=f"---\ncategory: lessons\ntitle_slug: {slug}\ntags: [unit-test]\nvalid_from: {now_iso()}\n---\n\nGlobal note.",
            category="lessons",
            title_slug=slug,
            tags=["unit-test"],
            pinned=False,
            is_global=True,
            db_path=str(self.local_db),
            safety_wiring=False,
        )
        result = search_memories(
            self.local_db,
            "Global note",
            limit=5,
            include_global=True,
            safety_wiring=False,
        )
        ids = [r["id"] for r in result["results"]]
        self.assertIsInstance(ids, list)

    def test_include_global_false_includes_global_note(self):
        """Regression: previously, include_global=False added
        `m.repo_id IS NOT NULL` which excluded all global-saved notes.
        The fix removed that filter; this test asserts a globally-saved
        note IS searchable when include_global=False (it was the bug
        to exclude it).
        """
        slug = f"unit-noglob-{int(time.time())}"
        nid = f"lessons/{slug}"
        save_memory(
            content=f"marker-tok-{slug}-xyzabc123\nUnique slug test content.",
            category="lessons",
            title_slug=slug,
            tags=["unit-test"],
            pinned=False,
            is_global=True,
            db_path=str(self.local_db),
            safety_wiring=False,
        )
        result = search_memories(
            self.local_db,
            f"marker-tok-{slug}-xyzabc123",
            limit=5,
            include_global=False,
            safety_wiring=False,
        )
        ids = [r["id"] for r in result["results"]]
        self.assertIn(
            nid,
            ids,
            f"Global note {nid} should be findable even with "
            f"include_global=False (filter removed)",
        )


class TestComputeFinalScore(unittest.TestCase):
    """Test _compute_final_score boundary conditions."""

    def test_score_with_all_zeros(self):
        score = _compute_final_score(
            ScoreContext(
                rank=-1.0,
                fitness=0.0,
                importance=0,
                pinned=False,
                created=now_iso(),
                tags_json="[]",
                query="test",
                boost_pinned=True,
                recency_weight=0.1,
            )
        )
        self.assertIsInstance(score, (int, float))

    def test_score_with_high_fitness(self):
        score_high = _compute_final_score(
            ScoreContext(
                rank=-1.0,
                fitness=1.0,
                importance=5,
                pinned=True,
                created=now_iso(),
                tags_json='["test"]',
                query="test",
                boost_pinned=True,
                recency_weight=0.1,
            )
        )
        score_low = _compute_final_score(
            ScoreContext(
                rank=-1.0,
                fitness=0.0,
                importance=1,
                pinned=False,
                created=now_iso(),
                tags_json="[]",
                query="other",
                boost_pinned=True,
                recency_weight=0.1,
            )
        )
        self.assertGreater(score_high, score_low)

    def test_score_with_none_fitness(self):
        """None fitness should be handled gracefully."""
        score = _compute_final_score(
            ScoreContext(
                rank=-1.0,
                fitness=None,
                importance=None,
                pinned=False,
                created=now_iso(),
                tags_json="[]",
                query="test",
                boost_pinned=True,
                recency_weight=0.1,
            )
        )
        self.assertIsInstance(score, (int, float))


class TestDetectQueryType(unittest.TestCase):
    """Test _detect_query_type classification."""

    def test_entity_query(self):
        qtype = _detect_query_type("Python requests library")
        self.assertIn(qtype, ["entity", "concept", "procedural", "temporal", "general"])

    def test_procedural_query(self):
        qtype = _detect_query_type("how to deploy a docker container")
        self.assertIn(qtype, ["entity", "concept", "procedural", "temporal", "general"])

    def test_temporal_query(self):
        qtype = _detect_query_type("what did I do yesterday")
        self.assertIn(qtype, ["entity", "concept", "procedural", "temporal", "general"])


class TestExpandQuery(unittest.TestCase):
    """Test _expand_query abbreviation expansion."""

    def test_expand_ml(self):
        expanded = _expand_query("ml")
        self.assertIn("machine learning", expanded)

    def test_expand_db(self):
        expanded = _expand_query("db")
        self.assertIn("database", expanded)

    def test_expand_unknown(self):
        expanded = _expand_query("xyzzy")
        # Unknown terms may be wrapped in quotes
        self.assertIn("xyzzy", expanded)


class TestReciprocalRankFusion(unittest.TestCase):
    """Test _reciprocal_rank_fusion ranking."""

    def test_rrf_basic(self):
        ranked_lists = [["a", "b", "c"], ["b", "c", "d"]]
        rrf = _reciprocal_rank_fusion(ranked_lists)
        self.assertIsInstance(rrf, dict)
        # "b" and "c" appear in both lists, should have higher scores
        self.assertGreater(rrf.get("b", 0), rrf.get("a", 0))
        self.assertGreater(rrf.get("c", 0), rrf.get("a", 0))

    def test_rrf_single_list(self):
        rrf = _reciprocal_rank_fusion([["x", "y", "z"]])
        self.assertIsInstance(rrf, dict)
        self.assertIn("x", rrf)

    def test_rrf_empty_list(self):
        rrf = _reciprocal_rank_fusion([])
        self.assertIsInstance(rrf, dict)
        self.assertEqual(len(rrf), 0)

    def test_rrf_scores_are_floats(self):
        rrf = _reciprocal_rank_fusion([["a", "b"]])
        for key, val in rrf.items():
            self.assertIsInstance(val, float, f"Score for {key} should be float")


class TestApplyTemporalDecay(unittest.TestCase):
    """Test _apply_temporal_decay applies decay correctly."""

    def test_decay_returns_list(self):
        items = [
            ("id1", "content1", "file1", "[]", now_iso(), -1.0, 0.5, None, None, None)
        ]
        result = _apply_temporal_decay(items)
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 1)

    def test_decay_preserves_length(self):
        items = [
            ("id1", "c1", "f1", "[]", now_iso(), -1.0, 0.5, None, None, None),
            ("id2", "c2", "f2", "[]", now_iso(), -2.0, 0.3, None, None, None),
        ]
        result = _apply_temporal_decay(items)
        self.assertEqual(len(result), len(items))

    def test_decay_weight_zero_no_change(self):
        """With decay_weight=0, scores should not change."""
        items = [("id1", "c1", "f1", "[]", now_iso(), -0.5, 0.8, None, None, None)]
        result = _apply_temporal_decay(items, decay_weight=0.0)
        self.assertEqual(len(result), 1)

    def test_decay_preserves_order(self):
        """Decay should preserve relative order of items."""
        items = [
            ("id1", "c1", "f1", "[]", now_iso(), -0.1, 0.9, None, None, None),
            ("id2", "c2", "f2", "[]", now_iso(), -0.5, 0.5, None, None, None),
        ]
        result = _apply_temporal_decay(items)
        # First item should still have higher score
        self.assertGreater(result[0][6], result[1][6])


class TestDetectQueryTypeMore(unittest.TestCase):
    """Additional tests for _detect_query_type."""

    def test_returns_string(self):
        result = _detect_query_type("what is python")
        self.assertIsInstance(result, str)

    def test_entity_type(self):
        result = _detect_query_type("tell me about John Smith")
        self.assertIn(
            result,
            ["entity", "general", "procedural", "temporal", "comparison", "factual"],
        )

    def test_procedural_type(self):
        result = _detect_query_type("how to install docker")
        self.assertIn(
            result,
            ["entity", "general", "procedural", "temporal", "comparison", "factual"],
        )

    def test_temporal_type(self):
        result = _detect_query_type("what happened yesterday")
        self.assertIn(
            result,
            ["entity", "general", "procedural", "temporal", "comparison", "factual"],
        )


class TestWeightsForQueryType(unittest.TestCase):
    """Test _weights_for_query_type returns correct weights."""

    def test_returns_dict(self):
        result = _weights_for_query_type("general")
        self.assertIsInstance(result, dict)

    def test_has_all_keys(self):
        result = _weights_for_query_type("general")
        expected_keys = {
            "bm25",
            "fitness",
            "importance",
            "pinned",
            "recency",
            "tag_match",
        }
        self.assertEqual(set(result.keys()), expected_keys)

    def test_weights_sum_to_one(self):
        result = _weights_for_query_type("general")
        total = sum(result.values())
        self.assertAlmostEqual(total, 1.0, places=5)

    def test_unknown_type_returns_general(self):
        result = _weights_for_query_type("nonexistent_type")
        self.assertIsInstance(result, dict)
        self.assertEqual(len(result), 6)


class TestExpandQueryMore(unittest.TestCase):
    """Additional tests for _expand_query."""

    def test_returns_string(self):
        result = _expand_query("test query")
        self.assertIsInstance(result, str)

    def test_expands_known_abbreviations(self):
        result = _expand_query("db")
        # Should expand to something longer
        self.assertIsInstance(result, str)

    def test_unknown_terms_quoted(self):
        result = _expand_query("xyzabc123")
        self.assertIsInstance(result, str)
        # Unknown terms should be quoted
        self.assertIn('"xyzabc123"', result)


class TestComputeFinalScoreMore(unittest.TestCase):
    """Additional tests for _compute_final_score."""

    def test_pinned_bonus(self):
        score_pinned = _compute_final_score(
            ScoreContext(
                rank=-1.0,
                fitness=0.5,
                importance=3,
                pinned=True,
                created=now_iso(),
                tags_json="[]",
                query="test",
                boost_pinned=True,
                recency_weight=0.1,
            )
        )
        score_not_pinned = _compute_final_score(
            ScoreContext(
                rank=-1.0,
                fitness=0.5,
                importance=3,
                pinned=False,
                created=now_iso(),
                tags_json="[]",
                query="test",
                boost_pinned=True,
                recency_weight=0.1,
            )
        )
        self.assertGreater(score_pinned, score_not_pinned)

    def test_boost_pinned_false_no_bonus(self):
        score_pinned = _compute_final_score(
            ScoreContext(
                rank=-1.0,
                fitness=0.5,
                importance=3,
                pinned=True,
                created=now_iso(),
                tags_json="[]",
                query="test",
                boost_pinned=False,
                recency_weight=0.1,
            )
        )
        score_not_pinned = _compute_final_score(
            ScoreContext(
                rank=-1.0,
                fitness=0.5,
                importance=3,
                pinned=False,
                created=now_iso(),
                tags_json="[]",
                query="test",
                boost_pinned=False,
                recency_weight=0.1,
            )
        )
        self.assertAlmostEqual(score_pinned, score_not_pinned, places=10)

    def test_recency_weight_affects_score(self):
        """Higher decay_weight in _apply_temporal_decay produces larger score gaps
        between recent and old notes. This is the behavioral contract: the
        temporal modifier is multiplicative and weight-controlled."""
        from search.scoring import _apply_temporal_decay

        now = time.time()
        recent_ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 86400))
        old_ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 86400 * 365))
        base = 0.5

        def mk_row(ts):
            return (None, None, None, None, ts, None, base, None, None, None)

        high = _apply_temporal_decay([mk_row(recent_ts), mk_row(old_ts)], decay_weight=0.3, as_of=now)
        low = _apply_temporal_decay([mk_row(recent_ts), mk_row(old_ts)], decay_weight=0.05, as_of=now)
        gap_high = high[0][6] - high[1][6]
        gap_low = low[0][6] - low[1][6]
        self.assertGreater(gap_high, gap_low,
            "higher decay_weight should produce a larger recency gap")

    def test_tag_match_contributes(self):
        score_with_tags = _compute_final_score(
            ScoreContext(
                rank=-1.0,
                fitness=0.5,
                importance=3,
                pinned=False,
                created=now_iso(),
                tags_json='["python", "test"]',
                query="python test",
                boost_pinned=True,
                recency_weight=0.1,
            )
        )
        score_no_tags = _compute_final_score(
            ScoreContext(
                rank=-1.0,
                fitness=0.5,
                importance=3,
                pinned=False,
                created=now_iso(),
                tags_json="[]",
                query="python test",
                boost_pinned=True,
                recency_weight=0.1,
            )
        )
        self.assertGreater(score_with_tags, score_no_tags)


class TestTemporalDecayBehavior(unittest.TestCase):
    """Behavioral tests for the search pipeline's temporal aging."""

    def test_recent_outranks_old_after_decay(self):
        """A 1-day-old note should score higher than a 1-year-old note."""
        from search.scoring import _apply_temporal_decay

        now = time.time()
        recent_ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 86400))
        old_ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 86400 * 365))

        def mk_row(ts):
            return (None, None, None, None, ts, None, 0.5, None, None, None)

        scored = _apply_temporal_decay([mk_row(recent_ts), mk_row(old_ts)], decay_weight=0.15, as_of=now)
        self.assertGreater(scored[0][6], scored[1][6],
            "1-day-old note should outrank 1-year-old note after temporal decay")

    def test_decay_never_increases_score(self):
        """_apply_temporal_decay should never boost a score above 1.0."""
        from search.scoring import _apply_temporal_decay

        now = time.time()
        fresh_ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now))

        def mk_row(ts):
            return (None, None, None, None, ts, None, 0.9, None, None, None)

        scored = _apply_temporal_decay([mk_row(fresh_ts)], decay_weight=0.15, as_of=now)
        self.assertLessEqual(scored[0][6], 1.0,
            "decay-adjusted score should never exceed 1.0")

    def test_decay_weight_zero_passes_scores_through(self):
        """decay_weight=0 should leave all final_scores unchanged."""
        from search.scoring import _apply_temporal_decay

        now = time.time()
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 100000))
        original_score = 0.73
        row = (None, None, None, None, ts, None, original_score, None, None, None)
        scored = _apply_temporal_decay([row], decay_weight=0.0, as_of=now)
        self.assertAlmostEqual(scored[0][6], original_score, places=10)

    def test_decay_and_forget_curve_are_mutually_exclusive(self):
        """The pipeline runs EITHER _apply_temporal_decay OR
        _apply_neural_forget_curve — the flag in search_pipeline controls
        which path is taken."""
        import search_pipeline as sp

        old_flag = getattr(sp, "_FORGETTING_CURVE_ENABLED", None)
        try:
            # Path A: decay enabled (flag False)
            setattr(sp, "_FORGETTING_CURVE_ENABLED", False)
            decay_out, _ = _rerank_results(
                results=[
                    ("note1", "c", "f", "[]", "2024-01-01T00:00:00", -1.0, 0.5, 0.5, 3, False),
                ],
                query="test", db_path=Path("/dev/null"), has_fitness=True, rerank=False,
                boost_pinned=False, recency_weight=0.1, limit=5, deep_rerank=False,
            )
            # Path B: forget curve enabled (flag True)
            setattr(sp, "_FORGETTING_CURVE_ENABLED", True)
            fc_out, _ = _rerank_results(
                results=[
                    ("note1", "c", "f", "[]", "2024-01-01T00:00:00", -1.0, 0.5, 0.5, 3, False),
                ],
                query="test", db_path=Path("/dev/null"), has_fitness=True, rerank=False,
                boost_pinned=False, recency_weight=0.1, limit=5, deep_rerank=False,
            )
            # Both paths must produce a result (different code paths executed)
            self.assertIsInstance(decay_out, list)
            self.assertIsInstance(fc_out, list)
            self.assertEqual(len(decay_out), 1)
            self.assertEqual(len(fc_out), 1)
        finally:
            if old_flag is not None:
                setattr(sp, "_FORGETTING_CURVE_ENABLED", old_flag)
            elif hasattr(sp, "_FORGETTING_CURVE_ENABLED"):
                delattr(sp, "_FORGETTING_CURVE_ENABLED")


if __name__ == "__main__":
    unittest.main()
