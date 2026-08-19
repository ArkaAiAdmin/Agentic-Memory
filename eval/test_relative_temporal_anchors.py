"""Unit tests for relative temporal anchor extraction and proximity scoring."""

import math
import time
from search.orchestrator import ScoreContext
from search.scoring import (
    _compute_final_score,
    _extract_relative_time_offset_days,
    _temporal_decay_factor,
)


def test_extract_relative_time_offset_days():
    # Days
    assert _extract_relative_time_offset_days("What did I buy 5 days ago?") == 5.0
    assert _extract_relative_time_offset_days("What did I buy ten days ago?") == 10.0
    assert _extract_relative_time_offset_days("Activity a day ago") == 1.0

    # Weeks
    assert _extract_relative_time_offset_days("What gardening activity did I do two weeks ago?") == 14.0
    assert _extract_relative_time_offset_days("Business milestone four weeks ago") == 28.0
    assert _extract_relative_time_offset_days("I mentioned an investment for a competition 4 weeks ago") == 28.0

    # Months
    assert _extract_relative_time_offset_days("I visited a museum two months ago") == 60.0
    assert _extract_relative_time_offset_days("Trip 3 months ago") == 90.0

    # Years
    assert _extract_relative_time_offset_days("Graduation 1 year ago") == 365.0

    # Non-relative queries return None
    assert _extract_relative_time_offset_days("What degree did I graduate with?") is None
    assert _extract_relative_time_offset_days("How many pets do I have?") is None
    assert _extract_relative_time_offset_days("") is None


def test_relative_temporal_scoring_boosts_matching_time_anchor():
    now_ts = 1700000000.0  # reference anchor
    target_offset_days = 14.0  # 2 weeks ago
    target_created_ts = now_ts - (target_offset_days * 86400.0)
    
    # 2 weeks ago session
    target_created_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(target_created_ts))
    # 1 day ago session (distractor)
    distractor_created_ts = now_ts - (1.0 * 86400.0)
    distractor_created_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(distractor_created_ts))

    query = "What gardening-related activity did I do two weeks ago?"

    ctx_target = ScoreContext(
        rank=0.0,
        fitness=0.5,
        importance=3,
        pinned=False,
        created=target_created_str,
        tags_json=None,
        query=query,
        boost_pinned=False,
        recency_weight=0.20,
        now_ts=now_ts,
    )
    ctx_distractor = ScoreContext(
        rank=0.0,
        fitness=0.5,
        importance=3,
        pinned=False,
        created=distractor_created_str,
        tags_json=None,
        query=query,
        boost_pinned=False,
        recency_weight=0.20,
        now_ts=now_ts,
    )

    score_target = _compute_final_score(ctx_target)
    score_distractor = _compute_final_score(ctx_distractor)

    # The 2-weeks-ago target should outscore the 1-day-ago distractor because the query asked for 2 weeks ago
    assert score_target > score_distractor


def test_standard_query_preserves_standard_recency():
    now_ts = 1700000000.0
    recent_ts = now_ts - (1.0 * 86400.0)
    old_ts = now_ts - (30.0 * 86400.0)

    recent_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(recent_ts))
    old_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(old_ts))

    query = "What is my current favorite color?"

    ctx_recent = ScoreContext(
        rank=0.0,
        fitness=0.5,
        importance=3,
        pinned=False,
        created=recent_str,
        tags_json=None,
        query=query,
        boost_pinned=False,
        recency_weight=0.20,
        now_ts=now_ts,
    )
    ctx_old = ScoreContext(
        rank=0.0,
        fitness=0.5,
        importance=3,
        pinned=False,
        created=old_str,
        tags_json=None,
        query=query,
        boost_pinned=False,
        recency_weight=0.20,
        now_ts=now_ts,
    )

    score_recent = _compute_final_score(ctx_recent)
    score_old = _compute_final_score(ctx_old)

    # For standard non-relative queries, recent notes outscore older notes as expected
    assert score_recent > score_old
