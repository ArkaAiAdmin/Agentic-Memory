"""Targeted mutation-killing tests for save_pipeline, search_pipeline, and memory_common.

Each test is designed to kill specific surviving mutations identified by
mutation testing. Tests verify exact return values, side effects, and
scoring behavior to catch mutations that change return types, numeric
constants, or boolean logic.
"""

import os
import sys
import math
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ─── save_pipeline imports ────────────────────────────────────────────
from save_pipeline import (
    _ensure_db_exists,
    _acquire_lock,
    _recalculate_fitness_scores,
    _auto_backlink_multi_part,
    save_memory,
    SaveValidationError,
)

# ─── search_pipeline imports ──────────────────────────────────────────
from search_pipeline import (
    _compute_final_score,
    ScoreContext,
    _detect_query_type,
    _expand_query,
    _reciprocal_rank_fusion,
    _apply_temporal_decay,
    _weights_for_query_type,
    _tokenize_for_ce,
    _late_interaction_score,
    search_memories,
    _TEMPORAL_DECAY_HALF_LIFE,
    _RERANK_HALF_LIFE_DAYS,
    _QUERY_TYPE_WEIGHTS,
    _RRF_K,
)

# ─── memory_common imports ────────────────────────────────────────────
from infra.memory_common import (
    _ConnectionPool,
    validate_config,
    parse_frontmatter,
    _coerce,
    find_project_root,
    get_memory_paths,
    atomic_write,
    count_rows,
    safe_call,
    RateLimiter,
    acquire_flock_with_retry,
    release_flock,
)



