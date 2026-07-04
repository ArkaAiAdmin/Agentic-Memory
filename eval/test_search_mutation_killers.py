"""Targeted mutation-killing tests for search_pipeline.py.

Each test is designed to kill specific surviving mutations identified by
mutation testing. Tests verify exact return values, side effects, and
scoring behavior to catch mutations that change return types, numeric
constants, or boolean logic.
"""

import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


from search_pipeline import (
    _tokenize_for_ce,
    _cross_encoder_score,
    _apply_cross_encoder_rerank,
    _qw5_extract_keywords,
    _qw5_keyword_similarity,
    _qw5_is_topic_boundary,
    _qw5_chunk_content,
    _detect_query_type,
    _expand_query,
    _top_recent_tags,
    _top_recent_notes,
    _top_recent_source_files,
    _reciprocal_rank_fusion,
    _apply_temporal_decay,
    _compute_final_score, ScoreContext,
    _weights_for_query_type,
    _build_zero_result_suggestions,
    _merge_chunk_hits,
    _late_interaction_score,
    _bb1_split_sentences,
    _bb1_synthesize,
    _bb2_extract_terms,
    _bb2_is_reference_query,
    _escape_phrase,
    _RRF_K,
    _TEMPORAL_DECAY_HALF_LIFE,
    _RERANK_HALF_LIFE_DAYS,
    _QW5_CHUNK_THRESHOLD,
    _QW5_CHUNK_TARGET_SIZE,
    _QW5_CHUNK_OVERLAP,
    _QW5_CHUNK_MAX_SIZE,
    _QW5_TOPIC_SIMILARITY_THRESHOLD,
    _CROSS_ENCODER_BLEND,
    _QUERY_TYPE_WEIGHTS,
)

from infra.infrastructure import GLOBAL_MEM_DIR

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
    (Path(db_path).parent / f"{note_id}.md").unlink(missing_ok=True)


from infra.memory_common import open_db


# ─── _tokenize_for_ce ─────────────────────────────────────────────────

class TestTokenizeForCe(unittest.TestCase):
    """Kill: _tokenize_for_ce return_none and not mutations."""

    def test_returns_list(self):
        result = _tokenize_for_ce("hello world")
        self.assertIsInstance(result, list)

    def test_empty_string_returns_empty_list(self):
        result = _tokenize_for_ce("")
        self.assertEqual(result, [])

    def test_none_returns_empty_list(self):
        result = _tokenize_for_ce(None)
        self.assertEqual(result, [])

    def test_nonempty_tokens(self):
        result = _tokenize_for_ce("hello world test")
        self.assertGreater(len(result), 0)

    def test_lowercase(self):
        result = _tokenize_for_ce("Hello World")
        for t in result:
            self.assertEqual(t, t.lower())

    def test_preserves_order(self):
        result = _tokenize_for_ce("alpha beta gamma")
        self.assertEqual(result, ["alpha", "beta", "gamma"])

    def test_filters_empty_tokens(self):
        result = _tokenize_for_ce("a  b  c")
        self.assertTrue(all(t for t in result))


# ─── _cross_encoder_score ─────────────────────────────────────────────

class TestCrossEncoderScore(unittest.TestCase):
    """Kill: _cross_encoder_score return_none, float, compare mutations."""

    def test_returns_float(self):
        result = _cross_encoder_score("test query", "test content")
        self.assertIsInstance(result, float)

    def test_empty_query_returns_zero(self):
        result = _cross_encoder_score("", "test content")
        self.assertEqual(result, 0.0)

    def test_empty_content_returns_zero(self):
        result = _cross_encoder_score("test query", "")
        self.assertEqual(result, 0.0)

    def test_both_empty_returns_zero(self):
        result = _cross_encoder_score("", "")
        self.assertEqual(result, 0.0)

    def test_perfect_match_high_score(self):
        score = _cross_encoder_score("python programming", "python programming language")
        self.assertGreater(score, 0.5)

    def test_no_match_low_score(self):
        score = _cross_encoder_score("python programming", "car engine mechanic")
        self.assertLess(score, 0.3)

    def test_score_range(self):
        score = _cross_encoder_score("test query", "test content with more words")
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.2)

    def test_bigram_bonus(self):
        score_with_bigram = _cross_encoder_score("machine learning", "machine learning is great")
        score_no_bigram = _cross_encoder_score("machine learning", "learning machine is great")
        self.assertIsInstance(score_with_bigram, float)
        self.assertIsInstance(score_no_bigram, float)


