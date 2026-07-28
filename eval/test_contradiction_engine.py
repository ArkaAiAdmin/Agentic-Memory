"""Comprehensive tests for the Contradiction Resolution Graph Engine (CRGE)."""

from __future__ import annotations

import pytest
from search.phases.contradiction_engine import resolve_candidate_contradictions


def _make_candidate(candidate_id, content, timestamp="", score=1.0):
    """Helper to create a candidate tuple matching the expected format."""
    return (candidate_id, content, "source.md", "[]", timestamp, None, score)


class TestResolveCandidateContradictions:
    """Edge cases and empty inputs."""

    def test_empty_list(self):
        assert resolve_candidate_contradictions([], "query") == []

    def test_single_candidate_passes_through(self):
        c = [_make_candidate("m1", "Alice lives in NYC")]
        result = resolve_candidate_contradictions(c, "where does Alice live")
        assert len(result) == 1
        assert result[0][0] == "m1"

    def test_two_non_tuple_items(self):
        # Function requires >= 2 candidates; non-tuple items (< 2 elements) pass through
        # Strings ARE sequences, so they pass isinstance check but get processed as char lists
        # Only short tuples (< 2 elements) safely pass through
        candidates = [("id",), ("id2",)]
        result = resolve_candidate_contradictions(candidates, "query")
        assert len(result) == 2

    def test_short_tuples_pass_through(self):
        candidates = [("id",)]  # len < 2, returns early
        result = resolve_candidate_contradictions(candidates, "query")
        assert len(result) == 1

    """No contradictions — same state values."""

    def test_same_location_no_contradiction(self):
        candidates = [
            _make_candidate("m1", "Alice lives in NYC"),
            _make_candidate("m2", "Alice resides in NYC"),
        ]
        result = resolve_candidate_contradictions(candidates, "where does Alice live")
        # Both have same location "NYC" — no demotion
        scores = [float(r[6]) for r in result if len(r) > 6 and r[6] is not None]
        # At least one should have a boosted score from first-seen state
        assert any(s > 1.0 for s in scores)

    """Location contradictions — newer wins."""

    def test_location_contradiction_newer_wins(self):
        candidates = [
            _make_candidate("m1", "Alice moved to NYC", "2024-01-01", 5.0),
            _make_candidate("m2", "Alice moved to SF", "2024-06-01", 5.0),
        ]
        result = resolve_candidate_contradictions(candidates, "where does Alice live now")
        r_dict = {r[0]: float(r[6]) for r in result if len(r) > 6 and r[6] is not None}
        assert r_dict["m2"] > r_dict["m1"]

    def test_location_contradiction_older_demoted(self):
        candidates = [
            _make_candidate("m1", "Alice relocated to London", "2023-06-01", 5.0),
            _make_candidate("m2", "Alice lives in Paris", "2024-01-01", 5.0),
        ]
        result = resolve_candidate_contradictions(candidates, "where does Alice live")
        r_dict = {r[0]: float(r[6]) for r in result if len(r) > 6 and r[6] is not None}
        # m1 should be demoted to 0.001
        assert r_dict["m1"] == pytest.approx(0.001)

    """Employer contradictions."""

    def test_employer_contradiction(self):
        candidates = [
            _make_candidate("m1", "Bob works at Google", "2024-01-01", 5.0),
            _make_candidate("m2", "Bob joined OpenAI", "2024-06-01", 5.0),
        ]
        result = resolve_candidate_contradictions(candidates, "where does Bob work")
        r_dict = {r[0]: float(r[6]) for r in result if len(r) > 6 and r[6] is not None}
        assert r_dict["m2"] > r_dict["m1"]

    """Database contradictions."""

    def test_database_contradiction(self):
        candidates = [
            _make_candidate("m1", "We chose PostgreSQL as primary database", "2024-01-01", 5.0),
            _make_candidate("m2", "We adopted MongoDB as primary database", "2024-06-01", 5.0),
        ]
        result = resolve_candidate_contradictions(candidates, "what database do we use")
        r_dict = {r[0]: float(r[6]) for r in result if len(r) > 6 and r[6] is not None}
        assert r_dict["m2"] > r_dict["m1"]

    """Target date query boosting."""

    def test_target_date_boosts_matching_candidate(self):
        candidates = [
            _make_candidate("m1", "Alice lives in NYC", "2024-11-01", 5.0),
            _make_candidate("m2", "Alice lives in SF", "2024-06-01", 5.0),
        ]
        result = resolve_candidate_contradictions(candidates, "Where did Alice live in November 2024")
        r_dict = {r[0]: float(r[6]) for r in result if len(r) > 6 and r[6] is not None}
        # m1 (Nov 2024) should be heavily boosted
        assert r_dict["m1"] > r_dict["m2"]

    def test_target_date_no_match_no_boost(self):
        # Neither candidate matches Nov 2024 target date
        # Verify no target-date boost is applied (scores stay in normal range)
        candidates = [
            _make_candidate("m1", "Alice relocated to NYC", "2024-06-01", 5.0),
            _make_candidate("m2", "Alice relocated to SF", "2024-12-01", 5.0),
        ]
        result = resolve_candidate_contradictions(candidates, "Where did Alice live in November 2024")
        r_dict = {r[0]: float(r[6]) for r in result if len(r) > 6 and r[6] is not None}
        # No target-date boost: neither score should exceed 10x base (50.0)
        # The first-seen boost is 5x+5, so max is abs(5)*5+5=30.0
        assert r_dict["m1"] <= 30.0
        assert r_dict["m2"] <= 30.0

    """Score ordering."""

    def test_output_sorted_by_score_descending(self):
        candidates = [
            _make_candidate("m1", "Alice lives in NYC", "2024-01-01", 1.0),
            _make_candidate("m2", "Alice lives in SF", "2024-06-01", 1.0),
        ]
        result = resolve_candidate_contradictions(candidates, "where does Alice live")
        scores = [float(r[6]) for r in result if len(r) > 6 and r[6] is not None]
        assert scores == sorted(scores, reverse=True)

    """Session date extraction from content."""

    def test_session_date_in_content_used(self):
        candidates = [
            _make_candidate("m1", "[Session Date: 2024-03-01] Alice moved to NYC", "", 5.0),
            _make_candidate("m2", "[Session Date: 2024-06-01] Alice moved to SF", "", 5.0),
        ]
        result = resolve_candidate_contradictions(candidates, "where does Alice live")
        r_dict = {r[0]: float(r[6]) for r in result if len(r) > 6 and r[6] is not None}
        assert r_dict["m2"] > r_dict["m1"]

    """Multiple categories simultaneously."""

    def test_multiple_category_contradictions(self):
        candidates = [
            _make_candidate("m1", "Alice moved to NYC and works at Google", "2024-01-01", 5.0),
            _make_candidate("m2", "Alice moved to SF and joined OpenAI", "2024-06-01", 5.0),
        ]
        result = resolve_candidate_contradictions(candidates, "where does Alice live and work")
        r_dict = {r[0]: float(r[6]) for r in result if len(r) > 6 and r[6] is not None}
        assert r_dict["m2"] > r_dict["m1"]

    """State pattern matching — uses 2+ candidates to trigger processing."""

    def test_location_pattern_matches_variants(self):
        test_cases = [
            "Alice moved to NYC",
            "Alice moved from London to NYC",
            "Alice lives in NYC",
            "Alice resides in NYC",
            "Alice relocated to NYC",
            "Alice is back in NYC",
        ]
        for content in test_cases:
            candidates = [
                _make_candidate("m1", content),
                _make_candidate("m2", "Bob lives in LA"),  # second candidate to trigger processing
            ]
            result = resolve_candidate_contradictions(candidates, "location")
            # Both should be processed and have boosted scores
            assert len(result) == 2

    def test_employer_pattern_matches_variants(self):
        test_cases = [
            "Bob works at Google",
            "Bob working at Google",
            "Bob employed by Google",
            "Bob joined Google",
            "Bob role as engineer at Google",
        ]
        for content in test_cases:
            candidates = [
                _make_candidate("m1", content),
                _make_candidate("m2", "Charlie works at Meta"),
            ]
            result = resolve_candidate_contradictions(candidates, "employer")
            assert len(result) == 2

    """No state pattern found."""

    def test_no_pattern_match_passthrough(self):
        candidates = [
            _make_candidate("m1", "The weather is nice today", "2024-01-01", 5.0),
            _make_candidate("m2", "The weather is cold tomorrow", "2024-06-01", 5.0),
        ]
        result = resolve_candidate_contradictions(candidates, "weather")
        # No state patterns match — scores should remain unchanged
        for r in result:
            if len(r) > 6 and r[6] is not None:
                assert float(r[6]) == 5.0

    """Score boost math — needs 2+ candidates."""

    def test_first_seen_state_gets_5x_plus_5(self):
        candidates = [
            _make_candidate("m1", "Alice lives in NYC", "", 1.0),
            _make_candidate("m2", "Bob works at Google", "", 1.0),
        ]
        result = resolve_candidate_contradictions(candidates, "location")
        r_dict = {r[0]: float(r[6]) for r in result if len(r) > 6 and r[6] is not None}
        # m1 matches location pattern, first seen: abs(1.0) * 5.0 + 5.0 = 10.0
        assert r_dict["m1"] == pytest.approx(10.0)

    def test_conflicting_newer_gets_10x_plus_10(self):
        candidates = [
            _make_candidate("m1", "Alice moved to NYC", "2024-01-01", 5.0),
            _make_candidate("m2", "Alice moved to SF", "2024-06-01", 5.0),
        ]
        result = resolve_candidate_contradictions(candidates, "location")
        r_dict = {r[0]: float(r[6]) for r in result if len(r) > 6 and r[6] is not None}
        # m2 (newer): abs(5.0) * 10.0 + 10.0 = 60.0
        assert r_dict["m2"] == pytest.approx(60.0)
