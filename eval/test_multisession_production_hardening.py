"""Unit tests for production-grade multi-session search solvers and span metrics."""

from datetime import datetime, timezone
import pytest

from search.phases.temporal_delta_solver import (
    calculate_temporal_delta,
    parse_natural_or_iso_date,
    parse_iso_date,
)
from search.phases.math_aggregator import (
    extract_and_aggregate_quantities,
    parse_numeric_val,
    format_numeric_val,
)
from eval.bench.metrics import compute_text_metrics


def test_holiday_date_parsing():
    res = parse_natural_or_iso_date("I volunteered at the charity dinner on Valentine's Day")
    assert res is not None
    dt, label = res
    assert dt.month == 2
    assert dt.day == 14
    assert label == "February 14th"


def test_order_to_arrival_temporal_delta():
    candidates = [
        ("m1", "[Session Date: 2023-05-10]\nI ordered the new remote shutter release online.", "", "", "2023-05-10 09:00:00"),
        ("m2", "[Session Date: 2023-05-15]\nThe package with the remote shutter release arrived today!", "", "", "2023-05-15 14:00:00"),
    ]
    query = "How many days did it take for me to receive the new remote shutter release after I ordered it?"
    res = calculate_temporal_delta(query, candidates)
    assert res is not None
    assert "5 days" in res


def test_quoted_multi_entity_sum():
    candidates = [
        ("m1", "I have listened to 15 episodes of 'How I Built This'.", "", "", ""),
        ("m2", "I tuned into 12 episodes of 'My Favorite Murder' during my commute.", "", "", ""),
    ]
    query = "What is the total number of episodes I've listened to from 'How I Built This' and 'My Favorite Murder'?"
    res = extract_and_aggregate_quantities(query, candidates)
    assert res is not None
    assert "27" in res


def test_compound_noun_multi_entity_cost():
    candidates = [
        ("m1", "I bought a premium car cover for $90.", "", "", ""),
        ("m2", "I picked up some detailing spray for $50.", "", "", ""),
    ]
    query = "What is the total cost of the car cover and detailing spray I purchased?"
    res = extract_and_aggregate_quantities(query, candidates)
    assert res is not None
    assert "$140" in res


def test_pages_left_subtraction():
    candidates = [
        ("m1", "The novel 'The Nightingale' is 450 pages long. I've finished reading 260 pages so far.", "", "", ""),
    ]
    query = "How many pages do I have left to read in 'The Nightingale'?"
    res = extract_and_aggregate_quantities(query, candidates)
    assert res is not None
    assert "190" in res


def test_savings_discount_calculation():
    candidates = [
        ("m1", "I saw the designer handbag was regularly $450, but I got it on sale for $150 at TK Maxx.", "", "", ""),
    ]
    query = "How much did I save on the designer handbag at TK Maxx?"
    res = extract_and_aggregate_quantities(query, candidates)
    assert res is not None
    assert "$300" in res


def test_dynamic_span_f1_evaluation():
    chunk = (
        "I'm planning to play tennis with my friends at the park. "
        "By the way, I'm really happy with my new tennis racket, which I got from a sports store downtown. "
        "It's been performing really well and improved my game."
    )
    expected = "the sports store downtown"
    res = compute_text_metrics(chunk, expected)
    assert res["overall_accuracy"] >= 0.5