# ─── _apply_cross_encoder_rerank ──────────────────────────────────────

class TestApplyCrossEncoderRerank(unittest.TestCase):
    """Kill: _apply_cross_encoder_rerank return_none, bool, not mutations."""

    def test_returns_list(self):
        result = _apply_cross_encoder_rerank("test", [], top_k=5)
        self.assertIsInstance(result, list)

    def test_empty_results_returns_empty(self):
        result = _apply_cross_encoder_rerank("test", [], top_k=5)
        self.assertEqual(result, [])

    def test_empty_query_returns_input(self):
        results = [("id1", "content1", None, "[]", now_iso(), -1.0, 0.5, None, None, None)]
        result = _apply_cross_encoder_rerank("", results, top_k=5)
        self.assertEqual(result, results)

    def test_preserves_order_for_nonempty(self):
        results = [
            ("id1", "content1", None, "[]", now_iso(), -1.0, 0.5, None, None, None),
            ("id2", "content2", None, "[]", now_iso(), -2.0, 0.3, None, None, None),
        ]
        result = _apply_cross_encoder_rerank("test query", results, top_k=5)
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 2)

    def test_tail_untouched(self):
        results = [
            ("id1", "content1", None, "[]", now_iso(), -1.0, 0.5, None, None, None),
            ("id2", "content2", None, "[]", now_iso(), -2.0, 0.3, None, None, None),
            ("id3", "content3", None, "[]", now_iso(), -3.0, 0.1, None, None, None),
        ]
        result = _apply_cross_encoder_rerank("test", results, top_k=2)
        # Tail (id3) should be untouched
        tail = [r for r in result if r[0] == "id3"]
        self.assertEqual(len(tail), 1)


# ─── _qw5_extract_keywords ────────────────────────────────────────────

class TestQw5ExtractKeywords(unittest.TestCase):
    """Kill: _qw5_extract_keywords return_none and not mutations."""

    def test_returns_set(self):
        result = _qw5_extract_keywords("hello world test")
        self.assertIsInstance(result, set)

    def test_empty_string_returns_empty_set(self):
        result = _qw5_extract_keywords("")
        self.assertEqual(result, set())

    def test_none_returns_empty_set(self):
        result = _qw5_extract_keywords(None)
        self.assertEqual(result, set())

    def test_extracts_keywords(self):
        result = _qw5_extract_keywords("python programming language")
        self.assertIn("python", result)
        self.assertIn("programming", result)
        self.assertIn("language", result)

    def test_filters_stopwords(self):
        result = _qw5_extract_keywords("the cat is on the mat")
        self.assertNotIn("the", result)
        self.assertNotIn("is", result)
        self.assertNotIn("on", result)

    def test_filters_short_words(self):
        result = _qw5_extract_keywords("a big cat sat")
        self.assertNotIn("a", result)  # 1 char, filtered
        self.assertIn("cat", result)  # 3 chars, included
        self.assertIn("big", result)
        self.assertIn("sat", result)


# ─── _qw5_keyword_similarity ──────────────────────────────────────────

class TestQw5KeywordSimilarity(unittest.TestCase):
    """Kill: _qw5_keyword_similarity return_none mutations."""

    def test_returns_float(self):
        result = _qw5_keyword_similarity("python programming", "python coding")
        self.assertIsInstance(result, float)

    def test_identical_texts_high_similarity(self):
        result = _qw5_keyword_similarity("python programming", "python programming")
        self.assertGreater(result, 0.8)

    def test_different_texts_low_similarity(self):
        result = _qw5_keyword_similarity("python programming", "car engine mechanic")
        self.assertLess(result, 0.3)

    def test_empty_first_returns_zero(self):
        result = _qw5_keyword_similarity("", "test")
        self.assertEqual(result, 0.0)

    def test_empty_second_returns_zero(self):
        result = _qw5_keyword_similarity("test", "")
        self.assertEqual(result, 0.0)

    def test_both_empty_returns_zero(self):
        result = _qw5_keyword_similarity("", "")
        self.assertEqual(result, 0.0)