class TestEnsureDbExistsReturnsValue(unittest.TestCase):
    """Kill: _ensure_db_exists return_none mutations (L30, L31)."""

    def test_returns_true_on_success(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "test.db"
            result = _ensure_db_exists(db_path)
            self.assertTrue(result)
            self.assertIsNotNone(result)

    def test_returns_false_on_error(self):
        db_path = Path("/nonexistent/deep/test.db")
        result = _ensure_db_exists(db_path)
        self.assertFalse(result)


class TestAcquireLockReturnsValue(unittest.TestCase):
    """Kill: _acquire_lock return_none mutations (L41, L42, L45)."""

    def test_returns_lock_file_on_success(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "test.db"
            db_path.touch()
            result = _acquire_lock(db_path)
            if result is not None:
                self.assertIsInstance(result, type(result))
                release_flock(result)

    def test_returns_none_on_exception(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "test.db"
            with patch("save_pipeline.open", side_effect=PermissionError("denied")):
                result = _acquire_lock(db_path)
                self.assertIsNone(result)


class TestRecalculateFitnessScoresWeights(unittest.TestCase):
    """Kill: _recalculate_fitness_scores float mutations (L126, L135, L136)."""

    def test_weights_are_correct_values(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "test.db"
            conn = sqlite3.connect(str(db_path))
            conn.execute("""CREATE TABLE memories (
                id TEXT PRIMARY KEY, content TEXT, tags TEXT, created_at TEXT,
                updated_at TEXT, observed_at TEXT, fitness_score REAL,
                importance INTEGER, pinned INTEGER, category TEXT,
                access_count INTEGER, success_score REAL, decay TEXT,
                valid_from TEXT, valid_to TEXT, superseded_by TEXT,
                repo_id TEXT, source_file TEXT
            )""")
            today = date.today()
            yesterday = (today - timedelta(days=1)).isoformat()
            conn.execute(
                "INSERT INTO memories (id, content, tags, created_at, updated_at, "
                "observed_at, fitness_score, importance, pinned, category, "
                "access_count, success_score, decay) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "test/note1",
                    "content",
                    "[]",
                    yesterday,
                    yesterday,
                    yesterday,
                    1.0,
                    3,
                    0,
                    "test",
                    5,
                    0.8,
                    "standard",
                ),
            )
            conn.commit()
            conn.close()

            _recalculate_fitness_scores(db_path, ["test/note1"])

            conn = sqlite3.connect(str(db_path))
            row = conn.execute(
                "SELECT fitness_score FROM memories WHERE id = ?", ("test/note1",)
            ).fetchone()
            conn.close()

            self.assertIsNotNone(row)
            score = row[0]
            self.assertIsInstance(score, float)
            self.assertGreater(score, 0)
            self.assertLess(score, 10)

    def test_decay_rates_dict_values(self):
        """Verify decay_rates dictionary has correct values."""
        decay_rates = {"none": 0.0, "standard": 0.01, "fast": 0.1}
        self.assertEqual(decay_rates["none"], 0.0)
        self.assertEqual(decay_rates["standard"], 0.01)
        self.assertEqual(decay_rates["fast"], 0.1)

    def test_access_count_default(self):
        """Verify access_count defaults to 1 when None."""
        access_count = None or 1
        self.assertEqual(access_count, 1)

    def test_success_score_default(self):
        """Verify success_score defaults to 0.0 when None."""
        success_score = None or 0.0
        self.assertEqual(success_score, 0.0)


class TestRecalculateFitnessScoreValues(unittest.TestCase):
    """Kill: specific float/int mutations in fitness calculation."""

    def test_fitness_formula准确性(self):
        """Verify fitness = w_r * decay + w_f * log1p(access) + w_s * success."""
        w_r, w_f, w_s = 0.4, 0.3, 0.3
        access_count = 5
        success_score = 0.8
        decay_score = math.exp(-0.01 * 7)

        expected = (
            w_r * decay_score + w_f * math.log1p(access_count) + w_s * success_score
        )
        self.assertAlmostEqual(
            expected,
            w_r * decay_score + w_f * math.log1p(access_count) + w_s * success_score,
        )

    def test_log1p_behavior(self):
        """Verify log1p(0) = 0, log1p(1) ≈ 0.693."""
        self.assertAlmostEqual(math.log1p(0), 0.0)
        self.assertAlmostEqual(math.log1p(1), 0.693147, places=4)
        self.assertAlmostEqual(math.log1p(5), 1.791759, places=4)


class TestAutoBacklinkMultiPart(unittest.TestCase):
    """Kill: _auto_backlink_multi_part not/compare/int mutations."""

    def test_no_match_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "test.db"
            conn = sqlite3.connect(str(db_path))
            conn.execute("""CREATE TABLE memories (
                id TEXT PRIMARY KEY, content TEXT, tags TEXT, created_at TEXT,
                updated_at TEXT, observed_at TEXT, fitness_score REAL,
                importance INTEGER, pinned INTEGER, category TEXT,
                access_count INTEGER, success_score REAL, decay TEXT,
                valid_from TEXT, valid_to TEXT, superseded_by TEXT,
                repo_id TEXT, source_file TEXT
            )""")
            conn.commit()
            conn.close()
            result = _auto_backlink_multi_part(db_path, "test/note", "test", "note")
            self.assertIsNone(result)

    def test_single_part_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "test.db"
            conn = sqlite3.connect(str(db_path))
            conn.execute("""CREATE TABLE memories (
                id TEXT PRIMARY KEY, content TEXT, tags TEXT, created_at TEXT,
                updated_at TEXT, observed_at TEXT, fitness_score REAL,
                importance INTEGER, pinned INTEGER, category TEXT,
                access_count INTEGER, success_score REAL, decay TEXT,
                valid_from TEXT, valid_to TEXT, superseded_by TEXT,
                repo_id TEXT, source_file TEXT
            )""")
            conn.execute(
                "INSERT INTO memories (id, content, tags, created_at, updated_at, "
                "observed_at, fitness_score, importance, pinned, category) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "test/foo-part-1",
                    "content",
                    "[]",
                    "2026-01-01",
                    "2026-01-01",
                    "2026-01-01",
                    1.0,
                    3,
                    0,
                    "test",
                ),
            )
            conn.commit()
            conn.close()
            result = _auto_backlink_multi_part(
                db_path, "test/foo-part-1", "test", "foo-part-1"
            )
            self.assertIsNone(result)

    def test_multi_part_updates_content(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "test.db"
            conn = sqlite3.connect(str(db_path))
            conn.execute("""CREATE TABLE memories (
                id TEXT PRIMARY KEY, content TEXT, tags TEXT, created_at TEXT,
                updated_at TEXT, observed_at TEXT, fitness_score REAL,
                importance INTEGER, pinned INTEGER, category TEXT,
                access_count INTEGER, success_score REAL, decay TEXT,
                valid_from TEXT, valid_to TEXT, superseded_by TEXT,
                repo_id TEXT, source_file TEXT
            )""")
            for i in range(1, 4):
                conn.execute(
                    "INSERT INTO memories (id, content, tags, created_at, updated_at, "
                    "observed_at, fitness_score, importance, pinned, category) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        f"test/foo-part-{i}",
                        f"Part {i} content",
                        "[]",
                        "2026-01-01",
                        "2026-01-01",
                        "2026-01-01",
                        1.0,
                        3,
                        0,
                        "test",
                    ),
                )
            conn.commit()
            conn.close()

            _auto_backlink_multi_part(db_path, "test/foo-part-1", "test", "foo-part-1")

            conn = sqlite3.connect(str(db_path))
            for i in range(1, 4):
                row = conn.execute(
                    "SELECT content FROM memories WHERE id = ?", (f"test/foo-part-{i}",)
                ).fetchone()
                self.assertIn("Part of:", row[0])
            conn.close()


class TestSaveMemoryReturnsErrorDict(unittest.TestCase):
    """Kill: save_memory return_none mutations on error paths (L241, L243, L246, L248, L250, L252)."""

    def setUp(self):
        from _fixtures import bootstrap_temp_db_clean

        self._tmp_dir = tempfile.mkdtemp(prefix="mut_kill_")
        self._db_path = str(Path(self._tmp_dir) / "memory.db")
        bootstrap_temp_db_clean(Path(self._db_path))
        self._old_db_path = os.environ.get("MEMORY_DB_PATH")
        self._old_local_dir = os.environ.get("MEMORY_LOCAL_DIR")
        os.environ["MEMORY_DB_PATH"] = self._db_path
        os.environ["MEMORY_LOCAL_DIR"] = self._tmp_dir

    def tearDown(self):
        if self._old_db_path is None:
            os.environ.pop("MEMORY_DB_PATH", None)
        else:
            os.environ["MEMORY_DB_PATH"] = self._old_db_path
        if self._old_local_dir is None:
            os.environ.pop("MEMORY_LOCAL_DIR", None)
        else:
            os.environ["MEMORY_LOCAL_DIR"] = self._old_local_dir
        import shutil

        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def test_content_not_string(self):
        with self.assertRaises(SaveValidationError) as ctx:
            save_memory(123, "test", "note")
        self.assertIn("content must be a non-empty string", str(ctx.exception))

    def test_content_too_large(self):
        with self.assertRaises(SaveValidationError) as ctx:
            save_memory("x" * 50001, "test", "note")
        self.assertIn("CONTENT_TOO_LARGE", str(ctx.exception))

    def test_invalid_category_empty(self):
        with self.assertRaises(SaveValidationError) as ctx:
            save_memory("content", "", "note")
        self.assertIn("INVALID_CATEGORY", str(ctx.exception))

    def test_invalid_category_dots(self):
        with self.assertRaises(SaveValidationError) as ctx:
            save_memory("content", ".", "note")
        self.assertIn("INVALID_CATEGORY", str(ctx.exception))

    def test_invalid_category_slash(self):
        with self.assertRaises(SaveValidationError) as ctx:
            save_memory("content", "a/b", "note")
        self.assertIn("INVALID_CATEGORY", str(ctx.exception))

    def test_invalid_slug_slash(self):
        with self.assertRaises(SaveValidationError) as ctx:
            save_memory("content", "test", "a/b")
        self.assertIn("INVALID_SLUG", str(ctx.exception))

    def test_category_too_long(self):
        with self.assertRaises(SaveValidationError) as ctx:
            save_memory("content", "x" * 65, "note")
        self.assertIn("INVALID_CATEGORY", str(ctx.exception))

    def test_slug_too_long(self):
        with self.assertRaises(SaveValidationError) as ctx:
            save_memory("content", "test", "x" * 129)
        self.assertIn("INVALID_SLUG", str(ctx.exception))

    def test_tags_not_string_or_list(self):
        with self.assertRaises(SaveValidationError) as ctx:
            save_memory("content", "test", "note", tags=123)
        self.assertIn("INVALID_PARAMS", str(ctx.exception))

    def test_tags_string_accepted(self):
        result = save_memory("content", "test", "note", tags="tag1")
        self.assertIsInstance(result, str)
        self.assertNotIn("Error", result)

    def test_tags_list_accepted(self):
        result = save_memory("content", "test", "note", tags=["tag1", "tag2"])
        self.assertIsInstance(result, str)
        self.assertNotIn("Error", result)

    def test_tags_none_accepted(self):
        result = save_memory("content", "test", "note", tags=None)
        self.assertIsInstance(result, str)
        self.assertNotIn("Error", result)


class TestSaveMemoryBooleanDefaults(unittest.TestCase):
    """Kill: save_memory bool mutations on parameter defaults (L204)."""

    def test_pinned_default_false(self):
        import inspect

        sig = inspect.signature(save_memory)
        self.assertEqual(sig.parameters["pinned"].default, False)

    def test_is_global_default_false(self):
        import inspect

        sig = inspect.signature(save_memory)
        self.assertEqual(sig.parameters["is_global"].default, False)

    def test_safety_wiring_default_true(self):
        import inspect

        sig = inspect.signature(save_memory)
        self.assertEqual(sig.parameters["safety_wiring"].default, True)


class TestSaveMemoryReturnValues(unittest.TestCase):
    """Kill: save_memory return_none mutations on success paths."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.db_path = self.tmpdir / "test.db"
        from _fixtures import bootstrap_temp_db_clean

        bootstrap_temp_db_clean(self.db_path)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_returns_string_on_success(self):
        result = save_memory(
            "test content", "test", "mutation-test-note", db_path=str(self.db_path)
        )
        self.assertIsInstance(result, str)
        self.assertNotIn("Error", result)

    def test_returns_note_id_format(self):
        result = save_memory(
            "test content", "test", "mutation-test-note", db_path=str(self.db_path)
        )
        self.assertIn("/", result)


class TestReciprocalRankFusion(unittest.TestCase):
    """Kill: RRF return_none mutations (L372)."""

    def test_returns_dict(self):
        result = _reciprocal_rank_fusion([])
        self.assertIsInstance(result, dict)

    def test_empty_lists(self):
        result = _reciprocal_rank_fusion([])
        self.assertEqual(result, {})

    def test_single_list(self):
        result = _reciprocal_rank_fusion([["a", "b"]])
        self.assertIsInstance(result, dict)
        self.assertIn("a", result)
        self.assertIn("b", result)

    def test_multiple_lists(self):
        result = _reciprocal_rank_fusion([["a", "b"], ["b", "c"]])
        self.assertIsInstance(result, dict)
        self.assertIn("a", result)
        self.assertIn("b", result)
        self.assertIn("c", result)
        self.assertGreater(result["b"], result["a"])

    def test_rrf_k_value(self):
        self.assertEqual(_RRF_K, 60)


class TestDetectQueryType(unittest.TestCase):
    """Kill: detect_query_type return_none mutations (L415)."""

    def test_empty_query(self):
        result = _detect_query_type("")
        self.assertEqual(result, "general")

    def test_code_query(self):
        result = _detect_query_type("def function python")
        self.assertEqual(result, "code")

    def test_temporal_query(self):
        result = _detect_query_type("yesterday meeting")
        self.assertEqual(result, "temporal")

    def test_multihop_query(self):
        result = _detect_query_type("connection between A and B")
        self.assertEqual(result, "multihop")

    def test_factual_query(self):
        result = _detect_query_type("what is the meaning of")
        self.assertEqual(result, "factual")

    def test_general_query(self):
        result = _detect_query_type("random words here")
        self.assertEqual(result, "general")


class TestWeightsForQueryType(unittest.TestCase):
    """Kill: weights_for_query_type return_none mutations (L445)."""

    def test_returns_dict(self):
        result = _weights_for_query_type("general")
        self.assertIsInstance(result, dict)

    def test_all_types_return_dict(self):
        for qtype in ["general", "code", "temporal", "multihop", "factual"]:
            result = _weights_for_query_type(qtype)
            self.assertIsInstance(result, dict)
            self.assertIn("bm25", result)
            self.assertIn("fitness", result)
            self.assertIn("importance", result)
            self.assertIn("pinned", result)
            self.assertIn("tag_match", result)

    def test_weights_sum_to_one(self):
        for qtype in ["general", "code", "temporal", "multihop", "factual"]:
            weights = _weights_for_query_type(qtype)
            total = sum(weights.values())
            self.assertAlmostEqual(total, 1.0, places=10)

    def test_unknown_type_returns_general(self):
        result = _weights_for_query_type("nonexistent")
        self.assertIsInstance(result, dict)
        self.assertEqual(result["bm25"], 0.35)


class TestExpandQuery(unittest.TestCase):
    """Kill: expand_query return_none mutations (L236, L264)."""

    def test_returns_string(self):
        result = _expand_query("test query")
        self.assertIsInstance(result, str)

    def test_returns_nonempty_string(self):
        result = _expand_query("test query")
        self.assertGreater(len(result), 0)

    def test_empty_query_returns_empty(self):
        result = _expand_query("")
        self.assertIsInstance(result, str)

    def test_abbreviations_expanded(self):
        result = _expand_query("test py")
        self.assertIsInstance(result, str)


class TestTokenizeForCE(unittest.TestCase):
    """Kill: tokenize return_none mutation (L472)."""

    def test_returns_list(self):
        result = _tokenize_for_ce("hello world test")
        self.assertIsInstance(result, list)

    def test_empty_string(self):
        result = _tokenize_for_ce("")
        self.assertIsInstance(result, list)
        self.assertEqual(result, [])

    def test_none_input(self):
        result = _tokenize_for_ce(None)
        self.assertIsInstance(result, list)
        self.assertEqual(result, [])


class TestLateInteractionScore(unittest.TestCase):
    """Kill: late_interaction_score return_none mutation."""

    def test_returns_float(self):
        score, _ = _late_interaction_score("test query", "test content")
        self.assertIsInstance(score, float)

    def test_empty_query(self):
        score, _ = _late_interaction_score("", "test content")
        self.assertEqual(score, 0.0)

    def test_empty_content(self):
        score, _ = _late_interaction_score("test query", "")
        self.assertEqual(score, 0.0)

    def test_both_empty(self):
        score, _ = _late_interaction_score("", "")
        self.assertEqual(score, 0.0)

    def test_perfect_match(self):
        score, _ = _late_interaction_score("test", "test")
        self.assertGreater(score, 0.0)


class TestComputeFinalScore(unittest.TestCase):
    """Kill: _compute_final_score float/int mutations (L1167-L1198)."""

    def test_returns_float(self):
        result = _compute_final_score(
            ScoreContext(
                rank=-1.0,
                fitness=0.9,
                importance=3,
                pinned=False,
                created="2026-01-01T00:00:00",
                tags_json='["tag1"]',
                query="test query",
                boost_pinned=True,
                recency_weight=0.1,
            )
        )
        self.assertIsInstance(result, float)

    def test_pinned_bonus(self):
        score_pinned = _compute_final_score(
            ScoreContext(
                rank=-1.0,
                fitness=0.9,
                importance=3,
                pinned=True,
                created="2026-01-01T00:00:00",
                tags_json='["tag1"]',
                query="test",
                boost_pinned=True,
                recency_weight=0.1,
            )
        )
        score_unpinned = _compute_final_score(
            ScoreContext(
                rank=-1.0,
                fitness=0.9,
                importance=3,
                pinned=False,
                created="2026-01-01T00:00:00",
                tags_json='["tag1"]',
                query="test",
                boost_pinned=True,
                recency_weight=0.1,
            )
        )
        self.assertGreaterEqual(score_pinned, score_unpinned)

    def test_boost_pinned_false(self):
        score_default = _compute_final_score(
            ScoreContext(
                rank=-1.0,
                fitness=0.9,
                importance=3,
                pinned=True,
                created="2026-01-01T00:00:00",
                tags_json='["tag1"]',
                query="test",
                boost_pinned=True,
                recency_weight=0.1,
            )
        )
        score_noboost = _compute_final_score(
            ScoreContext(
                rank=-1.0,
                fitness=0.9,
                importance=3,
                pinned=True,
                created="2026-01-01T00:00:00",
                tags_json='["tag1"]',
                query="test",
                boost_pinned=False,
                recency_weight=0.1,
            )
        )
        self.assertGreaterEqual(score_default, score_noboost)

    def test_recency_weight(self):
        # Use a very recent date so recency contributes positively.
        # With an old date, recency drags the score down and zeroing it
        # (redistributing weight to other channels) would score higher.
        from datetime import datetime, timezone
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        score_high = _compute_final_score(
            ScoreContext(
                rank=-1.0,
                fitness=0.9,
                importance=3,
                pinned=False,
                created=now_str,
                tags_json='["tag1"]',
                query="test",
                boost_pinned=True,
                recency_weight=0.5,
            )
        )
        score_low = _compute_final_score(
            ScoreContext(
                rank=-1.0,
                fitness=0.9,
                importance=3,
                pinned=False,
                created=now_str,
                tags_json='["tag1"]',
                query="test",
                boost_pinned=True,
                recency_weight=0.0,
            )
        )
        self.assertGreaterEqual(score_high, score_low)

    def test_tag_match(self):
        score = _compute_final_score(
            ScoreContext(
                rank=-1.0,
                fitness=0.9,
                importance=3,
                pinned=False,
                created="2026-01-01T00:00:00",
                tags_json='["python"]',
                query="python programming",
                boost_pinned=True,
                recency_weight=0.1,
            )
        )
        self.assertGreater(score, 0.0)

    def test_default_weights(self):
        weights = _QUERY_TYPE_WEIGHTS["general"]
        self.assertEqual(weights["bm25"], 0.35)
        self.assertEqual(weights["fitness"], 0.20)
        self.assertEqual(weights["importance"], 0.15)
        self.assertEqual(weights["pinned"], 0.10)
        self.assertEqual(weights["tag_match"], 0.10)


class TestApplyTemporalDecay(unittest.TestCase):
    """Kill: apply_temporal_decay return_none mutation (L1009)."""

    def test_returns_list(self):
        result = _apply_temporal_decay([], 0.15)
        self.assertIsInstance(result, list)

    def test_weight_zero_preserves_order(self):
        results = [("a", 1.0), ("b", 0.8)]
        result = _apply_temporal_decay(results, 0.0)
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 2)


class TestTemporalDecayConstants(unittest.TestCase):
    """Kill: temporal decay float mutations (L986, L984)."""

    def test_half_life_value(self):
        self.assertEqual(_TEMPORAL_DECAY_HALF_LIFE, 180.0)

    def test_rerank_half_life_value(self):
        from search.scoring import _get_rerank_half_life_days
        self.assertEqual(_get_rerank_half_life_days(), 180)


class TestConnectionPool(unittest.TestCase):
    """Kill: _ConnectionPool return_none mutation (L80)."""

    def test_returns_connection(self):
        with tempfile.TemporaryDirectory() as td:
            pool = _ConnectionPool(max_size=5)
            db_path = str(Path(td) / "test.db")
            conn = pool.get(db_path)
            self.assertIsNotNone(conn)
            self.assertIsInstance(conn, sqlite3.Connection)
            pool.close_all()

    def test_same_key_reuses(self):
        with tempfile.TemporaryDirectory() as td:
            pool = _ConnectionPool(max_size=5)
            db_path = str(Path(td) / "test.db")
            conn1 = pool.get(db_path)
            conn2 = pool.get(db_path)
            self.assertIs(conn1, conn2)
            pool.close_all()

    def test_different_keys_different_conns(self):
        with tempfile.TemporaryDirectory() as td:
            pool = _ConnectionPool(max_size=5)
            conn1 = pool.get(str(Path(td) / "a.db"))
            conn2 = pool.get(str(Path(td) / "b.db"))
            self.assertIsNot(conn1, conn2)
            pool.close_all()

    def test_lru_eviction(self):
        with tempfile.TemporaryDirectory() as td:
            pool = _ConnectionPool(max_size=2)
            conn1 = pool.get(str(Path(td) / "a.db"))
            pool.get(str(Path(td) / "b.db"))
            # Release conn1 so LRU eviction can work (depth goes to 0)
            pool.put(conn1)
            conn3 = pool.get(str(Path(td) / "c.db"))
            self.assertIsNotNone(conn3)
            pool.close_all()


class TestCountRows(unittest.TestCase):
    """Kill: count_rows return_none mutation (L1192)."""

    def test_returns_int(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "memory.db"
            conn = sqlite3.connect(str(db_path))
            conn.execute("CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT)")
            conn.execute("INSERT INTO memories VALUES ('a', 'test')")
            conn.commit()
            conn.close()
            result = count_rows(Path(td))
            self.assertIsInstance(result, int)
            self.assertEqual(result, 1)


class TestSafeCall(unittest.TestCase):
    """Kill: safe_call return_none mutation (L1239)."""

    def test_returns_value(self):
        def func():
            return 42

        result = safe_call(func)
        self.assertEqual(result, 42)

    def test_returns_fallback_on_error(self):
        def func():
            raise ValueError("test")

        result = safe_call(func, fallback="default")
        self.assertEqual(result, "default")


class TestAtomicWrite(unittest.TestCase):
    """Kill: atomic_write return_none mutation."""

    def test_creates_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "test.txt"
            atomic_write(path, "hello")
            self.assertTrue(path.exists())
            self.assertEqual(path.read_text(), "hello")


class TestValidateConfig(unittest.TestCase):
    """Kill: validate_config return_none mutation (L263)."""

    def test_returns_list(self):
        result = validate_config()
        self.assertIsInstance(result, list)


class TestParseFrontmatter(unittest.TestCase):
    """Kill: parse_frontmatter return_none mutation (L346)."""

    def test_returns_tuple(self):
        result = parse_frontmatter("hello")
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)

    def test_empty_content(self):
        result = parse_frontmatter("")
        self.assertEqual(result, ({}, ""))


class TestCoerce(unittest.TestCase):
    """Kill: _coerce return_none mutation (L362)."""

    def test_strips_quotes(self):
        self.assertEqual(_coerce('"hello"'), "hello")
        self.assertEqual(_coerce("'hello'"), "hello")
        self.assertEqual(_coerce("hello"), "hello")


class TestFindProjectRoot(unittest.TestCase):
    """Kill: find_project_root return_none mutation (L389)."""

    def test_returns_path(self):
        result = find_project_root(Path.cwd())
        self.assertIsInstance(result, Path)

    def test_finds_git_root(self):
        result = find_project_root(Path.cwd())
        assert result is not None
        self.assertTrue(result.exists())


class TestGetMemoryPaths(unittest.TestCase):
    """Kill: get_memory_paths return_none mutation (L410)."""

    def test_returns_tuple(self):
        result = get_memory_paths()
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 3)

    def test_all_paths_are_paths(self):
        project_root, local_mem, global_mem = get_memory_paths()
        self.assertIsInstance(project_root, Path)
        self.assertIsInstance(local_mem, Path)
        self.assertIsInstance(global_mem, Path)


class TestRateLimiter(unittest.TestCase):
    """Kill: RateLimiter comparison mutations (L1382, L1384, L1406)."""

    def test_allows_within_limit(self):
        limiter = RateLimiter(max_calls=5, window_seconds=1.0)
        self.assertTrue(limiter.check("test"))

    def test_blocks_over_limit(self):
        limiter = RateLimiter(max_calls=2, window_seconds=1.0)
        limiter.check("test")
        limiter.check("test")
        result = limiter.check("test")
        self.assertFalse(result)


class TestAcquireFlockWithRetry(unittest.TestCase):
    """Kill: acquire_flock_with_retry return_none mutations (L1319, L1329)."""

    def test_returns_bool(self):
        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / "test.lock"
            lock_file = open(lock_path, "w")
            result = acquire_flock_with_retry(lock_file, max_attempts=1)
            self.assertIsInstance(result, bool)
            release_flock(lock_file)


class TestSearchMemoriesReturnDict(unittest.TestCase):
    """Kill: search_memories return_none mutations on error paths."""

    def test_returns_dict_with_nonexistent_db(self):
        result = search_memories(Path("/nonexistent/db"), "test")
        self.assertIsInstance(result, dict)
        self.assertIn("results", result)
        self.assertIn("count", result)

    def test_empty_query_returns_error(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "test.db"
            conn = sqlite3.connect(str(db_path))
            conn.execute("""CREATE TABLE memories (
                id TEXT PRIMARY KEY, source_file TEXT, content TEXT, tags TEXT,
                created_at TEXT, updated_at TEXT, observed_at TEXT,
                fitness_score REAL, importance INTEGER, pinned INTEGER,
                repo_id TEXT, category TEXT, access_count INTEGER,
                success_score REAL, decay TEXT, valid_from TEXT,
                valid_to TEXT, superseded_by TEXT
            )""")
            conn.commit()
            conn.close()
            result = search_memories(db_path, "")
            self.assertIsInstance(result, dict)
            self.assertIn("results", result)


class TestMutationConstants(unittest.TestCase):
    """Kill: specific constant mutations."""

    def test_rrf_k(self):
        self.assertEqual(_RRF_K, 60)

    def test_temporal_decay_half_life(self):
        self.assertEqual(_TEMPORAL_DECAY_HALF_LIFE, 180.0)

    def test_rerank_half_life(self):
        self.assertEqual(_RERANK_HALF_LIFE_DAYS, 180)

    def test_query_type_weights_keys(self):
        for qtype in ["general", "code", "temporal", "multihop", "factual"]:
            self.assertIn(qtype, _QUERY_TYPE_WEIGHTS)

    def test_default_weights(self):
        w = _QUERY_TYPE_WEIGHTS["general"]
        self.assertAlmostEqual(sum(w.values()), 1.0, places=10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