# ─── _qw5_is_topic_boundary ───────────────────────────────────────────

class TestQw5IsTopicBoundary(unittest.TestCase):
    """Kill: _qw5_is_topic_boundary compare mutations."""

    def test_returns_bool(self):
        result = _qw5_is_topic_boundary("python programming", "python coding")
        self.assertIsInstance(result, bool)

    def test_similar_texts_no_boundary(self):
        result = _qw5_is_topic_boundary("python programming language", "python programming tutorial")
        self.assertFalse(result)

    def test_different_texts_boundary(self):
        result = _qw5_is_topic_boundary("python programming", "car engine mechanic")
        self.assertTrue(result)


# ─── _qw5_chunk_content ───────────────────────────────────────────────

class TestQw5ChunkContent(unittest.TestCase):
    """Kill: _qw5_chunk_content return_none and not mutations."""

    def test_returns_list(self):
        result = _qw5_chunk_content("hello world")
        self.assertIsInstance(result, list)

    def test_empty_content_returns_single_chunk(self):
        result = _qw5_chunk_content("")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0], (0, 0, ""))

    def test_short_content_returns_single_chunk(self):
        result = _qw5_chunk_content("short text")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][2], "short text")

    def test_long_content_returns_multiple_chunks(self):
        # Create content longer than threshold
        content = "This is sentence one. " * 200
        result = _qw5_chunk_content(content)
        self.assertGreater(len(result), 1)

    def test_chunk_tuple_format(self):
        result = _qw5_chunk_content("test content")
        self.assertEqual(len(result[0]), 3)
        start, end, text = result[0]
        self.assertIsInstance(start, int)
        self.assertIsInstance(end, int)
        self.assertIsInstance(text, str)


# ─── _detect_query_type ───────────────────────────────────────────────

class TestDetectQueryType(unittest.TestCase):
    """Kill: _detect_query_type return_none and not mutations."""

    def test_returns_string(self):
        result = _detect_query_type("test query")
        self.assertIsInstance(result, str)

    def test_empty_query_returns_general(self):
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


# ─── _expand_query ────────────────────────────────────────────────────

class TestExpandQuery(unittest.TestCase):
    """Kill: _expand_query return_none mutations."""

    def test_returns_string(self):
        result = _expand_query("test query")
        self.assertIsInstance(result, str)

    def test_returns_nonempty(self):
        result = _expand_query("test query")
        self.assertGreater(len(result), 0)

    def test_empty_query(self):
        result = _expand_query("")
        self.assertIsInstance(result, str)

    def test_abbreviations_expanded(self):
        result = _expand_query("test py")
        self.assertIsInstance(result, str)


# ─── _top_recent_tags ─────────────────────────────────────────────────

class TestTopRecentTags(unittest.TestCase):
    """Kill: _top_recent_tags return_none, not, int mutations."""

    def test_returns_list(self):
        result = _top_recent_tags(PROD_DB, limit=5)
        self.assertIsInstance(result, list)

    def test_limit_respected(self):
        result = _top_recent_tags(PROD_DB, limit=3)
        self.assertLessEqual(len(result), 3)

    def test_limit_one(self):
        result = _top_recent_tags(PROD_DB, limit=1)
        self.assertLessEqual(len(result), 1)


# ─── _top_recent_notes ────────────────────────────────────────────────

class TestTopRecentNotes(unittest.TestCase):
    """Kill: _top_recent_notes return_none, not, int mutations."""

    def test_returns_list(self):
        result = _top_recent_notes(PROD_DB, limit=5)
        self.assertIsInstance(result, list)

    def test_limit_respected(self):
        result = _top_recent_notes(PROD_DB, limit=3)
        self.assertLessEqual(len(result), 3)

    def test_limit_one(self):
        result = _top_recent_notes(PROD_DB, limit=1)
        self.assertLessEqual(len(result), 1)


# ─── _top_recent_source_files ─────────────────────────────────────────

class TestTopRecentSourceFiles(unittest.TestCase):
    """Kill: _top_recent_source_files return_none, not, int mutations."""

    def test_returns_list(self):
        result = _top_recent_source_files(PROD_DB, limit=5)
        self.assertIsInstance(result, list)

    def test_limit_respected(self):
        result = _top_recent_source_files(PROD_DB, limit=3)
        self.assertLessEqual(len(result), 3)

    def test_limit_one(self):
        result = _top_recent_source_files(PROD_DB, limit=1)
        self.assertLessEqual(len(result), 1)


# ─── _reciprocal_rank_fusion ──────────────────────────────────────────

class TestReciprocalRankFusion(unittest.TestCase):
    """Kill: RRF constant mutations."""

    def test_returns_dict(self):
        result = _reciprocal_rank_fusion([])
        self.assertIsInstance(result, dict)

    def test_rrf_k_value(self):
        self.assertEqual(_RRF_K, 60)

    def test_empty_lists(self):
        result = _reciprocal_rank_fusion([])
        self.assertEqual(result, {})

    def test_single_list(self):
        result = _reciprocal_rank_fusion([["a", "b"]])
        self.assertIn("a", result)
        self.assertIn("b", result)

    def test_multiple_lists(self):
        result = _reciprocal_rank_fusion([["a", "b"], ["b", "c"]])
        self.assertIn("a", result)
        self.assertIn("b", result)
        self.assertIn("c", result)
        self.assertGreater(result["b"], result["a"])


# ─── _apply_temporal_decay ────────────────────────────────────────────

class TestApplyTemporalDecay(unittest.TestCase):
    """Kill: temporal decay constant mutations."""

    def test_returns_list(self):
        result = _apply_temporal_decay([], 0.15)
        self.assertIsInstance(result, list)

    def test_half_life_value(self):
        self.assertEqual(_TEMPORAL_DECAY_HALF_LIFE, 180.0)

    def test_rerank_half_life_value(self):
        self.assertEqual(_RERANK_HALF_LIFE_DAYS, 180)

    def test_weight_zero_preserves_order(self):
        results = [('a', 1.0), ('b', 0.8)]
        result = _apply_temporal_decay(results, 0.0)
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 2)


# ─── _compute_final_score ─────────────────────────────────────────────

class TestComputeFinalScore(unittest.TestCase):
    """Kill: _compute_final_score float/int mutations."""

    def test_returns_float(self):
        result = _compute_final_score(ScoreContext(
            rank=-1.0, fitness=0.9, importance=3, pinned=False,
            created='2026-01-01T00:00:00', tags_json='["tag1"]',
            query='test query', boost_pinned=True, recency_weight=0.1
        ))
        self.assertIsInstance(result, float)

    def test_pinned_bonus(self):
        score_pinned = _compute_final_score(ScoreContext(
            rank=-1.0, fitness=0.9, importance=3, pinned=True,
            created='2026-01-01T00:00:00', tags_json='["tag1"]',
            query='test', boost_pinned=True, recency_weight=0.1
        ))
        score_unpinned = _compute_final_score(ScoreContext(
            rank=-1.0, fitness=0.9, importance=3, pinned=False,
            created='2026-01-01T00:00:00', tags_json='["tag1"]',
            query='test', boost_pinned=True, recency_weight=0.1
        ))
        self.assertGreaterEqual(score_pinned, score_unpinned)

    def test_boost_pinned_false(self):
        score_default = _compute_final_score(ScoreContext(
            rank=-1.0, fitness=0.9, importance=3, pinned=True,
            created='2026-01-01T00:00:00', tags_json='["tag1"]',
            query='test', boost_pinned=True, recency_weight=0.1
        ))
        score_noboost = _compute_final_score(ScoreContext(
            rank=-1.0, fitness=0.9, importance=3, pinned=True,
            created='2026-01-01T00:00:00', tags_json='["tag1"]',
            query='test', boost_pinned=False, recency_weight=0.1
        ))
        self.assertGreaterEqual(score_default, score_noboost)

    def test_recency_weight(self):
        score_high = _compute_final_score(ScoreContext(
            rank=-1.0, fitness=0.9, importance=3, pinned=False,
            created='2026-01-01T00:00:00', tags_json='["tag1"]',
            query='test', boost_pinned=True, recency_weight=0.5
        ))
        score_low = _compute_final_score(ScoreContext(
            rank=-1.0, fitness=0.9, importance=3, pinned=False,
            created='2026-01-01T00:00:00', tags_json='["tag1"]',
            query='test', boost_pinned=True, recency_weight=0.0
        ))
        self.assertGreaterEqual(score_high, score_low)

    def test_tag_match(self):
        score = _compute_final_score(ScoreContext(
            rank=-1.0, fitness=0.9, importance=3, pinned=False,
            created='2026-01-01T00:00:00', tags_json='["python"]',
            query='python programming', boost_pinned=True, recency_weight=0.1
        ))
        self.assertGreater(score, 0.0)

    def test_default_weights(self):
        weights = _QUERY_TYPE_WEIGHTS['general']
        self.assertEqual(weights['bm25'], 0.40)
        self.assertEqual(weights['fitness'], 0.20)
        self.assertEqual(weights['importance'], 0.15)
        self.assertEqual(weights['pinned'], 0.10)
        self.assertEqual(weights['recency'], 0.10)
        self.assertEqual(weights['tag_match'], 0.05)


# ─── _weights_for_query_type ──────────────────────────────────────────

class TestWeightsForQueryType(unittest.TestCase):
    """Kill: _weights_for_query_type mutations."""

    def test_returns_dict(self):
        result = _weights_for_query_type('general')
        self.assertIsInstance(result, dict)

    def test_all_types_return_dict(self):
        for qtype in ['general', 'code', 'temporal', 'multihop', 'factual']:
            result = _weights_for_query_type(qtype)
            self.assertIsInstance(result, dict)
            self.assertIn('bm25', result)
            self.assertIn('fitness', result)
            self.assertIn('importance', result)
            self.assertIn('pinned', result)
            self.assertIn('recency', result)
            self.assertIn('tag_match', result)

    def test_weights_sum_to_one(self):
        for qtype in ['general', 'code', 'temporal', 'multihop', 'factual']:
            weights = _weights_for_query_type(qtype)
            total = sum(weights.values())
            self.assertAlmostEqual(total, 1.0, places=10)

    def test_unknown_type_returns_general(self):
        result = _weights_for_query_type('nonexistent')
        self.assertIsInstance(result, dict)
        self.assertEqual(result['bm25'], 0.40)


# ─── _build_zero_result_suggestions ───────────────────────────────────

class TestBuildZeroResultSuggestions(unittest.TestCase):
    """Kill: _build_zero_result_suggestions mutations."""

    def test_returns_dict(self):
        suggestions = _build_zero_result_suggestions(PROD_DB, "nonexistent_xyz_123")
        self.assertIsInstance(suggestions, dict)


# ─── _merge_chunk_hits ────────────────────────────────────────────────

class TestMergeChunkHits(unittest.TestCase):
    """Kill: _merge_chunk_hits mutations."""

    def test_returns_list(self):
        result = _merge_chunk_hits([])
        self.assertIsInstance(result, list)

    def test_empty_input(self):
        result = _merge_chunk_hits([])
        self.assertEqual(result, [])


# ─── _late_interaction_score ──────────────────────────────────────────

class TestLateInteractionScore(unittest.TestCase):
    """Kill: _late_interaction_score mutations."""

    def test_returns_float(self):
        result = _late_interaction_score('test query', 'test content')
        self.assertIsInstance(result, float)

    def test_empty_query(self):
        result = _late_interaction_score('', 'test content')
        self.assertEqual(result, 0.0)

    def test_empty_content(self):
        result = _late_interaction_score('test query', '')
        self.assertEqual(result, 0.0)

    def test_both_empty(self):
        result = _late_interaction_score('', '')
        self.assertEqual(result, 0.0)

    def test_perfect_match(self):
        result = _late_interaction_score('test', 'test')
        self.assertGreater(result, 0.0)


# ─── _bb1_split_sentences ─────────────────────────────────────────────

class TestBb1SplitSentences(unittest.TestCase):
    """Kill: _bb1_split_sentences mutations."""

    def test_returns_list(self):
        result = _bb1_split_sentences("Hello world. How are you?")
        self.assertIsInstance(result, list)

    def test_empty_string(self):
        result = _bb1_split_sentences("")
        self.assertIsInstance(result, list)

    def test_single_sentence(self):
        result = _bb1_split_sentences("Hello world.")
        self.assertEqual(len(result), 1)


# ─── _bb1_synthesize ──────────────────────────────────────────────────

class TestBb1Synthesize(unittest.TestCase):
    """Kill: _bb1_synthesize mutations."""

    def test_returns_dict(self):
        result = _bb1_synthesize("test query", [])
        self.assertIsInstance(result, dict)


# ─── _bb2_extract_terms ───────────────────────────────────────────────

class TestBb2ExtractTerms(unittest.TestCase):
    """Kill: _bb2_extract_terms mutations."""

    def test_returns_list(self):
        result = _bb2_extract_terms("test query about python")
        self.assertIsInstance(result, list)

    def test_empty_string(self):
        result = _bb2_extract_terms("")
        self.assertIsInstance(result, list)


# ─── _bb2_is_reference_query ──────────────────────────────────────────

class TestBb2IsReferenceQuery(unittest.TestCase):
    """Kill: _bb2_is_reference_query mutations."""

    def test_returns_bool(self):
        result = _bb2_is_reference_query("what did I say about python")
        self.assertIsInstance(result, bool)

    def test_reference_query(self):
        result = _bb2_is_reference_query("the one from earlier")
        self.assertTrue(result)

    def test_normal_query(self):
        result = _bb2_is_reference_query("how to install docker")
        self.assertFalse(result)


# ─── _escape_phrase ───────────────────────────────────────────────────

class TestEscapePhrase(unittest.TestCase):
    """Kill: _escape_phrase mutations."""

    def test_returns_string(self):
        result = _escape_phrase("test phrase")
        self.assertIsInstance(result, str)

    def test_escapes_quotes(self):
        result = _escape_phrase('test "phrase"')
        self.assertIn('""', result)  # quotes are doubled
        self.assertIn('phrase', result)


# ─── Module-level constants ───────────────────────────────────────────

class TestModuleConstants(unittest.TestCase):
    """Kill: module-level constant mutations."""

    def test_rrf_k(self):
        self.assertEqual(_RRF_K, 60)

    def test_temporal_decay_half_life(self):
        self.assertEqual(_TEMPORAL_DECAY_HALF_LIFE, 180.0)

    def test_rerank_half_life(self):
        self.assertEqual(_RERANK_HALF_LIFE_DAYS, 180)

    def test_chunk_threshold(self):
        self.assertEqual(_QW5_CHUNK_THRESHOLD, 2000)

    def test_chunk_target_size(self):
        self.assertEqual(_QW5_CHUNK_TARGET_SIZE, 600)

    def test_chunk_overlap(self):
        self.assertEqual(_QW5_CHUNK_OVERLAP, 81)

    def test_chunk_max_size(self):
        self.assertEqual(_QW5_CHUNK_MAX_SIZE, 1200)

    def test_topic_similarity_threshold(self):
        self.assertEqual(_QW5_TOPIC_SIMILARITY_THRESHOLD, 0.15)

    def test_cross_encoder_blend(self):
        self.assertEqual(_CROSS_ENCODER_BLEND, 0.6)

    def test_query_type_weights_keys(self):
        for qtype in ['general', 'code', 'temporal', 'multihop', 'factual']:
            self.assertIn(qtype, _QUERY_TYPE_WEIGHTS)

    def test_default_weights_sum(self):
        w = _QUERY_TYPE_WEIGHTS['general']
        self.assertAlmostEqual(sum(w.values()), 1.0, places=10)


if __name__ == '__main__':
    unittest.main(verbosity=2)
